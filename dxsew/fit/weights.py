"""Fit one weight per fit column to the area's hourly target of the setup's year (decision K43).

The weighted sum of the groups' hourly curves is fitted to the hourly target of
``dxsew.fit.target`` by non-negative least squares on three weeks of every month
and checked on the rest of each month. Errors of energy, monthly peak and daily
range are reported for three models: the starting weights, one common scale and
the fitted weights. Method and outputs: docs/fit.md.

Formulas
--------
c_f(h)  Hourly curve of run feeder f in MW for the Central Standard Time (CST,
        UTC minus 6 hours) hour ending h: the mean of the four 15-minute kW
        values stamped h + 15 min to h + 60 min in Eastern Standard Time (EST,
        UTC minus 5 hours), divided by 1,000 (config/units.yaml). The building files' timestamps are
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
                   (scipy.optimize.nnls), no intercept. A measurement; it is not
                   used for weights (K71).

Sector factors (K71). R(h) and B(h) are the frame's homes and businesses curves
at starting weights (sums of the res: and com: end uses of stratum_curves.parquet).
For each sector k, from EIA-861 sales S_k and customers N_eia_k, the frame's annual
energy E_k, and the customers N_model_k (starting model) and N_frame_k (frame):
  M_k = S_k / E_k, e_k = (S_k / N_eia_k) / (E_k / N_model_k),
  m_k = N_frame_k / N_model_k, c_k = N_eia_k / N_frame_k, e_k m_k c_k = M_k.
sector_formula       M_res R(h) + M_com B(h), the K71 model; nothing is fitted.
sector_fit           s_res R(h) + s_com B(h), s >= 0 by nnls on the fit hours. Report only.
scale_plus_constant  a X(h) + b, least squares on the fit hours. Report only.

Run: uv run python -m dxsew.fit.weights --stamp <stamp> --curves <feeder_curves.parquet>
     --typical <typical_feeders csv> --target <fit target parquet>
     --stratum-curves <stratum_curves.parquet> --features <features parquet>
     --eia861 <EIA-861 zip> [--setup <id>]

Setup values: the block fit (last fit day, tolerances, the K71 texts and the
source texts of the sector inputs) with the sentence beside each value.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import nnls

from dxsew import config
from dxsew.config import load_local, load_runtime, load_units
from dxsew.sources import eia861, load_sources

POINTS_PER_HOUR = load_units()["points_per_hour"]
KW_PER_MW = load_units()["kw_per_mw"]
# EST (UTC minus 5 hours) is one hour ahead of CST (UTC minus 6 hours).
EST_MINUS_CST = pd.Timedelta(hours=1)
HOUR = pd.Timedelta(hours=1)


def _fit() -> dict:
    """The block ``fit`` of the setup of this process: last fit day, tolerances and texts.

    The day of an hour is the CST date of its hour start (the setup's sentence
    ``fit.hour_day``).
    """
    return config.setup()["fit"]


MODELS = ("starting", "common_scale", "fitted", "sector_formula", "sector_fit", "scale_plus_constant")
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


def split_of(hours, last_fit_day: int | None = None) -> np.ndarray:
    """``"fit"`` for days 1 to ``last_fit_day`` (the setup's ``fit.last_fit_day``) of each month, else ``"test"``."""
    last_fit_day = _fit()["last_fit_day"] if last_fit_day is None else last_fit_day
    day = hour_day(hours).day
    return np.where(day <= last_fit_day, "fit", "test")


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


# 7. Sector factors (K71). Method: docs/fit.md, section "Sector factors (K71)".

SECTORS = ("res", "com")
SECTOR_CLASS = {"res": "residential", "com": "commercial"}
SECTOR_NAME = {"res": "homes", "com": "businesses"}
HOURS_PER_POINT = 1.0 / POINTS_PER_HOUR

# The tolerances of the two checks of the sector split (fit.end_use_tolerance,
# fit.starting_tolerance), of the check e * m * c = M (fit.product_tolerance) and
# the three texts of K71 that the run fills and writes to summary.json are keys
# of the block fit of the setup, each with its sentence beside it. The same
# texts, with bare placeholders, are in docs/fit.md.


def sector_columns(columns, sector: str) -> list[str]:
    """The end-use columns ``<sector>:<end use>`` of the stratum curves."""
    return [c for c in columns if str(c).startswith(f"{sector}:")]


def sector_curves(stratum: pd.DataFrame, tolerance: float | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The frame's 15-minute homes and businesses curves at starting weights.

    ``stratum`` is ``stratum_curves.parquet`` of dxsew.curves.year (columns
    ``stratum``, ``timestamp``, ``kw``, ``res:<end use>``, ``com:<end use>``).
    R(t) is the sum over strata of every ``res:`` column and B(t) the same for
    ``com:``, in kW on the building files' own timestamps. Returns the curves
    (index ``timestamp``; columns ``res``, ``com`` and ``kw``, the sum of the
    strata's kw) and, per stratum, the largest gap |kw - (res sum + com sum)| in
    kW and relative to kw, its timestamp, and the points over ``tolerance`` (the
    setup's ``fit.end_use_tolerance`` by default).
    """
    tolerance = _fit()["end_use_tolerance"] if tolerance is None else tolerance
    res = stratum[sector_columns(stratum.columns, "res")].astype(float).sum(axis=1)
    com = stratum[sector_columns(stratum.columns, "com")].astype(float).sum(axis=1)
    kw = stratum["kw"].astype(float)
    table = pd.DataFrame({"stratum": stratum["stratum"], "timestamp": stratum["timestamp"],
                          "res": res, "com": com, "kw": kw})
    table["gap_kw"] = (kw - (res + com)).abs()
    table["gap_rel"] = table["gap_kw"] / kw.abs()
    checks = []
    for name, part in table.groupby("stratum", sort=True):
        worst = part["gap_rel"].idxmax()
        checks.append({"stratum": name, "points": len(part), "max_gap_kw": float(part["gap_kw"].max()),
                       "max_gap_rel": float(part.at[worst, "gap_rel"]),
                       "max_gap_rel_timestamp": part.at[worst, "timestamp"],
                       "points_over_tolerance": int((part["gap_rel"] > tolerance).sum())})
    curves = table.groupby("timestamp", sort=True)[["res", "com", "kw"]].sum()
    return curves, pd.DataFrame(checks)


def sector_hourly(curves: pd.DataFrame, hours: pd.DatetimeIndex) -> pd.DataFrame:
    """Hourly MW of the two sector curves on the CST hours ``hours``, by the rule of ``hourly_curves``."""
    long = curves[list(SECTORS)].rename_axis("timestamp").reset_index().melt(
        id_vars="timestamp", var_name="feeder", value_name="kw")
    mw, _ = hourly_curves(long, hours)
    return mw[list(SECTORS)]


def sector_energy_mwh(curves: pd.DataFrame) -> dict:
    """E_k: the sum of every 15-minute kW point of sector k times 0.25 h, divided by 1,000 (MWh)."""
    return {k: float(curves[k].sum()) * HOURS_PER_POINT / KW_PER_MW for k in SECTORS}


def sector_customers(typical: pd.DataFrame, features: pd.DataFrame) -> dict:
    """N_model_k and N_frame_k.

    N_model_k is the sum over the groups of customers / run_feeder_customers times
    the run feeder's own ``<k>_customers`` in the features file; N_frame_k is the
    sum of ``<k>_customers`` over every feeder of the features file (the frame).
    """
    if features["feeder"].duplicated().any():
        raise ValueError("the features file names a feeder twice")
    own = features.set_index("feeder")
    missing = sorted(set(typical["run_feeder"]) - set(own.index))
    if missing:
        raise KeyError(f"run feeders without features: {missing}")
    factor = typical["customers"] / typical["run_feeder_customers"]
    return {
        "N_model": {k: float((factor * typical["run_feeder"].map(own[f"{k}_customers"])).sum()) for k in SECTORS},
        "N_frame": {k: float(features[f"{k}_customers"].sum()) for k in SECTORS},
        "frame_feeders": len(features),
    }


def sector_factors(S: float, N_eia: float, E: float, N_model: float, N_frame: float,
                   tolerance: float | None = None) -> dict:
    """The K71 factors of one sector and the check e * m * c = M.

    M = S / E                          multiplier on the frame's sector curve for the ERCOT total
    e = (S / N_eia) / (E / N_model)    energy factor; the one that reaches each feeder's sector load
    m = N_frame / N_model              mix factor
    c = N_eia / N_frame                count factor

    ``tolerance`` is the setup's ``fit.product_tolerance`` by default.
    """
    tolerance = _fit()["product_tolerance"] if tolerance is None else tolerance
    M = S / E
    e = (S / N_eia) / (E / N_model)
    m = N_frame / N_model
    c = N_eia / N_frame
    product_gap = abs(e * m * c - M) / M
    if product_gap > tolerance:
        raise ArithmeticError(f"e * m * c differs from M by {product_gap:.3e} relative")
    return {"M": M, "e": e, "m": m, "c": c, "product_gap": product_gap}


def fit_sector_scales(res, com, target) -> tuple[float, float]:
    """s_res, s_com >= 0: least squares of s_res R + s_com B on the target (scipy.optimize.nnls). Report only."""
    x = np.column_stack([np.asarray(res, dtype=float), np.asarray(com, dtype=float)])
    scales, _ = nnls(x, np.asarray(target, dtype=float))
    return float(scales[0]), float(scales[1])


def fit_scale_plus_constant(model, target) -> tuple[float, float]:
    """a, b: least squares of a X + b on the target (numpy.linalg.lstsq), no bounds. Report only."""
    x = np.asarray(model, dtype=float)
    design = np.column_stack([x, np.ones_like(x)])
    (a, b), *_ = np.linalg.lstsq(design, np.asarray(target, dtype=float), rcond=None)
    return float(a), float(b)


def sector_inputs(sales: pd.DataFrame, state: str, ba_code: str) -> dict:
    """S_k and N_eia_k by EIA-861 class from the table of ``eia861.read_sales``,
    Bundled and Energy rows, with Delivery rows included beside them (report only)."""
    by_class = eia861.sales_by_class(sales, state, ba_code)
    customers = eia861.customers_by_class(sales, state, ba_code)
    return {
        "mwh": by_class["mwh"].to_dict(), "mwh_with_delivery": by_class["mwh_with_delivery"].to_dict(),
        "customers": customers["customers"].to_dict(),
        "customers_with_delivery": customers["customers_with_delivery"].to_dict(),
        "rows_by_service_type": eia861.service_type_counts(sales, state, ba_code),
    }


def sector_model(stratum: pd.DataFrame, typical: pd.DataFrame, features: pd.DataFrame, eia: dict, year: int,
                 hours: pd.DatetimeIndex, sources: dict) -> dict:
    """Sector curves, inputs and factors of K71.

    ``eia`` is the result of ``sector_inputs``; ``sources`` gives the source text of
    each input (keys ``eia861``, ``E``, ``N_model``, ``N_frame``).
    """
    rule = _fit()
    texts_key = load_runtime()["names"]["texts_key"]
    curves, checks = sector_curves(stratum, rule["end_use_tolerance"])
    hourly = sector_hourly(curves, hours)
    energy = sector_energy_mwh(curves)
    counts = sector_customers(typical, features)
    rows, factors = [], {}
    for k in SECTORS:
        cls = SECTOR_CLASS[k]
        S, N_eia = eia["mwh"][cls], eia["customers"][cls]
        f = sector_factors(S, N_eia, energy[k], counts["N_model"][k], counts["N_frame"][k], rule["product_tolerance"])
        factors[k] = f
        rows.append({
            "sector": k, "sector_name": SECTOR_NAME[k], "eia861_class": cls,
            "S_mwh": S, "S_source": sources["eia861"],
            "S_mwh_with_delivery_report_only": eia["mwh_with_delivery"][cls],
            "N_eia": N_eia, "N_eia_source": sources["eia861"],
            "N_eia_with_delivery_report_only": eia["customers_with_delivery"][cls],
            "E_mwh": energy[k], "E_points": len(curves), "E_source": sources["E"],
            "N_model": counts["N_model"][k], "N_model_source": sources["N_model"],
            "N_frame": counts["N_frame"][k], "N_frame_source": sources["N_frame"],
            "M": f["M"], "e": f["e"], "m": f["m"], "c": f["c"], "emc_product_gap": f["product_gap"],
        })
    values = {"year": year}
    for k in SECTORS:
        values.update({f"N_frame_{k}": counts["N_frame"][k], f"N_eia_{k}": eia["customers"][SECTOR_CLASS[k]],
                       f"e_{k}": factors[k]["e"]})
    return {"curves": curves, "checks": checks, "hourly": hourly, "energy": energy, "counts": counts,
            "factors": factors, "table": pd.DataFrame(rows),
            texts_key: {name: text.format(**values) for name, text in rule[texts_key].items()}}


# Driver.

def _write_csv(table: pd.DataFrame, path: Path) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.part")
    table.to_csv(temporary, index=False, lineterminator="\n", date_format="%Y-%m-%d %H:%M")
    temporary.replace(path)


def run(curves: pd.DataFrame, typical: pd.DataFrame, target: pd.DataFrame, sector: dict | None = None) -> dict:
    """The whole fit on in-memory tables; returns every output table.

    ``sector`` (optional) adds the K71 models: a dict with ``stratum`` (stratum
    curves), ``features``, ``eia`` (result of ``sector_inputs``), ``year`` and
    ``sources`` (see ``sector_model``). Without it only J5's three models are run.
    """
    rule = _fit()
    texts_key = load_runtime()["names"]["texts_key"]
    hours = pd.DatetimeIndex(target["cst_hour_ending"]).tz_localize(None)
    target_series = pd.Series(target["target_mw"].to_numpy(dtype=float), index=hours)
    feeder_mw, left_out = hourly_curves(curves, hours)
    columns, groups = fit_columns(typical, feeder_mw)
    used = columns.index
    y = target_series.loc[used]
    split = pd.Series(split_of(used, rule["last_fit_day"]), index=used)
    fit_mask = (split == "fit").to_numpy()

    weights, residual = fit_nnls(columns.to_numpy()[fit_mask], y.to_numpy()[fit_mask])
    weights = pd.Series(weights, index=columns.columns)
    scale = fit_common_scale(columns.to_numpy()[fit_mask], y.to_numpy()[fit_mask])
    base = columns.sum(axis=1)
    models = {"starting": base, "common_scale": base * scale, "fitted": columns @ weights}

    sector_out, sector_summary = None, None
    if sector is not None:
        sector_out = sector_model(sector["stratum"], typical, sector["features"], sector["eia"], sector["year"],
                                  hours, sector["sources"])
        by_hour = sector_out["hourly"].reindex(used)
        if by_hour.isna().any().any():
            raise ValueError("the sector curves lack hours that the feeder curves cover")
        res, com = by_hour["res"], by_hour["com"]
        M = {k: sector_out["factors"][k]["M"] for k in SECTORS}
        s_res, s_com = fit_sector_scales(res.to_numpy()[fit_mask], com.to_numpy()[fit_mask], y.to_numpy()[fit_mask])
        a, b = fit_scale_plus_constant(base.to_numpy()[fit_mask], y.to_numpy()[fit_mask])
        models["sector_formula"] = M["res"] * res + M["com"] * com
        models["sector_fit"] = s_res * res + s_com * com
        models["scale_plus_constant"] = a * base + b
        gap = ((res + com) - base).abs() / base.abs()
        table = sector_out["table"]
        table["fitted_scale"] = table["sector"].map({"res": s_res, "com": s_com})
        table["fitted_scale_gap"] = (table["fitted_scale"] - table["M"]) / table["M"]
        table["scale_plus_constant_a"] = a
        table["scale_plus_constant_b_mw"] = b
        energy = sector_out["energy"]
        sales = {k: sector["eia"]["mwh"][SECTOR_CLASS[k]] for k in SECTORS}
        formula_year = sum(M[k] * energy[k] for k in SECTORS)
        checks = sector_out["checks"]
        worst = checks["max_gap_rel"].idxmax()
        sector_summary = {
            "eia861": sector["eia"],
            "sources": sector["sources"],
            "E_mwh": energy, "E_points": len(sector_out["curves"]),
            "N_model": sector_out["counts"]["N_model"], "N_frame": sector_out["counts"]["N_frame"],
            "frame_feeders": sector_out["counts"]["frame_feeders"],
            "factors": sector_out["factors"],
            "s_res": s_res, "s_com": s_com, "a": a, "b": b,
            texts_key: sector_out[texts_key],
            "check_end_uses": {
                "tolerance": rule["end_use_tolerance"],
                "max_gap_rel": float(checks.at[worst, "max_gap_rel"]),
                "max_gap_rel_stratum": checks.at[worst, "stratum"],
                "max_gap_rel_timestamp": str(checks.at[worst, "max_gap_rel_timestamp"]),
                "max_gap_kw": float(checks["max_gap_kw"].max()),
                "points_over_tolerance": int(checks["points_over_tolerance"].sum()),
                "points": int(checks["points"].sum()),
                "by_stratum": checks.assign(max_gap_rel_timestamp=checks["max_gap_rel_timestamp"].astype(str))
                .to_dict(orient="records"),
                "year_mwh_kw_minus_end_uses": float((sector_out["curves"]["kw"] - sector_out["curves"]["res"]
                                                     - sector_out["curves"]["com"]).sum())
                * HOURS_PER_POINT / KW_PER_MW,
            },
            "check_starting": {
                "tolerance": rule["starting_tolerance"],
                "max_gap_rel": float(gap.max()),
                "max_gap_rel_hour": str(gap.idxmax()),
                "hours_over_tolerance": int((gap > rule["starting_tolerance"]).sum()),
                "hours": len(gap),
            },
            "sector_formula_year_mwh": formula_year,
            "sales_mwh": sum(sales.values()),
            "sector_formula_year_gap": (formula_year - sum(sales.values())) / sum(sales.values()),
        }

    errors, months = [], []
    for name in [m for m in MODELS if m in models]:
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
    if sector_out is not None:
        hourly["res_mw"] = res.to_numpy()
        hourly["com_mw"] = com.to_numpy()
        for name in ("sector_formula", "sector_fit", "scale_plus_constant"):
            hourly[f"{name}_mw"] = models[name].to_numpy()
    out = {
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
    if sector_out is not None:
        out["sector_factors"] = sector_out["table"]
        out["summary"]["sector"] = sector_summary
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--curves", type=Path, required=True, help="feeder_curves.parquet of dxsew.curves.year")
    parser.add_argument("--typical", type=Path, required=True, help="typical_feeders csv")
    parser.add_argument("--target", type=Path, required=True, help="fit target parquet of dxsew.fit.target")
    parser.add_argument("--target-summary", type=Path, default=None,
                        help="summary json of the fit target, default <target stem>_summary.json beside it")
    parser.add_argument("--stratum-curves", type=Path, required=True, help="stratum_curves.parquet of dxsew.curves.year")
    parser.add_argument("--features", type=Path, required=True,
                        help="features parquet of the frame feeders (res_customers, com_customers)")
    parser.add_argument("--eia861", type=Path, required=True, help="EIA-861 zip of the target's year")
    parser.add_argument("--out-root", type=Path, default=None, help="default <data_root>/<output_folder of the setup>")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    setup = config.setup()

    out_root = args.out_root or Path(load_local()["data_root"]) / setup["output_folder"]
    out_dir = out_root / f"fit_{args.stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)

    target_summary_path = args.target_summary or args.target.with_name(f"{args.target.stem}_summary.json")
    target_summary = json.loads(target_summary_path.read_text(encoding="utf-8"))
    year, state, ba_code = int(target_summary["year"]), target_summary["state"], target_summary["balancing_area"]
    eia = sector_inputs(eia861.read_sales(args.eia861, year), state, ba_code)
    sources = {"eia861": load_sources()[f"eia861_{year}"]["url"], **setup["fit"]["sources"]}
    sector = {"stratum": pd.read_parquet(args.stratum_curves), "features": pd.read_parquet(args.features),
              "eia": eia, "year": year, "sources": sources}
    result = run(pd.read_parquet(args.curves), pd.read_csv(args.typical), pd.read_parquet(args.target), sector)
    reference = target_summary["inputs"]["eia861_mwh_by_class"]["value"]
    result["summary"]["sector"]["eia861_mwh_equal_target_summary"] = {
        cls: eia["mwh"][cls] == float(reference[cls]) for cls in SECTOR_CLASS.values()}
    result["hourly"].to_parquet(out_dir / "hourly.parquet", index=False)
    _write_csv(result["weights"], out_dir / "weights.csv")
    _write_csv(result["errors"], out_dir / "errors.csv")
    _write_csv(result["months"], out_dir / "months.csv")
    _write_csv(result["left_out"], out_dir / "hours_left_out.csv")
    _write_csv(result["sector_factors"], out_dir / "sector_factors.csv")
    summary = {**result["summary"], "inputs": {"curves": str(args.curves), "typical": str(args.typical),
                                               "target": str(args.target), "target_summary": str(target_summary_path),
                                               "stratum_curves": str(args.stratum_curves),
                                               "features": str(args.features), "eia861": str(args.eia861)}}
    indent = load_runtime()["json_indent"]
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=indent, default=str), encoding="utf-8")
    print(json.dumps(result["summary"], indent=indent, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
