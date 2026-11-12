"""Staging of the sampled substations for DeltaE/dss-eulp-coupling, with digit-only names.

Run from the repository::

    uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB> \\
        [--previous-sample <csv>]

For each substation of ``sample_<stamp>.csv``:

1. Released copy. An untouched copy of its released ``opendss`` substation
   folder is placed in ``<data_root>/texas/released_<stamp>/<subregion>/<substation>/``.
   A folder already staged under the release names is copied when every file
   size matches the listing; otherwise the folder is listed with ListObjectsV2
   and downloaded. The copy is the reference of the checks and is never edited.
2. Names. Every token of its ``.dss`` files that looks like a load shape name
   is parsed with an anchored pattern (``names.LOADSHAPE``). A token that fits
   no form, or a zone ending with no code in ``configs/texas_zone_codes.csv``,
   stops the staging of that substation.
3. Staged tree. ``<staging>/<subregion>/<opendss_relpath>/<substation>/`` is
   rebuilt from the released copy, every load shape name replaced as a whole
   token by its digit-only name in every ``.dss`` file, nothing else changed.
4. Parquets. Each released parquet its loads refer to gets its SHA-256 and its
   new name in ``<staging>/<parquet_folder>/``: renamed in place when present,
   downloaded when absent. The EULP donor folders are not touched.
5. Name map and checks. ``<staging>/name_map.csv`` (copied to
   ``name_map_<stamp>.csv`` in the data folder) has one row per name. The four
   checks of ``names`` run on the substation; a failure stops it and its staged
   folder is removed.

The profile CSV files of the release (``profiles/``) are not staged: phase 7b of
the coupling rewrites every load shape to the day profiles it built itself
(``7_circuit_instantiation/instantiate_circuits.py``, the loop over
``New Loadshape`` lines). The two disk limits are arguments: they belong to the machine.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import requests

from . import dss, frame as frame_module, names, oedi
from .config import Paths, find_configs, load_local, load_sample_settings
from .sample import BYTES_PER_GB, get_listing, write_csv

ZONE_CODES_NAME = "texas_zone_codes.csv"
NAME_MAP_NAME = "name_map.csv"


def folder_bytes(folder: Path) -> int:
    """Bytes of all files below ``folder`` (0 when it does not exist)."""
    total = 0
    stack = [str(folder)] if Path(folder).is_dir() else []
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
    return total


class DiskGuard:
    """Says whether one more download keeps the staging inside its two limits."""

    def __init__(self, staging: Path, max_staging_bytes: float, min_free_bytes: float):
        self.staging = staging
        self.max_staging_bytes = max_staging_bytes
        self.min_free_bytes = min_free_bytes
        self.staged_bytes = folder_bytes(staging)
        self.reason: str | None = None

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.staging).free

    def allows(self, size: int, counts_in_staging: bool = True) -> bool:
        if counts_in_staging and self.staged_bytes + size > self.max_staging_bytes:
            self.reason = "the staging folder would pass its limit"
            return False
        if self.free_bytes() - size < self.min_free_bytes:
            self.reason = "free space on the drive would fall below its limit"
            return False
        if counts_in_staging:
            self.staged_bytes += size
        return True


def substation_folder(paths: Paths, settings: dict, subregion: str, substation: str) -> Path:
    return paths.staging / subregion / settings["staging"]["opendss_relpath"] / substation


def released_folder(paths: Paths, stamp: str, subregion: str, substation: str) -> Path:
    return paths.texas / f"released_{stamp}" / subregion / substation


def opendss_items(objects: pd.DataFrame, target: Path) -> list[tuple[str, int, Path]]:
    """``(key, size, target path)`` of every object of one substation folder."""
    items = []
    for row in objects.itertuples(index=False):
        name = row.key.rsplit("/", 1)[-1]
        items.append((row.key, int(row.size), target / row.feeder / name if row.feeder else target / name))
    return items


def expected_files(objects: pd.DataFrame) -> dict[str, int]:
    """``relative path -> size`` of one substation folder from the listing."""
    expected = {}
    for row in objects.itertuples(index=False):
        name = row.key.rsplit("/", 1)[-1]
        expected[f"{row.feeder}/{name}" if row.feeder else name] = int(row.size)
    return expected


def actual_files(folder: Path) -> dict[str, int]:
    if not Path(folder).is_dir():
        return {}
    return {p.relative_to(folder).as_posix(): p.stat().st_size for p in Path(folder).rglob("*") if p.is_file()}


def staged_references(folder: Path) -> set[str]:
    """Parquet names the loads of a folder staged under the release names refer to."""
    found: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name.lower() == frame_module.LOADS_FILE:
            loads = dss.parse_loads(path.read_text(encoding="utf-8", errors="replace"))
            found |= dss.load_references(loads)[0]
    return found


def holds_new_names(folder: Path, previous_map: list[dict]) -> bool:
    """True when a folder holds a token that the staging name map lists only as a new name."""
    new = {r["new_name"] for r in previous_map if r["kind"] == "loadshape"}
    old = {r["released_name"] for r in previous_map if r["kind"] == "loadshape"}
    for path in names.dss_files(folder):
        for _, token in names.shape_tokens(path.read_bytes()):
            if token in new and token not in old:
                return True
    return False


def place_released(unit, objects: pd.DataFrame, settings: dict, paths: Paths, stamp: str,
                   guard: DiskGuard, previous_map: list[dict], workers: int) -> dict:
    """Place the released copy of one substation (step 4); returns where it came from."""
    target = released_folder(paths, stamp, unit.subregion, unit.substation)
    expected = expected_files(objects)
    if actual_files(target) == expected:
        return {"released_source": "already in place"}
    staged = substation_folder(paths, settings, unit.subregion, unit.substation)
    if actual_files(staged) == expected and not holds_new_names(staged, previous_map):
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staged, target)
        source = "copy of the folder staged under the release names"
    else:
        release = settings["release"]
        prefix = f"{release['prefix']}{unit.subregion}/{release['opendss_tree'].strip('/')}/{unit.substation}/"
        found = oedi.list_prefix(release["bucket_url"], prefix)
        if dict(zip(found["key"], found["size"])) != dict(zip(objects["key"], objects["size"])):
            raise RuntimeError(f"{prefix}: ListObjectsV2 gives other keys or sizes than the saved listing")
        listed = objects
        done = oedi.fetch_many(
            release["bucket_url"], opendss_items(listed, target), workers,
            lambda size: guard.allows(size, counts_in_staging=False), "released",
        )
        if done["stopped"]:
            raise RuntimeError(f"disk guard before the released copy: {guard.reason}")
        source = "downloaded from OEDI"
    if actual_files(target) != expected:
        raise RuntimeError(f"{target}: released copy does not match the listing")
    return {"released_source": source}


def build_staged_tree(released: Path, staged: Path, forward: dict[bytes, bytes]) -> tuple[int, int]:
    """Rebuild the staged folder from the released copy; returns files and bytes written."""
    if staged.exists():
        shutil.rmtree(staged)
    files = written = 0
    for path in sorted(released.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.suffix.lower() == ".dss":
            data = names.replace_tokens(data, forward)
        out = staged / path.relative_to(released)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        files += 1
        written += len(data)
    return files, written


class ParquetConflict(RuntimeError):
    pass


def stage_parquet(released_name: str, new_name: str, size: int, key: str, folder: Path,
                  known_sha: dict[str, str], guard: DiskGuard, bucket_url: str,
                  session: requests.Session) -> tuple[str, str]:
    """Give one released parquet its new name in the parquet folder; returns its SHA-256 and the action."""
    old, new = folder / released_name, folder / new_name

    def settle(sha: str, action: str) -> tuple[str, str]:
        if new.exists():
            if names.sha256_of(new) != sha:
                raise ParquetConflict(f"{new_name} exists with another SHA-256 than {released_name}")
            old.unlink()
            return sha, f"{action}; new name already held the same bytes"
        old.replace(new)
        return sha, action

    if old.is_file() and old.stat().st_size == size:
        return settle(names.sha256_of(old), "renamed in place")
    if new.is_file() and new.stat().st_size == size and released_name in known_sha:
        if names.sha256_of(new) == known_sha[released_name]:
            return known_sha[released_name], "already renamed"
    if not guard.allows(size):
        raise RuntimeError(f"disk guard before {released_name}: {guard.reason}")
    oedi.fetch(session, oedi.key_url(bucket_url, key), old, size)
    return settle(names.sha256_of(old), "downloaded and renamed")


def stage(sample: pd.DataFrame, listing: pd.DataFrame, settings: dict, paths: Paths, stamp: str,
          codes: dict[str, str], guard: DiskGuard, workers: int) -> tuple[pd.DataFrame, pd.DataFrame, list[dict], dict]:
    """Stage the substations of the sample in order.

    Returns one result row per substation, one row per substation and check,
    the name map rows and the released parquet names per staged substation.
    """
    release = settings["release"]
    bucket = release["bucket_url"]
    objects = frame_module.opendss_objects(listing, settings)
    load_data = release["prefix"] + release["load_data_folder"].strip("/") + "/"
    profiles = release["prefix"] + "profiles/"
    keys = set(listing["key"])
    sizes = dict(zip(listing["key"], listing["size"].astype(int)))
    map_path = paths.staging / NAME_MAP_NAME
    previous_map = names.read_map(map_path) if map_path.is_file() else []
    known_sha = {r["released_name"]: r["sha256"] for r in previous_map if r["kind"] == "parquet" and r["sha256"]}

    def profile_key(name: str) -> str:
        key = f"{profiles}{name}.csv"
        return key if key in keys else ""

    rows, checks, all_map, referenced = [], [], {}, {}
    session = requests.Session()
    for unit in sample.itertuples(index=False):
        row = {"subregion": unit.subregion, "substation": unit.substation, "staged": False, "note": ""}
        rows.append(row)
        if guard.reason:
            row["note"] = f"not started: {guard.reason}"
            continue
        mine = objects[(objects["subregion"] == unit.subregion) & (objects["substation"] == unit.substation)]
        try:
            row.update(place_released(unit, mine, settings, paths, stamp, guard, previous_map, workers))
        except RuntimeError as error:
            row["note"] = f"released copy: {error}"
            continue
        released = released_folder(paths, stamp, unit.subregion, unit.substation)
        shapes, parquets, problems = names.scan_folder(released)
        if problems:
            row["note"] = "names that fit no form: " + "; ".join(problems[:20])
            continue
        unknown = sorted({s.zone for s in shapes.values()} - set(codes))
        if unknown:
            row["note"] = "zone endings with no code: " + " ".join(unknown)
            continue
        staged = substation_folder(paths, settings, unit.subregion, unit.substation)
        sha: dict[str, str] = {}
        actions: dict[str, int] = {}
        try:
            for shape in sorted(shapes.values(), key=lambda s: s.released):
                released_parquet = names.parquet_of(shape)
                if shape.qty != "kw" or released_parquet not in parquets or released_parquet in sha:
                    continue
                key = load_data + released_parquet
                if key not in sizes:
                    raise ParquetConflict(f"{released_parquet} is not in the release")
                value, action = stage_parquet(
                    released_parquet, names.new_parquet(shape, codes), sizes[key], key,
                    paths.staging_parquet, known_sha, guard, bucket, session,
                )
                sha[released_parquet] = known_sha[released_parquet] = value
                actions[action] = actions.get(action, 0) + 1
        except (ParquetConflict, RuntimeError) as error:
            row["note"] = f"parquets: {error}"
            continue
        unit_rows = names.map_rows(shapes.values(), codes, profile_key, lambda n: load_data + n, sha)
        forward, _ = names.loadshape_maps(unit_rows)
        files, written = build_staged_tree(released, staged, forward)
        results = {
            "1 same loads": names.check_same_loads(released, staged),
            "2 clean": names.check_clean(released, staged, unit_rows, paths.staging_parquet),
            "3 nothing lost": names.check_nothing_lost(released, staged, unit_rows),
            "4 map": names.check_map(staged, unit_rows, names.staged_parquets(staged)),
        }
        for check, failures in results.items():
            checks.append({
                "subregion": unit.subregion, "substation": unit.substation, "check": check,
                "passed": not failures, "failures": len(failures), "detail": "; ".join(failures[:20]),
            })
        row.update(
            staged_files=files, staged_bytes=written, loadshape_names=len(shapes), parquets=len(sha),
            parquet_bytes=sum(sizes[load_data + n] for n in sha),
            **{f"parquets_{k.split(';')[0].replace(' ', '_')}": v for k, v in actions.items()},
        )
        if any(results.values()):
            shutil.rmtree(staged)
            row["note"] = "a check failed; staged folder removed"
            continue
        clash = [r["released_name"] for r in unit_rows
                 if r["released_name"] in all_map and all_map[r["released_name"]]["new_name"] != r["new_name"]]
        if clash:
            shutil.rmtree(staged)
            row["note"] = "name map disagrees with an earlier substation: " + " ".join(clash[:20])
            continue
        all_map.update({r["released_name"]: r for r in unit_rows})
        referenced[(unit.subregion, unit.substation)] = set(sha)
        row["staged"] = True
    session.close()
    map_rows = sorted(all_map.values(), key=lambda r: (r["kind"], r["released_name"]))
    return pd.DataFrame(rows), pd.DataFrame(checks), map_rows, referenced


def recheck(sample: pd.DataFrame, settings: dict, paths: Paths, stamp: str, map_rows: list[dict]) -> pd.DataFrame:
    """Run the four checks again on the staged tree and the name map, with no change made."""
    checks = []
    for unit in sample.itertuples(index=False):
        released = released_folder(paths, stamp, unit.subregion, unit.substation)
        staged = substation_folder(paths, settings, unit.subregion, unit.substation)
        if not staged.is_dir() or not released.is_dir():
            checks.append({"subregion": unit.subregion, "substation": unit.substation, "check": "all",
                           "passed": False, "failures": 1, "detail": "staged or released folder missing"})
            continue
        shapes, _, _ = names.scan_folder(released)
        released_names = set(shapes) | {names.parquet_of(s) for s in shapes.values() if s.qty == "kw"}
        unit_rows = [r for r in map_rows if r["released_name"] in released_names]
        results = {
            "1 same loads": names.check_same_loads(released, staged),
            "2 clean": names.check_clean(released, staged, unit_rows, paths.staging_parquet),
            "3 nothing lost": names.check_nothing_lost(released, staged, unit_rows),
            "4 map": names.check_map(staged, map_rows, names.staged_parquets(staged)),
        }
        for check, failures in results.items():
            checks.append({"subregion": unit.subregion, "substation": unit.substation, "check": check,
                           "passed": not failures, "failures": len(failures), "detail": "; ".join(failures[:20])})
    return pd.DataFrame(checks)


def dropped_substations(previous: pd.DataFrame, sample: pd.DataFrame, settings: dict, paths: Paths,
                        referenced: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Substations of an earlier sample that left this one, and the parquets they share with it.

    Their staged folders stay on disk as they are and are never registered.
    """
    current = set(zip(sample["subregion"], sample["substation"]))
    renamed = set().union(*referenced.values()) if referenced else set()
    left, shared = [], []
    for unit in previous.itertuples(index=False):
        if (unit.subregion, unit.substation) in current:
            continue
        folder = substation_folder(paths, settings, unit.subregion, unit.substation)
        refs = staged_references(folder) if folder.is_dir() else set()
        left.append({"subregion": unit.subregion, "substation": unit.substation,
                     "staged_folder_present": folder.is_dir(), "parquets_referenced": len(refs),
                     "parquets_renamed_by_this_staging": len(refs & renamed)})
        shared += [{"subregion": unit.subregion, "substation": unit.substation, "released_parquet": name}
                   for name in sorted(refs & renamed)]
    return pd.DataFrame(left), pd.DataFrame(shared, columns=["subregion", "substation", "released_parquet"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of the sample to stage")
    parser.add_argument("--max-staging-gb", type=float, required=True,
                        help="staging stops before the staging folder passes this size")
    parser.add_argument("--min-free-gb", type=float, required=True,
                        help="staging stops before free space on its drive falls below this")
    parser.add_argument("--previous-sample", type=Path, default=None,
                        help="sample CSV of an earlier staging, to list substations that left the sample")
    parser.add_argument("--check-only", action="store_true",
                        help="run the four checks again on the staged tree and change nothing")
    parser.add_argument("--workers", type=int, default=16, help="download threads")
    args = parser.parse_args(argv)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    sample = pd.read_csv(paths.texas / f"sample_{args.stamp}.csv")
    if args.check_only:
        map_rows = names.read_map(paths.staging / NAME_MAP_NAME)
        checks = recheck(sample, settings, paths, args.stamp, map_rows)
        print(checks.to_string(index=False))
        print(f"checks passed: {int(checks['passed'].sum())} of {len(checks)}")
        return 0 if bool(checks["passed"].all()) else 1

    codes = names.read_zone_codes(find_configs() / ZONE_CODES_NAME)
    listing = get_listing(settings, paths, refresh=False)
    paths.staging_parquet.mkdir(parents=True, exist_ok=True)
    guard = DiskGuard(paths.staging, args.max_staging_gb * BYTES_PER_GB, args.min_free_gb * BYTES_PER_GB)

    result, checks, map_rows, referenced = stage(sample, listing, settings, paths, args.stamp, codes, guard, args.workers)
    names.write_map(map_rows, paths.staging / NAME_MAP_NAME)
    shutil.copyfile(paths.staging / NAME_MAP_NAME, paths.texas / f"name_map_{args.stamp}.csv")
    write_csv(result, paths.texas / f"staging_{args.stamp}.csv")
    write_csv(checks, paths.texas / f"checks_{args.stamp}.csv")
    if args.previous_sample:
        left, shared = dropped_substations(pd.read_csv(args.previous_sample), sample, settings, paths, referenced)
        write_csv(left, paths.texas / f"left_sample_{args.stamp}.csv")
        write_csv(shared, paths.texas / f"parquets_shared_with_left_{args.stamp}.csv")
        print(left.to_string(index=False))
    print(result.to_string(index=False))
    print(checks.to_string(index=False))
    print(f"name map rows: {len(map_rows)}")
    print(f"staging folder: {paths.staging}")
    print(f"staged substations: {int(result['staged'].sum())} of {len(result)}")
    print(f"staging folder size: {folder_bytes(paths.staging) / BYTES_PER_GB:.3f} GB")
    print(f"free space on its drive: {guard.free_bytes() / BYTES_PER_GB:.1f} GB")
    if guard.reason:
        print(f"disk guard stopped the staging: {guard.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
