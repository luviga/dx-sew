"""Feature table of every feeder of the ERCOT frame (decisions K60 and K61).

Run from the repository::

    uv run python -m dxsew.texas.features --stamp <stamp> --frame-stamp <frame stamp> \\
        [--workers 16] [--processes 8]

A feeder is a folder of the ``opendss`` tree that holds both ``loads.dss`` and
``loadshapes.dss`` (``frame.FEEDER_FILES``); the feeders read are those of the
substations with ``in_frame`` true in ``frame_<frame stamp>.parquet``. The
network files named in ``groups.feeder_network_files`` are downloaded first
(already present files are reused). Every file is read as text; no circuit is
compiled. Release: SMART-DS v0.9 Full Texas,
https://data.openei.org/submissions/2981

Writes ``<data_root>/texas/features_<stamp>.parquet``, one row per frame
feeder, and ``missing_network_files_<stamp>.csv`` when a feeder lacks a
network file. Columns:

Identity, from the frame
    ``subregion``, ``substation``, ``feeder`` (the folder name,
    ``<substation>--<feeder>``), ``stratum``, ``county``.
From ``Loads.dss``
    ``loads`` (load objects); ``customers`` (``dss.customer_count``: the
    ``_1`` and ``_2`` objects of a split-phase customer are one customer);
    ``res_customers`` and ``com_customers`` (customers whose load shape name
    starts ``res`` or ``com`` before ``_kw``, ``dss.load_sector``);
    ``load_kw`` (sum of released kW); ``com_kw_share`` (kW of commercial
    loads over ``load_kw``); ``kw_per_customer`` (``load_kw`` over
    ``customers``).
From ``Lines.dss`` (empty when the feeder has no such file)
    ``lines``; ``line_km`` (sum of lengths in km, by each line's ``units``;
    a line without ``length`` counts 0); ``lines_without_units`` (lines read
    in ``groups.line_units_default``); ``extent_km`` (longest shortest-path
    distance between two buses of the line graph, line lengths as weights,
    by two sweeps of Dijkstra's search in every connected part; ASSUMPTION:
    exact on a radial feeder and a lower bound where loops exist);
    ``graph_parts`` (connected parts of the line graph).
From the feeder ``Transformers.dss`` (empty when the feeder has no such file)
    ``service_transformers`` (transformer objects of the file);
    ``service_kva`` (sum of ratings, ``dss.parse_transformer_kva``);
    ``primary_kv`` (highest ``kv`` or ``kvs`` value of any winding in the
    file); ``kva_per_kw`` (``service_kva`` over ``load_kw``).
Flags of the coupling at its pin (problem log P1 and P2)
    ``no_res`` (no residential customer); ``no_com`` (no commercial
    customer); ``shared_shapes`` (load shapes used by loads of more than one
    phase count); ``shared_pairs_total`` (rows of the feeder after grouping
    its loads by phase count and load shape, as the coupling's loads list
    does); ``runnable_alone`` (no flag: both sectors present and
    ``shared_shapes`` 0).
"""

from __future__ import annotations

import argparse
import heapq
import math
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from . import dss, oedi
from .config import Paths, load_local, load_sample_settings
from .frame import FEEDER_FILES, LOADS_FILE, opendss_objects
from .sample import write_csv, write_parquet

BYTES_PER_GB = 1e9  # decimal gigabyte, as in sample.py
IDENTITY = ["subregion", "substation", "feeder", "stratum", "county"]
COLUMNS = IDENTITY + [
    "loads",
    "customers",
    "res_customers",
    "com_customers",
    "load_kw",
    "com_kw_share",
    "kw_per_customer",
    "lines",
    "line_km",
    "lines_without_units",
    "extent_km",
    "graph_parts",
    "service_transformers",
    "service_kva",
    "primary_kv",
    "kva_per_kw",
    "no_res",
    "no_com",
    "shared_shapes",
    "shared_pairs_total",
    "runnable_alone",
]
LINE_COLUMNS = ["lines", "line_km", "lines_without_units", "extent_km", "graph_parts"]
TRANSFORMER_COLUMNS = ["service_transformers", "service_kva", "primary_kv", "kva_per_kw"]


def frame_feeders(objects: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """Feeders of the frame substations: folders holding every file of ``FEEDER_FILES``."""
    in_frame = frame.loc[frame["in_frame"], ["subregion", "substation", "stratum", "county"]]
    in_feeder = objects[(objects["feeder"] != "") & objects["file"].isin(FEEDER_FILES)]
    complete = in_feeder.groupby(["subregion", "substation", "feeder"])["file"].nunique()
    feeders = complete[complete == len(FEEDER_FILES)].reset_index()[["subregion", "substation", "feeder"]]
    feeders = feeders.merge(in_frame, on=["subregion", "substation"], how="inner")
    return feeders.sort_values(["subregion", "substation", "feeder"]).reset_index(drop=True)


def feeder_files(objects: pd.DataFrame, feeders: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    """Objects named ``names`` (any case) or ``loads.dss`` inside the feeders given."""
    wanted = {name.lower() for name in names} | {LOADS_FILE}
    files = objects[(objects["feeder"] != "") & objects["file"].isin(wanted)]
    return files.merge(feeders[["subregion", "substation", "feeder"]], on=["subregion", "substation", "feeder"])


def file_name_table(objects: pd.DataFrame) -> pd.DataFrame:
    """Object count and bytes of every file name found inside feeder folders."""
    in_feeder = objects[objects["feeder"] != ""]
    return (
        in_feeder.groupby("file")
        .agg(objects=("size", "size"), bytes=("size", "sum"))
        .sort_values(["objects", "file"], ascending=[False, True])
        .reset_index()
    )


def extent(lines: list[tuple[str, str, float]]) -> tuple[float, int]:
    """Largest two-sweep extent over the connected parts of a line graph, and the part count.

    ``lines`` holds ``(bus1, bus2, km)``. Parallel lines keep the shortest
    length; a line from a bus to itself is left out. In each part a search
    from its first bus (in name order) finds the farthest bus, and a second
    search from there gives the part's extent. Ties go to the first bus in
    name order, so the result does not depend on file order.
    """
    graph: dict[str, dict[str, float]] = {}
    for bus1, bus2, km in lines:
        graph.setdefault(bus1, {})
        graph.setdefault(bus2, {})
        if bus1 == bus2:
            continue
        if km < graph[bus1].get(bus2, math.inf):
            graph[bus1][bus2] = km
            graph[bus2][bus1] = km

    def farthest(source: str) -> tuple[str, float, dict[str, float]]:
        distance = {source: 0.0}
        queue = [(0.0, source)]
        while queue:
            here, bus = heapq.heappop(queue)
            if here > distance[bus]:
                continue
            for other, km in graph[bus].items():
                there = here + km
                if there < distance.get(other, math.inf):
                    distance[other] = there
                    heapq.heappush(queue, (there, other))
        best = max(distance.values())
        far_bus = min(bus for bus, value in distance.items() if value == best)
        return far_bus, best, distance

    seen: set[str] = set()
    parts = 0
    longest = 0.0
    for bus in sorted(graph):
        if bus in seen:
            continue
        parts += 1
        start, _, reached = farthest(bus)
        seen.update(reached)
        _, span, _ = farthest(start)
        longest = max(longest, span)
    return longest, parts


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def read_feeder(task: dict) -> dict:
    """Features of one feeder; ``task`` holds the identity, the file paths and the unit default."""
    loads = dss.parse_loads(_read(task["loads"]))
    sectors = [dss.load_sector(load.yearly) for load in loads]
    res = [load for load, sector in zip(loads, sectors) if sector == "res"]
    com = [load for load, sector in zip(loads, sectors) if sector == "com"]
    load_kw = float(sum(load.kw for load in loads))
    customers = dss.customer_count(loads)
    phases_by_shape: dict[str, set] = {}
    for load in loads:
        shape = (load.yearly or "").lower()
        phases_by_shape.setdefault(shape, set()).add(load.phases)
    shared = sum(1 for counts in phases_by_shape.values() if len(counts) > 1)
    row = {key: task[key] for key in IDENTITY}
    row.update(
        loads=len(loads),
        customers=customers,
        res_customers=dss.customer_count(res),
        com_customers=dss.customer_count(com),
        load_kw=load_kw,
        com_kw_share=float(sum(load.kw for load in com)) / load_kw if load_kw else math.nan,
        kw_per_customer=load_kw / customers if customers else math.nan,
        other_sector_loads=sum(1 for sector in sectors if sector not in ("res", "com")),
    )
    row.update({column: math.nan for column in LINE_COLUMNS + TRANSFORMER_COLUMNS})
    if task["lines"]:
        lines = dss.parse_lines(_read(task["lines"]))
        lengths = [dss.line_length_km(line, task["default_unit"]) for line in lines]
        span, parts = extent([(line.bus1, line.bus2, km) for line, (km, _) in zip(lines, lengths)])
        row.update(
            lines=len(lines),
            line_km=float(sum(km for km, _ in lengths)),
            lines_without_units=sum(1 for _, known in lengths if not known),
            extent_km=span,
            graph_parts=parts,
        )
    if task["transformers"]:
        transformers = dss.parse_transformers(_read(task["transformers"]))
        ratings = [transformer.kva for transformer in transformers if transformer.kva is not None]
        kvs = [kv for transformer in transformers for kv in transformer.kvs]
        service_kva = float(sum(ratings))
        row.update(
            service_transformers=len(transformers),
            service_kva=service_kva,
            primary_kv=max(kvs) if kvs else math.nan,
            kva_per_kw=service_kva / load_kw if load_kw else math.nan,
        )
    row.update(
        no_res=row["res_customers"] == 0,
        no_com=row["com_customers"] == 0,
        shared_shapes=shared,
        shared_pairs_total=sum(len(counts) for counts in phases_by_shape.values()),
    )
    row["runnable_alone"] = not (row["no_res"] or row["no_com"] or shared > 0)
    return row


def feature_table(rows: list[dict]) -> tuple[pd.DataFrame, int]:
    """The table in ``COLUMNS`` order, and the count of loads of neither sector."""
    table = pd.DataFrame(rows)
    other = int(table["other_sector_loads"].sum()) if len(table) else 0
    table = table.reindex(columns=COLUMNS)
    for column in ["loads", "customers", "res_customers", "com_customers", "shared_shapes", "shared_pairs_total"]:
        table[column] = table[column].astype("int64")
    for column in ["lines", "lines_without_units", "graph_parts", "service_transformers"]:
        table[column] = table[column].astype("Int64")
    for column in ["no_res", "no_com", "runnable_alone"]:
        table[column] = table[column].astype(bool)
    return table, other


def substation_check(table: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """Substations whose feeder sums of loads, customers or kW differ from the frame."""
    sums = table.groupby(["subregion", "substation"])[["loads", "customers", "load_kw"]].sum()
    frame_values = frame.loc[frame["in_frame"]].set_index(["subregion", "substation"])[
        ["loads", "customers", "load_kw"]
    ]
    joined = frame_values.merge(
        sums, left_index=True, right_index=True, how="outer", suffixes=("", "_features")
    )
    relative = (joined["load_kw_features"] - joined["load_kw"]).abs() / joined["load_kw"].abs().clip(lower=1e-12)
    bad = (
        (joined["loads"] != joined["loads_features"])
        | (joined["customers"] != joined["customers_features"])
        | ~(relative <= 1e-6)
    )
    return joined[bad].reset_index()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="UTC stamp of the output names")
    parser.add_argument("--frame-stamp", required=True, help="stamp of frame_<stamp>.parquet")
    parser.add_argument("--workers", type=int, default=16, help="download threads")
    parser.add_argument("--processes", type=int, default=8, help="reader processes")
    args = parser.parse_args(argv)
    started = time.perf_counter()

    settings = load_sample_settings()
    rule = settings["groups"]
    paths = Paths(load_local(), settings)
    out = paths.texas
    frame = pd.read_parquet(out / f"frame_{args.frame_stamp}.parquet")
    listing = pd.read_parquet(paths.listing)
    objects = opendss_objects(listing, settings)

    names = file_name_table(objects)
    print("file names inside feeder folders (objects, bytes):")
    print(names.to_string(index=False))

    feeders = frame_feeders(objects, frame)
    files = feeder_files(objects, feeders, rule["feeder_network_files"])
    network = files[files["file"] != LOADS_FILE]
    plan = network.groupby("file").agg(objects=("size", "size"), bytes=("size", "sum")).reset_index()
    print(f"frame feeders: {len(feeders)}")
    print("network files of the frame feeders (objects, bytes):")
    print(plan.to_string(index=False))
    total_gb = float(network["size"].sum()) / BYTES_PER_GB
    if total_gb > float(rule["download_max_gb"]):
        print(f"stopped: the plan holds {total_gb:.2f} GB, over {rule['download_max_gb']} GB", file=sys.stderr)
        return 1
    free_gb = shutil.disk_usage(paths.release_files.anchor).free / BYTES_PER_GB
    if free_gb - total_gb < float(rule["download_min_free_gb"]):
        print(f"stopped: {free_gb:.1f} GB free, under the guard", file=sys.stderr)
        return 1

    present = sum(paths.release_file(key).is_file() for key in network["key"])
    loaded = time.perf_counter()
    items = [(key, int(size), paths.release_file(key)) for key, size in zip(network["key"], network["size"])]
    result = oedi.fetch_many(settings["release"]["bucket_url"], items, args.workers, label="network files")
    print(
        f"network files: {result['downloaded']} downloaded ({result['bytes_downloaded'] / BYTES_PER_GB:.3f} GB), "
        f"{result['reused']} reused, {present} present before, "
        f"{time.perf_counter() - loaded:.0f} s"
    )
    if result["left"]:
        print(f"stopped: {len(result['left'])} files left by the guard", file=sys.stderr)
        return 1

    by_feeder = {
        (row.subregion, row.substation, row.feeder, row.file): str(paths.release_file(row.key))
        for row in files.itertuples(index=False)
    }
    tasks = []
    missing = []
    network_names = [name.lower() for name in rule["feeder_network_files"]]
    for row in feeders.itertuples(index=False):
        task = {key: getattr(row, key) for key in IDENTITY}
        identity = (row.subregion, row.substation, row.feeder)
        task["loads"] = by_feeder[(*identity, LOADS_FILE)]
        task["lines"] = by_feeder.get((*identity, network_names[0]))
        task["transformers"] = by_feeder.get((*identity, network_names[1]))
        task["default_unit"] = rule["line_units_default"]
        for name, key in zip(network_names, ("lines", "transformers")):
            if task[key] is None:
                missing.append({**{k: task[k] for k in IDENTITY}, "file": name})
        tasks.append(task)

    read_start = time.perf_counter()
    if args.processes > 1:
        with ProcessPoolExecutor(max_workers=args.processes) as pool:
            rows = list(pool.map(read_feeder, tasks, chunksize=32))
    else:
        rows = [read_feeder(task) for task in tasks]
    table, other = feature_table(rows)
    print(f"read {len(table)} feeders in {time.perf_counter() - read_start:.0f} s")
    print(f"loads of neither sector: {other}")
    write_parquet(table, out / f"features_{args.stamp}.parquet")
    if missing:
        write_csv(pd.DataFrame(missing), out / f"missing_network_files_{args.stamp}.csv")
    print(f"feeders missing a network file: {len({tuple(m[k] for k in IDENTITY) for m in missing})}")

    bad = substation_check(table, frame)
    print(f"substations whose sums differ from the frame: {len(bad)}")
    if len(bad):
        print(bad.head(20).to_string(index=False))
    print(f"written to {out} with stamp {args.stamp} in {time.perf_counter() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
