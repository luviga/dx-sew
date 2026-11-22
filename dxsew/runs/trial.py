"""Settings and cases of the power-flow trial, one set per pass (decision K73).

Run from the repository::

    uv run python -m dxsew.runs.trial --stamp <stamp> --typical <csv> --features <parquet> \\
        --given-days <given_days csv> --runs-root pass_1=<folder> --runs-root pass_2=<folder> \\
        --base-local <coupling local.yaml> --runs-staging <staging_runs csv> \\
        --check-sample <sample csv> --check-staging <staging csv> [--setup <id>]

The trial runs, through DeltaE/dss-eulp-coupling at the pin, the run feeder of
each stratum's largest group (``trial_feeders``) alone, and every substation of
the setup's ``trial.check_set`` in full. For each pass of ``trial.passes`` it
writes under ``<data_root>/<output_folder>/``:

- ``coupling_settings_trial_<pass>_<stamp>/``: ``local.yaml`` (every key of the
  coupling ``local.yaml`` given, ``runs_root`` set to the pass's folder),
  ``topology_registry.yaml`` (one alone entry per run feeder, from
  ``dxsew.runs.cases.registry_entry``, and one entry per check-set substation,
  from ``dxsew.runs.settings.registry_entry``), ``pipeline_config.yaml``
  (``runs.pipeline_override`` with the pass's ``peak_day_rule``) and, for a pass
  that names a ``given_day``, ``given_days.csv``: the run feeders' rows of the given
  days file, and the same dates for every feeder of the check set;
- ``cases_trial_<pass>_<stamp>/``: one case per run feeder and per substation;

and ``trial_cases_<stamp>.csv`` (one row per pass and case, with its loads and
whether it is in the timing set), then a dry run (``--phase pre --dry-run``) of
each pass's first case and of each check-set case, saved as
``dry_run_trial_<pass>_<case>_<stamp>.txt``.

Before anything is written, each run feeder's alone tree is checked against its
staged folder with ``dxsew.runs.cases.build_alone_tree``; a missing tree, or one
with other files, stops that feeder, and nothing is copied over it.

Coupling repository: https://github.com/DeltaE/dss-eulp-coupling (README,
"Settings from a caller" and "Peak-day rules").
"""

from __future__ import annotations

import argparse
import copy
import csv
import sys
from pathlib import Path

import pandas as pd

from dxsew import config
from dxsew.config import Paths, load_local, load_sample_settings
from dxsew.frame.sample import write_csv
from dxsew.runs import cases as cases_module
from dxsew.runs import coupling as coupling_module
from dxsew.runs import settings as settings_module
from dxsew.runs.stage import substation_folder

RULE_KEY = "peak_day_rule"
# A pass that names a given day (setup key trial.passes.<pass>.given_day) gets given_days.csv.
GIVEN_DAY_KEY = "given_day"


def trial_feeders(typical: pd.DataFrame) -> pd.DataFrame:
    """Per stratum, the run feeder of the group with the most customers among the groups with a typical feeder of their own.

    A placeholder group (``placeholder_group`` filled) has no runnable feeder and
    borrows the run feeder of another group, so it is not a candidate; the
    largest group is then the largest group that names its own typical feeder.
    A tie on customers goes to the lower group number.
    """
    own = typical[typical["placeholder_group"].isna()]
    best = own.sort_values(["stratum", "customers", "group"], ascending=[True, False, True], kind="mergesort")
    best = best.groupby("stratum", sort=True).head(1)
    return best[["stratum", "group", "customers", "run_feeder", "run_feeder_loads",
                 "run_feeder_customers"]].reset_index(drop=True)


def timing_set(loads: dict[str, int], count: int) -> list[str]:
    """The ``count`` names whose loads are nearest the median of all; a tie goes to name order."""
    median = float(pd.Series(list(loads.values()), dtype=float).median())
    ranked = sorted(loads, key=lambda name: (abs(loads[name] - median), name))
    return sorted(ranked[:count])


def pass_pipeline(settings: dict, pass_name: str) -> dict:
    """``pipeline_config.yaml`` of a pass: ``runs.pipeline_override`` with the pass's ``peak_day_rule``."""
    pipeline = copy.deepcopy(settings["runs"]["pipeline_override"])
    pipeline[RULE_KEY] = settings["trial"]["passes"][pass_name][RULE_KEY]
    return pipeline


def check_set_unit(subregion: str, substation: str, settings: dict, runs_staging: pd.DataFrame,
                   check_sample: pd.DataFrame, check_staging: pd.DataFrame) -> tuple[dict, dict, str]:
    """The sample row of a check-set substation, the settings whose staging holds it, and which staging that is.

    The staging of the runs (``runs.staging_folder``) is used when its staging
    table marks the substation staged; else the staging of the sample
    (``staging.folder``) when the sample's staging table does. Neither stops.
    """
    def staged(table: pd.DataFrame) -> bool:
        hit = table[(table["subregion"] == subregion) & (table["substation"] == substation)]
        return bool(len(hit)) and bool(hit["staged"].astype(bool).any())

    row = check_sample[(check_sample["subregion"] == subregion) & (check_sample["substation"] == substation)]
    if staged(runs_staging):
        if len(row) != 1:
            raise ValueError(f"{subregion}/{substation} is staged for the runs but not in the sample given")
        return row.iloc[0].to_dict(), cases_module.runs_settings(settings), "runs.staging_folder"
    if len(row) == 1 and staged(check_staging):
        return row.iloc[0].to_dict(), copy.deepcopy(settings), "staging.folder"
    raise ValueError(f"{subregion}/{substation} is staged in neither staging table")


def check_alone_tree(paths: Paths, settings: dict, unit: dict) -> str:
    """Empty when the run feeder's alone tree exists and holds exactly its staged files; else the reason."""
    target = cases_module.alone_folder(paths, settings, unit)
    if not target.is_dir():
        return f"alone tree missing: {target}"
    try:
        cases_module.build_alone_tree(cases_module.staged_feeder_folder(paths, settings, unit), target)
    except (OSError, FileExistsError) as error:
        return f"alone tree: {error}"
    return ""


def given_rows(given: pd.DataFrame, run_feeders: list[str], check_feeders: list[str]) -> pd.DataFrame:
    """Rows of ``given_days.csv``: the run feeders' rows, and each season's date for every check-set feeder.

    Each season must carry one date over the run feeders' rows.
    """
    mine = given[given["Feeder"].isin(run_feeders)]
    missing = sorted(set(run_feeders) - set(mine["Feeder"]))
    if missing:
        raise ValueError(f"given days lack the feeders: {', '.join(missing)}")
    dates = mine[["Season", "Month", "Day"]].drop_duplicates()
    if dates["Season"].duplicated().any():
        raise ValueError("the given days carry more than one date in a season")
    extra = settings_module.given_days(dates, check_feeders) if check_feeders else mine.iloc[:0]
    return settings_module.given_days(pd.concat([mine, extra], ignore_index=True), [])


def write_pass(pass_name: str, settings: dict, run_units: list[dict], check: list[tuple[dict, dict]],
               local: dict, given: pd.DataFrame | None, folder: Path, cases_folder: Path) -> list[Path]:
    """Write the settings folder and the cases of one pass; returns the case files in order (run feeders, then check set)."""
    folder.mkdir(parents=True, exist_ok=False)
    cases_folder.mkdir(parents=True, exist_ok=False)
    runs = cases_module.runs_settings(settings)
    state = settings["coupling"]["topology_state"]
    topologies = {cases_module.topology_id(runs, unit): cases_module.registry_entry(unit, runs) for unit in run_units}
    for unit, unit_settings in check:
        topologies[settings_module.topology_id(state, unit["subregion"], unit["substation"])] = (
            settings_module.registry_entry(unit, unit_settings))
    settings_module._dump(local, folder / "local.yaml")
    settings_module._dump({"topologies": topologies}, folder / "topology_registry.yaml")
    settings_module._dump(pass_pipeline(settings, pass_name), folder / "pipeline_config.yaml")
    if given is not None:
        with (folder / "given_days.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(settings_module.GIVEN_DAYS_COLUMNS)
            writer.writerows(given.itertuples(index=False, name=None))
    written = []
    for unit in run_units:
        case = cases_module.case_file(unit, runs)
        written.append(cases_folder / f"{case['case_id']}.yaml")
        settings_module._dump(case, written[-1])
    for unit, _ in check:
        case = settings_module.case_file(unit, settings)
        written.append(cases_folder / f"{case['case_id']}.yaml")
        settings_module._dump(case, written[-1])
    return written


def _runs_roots(items: list[str], passes: dict) -> dict[str, Path]:
    roots = {}
    for item in items:
        name, _, folder = item.partition("=")
        if name not in passes or not folder:
            raise SystemExit(f"--runs-root takes <pass>=<folder> with a pass of the setup ({', '.join(passes)})")
        roots[name] = Path(folder)
    if set(roots) != set(passes):
        raise SystemExit(f"--runs-root is needed for every pass: {', '.join(passes)}")
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--typical", type=Path, required=True, help="typical_feeders_<stamp>.csv")
    parser.add_argument("--features", type=Path, required=True, help="features_<stamp>.parquet")
    parser.add_argument("--given-days", type=Path, required=True,
                        help="given_days_<day>_<stamp>.csv of dxsew.curves.year for the pass that names a given day")
    parser.add_argument("--runs-root", action="append", default=[], help="<pass>=<absolute folder>, once per pass")
    parser.add_argument("--base-local", type=Path, required=True, help="coupling local.yaml whose keys are all copied")
    parser.add_argument("--runs-staging", type=Path, required=True, help="staging_runs_<stamp>.csv of dxsew.runs.cases")
    parser.add_argument("--check-sample", type=Path, required=True, help="sample_<stamp>.csv holding the check set")
    parser.add_argument("--check-staging", type=Path, required=True, help="staging_<stamp>.csv of that sample")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)

    settings = load_sample_settings()
    trial = settings["trial"]
    roots = _runs_roots(args.runs_root, trial["passes"])
    local = load_local()
    runs = cases_module.runs_settings(settings)
    paths = Paths(local, runs)
    typical = pd.read_csv(args.typical)
    features = pd.read_parquet(args.features)

    chosen = trial_feeders(typical)
    print(chosen.to_string(index=False))
    print(f"trial feeders: {len(chosen)}, customers on them: {int(chosen['run_feeder_customers'].sum())}")
    units = cases_module.run_units(typical, features)
    units = units[units["feeder"].isin(chosen["run_feeder"])].to_dict("records")
    ready, stopped = [], {}
    for unit in units:
        reason = check_alone_tree(paths, runs, unit)
        if reason:
            stopped[unit["feeder"]] = reason
            print(f"{unit['feeder']}: stopped, {reason}")
        else:
            ready.append(unit)
    print(f"alone trees kept as they are: {len(ready)} of {len(units)}")

    check = []
    for item in trial["check_set"]:
        subregion, substation = item.split("/", 1)
        unit, unit_settings, which = check_set_unit(subregion, substation, settings, pd.read_csv(args.runs_staging),
                                                    pd.read_csv(args.check_sample), pd.read_csv(args.check_staging))
        unit_paths = Paths(local, unit_settings)
        unit["feeder_folders"] = settings_module.feeders_of(
            substation_folder(unit_paths, unit_settings, subregion, substation))
        print(f"check set {item}: staging {which} ({unit_settings['staging']['folder']}), "
              f"feeders {', '.join(unit['feeder_folders'])}")
        check.append((unit, unit_settings))

    loads = {cases_module.case_file(unit, runs)["case_id"]: int(unit["loads"]) for unit in ready}
    timing = timing_set(loads, trial["timing_cases"]) if loads else []
    print(f"timing set: {', '.join(timing)}")
    base_local = cases_module._read_yaml(args.base_local)
    rows = []
    clone = coupling_module.read_local()
    repo, python = Path(clone["coupling_repo"]), Path(clone["coupling_python"])
    for pass_name, rule in trial["passes"].items():
        given = None
        if GIVEN_DAY_KEY in rule:
            check_feeders = [feeder for unit, _ in check for feeder in unit["feeder_folders"]]
            given = given_rows(pd.read_csv(args.given_days), [unit["feeder"] for unit in ready], check_feeders)
            print(f"{pass_name}: {len(given)} given days")
        folder = paths.output / f"coupling_settings_trial_{pass_name}_{args.stamp}"
        coupling_local = settings_module.local_settings(base_local, {}, roots[pass_name])
        written = write_pass(pass_name, settings, ready, check, coupling_local, given, folder,
                             paths.output / f"cases_trial_{pass_name}_{args.stamp}")
        for path, unit, kind in [*[(p, u, "run_feeder") for p, u in zip(written, ready)],
                                 *[(p, u, "check_set") for p, (u, _) in zip(written[len(ready):], check)]]:
            case_id = path.stem
            rows.append({"pass": pass_name, "peak_day_rule": rule[RULE_KEY], "case_id": case_id, "kind": kind,
                         "subregion": unit["subregion"], "substation": unit["substation"],
                         "feeders": " ".join(unit.get("feeder_folders", [unit.get("feeder", "")])),
                         "loads": int(unit["loads"]), "stratum": unit.get("stratum", ""),
                         "timing": case_id in timing, "case_file": path.as_posix(),
                         "settings_folder": folder.as_posix(), "runs_root": roots[pass_name].as_posix()})
        print(f"pin ok: {coupling_module.check_pin(repo)}")
        for path in [written[0], *written[len(ready):]]:
            log = paths.output / f"dry_run_trial_{pass_name}_{path.stem}_{args.stamp}.txt"
            code, values = cases_module.dry_run(path, folder, repo, python, log)
            print(f"{pass_name} dry run {path.stem}: exit {code}; "
                  + "; ".join(f"{key}={value}" for key, value in sorted(values.items())))
        print(f"pin ok: {coupling_module.check_pin(repo)}")
    table = pd.DataFrame(rows)
    write_csv(table, paths.output / f"trial_cases_{args.stamp}.csv")
    for feeder, reason in stopped.items():
        print(f"stopped: {feeder}: {reason}")
    print(f"written: {paths.output / f'trial_cases_{args.stamp}.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
