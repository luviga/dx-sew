"""Frame of whole substations of the SMART-DS Full Texas release.

One row per substation folder of the ``opendss`` tree of the scenario, built
from the listing of the release and from three kinds of small files: the
``Loads.dss`` of each feeder, and the ``Buscoords.dss`` and
``Transformers.dss`` of the substation folder.

Release: SMART-DS v0.9 Full Texas on the Open Energy Data Initiative data
lake, https://data.openei.org/s3_viewer?bucket=oedi-data-lake&prefix=SMART-DS%2Fv0.9%2F
"""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from . import dss, oedi
from .config import Paths

FEEDER_FILES = ("loads.dss", "loadshapes.dss")  # a feeder folder holds both
LOADS_FILE = "loads.dss"
SUBSTATION_FILES = ("buscoords.dss", "transformers.dss")


def opendss_objects(listing: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Objects of the ``opendss`` tree with their place in it.

    Adds the columns ``subregion``, ``substation``, ``feeder`` (empty for a
    file of the substation folder itself) and ``file`` (lower case). Folders
    named in ``release.not_substations`` are left out.
    """
    release = settings["release"]
    prefix = release["prefix"]
    tree = release["opendss_tree"].strip("/")
    depth = tree.count("/") + 1
    keys = listing["key"]
    relative = keys.str[len(prefix):]
    parts = relative.str.split("/")
    inside = keys.str.startswith(prefix) & (
        parts.str[1 : 1 + depth].str.join("/") == tree
    )
    objects = listing[inside].copy()
    parts = parts[inside]
    below = parts.str[1 + depth :]
    objects["subregion"] = parts.str[0]
    objects["substation"] = below.str[0]
    objects["feeder"] = below.str[1].where(below.str.len() == 3, "")
    objects["file"] = below.str[-1].str.lower()
    objects = objects[below.str.len().isin([2, 3])]
    return objects[~objects["substation"].isin(release["not_substations"])].reset_index(drop=True)


def substation_table(objects: pd.DataFrame) -> pd.DataFrame:
    """One row per substation from the listing alone: feeders and bytes."""
    in_feeder = objects[objects["feeder"] != ""]
    marks = in_feeder[in_feeder["file"].isin(FEEDER_FILES)]
    complete = marks.groupby(["subregion", "substation", "feeder"])["file"].nunique()
    feeders = (
        complete[complete == len(FEEDER_FILES)]
        .reset_index()
        .groupby(["subregion", "substation"])["feeder"]
        .agg(feeders="count")
    )
    table = objects.groupby(["subregion", "substation"]).agg(
        opendss_files=("size", "size"), opendss_bytes=("size", "sum")
    )
    table = table.join(feeders).fillna({"feeders": 0}).astype({"feeders": "int64"})
    table = table.reset_index()
    table.insert(1, "kind", table["subregion"].str[-1])
    return table


def frame_files(objects: pd.DataFrame) -> pd.DataFrame:
    """The objects the frame reads: feeder loads, substation coordinates and transformers."""
    feeder_loads = (objects["feeder"] != "") & (objects["file"] == LOADS_FILE)
    substation_level = (objects["feeder"] == "") & objects["file"].isin(SUBSTATION_FILES)
    return objects[feeder_loads | substation_level]


def download_frame_files(files: pd.DataFrame, settings: dict, paths: Paths, workers: int) -> dict:
    items = [
        (key, int(size), paths.release_file(key))
        for key, size in zip(files["key"], files["size"])
    ]
    return oedi.fetch_many(settings["release"]["bucket_url"], items, workers, label="frame files")


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def read_substation(task: dict) -> tuple[dict, list[str]]:
    """Read the files of one substation; returns its frame values and parquet names.

    ``task`` holds ``subregion``, ``substation``, ``loads`` (feeder name to
    path of its ``Loads.dss``), ``buscoords`` and ``transformers`` (paths or
    None).
    """
    loads: list[dss.Load] = []
    feeders_with_loads = 0
    for _, path in sorted(task["loads"].items()):
        feeder_loads = dss.parse_loads(_read(path))
        feeders_with_loads += bool(feeder_loads)
        loads.extend(feeder_loads)
    references, without_reference = dss.load_references(loads)
    coords = dss.parse_buscoords(_read(task["buscoords"])) if task["buscoords"] else {}
    buses = {load.bus for load in loads}
    located = [coords[bus] for bus in buses if bus in coords]
    ratings = dss.parse_transformer_kva(_read(task["transformers"])) if task["transformers"] else []
    count = len(located)
    row = {
        "subregion": task["subregion"],
        "substation": task["substation"],
        "feeders_with_loads": feeders_with_loads,
        "loads": len(loads),
        "customers": dss.customer_count(loads),
        "load_kw": float(sum(load.kw for load in loads)),
        "load_buses": len(buses),
        "load_buses_located": count,
        "centroid_x": sum(x for x, _ in located) / count if count else float("nan"),
        "centroid_y": sum(y for _, y in located) / count if count else float("nan"),
        "transformers": len(ratings),
        "transformer_kva": float(sum(ratings)) if ratings else float("nan"),
        "loads_without_reference": without_reference,
        "parquets_referenced": len(references),
    }
    return row, sorted(references)


def substation_tasks(objects: pd.DataFrame, paths: Paths) -> list[dict]:
    files = frame_files(objects)
    tasks: dict[tuple[str, str], dict] = {}
    for subregion, substation in (
        objects[["subregion", "substation"]].drop_duplicates().itertuples(index=False)
    ):
        tasks[(subregion, substation)] = {
            "subregion": subregion,
            "substation": substation,
            "loads": {},
            "buscoords": None,
            "transformers": None,
        }
    for row in files.itertuples(index=False):
        task = tasks[(row.subregion, row.substation)]
        path = str(paths.release_file(row.key))
        if row.feeder:
            task["loads"][row.feeder] = path
        elif row.file == "buscoords.dss":
            task["buscoords"] = path
        else:
            task["transformers"] = path
    return [tasks[key] for key in sorted(tasks)]


def build_frame(
    listing: pd.DataFrame, settings: dict, paths: Paths, workers: int, processes: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download what the frame needs and build it.

    Returns the frame (one row per substation) and the references (one row per
    substation and load-data parquet it refers to, with the size of that
    parquet in the listing; the size is missing when the release has no such
    file).
    """
    objects = opendss_objects(listing, settings)
    table = substation_table(objects)
    result = download_frame_files(frame_files(objects), settings, paths, workers)
    print(
        f"frame files: {result['downloaded']} downloaded, {result['reused']} reused",
        file=sys.stderr,
    )
    tasks = substation_tasks(objects, paths)
    if processes > 1:
        with ProcessPoolExecutor(max_workers=processes) as pool:
            outcomes = list(pool.map(read_substation, tasks, chunksize=16))
    else:
        outcomes = [read_substation(task) for task in tasks]
    rows = pd.DataFrame([row for row, _ in outcomes])
    frame = table.merge(rows, on=["subregion", "substation"], how="left", validate="one_to_one")

    load_data = settings["release"]["prefix"] + settings["release"]["load_data_folder"].strip("/") + "/"
    in_load_data = listing[listing["key"].str.startswith(load_data)]
    sizes = dict(zip(in_load_data["key"].str[len(load_data):], in_load_data["size"]))
    references = pd.DataFrame(
        [
            (row["subregion"], row["substation"], name)
            for row, names in outcomes
            for name in names
        ],
        columns=["subregion", "substation", "parquet"],
    )
    references["bytes"] = references["parquet"].map(sizes).astype("Int64")
    missing = references[references["bytes"].isna()].groupby(["subregion", "substation"]).size()
    frame = frame.merge(
        missing.rename("parquets_missing").reset_index(), on=["subregion", "substation"], how="left"
    ).fillna({"parquets_missing": 0})
    frame["parquets_missing"] = frame["parquets_missing"].astype("int64")
    return frame, references
