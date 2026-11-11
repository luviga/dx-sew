"""Coupling settings of the staged substations.

Run from the repository::

    uv run python -m dxsew.texas.settings --stamp <stamp>

Writes, under ``<data_root>/texas/coupling_settings/``, the inputs that
DeltaE/dss-eulp-coupling reads for a case:

- ``topology_registry.yaml`` with one entry per staged substation, in the
  keys of ``configs/topology_registry.yaml`` of the coupling;
- ``cases/<case_id>.yaml`` with one case per staged substation.

The circuit root of an entry is ``<smart_ds_base>/<smart_ds_relpath>/<circuit_folder>``
and its parquet root is ``<smart_ds_base>/<parquet_relpath>`` (``run_case.py``
of the coupling, function ``resolve_case``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

from .config import Paths, load_local, load_sample_settings

REGISTRY_KEYS = (
    "topology_state",
    "geography",
    "urbanicity",
    "feeder_id",
    "smart_ds_relpath",
    "circuit_folder",
    "parquet_relpath",
    "status",
    "description",
)


def topology_id(state: str, subregion: str, substation: str) -> str:
    return f"{state}_{subregion}_{substation}"


def registry_entry(unit: dict, settings: dict) -> dict:
    """Registry entry of one staged substation (``unit`` is its sample row)."""
    staging, coupling = settings["staging"], settings["coupling"]
    description = (
        f"SMART-DS v0.9 Full Texas {unit['subregion']} {unit['substation']}, "
        f"{unit['county']} County, {unit['zone']}: "
        f"{int(unit['feeders'])} feeders, {int(unit['loads'])} loads"
    )
    values = {
        "topology_state": coupling["topology_state"],
        "geography": f"{unit['county']} County",
        "urbanicity": unit["subregion"],
        "feeder_id": unit["substation"],
        "smart_ds_relpath": f"{staging['folder']}/{unit['subregion']}/{staging['opendss_relpath']}",
        "circuit_folder": unit["substation"],
        "parquet_relpath": f"{staging['folder']}/{staging['parquet_folder']}",
        "status": coupling["status"],
        "description": description,
    }
    return {key: values[key] for key in REGISTRY_KEYS}


def case_file(unit: dict, settings: dict) -> dict:
    """Case of one staged substation: its topology with the donor of the settings."""
    coupling = settings["coupling"]
    topology = topology_id(coupling["topology_state"], unit["subregion"], unit["substation"])
    return {
        "case_id": f"{topology}__{coupling['donor_id']}",
        "topology_id": topology,
        "donor_id": coupling["donor_id"],
        "execution": {"max_feeders": int(unit["feeders"])},
    }


def staged_units(sample: pd.DataFrame, staging: pd.DataFrame) -> list[dict]:
    """Sample rows of the substations that the staging marked as staged."""
    done = staging[staging["staged"].astype(bool)][["subregion", "substation"]]
    return sample.merge(done, on=["subregion", "substation"]).to_dict("records")


def write_settings(units: list[dict], settings: dict, folder: Path) -> list[Path]:
    """Write the registry and the case files; returns the paths written."""
    state = settings["coupling"]["topology_state"]
    registry = {
        "topologies": {
            topology_id(state, unit["subregion"], unit["substation"]): registry_entry(unit, settings)
            for unit in units
        }
    }
    (folder / "cases").mkdir(parents=True, exist_ok=True)
    written = [folder / "topology_registry.yaml"]
    with written[0].open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(registry, handle, sort_keys=False, allow_unicode=True, width=1000)
    for unit in units:
        case = case_file(unit, settings)
        path = folder / "cases" / f"{case['case_id']}.yaml"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(case, handle, sort_keys=False)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of the sample and its staging table")
    args = parser.parse_args(argv)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    sample = pd.read_csv(paths.texas / f"sample_{args.stamp}.csv")
    staging = pd.read_csv(paths.texas / f"staging_{args.stamp}.csv")
    units = staged_units(sample, staging)
    written = write_settings(units, settings, paths.coupling_settings)
    print(f"{len(units)} staged substations of {len(sample)} sampled")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
