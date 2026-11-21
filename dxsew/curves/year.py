"""Year curves of the run feeders, rebuilt from the coupling's recipe, and common days (K48 with K60, K66, K67).

Run from the repository::

    uv run python -m dxsew.texas.curves --stamp <stamp> --typical <csv> --runs-root <folder> --features <parquet> \\
        [--fit-target <parquet>] [--workers <n>]

1. Year curve per run feeder (K66). From the feeder's workspace of
   DeltaE/dss-eulp-coupling, the recipe ``TX_required_parquets_per_feeder.csv``
   (phase 5b) and, row by row in file order, the building file
   ``<parquet root>/<Parquet_Folder>/<Parquet_File>``. Feeder total in kW = sum
   over rows of ``out.electricity.total.energy_consumption`` x 4 x
   ``REAL_LOAD_COUNT``; the same per sector and end use. An unreadable file, or
   timestamps other than the first file's, stops that feeder.
2. Check (K66). The total is placed by position on the coupling's calendar and
   the coupling's peak-day rule is applied; month, day and peak must equal the
   workspace's ``TX_peak_days.csv``. A failed feeder stays out of every sum.
3. Timestamps of the building files, per sector.
4. Stratum curves: each group adds its run feeder's curve x customers of the
   group / customers of the run feeder (K47 as amended by K60).
5. to 8. Day A (the stratum curve's seasonal peak day), day B (ERCOT's seasonal
   peak day, Central Standard Time, hour ending) and day C (the day most run
   feeders of the stratum peak on, reported only), with the measures of each.
9. Given days for the coupling, ``given_days_A_<stamp>.csv`` and
   ``given_days_B_<stamp>.csv`` (columns ``Feeder, Season, Month, Day``).

The coupling's recipe and rule are in
https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/5b_profile_generation/find_max_day_curve_impl.py
(lines 137 to 197 and 513 to 549). dx-sew repeats the formula only, never the code.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

from dxsew.config import Paths, load_local, load_sample_settings
from dxsew.frame.sample import write_csv
from dxsew.runs.cases import run_units, runs_settings, topology_id

STATE = "TX"
RECIPE = f"{STATE}_required_parquets_per_feeder.csv"
PEAK_DAYS = f"{STATE}_peak_days.csv"
TOTAL = "total"
NOT_END_USES = ("total", "net")
COLUMN_PREFIX, COLUMN_SUFFIX = "out.electricity.", ".energy_consumption"
SEASONS = {"winter": (12, 1, 2), "summer": (6, 7, 8)}
# The coupling's calendar: point 1 at 2025-01-01 00:00, 15 minutes apart
# (find_max_day_curve_impl.py, monthly_peak_curves).
COUPLING_START = pd.Timestamp("2025-01-01 00:00:00")
STEP = pd.Timedelta(minutes=15)
POINTS_PER_DAY = 96
REL_TOLERANCE = 1e-6
TOP_END_USES = 5
YEAR = 2018
# Source of the timestamp convention of the building files, read on OEDI (section "Units and timestamps").
TIMESTAMP_SOURCE = "https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/README.md"
TIMESTAMP_ASSUMPTION = (
    "ASSUMPTION: each timestamp of a building file marks the end of its 15-minute interval, so the date of a point "
    "is the date of its interval start and 2019-01-01 00:00 belongs to 31 December 2018. The OEDI README of the "
    "End Use Load Profiles states that results timestamps are 15-minute period-ending in EST; the release folders' "
    "own READMEs state no convention. The dates are taken as written, with no shift from Eastern to Central time."
)
END_USE_ASSUMPTION = (
    "ASSUMPTION: the end uses of a building file are its columns out.electricity.<end use>.energy_consumption other "
    "than total and net; net is total less PV output, not an end use, and the end uses add to total within the "
    "rounding of the files."
)


class FeederError(RuntimeError):
    """A building file of the feeder cannot be read, or its timestamps differ."""


def sector_of(folder: str) -> str:
    """``res`` for the residential download folder, ``com`` for the commercial one."""
    name = str(folder).lower()
    if "residential" in name:
        return "res"
    if "commercial" in name:
        return "com"
    raise FeederError(f"folder {folder} is neither residential nor commercial")


def end_use_columns(columns) -> dict[str, str]:
    """``end use -> column`` for every ``out.electricity.<end use>.energy_consumption`` column."""
    found = {}
    for column in columns:
        if column.startswith(COLUMN_PREFIX) and column.endswith(COLUMN_SUFFIX):
            found[column[len(COLUMN_PREFIX):-len(COLUMN_SUFFIX)]] = column
    return found


def feeder_curve(recipe: pd.DataFrame, parquet_root: Path) -> dict:
    """Rebuild one feeder's year curve from its recipe rows, in file order (K66).

    Returns ``total`` (kW, float64, summed as (energy x 4) x REAL_LOAD_COUNT row
    after row), ``end_uses`` ({(sector, end use): kW}), ``timestamps`` (of the
    first file), ``first_by_sector`` ({sector: timestamps}) and ``rows_read``.
    """
    total = None
    end_uses: dict[tuple[str, str], np.ndarray] = {}
    timestamps = None
    first_by_sector: dict[str, np.ndarray] = {}
    cache: dict[Path, pd.DataFrame] = {}
    rows_read = 0
    for row in recipe.itertuples(index=False):
        path = Path(parquet_root) / row.Parquet_Folder / row.Parquet_File
        sector = sector_of(row.Parquet_Folder)
        if path not in cache:
            try:
                schema = pq.read_schema(path)
                columns = ["timestamp", *end_use_columns(schema.names).values()]
                cache[path] = pq.read_table(path, columns=columns).to_pandas()
            except Exception as error:  # noqa: BLE001 - any unreadable file stops the feeder
                raise FeederError(f"cannot read {path}: {error}") from error
        table = cache[path]
        stamps = table["timestamp"].to_numpy()
        if timestamps is None:
            timestamps = stamps
        elif len(stamps) != len(timestamps) or not np.array_equal(stamps, timestamps):
            raise FeederError(f"{path}: timestamps differ from the first file's")
        first_by_sector.setdefault(sector, stamps)
        columns = end_use_columns(table.columns)
        if TOTAL not in columns:
            raise FeederError(f"{path}: no out.electricity.total.energy_consumption")
        count = row.REAL_LOAD_COUNT
        scaled = (table[columns[TOTAL]].to_numpy() * 4) * count
        total = scaled.copy() if total is None else total + scaled
        for name, column in columns.items():
            if name in NOT_END_USES:
                continue
            values = (table[column].to_numpy() * 4) * count
            key = (sector, name)
            end_uses[key] = values.copy() if key not in end_uses else end_uses[key] + values
        rows_read += 1
    if total is None:
        raise FeederError("the recipe has no row for this feeder")
    return {"total": total, "end_uses": end_uses, "timestamps": timestamps,
            "first_by_sector": first_by_sector, "rows_read": rows_read}


def coupling_peaks(total: np.ndarray) -> dict[str, tuple[int, int, float]]:
    """The coupling's seasonal peak days of a curve: ``season -> (month, day, peak kW)``.

    Point i is placed at 2025-01-01 00:00 + 15 min x i. Per month, the day with
    the highest point (the first such day on a tie) is kept when it has 96 points;
    a season's day is its kept month with the highest such point (the first month
    in calendar order on a tie).
    """
    times = pd.date_range(COUPLING_START, periods=len(total), freq=STEP)
    frame = pd.DataFrame({"month": times.month, "date": times.normalize(), "kw": total})
    daily = frame.groupby(["month", "date"])["kw"].agg(["max", "size"])
    kept = {}
    for month, days in daily.groupby(level="month"):
        best = days["max"].idxmax()
        if days.loc[best, "size"] == POINTS_PER_DAY:
            kept[month] = (best[1], days.loc[best, "max"])
    result = {}
    for season, months in SEASONS.items():
        candidates = [(month, kept[month]) for month in sorted(kept) if month in months]
        if not candidates:
            continue
        month, (date, peak) = max(candidates, key=lambda item: item[1][1])
        # max() keeps the first of equal values, which is the first month in calendar order.
        result[season] = (int(date.month), int(date.day), float(peak))
    return result


def check_feeder(feeder: str, rebuilt: dict, coupling: pd.DataFrame, rows_read: int, rows_recipe: int) -> list[dict]:
    """Rows of ``curves_check``: the rebuilt peak days against ``TX_peak_days.csv``, both seasons."""
    rows = []
    for season in SEASONS:
        found = coupling[(coupling["Feeder"] == feeder) & (coupling["Season"] == season)]
        mine = rebuilt.get(season)
        row = {"feeder": feeder, "season": season,
               "coupling_month": None, "coupling_day": None, "coupling_peak_kw": None,
               "rebuilt_month": mine[0] if mine else None, "rebuilt_day": mine[1] if mine else None,
               "rebuilt_peak_kw": mine[2] if mine else None, "relative_difference": None, "passed": False,
               "rows_read": rows_read, "rows_in_recipe": rows_recipe, "note": ""}
        if len(found) != 1:
            row["note"] = f"{len(found)} rows in {PEAK_DAYS}"
        elif mine is None:
            row["note"] = "no rebuilt peak day"
        else:
            ref = found.iloc[0]
            row.update(coupling_month=int(ref["Month"]), coupling_day=int(ref["Day"]),
                       coupling_peak_kw=float(ref["Peak_kW"]))
            denominator = abs(row["coupling_peak_kw"]) or 1.0
            row["relative_difference"] = abs(mine[2] - row["coupling_peak_kw"]) / denominator
            row["passed"] = bool(mine[0] == row["coupling_month"] and mine[1] == row["coupling_day"]
                                 and row["relative_difference"] <= REL_TOLERANCE)
            if rows_read != rows_recipe:
                row["passed"], row["note"] = False, "rows read differ from rows in the recipe"
        rows.append(row)
    return rows


def interval_start(timestamps: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Start of each interval: one step before a period-ending timestamp.

    The files are period-ending when their first timestamp is one step after
    midnight of 1 January; a first timestamp at midnight is read as period-beginning.
    """
    timestamps = pd.DatetimeIndex(timestamps)
    step = timestamps[1] - timestamps[0]
    first = timestamps[0]
    ending = first == first.normalize().replace(month=1, day=1) + step
    return timestamps - step if ending else timestamps


def point_dates(timestamps) -> pd.DatetimeIndex:
    """Date of each point: the date of its interval start."""
    return interval_start(timestamps).normalize()


def season_mask(dates: pd.DatetimeIndex, season: str, year: int = YEAR) -> np.ndarray:
    return np.asarray((dates.year == year) & dates.month.isin(SEASONS[season]))


def peak_date(curve: np.ndarray, dates: pd.DatetimeIndex, season: str) -> pd.Timestamp:
    """Date of the curve's highest point in the season's months of the year (first on a tie)."""
    mask = season_mask(dates, season)
    index = np.flatnonzero(mask)[int(np.argmax(np.asarray(curve)[mask]))]
    return dates[index]


def ercot_days(fit: pd.DataFrame) -> pd.DataFrame:
    """ERCOT demand with the Central Standard Time date of each hour (an hour ending 00:00 belongs to the day before)."""
    hours = pd.DatetimeIndex(fit["cst_hour_ending"])
    if hours.tz is not None:
        hours = hours.tz_localize(None)  # keeps the Central Standard Time wall clock
    return pd.DataFrame({"date": (hours - pd.Timedelta(hours=1)).normalize(), "demand_mw": fit["demand_mw"].to_numpy()})


def day_b(ercot: pd.DataFrame, season: str) -> pd.Timestamp:
    """Date of ERCOT's highest ``demand_mw`` within the season's months of 2018 (first on a tie)."""
    mask = season_mask(pd.DatetimeIndex(ercot["date"]), season)
    part = ercot[mask]
    return pd.Timestamp(part["date"].iloc[int(np.argmax(part["demand_mw"].to_numpy()))])


def day_c(feeder_dates: dict[str, pd.Timestamp], curve: np.ndarray, dates: pd.DatetimeIndex) -> tuple[pd.Timestamp, list]:
    """Date on which the most run feeders peak; a tie goes to the date with the higher stratum curve.

    Returns the date and the tied dates (empty when there was no tie).
    """
    counts = pd.Series(list(feeder_dates.values())).value_counts()
    best = counts[counts == counts.max()].index.tolist()
    if len(best) == 1:
        return pd.Timestamp(best[0]), []
    highs = {d: float(np.max(np.asarray(curve)[np.asarray(dates == d)])) for d in best}
    chosen = max(sorted(highs), key=lambda d: highs[d])
    return pd.Timestamp(chosen), sorted(pd.Timestamp(d) for d in best)


def day_measures(date: pd.Timestamp, season: str, curve: np.ndarray, end_uses: pd.DataFrame,
                 dates: pd.DatetimeIndex, feeder_dates: dict[str, pd.Timestamp],
                 feeder_customers: dict[str, float], ercot: pd.DataFrame) -> dict:
    """Measures of one stratum, season and date (step 8)."""
    curve = np.asarray(curve)
    on_day = np.asarray(dates == date)
    seasonal_high = float(curve[season_mask(dates, season)].max())
    index = np.flatnonzero(on_day)[int(np.argmax(curve[on_day]))]
    at_peak = end_uses.iloc[index].sort_values(ascending=False)
    total_at = float(curve[index])
    peaking = [f for f, d in feeder_dates.items() if d == date]
    customers = sum(feeder_customers.values())
    ercot_season = ercot[season_mask(pd.DatetimeIndex(ercot["date"]), season)]
    ercot_day = ercot[ercot["date"] == date]
    row = {
        "date": date.date().isoformat(), "weekday": date.day_name(),
        "stratum_peak_on_day_kw": float(curve[on_day].max()), "stratum_seasonal_peak_kw": seasonal_high,
        "stratum_share_of_seasonal_peak": float(curve[on_day].max()) / seasonal_high,
        "feeders": len(feeder_dates), "feeders_peaking_on_day": len(peaking),
        "feeder_share_by_count": len(peaking) / len(feeder_dates) if feeder_dates else np.nan,
        "feeder_share_by_customers": (sum(feeder_customers[f] for f in peaking) / customers) if customers else np.nan,
        "ercot_peak_on_day_mw": float(ercot_day["demand_mw"].max()) if len(ercot_day) else np.nan,
        "ercot_seasonal_peak_mw": float(ercot_season["demand_mw"].max()),
        "stratum_peak_time": pd.Timestamp(interval_start(pd.DatetimeIndex(end_uses.index))[index]).isoformat(),
    }
    row["ercot_share_of_seasonal_peak"] = row["ercot_peak_on_day_mw"] / row["ercot_seasonal_peak_mw"]
    for rank in range(TOP_END_USES):
        name = at_peak.index[rank] if rank < len(at_peak) else ""
        kw = float(at_peak.iloc[rank]) if rank < len(at_peak) else np.nan
        row[f"end_use_{rank + 1}"] = name
        row[f"end_use_{rank + 1}_kw"] = kw
        row[f"end_use_{rank + 1}_share"] = kw / total_at if total_at else np.nan
    return row


def stratum_curve(rows: pd.DataFrame, passed: dict, points: int, keys: list) -> dict:
    """Sum of one stratum (K47 as amended by K60).

    Each group (row of the typical table) adds its run feeder's curve times
    ``customers / run_feeder_customers``; groups whose run feeder is not in
    ``passed`` are left out and counted.
    """
    total = np.zeros(points)
    uses = {key: np.zeros(points) for key in keys}
    feeders: dict[str, float] = {}
    used = rows["run_feeder"].isin(list(passed))
    for row in rows[used].itertuples(index=False):
        factor = row.customers / row.run_feeder_customers
        total += factor * np.asarray(passed[row.run_feeder]["total"], dtype=float)
        for key, values in passed[row.run_feeder]["end_uses"].items():
            uses[key] += factor * np.asarray(values, dtype=float)
        feeders[row.run_feeder] = feeders.get(row.run_feeder, 0.0) + row.customers
    left = rows[~used]
    return {"total": total, "end_uses": uses, "feeders": feeders, "groups": len(rows), "groups_used": int(used.sum()),
            "customers": float(rows["customers"].sum()), "customers_left_out": float(left["customers"].sum()),
            "feeders_left_out": sorted(set(left["run_feeder"]))}


def workspace_of(runs_root: Path, case_id: str, season: str = "summer") -> Path:
    return Path(runs_root) / case_id / season / "workspace"


def parquet_root_of(runs_root: Path, case_id: str, topology: str, season: str = "summer") -> Path:
    """The parquet root the run used: ``smart_ds_base`` and ``parquet_relpath`` of the settings copied into its run folder."""
    run_dir = Path(runs_root) / case_id / season
    with (run_dir / "settings" / "local.yaml").open(encoding="utf-8") as handle:
        local = yaml.safe_load(handle)
    with (run_dir / "settings" / "topology_registry.yaml").open(encoding="utf-8") as handle:
        registry = yaml.safe_load(handle)
    return Path(local["smart_ds_base"]) / registry["topologies"][topology]["parquet_relpath"]


def _one_feeder(job: dict) -> dict:
    """Worker: rebuild and check one feeder."""
    feeder, workspace, parquet_root = job["feeder"], Path(job["workspace"]), Path(job["parquet_root"])
    out = {"feeder": feeder, "error": "", "check": []}
    recipe_path = workspace / "5b_profile_generation" / RECIPE
    peaks_path = workspace / "5b_profile_generation" / PEAK_DAYS
    if not recipe_path.is_file() or not peaks_path.is_file():
        out["error"] = f"missing {RECIPE} or {PEAK_DAYS} in {workspace}"
        return out
    recipe = pd.read_csv(recipe_path)
    mine = recipe[recipe["Feeder"] == feeder]
    try:
        curve = feeder_curve(mine, parquet_root)
    except FeederError as error:
        out["error"] = str(error)
        out["check"] = check_feeder(feeder, {}, pd.read_csv(peaks_path), 0, len(mine))
        for row in out["check"]:
            row["note"] = f"stopped: {error}"
        return out
    out.update(curve)
    out["check"] = check_feeder(feeder, coupling_peaks(curve["total"]), pd.read_csv(peaks_path),
                                curve["rows_read"], len(mine))
    return out


def timestamp_rows(feeder: str, first_by_sector: dict) -> list[dict]:
    rows = []
    for sector, stamps in sorted(first_by_sector.items()):
        index = pd.DatetimeIndex(stamps)
        steps = sorted({str(s) for s in pd.Series(index[1:] - index[:-1]).unique()})
        rows.append({"feeder": feeder, "sector": sector, "first": index[0].isoformat(), "last": index[-1].isoformat(),
                     "count": len(index), "steps": " ".join(steps)})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--typical", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--fit-target", type=Path, default=None,
                        help="default <data_root>/derived/ERCO_2018_fit_target.parquet")
    parser.add_argument("--workers", type=int, default=8, help="worker processes (at most 8)")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be 1 to 8")

    settings = runs_settings(load_sample_settings())
    paths = Paths(load_local(), settings)
    out_dir = paths.texas / f"curves_{args.stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    fit_path = args.fit_target or paths.data_root / "derived" / "ERCO_2018_fit_target.parquet"
    ercot = ercot_days(pd.read_parquet(fit_path))
    typical = pd.read_csv(args.typical)
    features = pd.read_parquet(args.features)
    units = run_units(typical, features)
    own_stratum = dict(zip(units["feeder"], units["stratum"]))

    jobs = []
    for unit in units.to_dict("records"):
        topology = topology_id(settings, unit)
        case_id = f"{topology}__{settings['coupling']['donor_id']}"
        workspace = workspace_of(args.runs_root, case_id)
        try:
            root = parquet_root_of(args.runs_root, case_id, topology)
        except (OSError, KeyError) as error:
            root = Path(".")
            print(f"{unit['feeder']}: no run settings ({error})")
        jobs.append({"feeder": unit["feeder"], "workspace": str(workspace), "parquet_root": str(root)})

    checks, stamps_rows, errors = [], [], {}
    passed: dict[str, dict] = {}
    timestamps = None
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(_one_feeder, jobs):
            feeder = result["feeder"]
            checks += result["check"] or [{"feeder": feeder, "season": s, "passed": False, "note": result["error"]}
                                          for s in SEASONS]
            if result["error"]:
                errors[feeder] = result["error"]
                print(f"{feeder}: {result['error']}")
                continue
            stamps_rows += timestamp_rows(feeder, result["first_by_sector"])
            ok = all(row["passed"] for row in result["check"])
            print(f"{feeder}: rows {result['rows_read']}, check {'passed' if ok else 'FAILED'}")
            if not ok:
                continue
            if timestamps is None:
                timestamps = result["timestamps"]
            elif not np.array_equal(timestamps, result["timestamps"]):
                errors[feeder] = "timestamps differ from the other feeders'"
                continue
            passed[feeder] = {"total": result["total"],
                              "end_uses": {k: v.astype(np.float32) for k, v in result["end_uses"].items()}}
    check_table = pd.DataFrame(checks)
    write_csv(check_table, out_dir / "curves_check.csv")
    stamps_table = pd.DataFrame(stamps_rows)
    if len(stamps_table):
        summary = (stamps_table.groupby(["sector", "first", "last", "count", "steps"]).size()
                   .rename("feeders").reset_index())
    else:
        summary = pd.DataFrame(columns=["sector", "first", "last", "count", "steps", "feeders"])
    summary["convention"] = TIMESTAMP_ASSUMPTION
    summary["source"] = TIMESTAMP_SOURCE
    write_csv(summary, out_dir / "timestamps.csv")
    if timestamps is None:
        print("no feeder passed the check; no curves or days")
        return 1

    index = pd.DatetimeIndex(timestamps)
    dates = point_dates(index)
    feeder_long = pd.DataFrame({
        "feeder": np.repeat(list(passed), len(index)),
        "timestamp": np.tile(index, len(passed)),
        "kw": np.concatenate([passed[f]["total"] for f in passed]).astype(np.float32),
    })
    feeder_long.to_parquet(out_dir / "feeder_curves.parquet", index=False)

    # 4. Stratum curves.
    keys = sorted({key for f in passed.values() for key in f["end_uses"]})
    strata_rows, stratum_curves, stratum_end_uses, stratum_feeders = [], {}, {}, {}
    for stratum, rows in typical.groupby("stratum", sort=True):
        summed = stratum_curve(rows, passed, len(index), keys)
        total = summed["total"]
        stratum_curves[stratum], stratum_feeders[stratum] = total, summed["feeders"]
        stratum_end_uses[stratum] = pd.DataFrame({f"{s}:{u}": v for (s, u), v in summed["end_uses"].items()},
                                                 index=index)
        strata_rows.append({
            "stratum": stratum, "groups": summed["groups"], "groups_used": summed["groups_used"],
            "customers": int(summed["customers"]), "customers_left_out": int(summed["customers_left_out"]),
            "customer_share_left_out": summed["customers_left_out"] / summed["customers"],
            "run_feeders_used": len(summed["feeders"]), "run_feeders_left_out": " ".join(summed["feeders_left_out"]),
            "end_use_residual_max_kw": float(np.abs(stratum_end_uses[stratum].sum(axis=1).to_numpy() - total).max()),
        })
    write_csv(pd.DataFrame(strata_rows), out_dir / "strata.csv")
    stratum_long = []
    for stratum, total in stratum_curves.items():
        part = stratum_end_uses[stratum].astype(np.float32)
        part.insert(0, "kw", total.astype(np.float32))
        part.insert(0, "timestamp", index)
        part.insert(0, "stratum", stratum)
        stratum_long.append(part.reset_index(drop=True))
    pd.concat(stratum_long, ignore_index=True).to_parquet(out_dir / "stratum_curves.parquet", index=False)

    # Each run feeder's own seasonal peak date on the 2018 calendar.
    own = {(f, s): peak_date(passed[f]["total"], dates, s) for f in passed for s in SEASONS}
    write_csv(pd.DataFrame([{"feeder": f, "season": s, "date": d.date().isoformat()} for (f, s), d in own.items()]),
              out_dir / "feeder_peaks.csv")

    # 5 to 8. Days A, B and C.
    b_days = {season: day_b(ercot, season) for season in SEASONS}
    common, a_days = [], {}
    for stratum, total in stratum_curves.items():
        if not stratum_feeders[stratum]:
            continue
        for season in SEASONS:
            feeder_dates = {f: own[(f, season)] for f in stratum_feeders[stratum]}
            a = peak_date(total, dates, season)
            c, ties = day_c(feeder_dates, total, dates)
            a_days[(stratum, season)] = a
            for option, date in (("A", a), ("B", b_days[season]), ("C", c)):
                row = {"stratum": stratum, "season": season, "option": option}
                row.update(day_measures(date, season, total, stratum_end_uses[stratum], dates, feeder_dates,
                                        stratum_feeders[stratum], ercot))
                row["ties"] = " ".join(d.date().isoformat() for d in ties) if option == "C" else ""
                common.append(row)
    write_csv(pd.DataFrame(common), out_dir / "common_days.csv")

    # 9. Given days.
    multi = typical.groupby("run_feeder")["stratum"].nunique()
    multi = sorted(multi[multi > 1].index)
    for option in ("A", "B"):
        rows = []
        for feeder in units["feeder"]:
            stratum = own_stratum[feeder]
            for season in SEASONS:
                date = a_days.get((stratum, season)) if option == "A" else b_days[season]
                if date is not None:
                    rows.append({"Feeder": feeder, "Season": season, "Month": date.month, "Day": date.day})
        write_csv(pd.DataFrame(rows, columns=["Feeder", "Season", "Month", "Day"]),
                  out_dir / f"given_days_{option}_{args.stamp}.csv")
    print(f"run feeders in two strata (own stratum used): {', '.join(multi) or 'none'}")
    print(f"K66 check passed: {int(check_table['passed'].sum())} of {len(check_table)}")
    print(f"feeders stopped: {len(errors)}")
    print(f"written: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
