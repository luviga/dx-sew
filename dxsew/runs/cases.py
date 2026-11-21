"""Run feeders of the typical-feeder table one at a time through the coupling (decisions K61, K69).

Run from the repository::

    uv run python -m dxsew.runs.cases --stamp <stamp> --typical <csv> --features <parquet> \\
        --runs-root <folder> --base-local <coupling local.yaml> --max-staging-gb <GB> --min-free-gb <GB> \\
        [--setup <id>]

For the distinct ``run_feeder`` values of the typical-feeder table:

1. Staging. Their substations are staged with ``stage.stage()`` into a root of
   their own, ``<smart_ds_base>/<runs.staging_folder>``: the function gets a copy
   of the settings whose ``staging.folder`` is ``runs.staging_folder``, so the
   staged tree, its parquet folder and its ``name_map.csv`` are all new. The
   staging of an earlier sample (``staging.folder``) is never written. A zone
   ending with no code is added to the zone code table (setup key ``inputs.zone_codes``) with
   ``names.extend_zone_codes`` (codes already there keep their meaning) and the
   substations are staged again.
2. Alone trees. Each run feeder gets a copy of its staged feeder folder only, at
   ``<smart_ds_base>/<runs.alone_folder>/<feeder>/<subregion>/<opendss_relpath>/<substation>/<feeder>/``,
   so the coupling's feeder discovery finds that one feeder.
3. Settings folder ``<data_root>/<output_folder>/coupling_settings_runs_<stamp>/``:
   ``local.yaml`` (every key of the coupling ``local.yaml`` given, ``runs_root``
   absolute), ``topology_registry.yaml`` (one entry per run feeder) and
   ``pipeline_config.yaml`` with the setup's ``runs.pipeline_override`` (decision K22).
4. One case file per run feeder in ``<data_root>/<output_folder>/cases_runs_<stamp>/``,
   ``execution: max_feeders: 1``.
5. A dry run of the first case, phase ``pre``, that shows the circuit root and
   the parquet root the coupling resolves.

Coupling repository: https://github.com/DeltaE/dss-eulp-coupling (``run_case.py``
``resolve_case``: circuit root ``<smart_ds_base>/<smart_ds_relpath>/<circuit_folder>``,
parquet root ``<smart_ds_base>/<parquet_relpath>``).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml

from dxsew import config
from dxsew.config import Paths, input_path, load_local, load_runtime, load_sample_settings
from dxsew.frame import frame as frame_module
from dxsew.frame.sample import BYTES_PER_GB, get_listing, write_csv
from dxsew.runs import coupling as coupling_module
from dxsew.runs import settings as settings_module
from dxsew.runs import stage as stage_module
from dxsew.sources import oedi
from dxsew.sources import smartds_names as names

RUNS_KEYS = ("staging_folder", "alone_folder", "parallel", "pipeline_override")
# The pipeline_config.yaml of the runs is the setup's runs.pipeline_override
# (decision K22, the ResStock release of the setup's weather year).
UNKNOWN_ZONES = "zone endings with no code: "


def runs_settings(settings: dict) -> dict:
    """Copy of the sample settings whose staging root is ``runs.staging_folder``.

    The original mapping is not changed, so the staging folder of the sample
    (``staging.folder``) is never the target of a write made with the copy.
    """
    runs = settings.get("runs") or {}
    missing = [key for key in RUNS_KEYS if key not in runs]
    if missing:
        raise KeyError(f"the setup lacks runs keys: {', '.join(missing)}")
    if runs["staging_folder"] == settings["staging"]["folder"] or runs["alone_folder"] == settings["staging"]["folder"]:
        raise ValueError("runs folders must differ from staging.folder")
    if runs["staging_folder"] == runs["alone_folder"]:
        raise ValueError("runs.staging_folder and runs.alone_folder must differ")
    copied = copy.deepcopy(settings)
    copied["staging"]["folder"] = runs["staging_folder"]
    return copied


def short_feeder(feeder: str) -> str:
    """Part of a feeder folder name after ``--`` (the whole name when it has none)."""
    return feeder.split("--", 1)[1] if "--" in feeder else feeder


def run_units(typical: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """One row per distinct run feeder, with its place in the release and its groups."""
    groups = typical.groupby("run_feeder", sort=False).agg(
        groups=("group", "size"), group_customers=("customers", "sum"),
        strata=("stratum", lambda s: "; ".join(sorted(set(s)))),
        run_feeder_loads=("run_feeder_loads", "first"), run_feeder_customers=("run_feeder_customers", "first"),
        run_is_placeholder=("run_is_placeholder", "any"),
    ).reset_index().rename(columns={"run_feeder": "feeder"})
    found = features[["subregion", "substation", "feeder", "county", "stratum", "loads"]]
    units = groups.merge(found, on="feeder", how="left", validate="one_to_one")
    missing = units[units["subregion"].isna()]["feeder"].tolist()
    if missing:
        raise ValueError(f"run feeders not in the features file: {', '.join(missing)}")
    units["zone"] = units["stratum"].str.split("|", n=1).str[1]
    return units.sort_values(["run_feeder_loads", "feeder"], kind="mergesort").reset_index(drop=True)


def topology_id(settings: dict, unit: dict) -> str:
    """``TX_<subregion>_<substation>_<feeder>`` with the short feeder name (no ``--`` in ids)."""
    state = settings["coupling"]["topology_state"]
    return f"{state}_{unit['subregion']}_{unit['substation']}_{short_feeder(unit['feeder'])}"


def alone_relpath(settings: dict, unit: dict) -> str:
    """``smart_ds_relpath`` of a run feeder: its alone tree down to the opendss folder."""
    return f"{settings['runs']['alone_folder']}/{unit['feeder']}/{unit['subregion']}/{settings['staging']['opendss_relpath']}"


def alone_folder(paths: Paths, settings: dict, unit: dict) -> Path:
    """The feeder folder inside the run feeder's alone tree."""
    return paths.smart_ds_base / alone_relpath(settings, unit) / unit["substation"] / unit["feeder"]


def staged_feeder_folder(paths: Paths, settings: dict, unit: dict) -> Path:
    return stage_module.substation_folder(paths, settings, unit["subregion"], unit["substation"]) / unit["feeder"]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(load_runtime()["hash_block_bytes"]), b""):
            digest.update(block)
    return digest.hexdigest()


def build_alone_tree(source: Path, target: Path) -> tuple[int, int]:
    """Copy the files of one staged feeder folder to its alone tree; returns files and bytes.

    The copy is checked file by file with SHA-256. A target that already holds
    exactly the same files is kept as it is.
    """
    source, target = Path(source), Path(target)
    if not source.is_dir():
        raise FileNotFoundError(f"staged feeder folder missing: {source}")
    wanted = {p.relative_to(source).as_posix(): p for p in sorted(source.rglob("*")) if p.is_file()}
    if target.exists():
        have = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        if have != set(wanted) or any(_sha(target / rel) != _sha(src) for rel, src in wanted.items()):
            raise FileExistsError(f"{target} exists with other files than {source}")
    else:
        for rel, src in wanted.items():
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            if _sha(out) != _sha(src):
                raise OSError(f"copy of {src} differs from its source")
    return len(wanted), sum(p.stat().st_size for p in wanted.values())


def registry_entry(unit: dict, settings: dict) -> dict:
    """Registry entry of one run feeder, from ``settings.registry_entry`` of its substation."""
    entry = settings_module.registry_entry({**unit, "feeders": 1, "loads": unit["loads"]}, settings)
    entry["smart_ds_relpath"] = alone_relpath(settings, unit)
    entry["circuit_folder"] = unit["substation"]
    entry["parquet_relpath"] = f"{settings['staging']['folder']}/{settings['staging']['parquet_folder']}"
    entry["description"] = (
        f"{settings['release']['description']} {unit['subregion']} {unit['substation']}, feeder {unit['feeder']} alone, "
        f"{unit['county']} County, {unit['zone']}: 1 feeder, {int(unit['loads'])} loads"
    )
    return entry


def case_file(unit: dict, settings: dict) -> dict:
    """Case of one run feeder: its own topology, the donor of the settings, one feeder."""
    case = settings_module.case_file({**unit, "feeders": 1}, settings)
    topology = topology_id(settings, unit)
    case["case_id"] = f"{topology}__{settings['coupling']['donor_id']}"
    case["topology_id"] = topology
    case["execution"] = {"max_feeders": 1}
    return case


def write_settings_folder(units: list[dict], settings: dict, folder: Path, local: dict) -> list[Path]:
    """``local.yaml``, ``topology_registry.yaml`` and ``pipeline_config.yaml``; no ``given_days.csv``."""
    folder.mkdir(parents=True, exist_ok=False)
    registry = {"topologies": {topology_id(settings, unit): registry_entry(unit, settings) for unit in units}}
    local_path, registry_path, pipeline_path = written = [
        folder / "local.yaml", folder / "topology_registry.yaml", folder / "pipeline_config.yaml"]
    settings_module._dump(local, local_path)
    settings_module._dump(registry, registry_path)
    settings_module._dump(copy.deepcopy(settings["runs"]["pipeline_override"]), pipeline_path)
    return written


def write_cases(units: list[dict], settings: dict, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=False)
    written = []
    for unit in units:
        case = case_file(unit, settings)
        path = folder / f"{case['case_id']}.yaml"
        settings_module._dump(case, path)
        written.append(path)
    return written


def unknown_endings(result: pd.DataFrame) -> set[str]:
    """Zone endings that ``stage.stage()`` reported without a code."""
    found: set[str] = set()
    for note in result.get("note", pd.Series(dtype=str)).fillna(""):
        if note.startswith(UNKNOWN_ZONES):
            found |= set(note[len(UNKNOWN_ZONES):].split())
    return found


def prefetch_parquets(sample: pd.DataFrame, listing: pd.DataFrame, settings: dict, paths: Paths, stamp: str,
                      guard: stage_module.DiskGuard, workers: int) -> dict:
    """Place the released copies and download the released parquets they use, in parallel.

    ``stage.stage()`` then finds each parquet in its folder under the release
    name and renames it in place; it would otherwise download them one by one.
    Nothing here changes what the staging writes.
    """
    release = settings["release"]
    objects = frame_module.opendss_objects(listing, settings)
    load_data = release["prefix"] + release["load_data_folder"].strip("/") + "/"
    sizes = dict(zip(listing["key"], listing["size"].astype(int)))
    map_path = paths.staging / stage_module.NAME_MAP_NAME
    previous_map = names.read_map(map_path) if map_path.is_file() else []
    done_names = {r["released_name"] for r in previous_map if r["kind"] == "parquet"}
    items, seen, problems = [], set(), []
    for unit in sample.itertuples(index=False):
        mine = objects[(objects["subregion"] == unit.subregion) & (objects["substation"] == unit.substation)]
        try:
            stage_module.place_released(unit, mine, settings, paths, stamp, guard, previous_map, workers)
        except RuntimeError as error:
            problems.append(f"{unit.subregion}/{unit.substation}: {error}")
            continue
        shapes, parquets, _ = names.scan_folder(stage_module.released_folder(paths, stamp, unit.subregion, unit.substation))
        for shape in shapes.values():
            name = names.parquet_of(shape)
            key = load_data + name
            if shape.qty != "kw" or name not in parquets or name in seen or name in done_names or key not in sizes:
                continue
            seen.add(name)
            items.append((key, sizes[key], paths.staging_parquet / name))
    result = oedi.fetch_many(release["bucket_url"], items, workers, guard.allows, "parquets")
    result["problems"] = problems
    result["listed"] = len(items)
    return result


def dry_run(case: Path, settings_folder: Path, repo: Path, python: Path, log: Path) -> tuple[int, dict]:
    """``run_case.py --case <case> --phase pre --dry-run --settings <folder>``; returns the code and PIPELINE_ values."""
    cmd = [str(python), "run_case.py", "--case", str(Path(case).resolve()), "--phase", "pre", "--dry-run",
           "--settings", str(Path(settings_folder).resolve())]
    with Path(log).open("w", encoding="utf-8") as handle:
        done = subprocess.run(cmd, cwd=repo, stdout=handle, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONUNBUFFERED": "1"})
    values = {}
    for line in Path(log).read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(?P<key>PIPELINE_[A-Z_]+)=(?P<value>.*)$", line)
        if match:
            values[match.group("key")] = match.group("value").strip()
    return done.returncode, values


def _read_yaml(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a mapping.")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of this job, in every output name")
    parser.add_argument("--typical", type=Path, required=True, help="typical_feeders_<stamp>.csv")
    parser.add_argument("--features", type=Path, required=True,
                        help="features_<stamp>.parquet that gives each run feeder's subregion, substation and county")
    parser.add_argument("--runs-root", type=Path, required=True, help="absolute folder of the coupling runs")
    parser.add_argument("--base-local", type=Path, required=True, help="coupling local.yaml whose keys are all copied")
    parser.add_argument("--max-staging-gb", type=float, required=True)
    parser.add_argument("--min-free-gb", type=float, required=True)
    most = load_runtime()["workers"]["cases_threads"]
    parser.add_argument("--workers", type=int, default=most, help=f"download threads (at most {most})")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= most:
        parser.error(f"--workers must be 1 to {most}")
    config.use_setup(args.setup)

    base_settings = load_sample_settings()
    settings = runs_settings(base_settings)
    local = load_local()
    paths = Paths(local, settings)
    typical = pd.read_csv(args.typical)
    units = run_units(typical, pd.read_parquet(args.features))
    sample = units[["subregion", "substation"]].drop_duplicates().sort_values(["subregion", "substation"])
    sample = sample.reset_index(drop=True)
    print(f"{len(units)} run feeders in {len(sample)} substations; staging root {paths.staging}")

    # 1. Staging into the runs root.
    zone_path = input_path(settings["inputs"]["zone_codes"])
    listing = get_listing(settings, paths, refresh=False)
    paths.staging_parquet.mkdir(parents=True, exist_ok=True)
    guard = stage_module.DiskGuard(paths.staging, args.max_staging_gb * BYTES_PER_GB, args.min_free_gb * BYTES_PER_GB)
    pre = prefetch_parquets(sample, listing, settings, paths, args.stamp, guard, args.workers)
    print(f"prefetch: {pre['listed']} parquets listed, {pre['downloaded']} downloaded "
          f"({pre['bytes_downloaded']} bytes), {pre['reused']} reused, stopped {pre['stopped']}")
    added: dict[str, str] = {}
    for again in (False, True):  # stage, and once more after new zone codes
        codes = names.read_zone_codes(zone_path)
        result, checks, map_rows, _ = stage_module.stage(sample, listing, settings, paths, args.stamp, codes,
                                                          guard, args.workers)
        names.write_map(map_rows, paths.staging / stage_module.NAME_MAP_NAME)
        new = unknown_endings(result)
        if not new or again:
            break
        extended = names.extend_zone_codes(codes, new)
        added = {ending: extended[ending] for ending in sorted(new)}
        names.write_zone_codes(extended, zone_path)
        print(f"zone endings added to {zone_path.name}: {added}; staging again")
    shutil.copyfile(paths.staging / stage_module.NAME_MAP_NAME, paths.output / f"name_map_runs_{args.stamp}.csv")
    result["zone_codes_added"] = " ".join(f"{k}={v}" for k, v in added.items())
    write_csv(result, paths.output / f"staging_runs_{args.stamp}.csv")
    write_csv(checks, paths.output / f"checks_runs_{args.stamp}.csv")
    staged = set(zip(result.loc[result["staged"].astype(bool), "subregion"],
                     result.loc[result["staged"].astype(bool), "substation"]))

    # 2. Alone trees.
    rows = []
    for unit in units.to_dict("records"):
        row = {key: unit[key] for key in ("feeder", "subregion", "substation", "strata", "groups", "group_customers",
                                          "run_feeder_loads", "run_feeder_customers", "run_is_placeholder")}
        row.update(topology_id=topology_id(settings, unit), staged=(unit["subregion"], unit["substation"]) in staged,
                   alone_folder=alone_folder(paths, settings, unit).as_posix(), alone_files=0, alone_bytes=0, note="")
        if row["staged"]:
            try:
                row["alone_files"], row["alone_bytes"] = build_alone_tree(staged_feeder_folder(paths, settings, unit),
                                                                          alone_folder(paths, settings, unit))
            except (OSError, FileExistsError) as error:
                row["staged"], row["note"] = False, f"alone tree: {error}"
        else:
            row["note"] = "substation not staged"
        rows.append(row)
    table = pd.DataFrame(rows)
    ready = [u for u, r in zip(units.to_dict("records"), rows) if r["staged"]]

    # 3 and 4. Settings folder and cases.
    coupling_local = settings_module.local_settings(_read_yaml(args.base_local), {}, args.runs_root)
    folder = paths.output / f"coupling_settings_runs_{args.stamp}"
    written = write_settings_folder(ready, settings, folder, coupling_local)
    cases = write_cases(ready, settings, paths.output / f"cases_runs_{args.stamp}")
    by_topology = {yaml.safe_load(p.read_text(encoding="utf-8"))["topology_id"]: p for p in cases}
    table["case_file"] = table["topology_id"].map(lambda t: by_topology[t].as_posix() if t in by_topology else "")
    write_csv(table, paths.output / f"runs_{args.stamp}.csv")

    # 5. Dry run of the first case.
    if ready:
        clone = coupling_module.read_local()
        repo, python = Path(clone["coupling_repo"]), Path(clone["coupling_python"])
        print(f"pin ok: {coupling_module.check_pin(repo)}")
        first = ready[0]
        log = paths.output / f"dry_run_runs_{args.stamp}.txt"
        code, values = dry_run(by_topology[topology_id(settings, first)], folder, repo, python, log)
        print(f"pin ok: {coupling_module.check_pin(repo)}")
        expect_root = (paths.smart_ds_base / alone_relpath(settings, first) / first["substation"]).as_posix()
        expect_parquet = paths.staging_parquet.as_posix()
        print(f"dry run exit {code}: PIPELINE_SMART_DS_ROOT={values.get('PIPELINE_SMART_DS_ROOT')} "
              f"(expected {expect_root}); PIPELINE_PARQUET_ROOT={values.get('PIPELINE_PARQUET_ROOT')} "
              f"(expected {expect_parquet})")
        ok = (code == 0 and values.get("PIPELINE_SMART_DS_ROOT") == expect_root
              and values.get("PIPELINE_PARQUET_ROOT") == expect_parquet)
        print(f"dry run gate: {'passed' if ok else 'FAILED'}")

    print(result.to_string(index=False))
    print(f"staged substations: {int(result['staged'].sum())} of {len(result)}")
    print(f"run feeders ready: {len(ready)} of {len(units)}; alone files {int(table['alone_files'].sum())}, "
          f"bytes {int(table['alone_bytes'].sum())}")
    print(f"staging folder size: {stage_module.folder_bytes(paths.staging) / BYTES_PER_GB:.3f} GB; "
          f"free on its drive: {guard.free_bytes() / BYTES_PER_GB:.1f} GB")
    if guard.reason:
        print(f"disk guard stopped the staging: {guard.reason}")
    for path in [*written, *cases[: load_runtime()["print_paths"]]]:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
