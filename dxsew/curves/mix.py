"""Year curves per mix of the trial's feeders, rebuilt from the coupling's weights, and their checks (decision K73).

Run from the repository::

    uv run python -m dxsew.curves.mix --stamp <stamp> --trial-cases <trial_cases csv> --curves <curves folder> \\
        [--workers <n>] [--setup <id>]

For every case of the trial table (``dxsew.runs.trial``), from the workspaces of
DeltaE/dss-eulp-coupling at the pin:

1. Baseline reproduction (K66). Each feeder of the first pass's summer workspace
   is rebuilt by ``dxsew.curves.year`` from its recipe and checked against the
   workspace's peak days; a run feeder's curve is set against the curve of the
   curves folder given (``feeder_curves.parquet``), point by point.
2. Mix curves. For each feeder and mix of the weights file (setup key
   ``curves.circuit_weights_file``, phase ``mix_days``), per heating group in the
   order of ``curves.mix_group_lists`` and per row of that group's required-parquet
   list in file order: ``out.electricity.total.energy_consumption`` x points per
   hour (4) x ``Weight``, the weight looked up by group and ``Parquet_File``; a
   row without a weight adds nothing. The same per sector and end use. The
   curve keeps the building files' own timestamps. An unreadable or missing
   building file, or timestamps other than the first file's, stops that feeder
   and mix with a message.
3. Checks. Placed on the coupling's position calendar, each mix curve of a pass
   whose rule picks the day itself must peak in each season on the month and day
   of the season workspace's ``curves.circuit_days_file``, at its ``Peak_kW``
   within ``curves.check_rel_tolerance``; in a pass that names a given day, every
   row must carry the given day, and its ``Peak_kW`` must equal the curve's
   highest point on that day within the same tolerance. The winter workspace's
   weights file must equal the summer one byte for byte.

Writes ``<data_root>/<output_folder>/trial_<stamp>/``: ``mix_curves.parquet``
(feeder, mix, timestamp, kW as float32), ``mix_checks.csv``, ``curves_repro.csv``
``baseline_curves.parquet`` (the reproduction's curves, feeder, timestamp, kW
as float32) and ``mix_days.csv`` (per feeder, mix and season: the first pass's own worst day
and peak, the curve's highest point on day B, its ratio to the own peak, and the
end uses at the own peak).

The coupling's rule is in
https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/5b_profile_generation/find_max_day_curve_impl.py
(``run_mix``, steps 2 to 4). dx-sew repeats the formula only, never the code.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

from dxsew import config
from dxsew.config import Paths, load_local, load_runtime, load_sample_settings
from dxsew.curves import year
from dxsew.frame.sample import write_csv
from dxsew.runs import coupling as coupling_module
from dxsew.runs.batch import DEFAULTS_FILE, coupling_order
from dxsew.runs.cases import runs_settings

class MixError(RuntimeError):
    """A building file of a feeder and mix cannot be read, or its timestamps differ."""


def read_exact(path: Path, **kwargs) -> pd.DataFrame:
    """A CSV of the coupling with every float read back exactly as written.

    The float parser of pandas 3 is exact by default (tests/regression/test_trial.py checks it).
    """
    return pd.read_csv(path, **kwargs)


def group_lists(workspace: Path, rule: dict) -> dict[str, pd.DataFrame]:
    """Required-parquet list of each heating group (setup key ``curves.mix_group_lists``), in that order."""
    return {group: pd.read_csv(Path(workspace) / spec["folder"] / spec["file"])
            for group, spec in rule["mix_group_lists"].items()}


def mix_curve(feeder: str, mix: str, weights: pd.DataFrame, lists: dict[str, pd.DataFrame],
              parquet_root: Path) -> dict:
    """Rebuild one feeder's year curve under one mix (step 2 of the module text).

    Returns ``total`` (kW), ``end_uses`` ({(sector, end use): kW}), ``timestamps``
    and ``rows_used``.
    """
    mine = weights[(weights["Feeder"] == feeder) & (weights["Mix"] == mix)]
    lookup = {(row.Group, row.Parquet_File): float(row.Weight)
              for row in mine.drop_duplicates(["Group", "Parquet_File"]).itertuples(index=False)}
    total, timestamps, rows_used = None, None, 0
    end_uses: dict[tuple[str, str], np.ndarray] = {}
    for group, table in lists.items():
        for row in table[table["Feeder"] == feeder].itertuples(index=False):
            weight = lookup.get((group, row.Parquet_File), 0.0)
            if weight == 0:
                continue
            path = Path(parquet_root) / row.Parquet_Folder / row.Parquet_File
            try:
                columns = ["timestamp", *year.end_use_columns(pq.read_schema(path).names).values()]
                data = pq.read_table(path, columns=columns).to_pandas()
            except Exception as error:  # noqa: BLE001 - any unreadable file stops the feeder and mix
                raise MixError(f"{feeder} {mix}: cannot read {path}: {error}") from error
            stamps = data["timestamp"].to_numpy()
            if timestamps is None:
                timestamps = stamps
            elif len(stamps) != len(timestamps) or not np.array_equal(stamps, timestamps):
                raise MixError(f"{feeder} {mix}: {path}: timestamps differ from the first file's")
            found = year.end_use_columns(data.columns)
            if year.TOTAL not in found:
                raise MixError(f"{feeder} {mix}: {path}: no out.electricity.total.energy_consumption")
            scaled = (data[found[year.TOTAL]].to_numpy() * year.POINTS_PER_HOUR) * weight
            total = scaled.copy() if total is None else total + scaled
            sector = year.sector_of(row.Parquet_Folder)
            for name, column in found.items():
                if name in year.NOT_END_USES:
                    continue
                values = (data[column].to_numpy() * year.POINTS_PER_HOUR) * weight
                key = (sector, name)
                end_uses[key] = values.copy() if key not in end_uses else end_uses[key] + values
            rows_used += 1
    if total is None:
        raise MixError(f"{feeder} {mix}: no weighted row")
    return {"total": total, "end_uses": end_uses, "timestamps": timestamps, "rows_used": rows_used}


def peak_check(feeder: str, mix: str, season: str, curve: np.ndarray, days: pd.DataFrame, rule: dict,
               given: tuple[int, int] | None = None) -> dict:
    """One row of ``mix_checks``: the rebuilt curve against one row of the circuit days file.

    Without ``given``, the curve's seasonal peak day on the coupling's position
    calendar must be the row's month and day at its ``Peak_kW``; with ``given``
    (month, day), the row must carry that day and its ``Peak_kW`` must be the
    curve's highest point on it.
    """
    found = days[(days["Feeder"] == feeder) & (days["Season"] == season) & (days["Mix"] == mix)]
    row = {"feeder": feeder, "mix": mix, "season": season, "coupling_month": None, "coupling_day": None,
           "coupling_peak_kw": None, "rebuilt_month": None, "rebuilt_day": None, "rebuilt_peak_kw": None,
           "relative_difference": None, "passed": False, "note": ""}
    if len(found) != 1:
        row["note"] = f"{len(found)} rows in {rule['circuit_days_file']}"
        return row
    ref = found.iloc[0]
    row.update(coupling_month=int(ref["Month"]), coupling_day=int(ref["Day"]), coupling_peak_kw=float(ref["Peak_kW"]))
    if given is None:
        mine = year.coupling_peaks(curve, rule).get(season)
        if mine is None:
            row["note"] = "no rebuilt peak day"
            return row
        month, day, peak = mine
    else:
        month, day = given
        times = pd.date_range(pd.Timestamp(rule["coupling_start"]), periods=len(curve), freq=year.STEP)
        on_day = np.asarray((times.month == month) & (times.day == day))
        peak = float(np.asarray(curve)[on_day].max())
    row.update(rebuilt_month=month, rebuilt_day=day, rebuilt_peak_kw=peak)
    denominator = abs(row["coupling_peak_kw"]) or 1.0
    row["relative_difference"] = abs(peak - row["coupling_peak_kw"]) / denominator
    row["passed"] = bool(month == row["coupling_month"] and day == row["coupling_day"]
                         and row["relative_difference"] <= rule["check_rel_tolerance"])
    return row


def end_uses_at(index: int, end_uses: dict, total_at: float, count: int) -> dict:
    """The ``count`` largest end uses at one point, as ``dxsew.curves.year.day_measures`` lists them."""
    values = pd.Series({f"{sector}:{name}": float(np.asarray(v)[index]) for (sector, name), v in end_uses.items()})
    at_peak = values.sort_values(ascending=False)
    row = {}
    for rank in range(count):
        name = at_peak.index[rank] if rank < len(at_peak) else ""
        kw = float(at_peak.iloc[rank]) if rank < len(at_peak) else np.nan
        row[f"end_use_{rank + 1}"] = name
        row[f"end_use_{rank + 1}_kw"] = kw
        row[f"end_use_{rank + 1}_share"] = kw / total_at if total_at else np.nan
    return row


def baseline_mix(rule: dict, repo: Path | None = None) -> str | None:
    """The mix of the coupling's mixes file whose share of the first heating group (``curves.mix_group_lists``) is 1.

    The mixes file is ``mixes_file`` of the clone's ``configs/defaults.yaml``.
    """
    repo = Path(repo or coupling_module.read_local()["coupling_repo"])
    with repo.joinpath(*DEFAULTS_FILE).open(encoding="utf-8") as handle:
        mixes_file = Path(yaml.safe_load(handle)["mixes_file"])
    with (mixes_file if mixes_file.is_absolute() else repo / mixes_file).open(encoding="utf-8") as handle:
        mixes = yaml.safe_load(handle)
    first = next(iter(rule["mix_group_lists"]))
    return next((name for name, mix in mixes.items() if float(mix["shares"].get(first, 0)) == 1), None)


def _feeder_mixes(job: dict) -> dict:
    """Worker: every mix curve of one feeder of one pass."""
    rule = job["rule"]
    workspace = Path(job["workspace"])
    weights = read_exact(workspace / rule["mix_days_folder"] / rule["circuit_weights_file"])
    lists = group_lists(workspace, rule)
    out = {"feeder": job["feeder"], "pass": job["pass"], "curves": {}, "errors": {}}
    for mix in job["mixes"]:
        try:
            out["curves"][mix] = mix_curve(job["feeder"], mix, weights, lists, Path(job["parquet_root"]))
        except MixError as error:
            out["errors"][mix] = str(error)
    return out


def _one_baseline(job: dict) -> dict:
    """Worker: the K66 rebuild of one feeder (``dxsew.curves.year``)."""
    return year._one_feeder(job)


def registry_feeders(workspace: Path, rule: dict) -> list[str]:
    """Feeder folders of a workspace, in circuit order, from its feeder registry."""
    with (Path(workspace) / rule["feeder_registry_file"]).open(encoding="utf-8") as handle:
        registry = json.load(handle)
    return [entry["feeder_name"] for entry in sorted(registry["feeders"], key=lambda e: int(e["circuit_id"]))]


def same_files(first: Path, second: Path) -> bool:
    return Path(first).is_file() and Path(second).is_file() and filecmp.cmp(first, second, shallow=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--trial-cases", type=Path, required=True, help="trial_cases_<stamp>.csv of dxsew.runs.trial")
    parser.add_argument("--curves", type=Path, required=True,
                        help="curves folder of dxsew.curves.year: feeder_curves.parquet and common_days.csv")
    most = load_runtime()["workers"]["curves_processes"]
    parser.add_argument("--workers", type=int, default=most, help=f"worker processes (at most {most})")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= most:
        parser.error(f"--workers must be 1 to {most}")
    config.use_setup(args.setup)

    settings = runs_settings(load_sample_settings())
    rule = settings["curves"]
    paths = Paths(load_local(), settings)
    out_dir = paths.output / f"trial_{args.stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    seasons = list(year.seasons(rule))
    # The workspace of the reproduction: the first season of the coupling's own order.
    first_season = coupling_order(Path(coupling_module.read_local()["coupling_repo"]))[1][0]
    cases = pd.read_csv(args.trial_cases)
    passes = list(dict.fromkeys(cases["pass"]))
    trial = settings["trial"]["passes"]
    given_letter = next(spec["given_day"] for spec in trial.values() if "given_day" in spec)
    common = pd.read_csv(args.curves / "common_days.csv")
    b_days = {season: pd.Timestamp(common[(common["option"] == given_letter)
                                          & (common["season"] == season)]["date"].iloc[0]) for season in seasons}

    # Workspaces: (pass, case) -> season -> folder; feeders of each case from its registry.
    feeders_of, jobs, base_jobs = {}, [], []
    for row in cases.to_dict("records"):
        runs_root, case_id, pass_name = Path(row["runs_root"]), row["case_id"], row["pass"]
        topology = case_id.rsplit("__", 1)[0]
        workspace = year.workspace_of(runs_root, case_id, first_season)
        try:
            feeders = registry_feeders(workspace, rule)
            root = year.parquet_root_of(runs_root, case_id, topology, first_season)
        except (OSError, KeyError) as error:
            print(f"{pass_name} {case_id}: no workspace ({error})")
            continue
        feeders_of[(pass_name, case_id)] = feeders
        weights_path = workspace / rule["mix_days_folder"] / rule["circuit_weights_file"]
        mixes = list(dict.fromkeys(read_exact(weights_path)["Mix"])) if weights_path.is_file() else []
        for feeder in feeders:
            jobs.append({"pass": pass_name, "case_id": case_id, "feeder": feeder, "workspace": str(workspace),
                         "parquet_root": str(root), "mixes": mixes, "rule": rule, "runs_root": str(runs_root)})
            if pass_name == passes[0]:
                base_jobs.append({"feeder": feeder, "workspace": str(workspace), "parquet_root": str(root),
                                  "rule": rule, "case_id": case_id, "kind": row["kind"]})

    # 1. Baseline reproduction.
    reference = pd.read_parquet(args.curves / "feeder_curves.parquet")
    repro, base_curves = [], {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for job, result in zip(base_jobs, pool.map(_one_baseline, base_jobs)):
            feeder = job["feeder"]
            checks = result["check"] or []
            row = {"case_id": job["case_id"], "kind": job["kind"], "feeder": feeder, "error": result["error"],
                   "rows_read": result.get("rows_read")}
            for check in checks:
                season = check["season"]
                row[f"k66_{season}_passed"] = bool(check["passed"])
                row[f"k66_{season}_relative_difference"] = check.get("relative_difference")
                row[f"k66_{season}_day"] = f"{check.get('rebuilt_month')}-{check.get('rebuilt_day')}"
            if not result["error"]:
                base_curves[feeder] = result
                ref = reference[reference["feeder"] == feeder]
                if len(ref):
                    mine = np.asarray(result["total"], dtype=np.float32)
                    theirs = ref["kw"].to_numpy()
                    same_time = bool(np.array_equal(ref["timestamp"].to_numpy(), np.asarray(result["timestamps"])))
                    row.update(reference_points=len(theirs), same_timestamps=same_time,
                               largest_abs_difference_kw=float(np.max(np.abs(mine.astype(np.float64) - theirs)))
                               if len(theirs) == len(mine) else np.nan)
            repro.append(row)
            print(f"repro {feeder}: {'error ' + result['error'] if result['error'] else 'ok'}")
    write_csv(pd.DataFrame(repro), out_dir / "curves_repro.csv")
    if base_curves:
        pd.concat([pd.DataFrame({"feeder": feeder, "timestamp": curve["timestamps"],
                                 "kw": np.asarray(curve["total"]).astype(np.float32)})
                   for feeder, curve in base_curves.items()], ignore_index=True).to_parquet(
            out_dir / "baseline_curves.parquet", index=False)

    # 2. Mix curves, rebuilt once per pass unless the pass's weights and lists equal the first pass's.
    first_ws = {(job["case_id"], job["feeder"]): job["workspace"] for job in jobs if job["pass"] == passes[0]}

    def same_inputs(job: dict) -> bool:
        other = first_ws.get((job["case_id"], job["feeder"]))
        if other is None:
            return False
        names = [Path(rule["mix_days_folder"]) / rule["circuit_weights_file"],
                 *[Path(spec["folder"]) / spec["file"] for spec in rule["mix_group_lists"].values()]]
        return all(same_files(Path(other) / name, Path(job["workspace"]) / name) for name in names)

    todo = [job for job in jobs if job["pass"] == passes[0] or not same_inputs(job)]
    reused = [job for job in jobs if job not in todo]
    curves: dict[tuple[str, str, str], dict] = {}
    errors = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(_feeder_mixes, todo):
            for mix, curve in result["curves"].items():
                curves[(result["pass"], result["feeder"], mix)] = curve
            for mix, error in result["errors"].items():
                errors.append({"pass": result["pass"], "feeder": result["feeder"], "mix": mix, "error": error})
                print(error)
            print(f"mix curves {result['pass']} {result['feeder']}: {len(result['curves'])} built")
    for job in reused:
        for mix in job["mixes"]:
            if (passes[0], job["feeder"], mix) in curves:
                curves[(job["pass"], job["feeder"], mix)] = curves[(passes[0], job["feeder"], mix)]
    print(f"passes whose weights and lists equal the first pass's (curves reused): "
          f"{sorted({(job['pass'], job['feeder']) for job in reused})}")

    # 3. Checks.
    checks = []
    for job in jobs:
        for season in seasons:
            workspace = year.workspace_of(Path(job["runs_root"]), job["case_id"], season)
            days_path = workspace / rule["mix_days_folder"] / rule["circuit_days_file"]
            weights_season = workspace / rule["mix_days_folder"] / rule["circuit_weights_file"]
            weights_first = Path(job["workspace"]) / rule["mix_days_folder"] / rule["circuit_weights_file"]
            days = read_exact(days_path) if days_path.is_file() else pd.DataFrame(
                columns=["Feeder", "Season", "Mix", "Month", "Day", "Peak_kW"])
            given_day = trial[job["pass"]].get("given_day")
            for mix in job["mixes"]:
                curve = curves.get((job["pass"], job["feeder"], mix))
                given = (b_days[season].month, b_days[season].day) if given_day else None
                if curve is None:
                    row = {"feeder": job["feeder"], "mix": mix, "season": season, "passed": False,
                           "note": "no rebuilt curve"}
                else:
                    row = peak_check(job["feeder"], mix, season, curve["total"], days, rule, given)
                row.update({"pass": job["pass"], "case_id": job["case_id"], "rule": trial[job["pass"]]["peak_day_rule"],
                            "weights_equal_to_first_season": same_files(weights_first, weights_season)})
                checks.append(row)
    check_table = pd.DataFrame(checks)
    front = ["pass", "case_id", "rule", "feeder", "mix", "season"]
    check_table = check_table[front + [c for c in check_table.columns if c not in front]]
    write_csv(check_table, out_dir / "mix_checks.csv")

    # Curves and the mix days table (first pass).
    long = []
    timestamps = None
    for (pass_name, feeder, mix), curve in curves.items():
        if pass_name != passes[0]:
            continue
        timestamps = curve["timestamps"]
        long.append(pd.DataFrame({"feeder": feeder, "mix": mix, "timestamp": curve["timestamps"],
                                  "kw": np.asarray(curve["total"]).astype(np.float32)}))
    if long:
        pd.concat(long, ignore_index=True).to_parquet(out_dir / "mix_curves.parquet", index=False)
    days_rows = []
    first_checks = check_table[check_table["pass"] == passes[0]]
    for (pass_name, feeder, mix), curve in curves.items():
        if pass_name != passes[0]:
            continue
        dates = year.point_dates(pd.DatetimeIndex(curve["timestamps"]))
        total = np.asarray(curve["total"])
        for season in seasons:
            own = first_checks[(first_checks["feeder"] == feeder) & (first_checks["mix"] == mix)
                               & (first_checks["season"] == season)]
            row = {"feeder": feeder, "mix": mix, "season": season}
            if len(own) and pd.notna(own["coupling_month"].iloc[0]):
                month, day = int(own["coupling_month"].iloc[0]), int(own["coupling_day"].iloc[0])
                own_date = pd.Timestamp(year=config.setup()["year"], month=month, day=day)
                on_own = np.asarray(dates == own_date)
                index = np.flatnonzero(on_own)[int(np.argmax(total[on_own]))]
                own_peak = float(own["coupling_peak_kw"].iloc[0])
                on_b = np.asarray(dates == b_days[season])
                b_high = float(total[on_b].max())
                row.update(own_month=month, own_day=day, own_peak_kw=own_peak,
                           own_peak_time=pd.Timestamp(year.interval_start(pd.DatetimeIndex(curve["timestamps"]))[index]).isoformat(),
                           day_b=b_days[season].date().isoformat(), day_b_high_kw=b_high,
                           day_b_over_own_peak=b_high / own_peak if own_peak else np.nan)
                row.update(end_uses_at(index, curve["end_uses"], float(total[index]), rule["top_end_uses"]))
            days_rows.append(row)
    write_csv(pd.DataFrame(days_rows), out_dir / "mix_days.csv")
    if errors:
        write_csv(pd.DataFrame(errors), out_dir / "mix_errors.csv")
    print(f"K66 rows passed: {sum(bool(v) for r in repro for k, v in r.items() if k.endswith('_passed'))}")
    print(f"mix checks passed: {int(check_table['passed'].sum())} of {len(check_table)}")
    print(f"written: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
