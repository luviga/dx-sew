"""Settings folder of the coupling for staged Texas substations (decision K53).

Run from the repository::

    uv run python -m dxsew.texas.settings --stamp <stamp> --runs-root <folder> \\
        --base-local <coupling local.yaml> [--extra-local <yaml> --extra-key <key> ...] \\
        --days <csv> [--substation <subregion>/<substation> ...]

Writes ``<data_root>/texas/coupling_settings_<stamp>/`` with the files that
``run_case.py --settings <folder>`` of DeltaE/dss-eulp-coupling merges over the
repository's own (README, "Settings from a caller"):

- ``local.yaml``: every key of the coupling ``local.yaml`` given, the extra keys
  taken from another file, and ``runs_root`` set to the absolute folder given;
- ``topology_registry.yaml``: one entry per substation;
- ``pipeline_config.yaml``: ``peak_day_rule: given``;
- ``given_days.csv``: columns ``Feeder``, ``Season``, ``Month``, ``Day``, one row per
  feeder and season (README, "Peak-day rules").

and one case file per substation in ``<data_root>/texas/cases_<stamp>/``. The
circuit root of an entry is ``<smart_ds_base>/<smart_ds_relpath>/<circuit_folder>``
and its parquet root ``<smart_ds_base>/<parquet_relpath>`` (``run_case.py``,
``resolve_case``). Machine paths go only into these generated files.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd
import yaml

from .config import Paths, load_local, load_sample_settings
from .frame import FEEDER_FILES
from .stage import substation_folder

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
GIVEN_DAYS_COLUMNS = ("Feeder", "Season", "Month", "Day")


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
    """Case of one staged substation: its topology, the donor of the settings, every feeder."""
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


def feeders_of(folder: Path) -> list[str]:
    """Feeder folders of a staged substation: subfolders holding both ``Loads.dss`` and ``LoadShapes.dss``."""
    found = []
    for child in sorted(Path(folder).iterdir()):
        files = {p.name.lower() for p in child.iterdir()} if child.is_dir() else set()
        if all(name in files for name in FEEDER_FILES):
            found.append(child.name)
    return found


def given_days(days: pd.DataFrame, feeders: list[str]) -> pd.DataFrame:
    """Rows of ``given_days.csv``.

    ``days`` either holds the four columns already, or ``Season``, ``Month`` and
    ``Day`` only, which then apply to every feeder given.
    """
    if "Feeder" in days.columns:
        table = days
    else:
        table = pd.DataFrame(
            [(feeder, row.Season, int(row.Month), int(row.Day)) for row in days.itertuples(index=False)
             for feeder in feeders],
            columns=GIVEN_DAYS_COLUMNS,
        )
    table = table.reindex(columns=GIVEN_DAYS_COLUMNS)
    if table.isna().any().any() or table.duplicated(["Feeder", "Season"]).any():
        raise ValueError("given days: a value is missing or a feeder and season is given twice")
    return table.sort_values(["Season", "Feeder"], kind="mergesort").reset_index(drop=True)


def local_settings(base: dict, extra: dict, runs_root: Path) -> dict:
    """``local.yaml`` of the folder: the base keys, the extra keys and an absolute ``runs_root``."""
    runs_root = Path(runs_root)
    if not runs_root.is_absolute():
        raise ValueError(f"runs_root must be an absolute folder, not {runs_root}")
    return {**base, **extra, "runs_root": runs_root.as_posix()}


def _dump(data: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True, width=1000)


def write_settings_folder(units: list[dict], settings: dict, folder: Path, local: dict,
                          days: pd.DataFrame) -> list[Path]:
    """Write the four settings files; returns the paths written."""
    folder.mkdir(parents=True, exist_ok=True)
    state = settings["coupling"]["topology_state"]
    registry = {
        "topologies": {
            topology_id(state, unit["subregion"], unit["substation"]): registry_entry(unit, settings)
            for unit in units
        }
    }
    written = [folder / "local.yaml", folder / "topology_registry.yaml", folder / "pipeline_config.yaml",
               folder / "given_days.csv"]
    _dump(local, written[0])
    _dump(registry, written[1])
    _dump({"peak_day_rule": "given"}, written[2])
    with written[3].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(GIVEN_DAYS_COLUMNS)
        writer.writerows(days.itertuples(index=False, name=None))
    return written


def write_cases(units: list[dict], settings: dict, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for unit in units:
        case = case_file(unit, settings)
        path = folder / f"{case['case_id']}.yaml"
        _dump(case, path)
        written.append(path)
    return written


def _read_yaml(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a mapping.")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of the sample and its staging table")
    parser.add_argument("--runs-root", type=Path, required=True, help="absolute folder of the coupling runs")
    parser.add_argument("--base-local", type=Path, required=True, help="coupling local.yaml whose keys are all copied")
    parser.add_argument("--extra-local", type=Path, default=None, help="yaml file holding the extra keys")
    parser.add_argument("--extra-key", action="append", default=[], help="key taken from --extra-local")
    parser.add_argument("--days", type=Path, required=True,
                        help="CSV of Season,Month,Day (every feeder) or Feeder,Season,Month,Day")
    parser.add_argument("--substation", action="append", default=[],
                        help="<subregion>/<substation> to include; default every staged substation")
    args = parser.parse_args(argv)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    sample = pd.read_csv(paths.texas / f"sample_{args.stamp}.csv")
    staging = pd.read_csv(paths.texas / f"staging_{args.stamp}.csv")
    units = staged_units(sample, staging)
    if args.substation:
        wanted = {tuple(item.split("/", 1)) for item in args.substation}
        units = [u for u in units if (u["subregion"], u["substation"]) in wanted]
        if len(units) != len(wanted):
            raise SystemExit("a --substation is not a staged substation of this sample")
    feeders = [feeder for unit in units
               for feeder in feeders_of(substation_folder(paths, settings, unit["subregion"], unit["substation"]))]
    extra_source = _read_yaml(args.extra_local) if args.extra_local else {}
    missing = [key for key in args.extra_key if key not in extra_source]
    if missing:
        raise SystemExit(f"keys not in {args.extra_local}: {', '.join(missing)}")
    local = local_settings(_read_yaml(args.base_local), {k: extra_source[k] for k in args.extra_key}, args.runs_root)
    days = given_days(pd.read_csv(args.days), feeders)
    folder = paths.texas / f"coupling_settings_{args.stamp}"
    written = write_settings_folder(units, settings, folder, local, days)
    written += write_cases(units, settings, paths.texas / f"cases_{args.stamp}")
    print(f"{len(units)} staged substations, {len(feeders)} feeders, {len(days)} given days")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
