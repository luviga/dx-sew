"""Fit one weight per fit column to ERCOT's 2018 hourly target (decision K43).

The weighted sum of the groups' hourly curves is fitted to the hourly target of
``dxsew.fit_target`` by non-negative least squares on three weeks of every month
and checked on the rest of each month. Errors of energy, monthly peak and daily
range are reported for three models: the starting weights, one common scale and
the fitted weights. Method and outputs: docs/fit.md.

Formulas
--------
c_f(h)  Hourly curve of run feeder f in MW for the Central Standard Time (CST,
        UTC minus 6 hours) hour ending h: the mean of the four 15-minute kW
        values stamped h + 15 min to h + 60 min in Eastern Standard Time (EST,
        UTC minus 5 hours), divided by 1,000. The building files' timestamps are
        "15-minute period-ending in EST" (README of the End Use Load Profiles,
        section "Units and timestamps",
        https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/README.md).
X_j(h)  Fit column j, one per distinct run feeder f(j): the sum over the groups g
        whose run feeder is f(j) of customers_g / run_feeder_customers_g * c_f(h)
        (K47 as amended by K60).
T(h)    target_mw of the fit target.
Starting weights   w_j = 1 for every column (each group at its own customers).
Common scale       w_j = s for every column, s = max(0, sum X T / sum X^2) with
                   X(h) = sum_j X_j(h), least squares on the fit hours.
Fitted weights     w = argmin_{w >= 0} || X w - T ||^2 on the fit hours
                   (scipy.optimize.nnls), no intercept.

Run: uv run python -m dxsew.fit --stamp <stamp> --curves <feeder_curves.parquet>
     --typical <typical_feeders csv> --target <fit target parquet>
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls

from dxsew.texas.config import load_local

POINTS_PER_HOUR = 4
KW_PER_MW = 1000.0
# EST (UTC minus 5 hours) is one hour ahead of CST (UTC minus 6 hours).
EST_MINUS_CST = pd.Timedelta(hours=1)
HOUR = pd.Timedelta(hours=1)

# ASSUMPTION: "three weeks of every month" in K43 is read as days 1 to 21 of each
# month, and the fourth week as day 22 to the month's end, so every hour of the
# year is used once.
LAST_FIT_DAY = 21

# ASSUMPTION: the day of an hour is the CST date of its hour start, so an hour
# ending 00:00 belongs to the day before (the hour-ending convention of the fit
# target and of the common days in docs/curves.md).

MODELS = ("starting", "common_scale", "fitted")
SPLITS = ("test", "fit")


# 1. Hourly curves in CST.

def cst_hour_ending(est_timestamps) -> pd.DatetimeIndex:
    """CST hour ending of each 15-minute point stamped (period-ending) in EST.

    The point stamped t in EST covers (t - 15 min, t] EST. The CST hour ending h
    covers (h - 1 h, h] CST, which is (h, h + 1 h] EST, so it holds the points
    stamped h + 15 min to h + 60 min EST: h = ceil_hour(t) - 1 h.
    """
    t = pd.DatetimeIndex(est_timestamps)
    return t.ceil("h") - EST_MINUS_CST


def hourly_curves(curves: pd.DataFrame, hours: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hourly MW of each feeder on the CST hours ``hours`` (naive CST hour ending).

    ``curves`` is the long table ``feeder, timestamp, kw`` with naive EST
    timestamps. Returns the wide table (index ``hours``, one column per feeder,
    only hours that every feeder covers with all four points) and the table of
    left-out hours: hours of ``hours`` without four points for some feeder, and
    CST hours of the curves that are not in ``hours`` (with the points they hold).
    """
    frame = curves[["feeder", "timestamp", "kw"]].copy()
    frame["hour"] = cst_hour_ending(frame["timestamp"])
    grouped = frame.groupby(["hour", "feeder"], sort=True)["kw"]
    total = grouped.sum().unstack("feeder")
    count = grouped.size().unstack("feeder")
    feeders = sorted(frame["feeder"].unique())
    total = total.reindex(index=hours, columns=feeders)
    count = count.reindex(index=hours, columns=feeders).fillna(0).astype(int)
    complete = (count == POINTS_PER_HOUR).all(axis=1)
    left = []
    for hour in hours[~complete.to_numpy()]:
        left.append({"cst_hour_ending": hour, "in_target": True,
                     "min_points": int(count.loc[hour].min()), "max_points": int(count.loc[hour].max()),
                     "reason": "fewer than four 15-minute points for some feeder"})
    outside = sorted(set(frame["hour"].unique()) - set(hours))
    for hour in outside:
        points = frame.loc[frame["hour"] == hour].groupby("feeder").size()
        left.append({"cst_hour_ending": pd.Timestamp(hour), "in_target": False,
                     "min_points": int(points.min()), "max_points": int(points.max()),
                     "reason": "curve points outside the target's hours"})
    mw = total.loc[complete.to_numpy()] / POINTS_PER_HOUR / KW_PER_MW
    left_table = pd.DataFrame(left, columns=["cst_hour_ending", "in_target", "min_points", "max_points", "reason"])
    return mw, left_table


# 2. Fit columns.

def fit_columns(typical: pd.DataFrame, feeder_mw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One column per distinct run feeder and the table of the groups it holds.

    Column j is the sum over its groups of ``customers / run_feeder_customers``
    times the run feeder's hourly curve. Groups that share a run feeder have
    proportional curves, so they are merged into one column.
    """
    missing = sorted(set(typical["run_feeder"]) - set(feeder_mw.columns))
    if missing:
        raise KeyError(f"run feeders without a curve: {missing}")
    rows = typical.copy()
    rows["factor"] = rows["customers"] / rows["run_feeder_customers"]
    factor = rows.groupby("run_feeder", sort=True)["factor"].sum()
    columns = feeder_mw[factor.index].mul(factor, axis=1)
    columns.columns.name = "column"
    rows["column"] = rows["run_feeder"]
    return columns, rows


# 3. Split.

def hour_day(hours) -> pd.DatetimeIndex:
    """CST date of each hour ending (the date of its hour start)."""
    return (pd.DatetimeIndex(hours) - HOUR).normalize()


def split_of(hours) -> np.ndarray:
    """``"fit"`` for days 1 to LAST_FIT_DAY of each month, else ``"test"``."""
    day = hour_day(hours).day
    return np.where(day <= LAST_FIT_DAY, "fit", "test")


# 4. Fit.

def fit_nnls(columns: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, float]:
    """Non-negative least squares without intercept: weights and residual norm."""
    weights, residual = nnls(np.asarray(columns, dtype=float), np.asarray(target, dtype=float))
    return weights, float(residual)


def fit_common_scale(columns: np.ndarray, target: np.ndarray) -> float:
    """One non-negative scale on the sum of all columns, least squares."""
    x = np.asarray(columns, dtype=float).sum(axis=1)
    denom = float(x @ x)
    return max(0.0, float(x @ np.asarray(target, dtype=float)) / denom) if denom > 0 else 0.0


# 5. Errors.

def energy_error(model, target) -> float:
    """(model energy - target energy) / target energy."""
    return float((np.sum(model) - np.sum(target)) / np.sum(target))


def peak_errors(model: pd.Series, target: pd.Series) -> pd.Series:
    """Per month: (highest model hour - highest target hour) / highest target hour.

    Both series share a CST hour-ending index; the month is that of the hour's day.
    """
    month = hour_day(model.index).month
    m = model.groupby(month).max()
    t = target.groupby(month).max()
    return (m - t) / t


def range_errors(model: pd.Series, target: pd.Series) -> pd.Series:
    """Per day: ((model max - model min) - (target max - target min)) / target range.

    Index of the result: the CST date of each day.
    """
    day = hour_day(model.index)
    m = model.groupby(day).agg(lambda v: v.max() - v.min())
    t = target.groupby(day).agg(lambda v: v.max() - v.min())
    return (m - t) / t


def cv_rmse(model, target) -> float:
    """Root mean square error over the mean target (a hub addition to K43)."""
    model, target = np.asarray(model, dtype=float), np.asarray(target, dtype=float)
    return float(np.sqrt(np.mean((model - target) ** 2)) / np.mean(target))


def error_row(model: pd.Series, target: pd.Series) -> dict:
    """All errors of one model on one set of hours."""
    peaks = peak_errors(model, target)
    ranges = range_errors(model, target)
    model_peak, target_peak = model.idxmax(), target.idxmax()
    return {
        "hours": len(model),
        "days": len(ranges),
        "model_mwh": float(model.sum()),
        "target_mwh": float(target.sum()),
        "energy_error": energy_error(model, target),
        "peak_error_mean_abs": float(peaks.abs().mean()),
        "range_error_mean_abs": float(ranges.abs().mean()),
        "cv_rmse": cv_rmse(model, target),
        "model_peak_mw": float(model.max()),
        "model_peak_hour": model_peak,
        "target_peak_mw": float(target.max()),
        "target_peak_hour": target_peak,
        "peak_hour_gap_hours": float((model_peak - target_peak) / HOUR),
    }


def month_rows(model: pd.Series, target: pd.Series) -> pd.DataFrame:
    """Per month: energy, peak and mean absolute daily range errors."""
    month = hour_day(model.index).month
    ranges = range_errors(model, target)
    range_month = ranges.abs().groupby(ranges.index.month).mean()
    energy = model.groupby(month).sum()
    target_energy = target.groupby(month).sum()
    return pd.DataFrame({
        "month": energy.index,
        "hours": model.groupby(month).size().to_numpy(),
        "energy_error": ((energy - target_energy) / target_energy).to_numpy(),
        "peak_error": peak_errors(model, target).reindex(energy.index).to_numpy(),
        "range_error_mean_abs": range_month.reindex(energy.index).to_numpy(),
    })


# 6. Weights table.

def weights_table(groups: pd.DataFrame, weights: pd.Series) -> pd.DataFrame:
    """Per group: its column, the column's groups, strata, customers and fitted weight;
    implied weight = fitted weight * starting weight, and its share of the stratum's sum."""
    table = groups.copy()
    table["_label"] = table["stratum"].astype(str) + "#" + table["group"].astype(str)
    held = table.groupby("column")
    table["column_groups"] = table["column"].map(held["_label"].agg(";".join))
    table["column_strata"] = table["column"].map(held["stratum"].agg(lambda s: ";".join(sorted(set(s)))))
    table["column_customers"] = table["column"].map(held["customers"].sum())
    table["fitted_weight"] = table["column"].map(weights)
    table["implied_weight"] = table["fitted_weight"] * table["starting_weight"]
    stratum_sum = table.groupby("stratum")["implied_weight"].transform("sum")
    table["fitted_share"] = np.where(stratum_sum > 0, table["implied_weight"] / stratum_sum.where(stratum_sum > 0, 1), np.nan)
    keep = ["stratum", "group", "customers", "starting_weight", "run_feeder", "run_is_placeholder",
            "run_feeder_customers", "factor", "column", "column_groups", "column_strata", "column_customers",
            "fitted_weight", "implied_weight", "fitted_share"]
    return table[keep].sort_values(["stratum", "group"]).reset_index(drop=True)


# Driver.

def _write_csv(table: pd.DataFrame, path: Path) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.part")
    table.to_csv(temporary, index=False, lineterminator="\n", date_format="%Y-%m-%d %H:%M")
    temporary.replace(path)


def run(curves: pd.DataFrame, typical: pd.DataFrame, target: pd.DataFrame) -> dict:
    """The whole fit on in-memory tables; returns every output table."""
    hours = pd.DatetimeIndex(target["cst_hour_ending"]).tz_localize(None)
    target_series = pd.Series(target["target_mw"].to_numpy(dtype=float), index=hours)
    feeder_mw, left_out = hourly_curves(curves, hours)
    columns, groups = fit_columns(typical, feeder_mw)
    used = columns.index
    y = target_series.loc[used]
    split = pd.Series(split_of(used), index=used)
    fit_mask = (split == "fit").to_numpy()

    weights, residual = fit_nnls(columns.to_numpy()[fit_mask], y.to_numpy()[fit_mask])
    weights = pd.Series(weights, index=columns.columns)
    scale = fit_common_scale(columns.to_numpy()[fit_mask], y.to_numpy()[fit_mask])
    base = columns.sum(axis=1)
    models = {"starting": base, "common_scale": base * scale, "fitted": columns @ weights}

    errors, months = [], []
    for name in MODELS:
        for part in SPLITS:
            mask = (split == part).to_numpy()
            errors.append({"model": name, "split": part, **error_row(models[name][mask], y[mask])})
            m = month_rows(models[name][mask], y[mask])
            m.insert(0, "split", part)
            m.insert(0, "model", name)
            months.append(m)

    stamps = target[["cst_hour_ending", "utc_hour_ending"]].set_axis(hours).loc[used]
    hourly = pd.DataFrame({
        "cst_hour_ending": stamps["cst_hour_ending"].reset_index(drop=True),
        "utc_hour_ending": stamps["utc_hour_ending"].reset_index(drop=True),
        "split": split.to_numpy(),
        "target_mw": y.to_numpy(),
        "starting_mw": models["starting"].to_numpy(),
        "common_scale_mw": models["common_scale"].to_numpy(),
        "fitted_mw": models["fitted"].to_numpy(),
    })
    hourly = pd.concat([hourly, columns.reset_index(drop=True)], axis=1)
    return {
        "hourly": hourly,
        "weights": weights_table(groups, weights),
        "errors": pd.DataFrame(errors),
        "months": pd.concat(months, ignore_index=True),
        "left_out": left_out,
        "summary": {
            "hours_in_target": len(hours), "hours_used": len(used), "hours_left_out_of_target": len(hours) - len(used),
            "fit_hours": int(fit_mask.sum()), "test_hours": int((~fit_mask).sum()),
            "columns": int(columns.shape[1]), "groups": len(groups),
            "common_scale": scale, "nnls_residual_norm": residual,
            "columns_at_zero": int((weights == 0).sum()),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--curves", type=Path, required=True, help="feeder_curves.parquet of dxsew.texas.curves")
    parser.add_argument("--typical", type=Path, required=True, help="typical_feeders csv")
    parser.add_argument("--target", type=Path, required=True, help="fit target parquet of dxsew.fit_target")
    parser.add_argument("--out-root", type=Path, default=None, help="default <data_root>/texas")
    args = parser.parse_args(argv)

    out_root = args.out_root or Path(load_local()["data_root"]) / "texas"
    out_dir = out_root / f"fit_{args.stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)

    result = run(pd.read_parquet(args.curves), pd.read_csv(args.typical), pd.read_parquet(args.target))
    result["hourly"].to_parquet(out_dir / "hourly.parquet", index=False)
    _write_csv(result["weights"], out_dir / "weights.csv")
    _write_csv(result["errors"], out_dir / "errors.csv")
    _write_csv(result["months"], out_dir / "months.csv")
    _write_csv(result["left_out"], out_dir / "hours_left_out.csv")
    summary = {**result["summary"], "inputs": {"curves": str(args.curves), "typical": str(args.typical),
                                               "target": str(args.target)}}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
