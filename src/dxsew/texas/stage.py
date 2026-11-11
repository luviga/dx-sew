"""Staging of the sampled substations for DeltaE/dss-eulp-coupling.

Run from the repository::

    uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB>

For each substation of ``sample_<stamp>.csv`` the ``opendss`` substation
folder of the release is downloaded to
``<staging>/<subregion>/<opendss_relpath>/<substation>/`` and the load-data
parquets its loads refer to are downloaded, flat, into the shared parquet
folder of the staging. Files already there with the listed size are skipped.

The profile CSV files of the release (``profiles/``) are not staged: no phase
of the coupling reads them. Phase 7b rewrites every load shape to the day
profiles it built itself (``7_circuit_instantiation/instantiate_circuits.py``
of DeltaE/dss-eulp-coupling, the loop over ``New Loadshape`` lines).

The two disk limits are arguments, not settings: they belong to the machine.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import pandas as pd

from . import dss, frame as frame_module, oedi
from .config import Paths, load_local, load_sample_settings
from .sample import BYTES_PER_GB, get_listing, write_csv


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

    def allows(self, size: int) -> bool:
        if self.staged_bytes + size > self.max_staging_bytes:
            self.reason = "the staging folder would pass its limit"
            return False
        if self.free_bytes() - size < self.min_free_bytes:
            self.reason = "free space on the drive would fall below its limit"
            return False
        self.staged_bytes += size
        return True


def substation_folder(paths: Paths, settings: dict, subregion: str, substation: str) -> Path:
    return paths.staging / subregion / settings["staging"]["opendss_relpath"] / substation


def opendss_items(objects: pd.DataFrame, target: Path) -> list[tuple[str, int, Path]]:
    """``(key, size, target path)`` of every object of one substation folder."""
    items = []
    for row in objects.itertuples(index=False):
        name = row.key.rsplit("/", 1)[-1]
        items.append((row.key, int(row.size), target / row.feeder / name if row.feeder else target / name))
    return items


def staged_references(folder: Path) -> set[str]:
    """Parquet names the loads of a staged substation folder refer to."""
    names: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name.lower() == frame_module.LOADS_FILE:
            loads = dss.parse_loads(path.read_text(encoding="utf-8", errors="replace"))
            names |= dss.load_references(loads)[0]
    return names


def parquet_items(names: set[str], listing_sizes: dict[str, int], settings: dict, paths: Paths):
    """Download items of the parquets named, and the names the release lacks."""
    prefix = settings["release"]["prefix"] + settings["release"]["load_data_folder"].strip("/") + "/"
    items, missing = [], []
    for name in sorted(names):
        if name in listing_sizes:
            items.append((prefix + name, listing_sizes[name], paths.staging_parquet / name))
        else:
            missing.append(name)
    return items, missing


def stage(sample: pd.DataFrame, listing: pd.DataFrame, settings: dict, paths: Paths,
          guard: DiskGuard, workers: int) -> pd.DataFrame:
    """Stage the substations of the sample in order; one result row per substation."""
    bucket = settings["release"]["bucket_url"]
    objects = frame_module.opendss_objects(listing, settings)
    load_data = settings["release"]["prefix"] + settings["release"]["load_data_folder"].strip("/") + "/"
    in_load_data = listing[listing["key"].str.startswith(load_data)]
    sizes = dict(zip(in_load_data["key"].str[len(load_data):], in_load_data["size"].astype(int)))

    rows = []
    for unit in sample.itertuples(index=False):
        row = {"subregion": unit.subregion, "substation": unit.substation, "staged": False}
        if guard.reason:
            rows.append({**row, "note": f"not started: {guard.reason}"})
            continue
        target = substation_folder(paths, settings, unit.subregion, unit.substation)
        mine = objects[(objects["subregion"] == unit.subregion) & (objects["substation"] == unit.substation)]
        circuit = oedi.fetch_many(bucket, opendss_items(mine, target), workers, guard.allows, "opendss")
        row.update(opendss_files=len(mine), opendss_bytes=int(mine["size"].sum()))
        if circuit["stopped"]:
            rows.append({**row, "note": f"stopped in the opendss folder: {guard.reason}"})
            continue
        names = staged_references(target)
        items, missing = parquet_items(names, sizes, settings, paths)
        parquets = oedi.fetch_many(bucket, items, workers, guard.allows, "parquets")
        row.update(
            parquets=len(names),
            parquet_bytes=sum(size for _, size, _ in items),
            parquets_downloaded=parquets["downloaded"],
            parquets_already_there=parquets["reused"],
            parquets_not_in_release=len(missing),
        )
        if parquets["stopped"]:
            row["note"] = f"stopped in the parquets: {guard.reason}"
        elif missing:
            row["note"] = "parquets missing in the release: " + " ".join(missing)
        else:
            row["staged"] = True
            row["note"] = ""
        rows.append(row)
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of the sample to stage")
    parser.add_argument("--max-staging-gb", type=float, required=True,
                        help="staging stops before the staging folder passes this size")
    parser.add_argument("--min-free-gb", type=float, required=True,
                        help="staging stops before free space on its drive falls below this")
    parser.add_argument("--workers", type=int, default=16, help="download threads")
    args = parser.parse_args(argv)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    sample = pd.read_csv(paths.texas / f"sample_{args.stamp}.csv")
    listing = get_listing(settings, paths, refresh=False)
    paths.staging_parquet.mkdir(parents=True, exist_ok=True)
    guard = DiskGuard(paths.staging, args.max_staging_gb * BYTES_PER_GB, args.min_free_gb * BYTES_PER_GB)

    result = stage(sample, listing, settings, paths, guard, args.workers)
    write_csv(result, paths.texas / f"staging_{args.stamp}.csv")
    print(result.to_string(index=False))
    print(f"staging folder: {paths.staging}")
    print(f"staged substations: {int(result['staged'].sum())} of {len(result)}")
    print(f"staging folder size: {folder_bytes(paths.staging) / BYTES_PER_GB:.3f} GB")
    print(f"free space on its drive: {guard.free_bytes() / BYTES_PER_GB:.1f} GB")
    if guard.reason:
        print(f"disk guard stopped the staging: {guard.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
