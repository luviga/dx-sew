"""ERCOT hourly fit target: residential and commercial load at the customer meter.

The circuit weights of dx-sew are fitted to an observed hourly curve. The
bottom-up circuits produce residential and commercial load at the customer
meter, so the observed balancing-area demand is brought to that same quantity:
industrial and transportation load and transmission and distribution losses are
taken out. Settings are in configs/fit_target.yaml and URLs in
configs/sources.yaml.

Formulas
--------
D_t   Hourly demand of the balancing area in MW (equal to MWh in that hour), for
      the hours ending <year>-01-01 01:00 through <year+1>-01-01 00:00 in Central
      Standard Time (UTC minus 6 hours, no daylight saving), which for 2018 are the
      8,760 hours ending 2018-01-01 07:00 UTC through 2019-01-01 06:00 UTC.
      Source: EIA-930 six-month Balance files, column "Demand (MW) (Adjusted)" when
      present, otherwise "Demand (MW)";
      https://www.eia.gov/electricity/930-content/about.html.
      Hours with missing or non-positive demand are filled by the gap rule below.
lambda = L / (S - U)
      Loss share. L estimated losses, S total disposition, U direct use, state and
      year of the settings, from EIA's State Electricity Profile, Table 10
      (https://www.eia.gov/electricity/state/texas/, Full data tables); formula
      from https://www.eia.gov/tools/faqs/faq.php?id=105.
X = I + Tr
      Removed classes: industrial plus transportation sales to ultimate customers
      in MWh, state and balancing authority of the settings, service types Bundled
      and Energy, from EIA-861 (https://www.eia.gov/electricity/data/eia861/).
T_t = D_t * (1 - lambda) - X / H
      Target at the customer meter in MW, where H is the number of hours in the
      year (8,760 for 2018).

Run: uv run python -m dxsew.fit_target
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from dxsew.sources import eia861, eia930, eia_state_profile, load_sources

REPO_ROOT = Path(__file__).resolve().parents[2]

# United States Central Standard Time is UTC minus 6 hours: 15 U.S.C. 261(a),
# "the standard time of the sixth zone", https://www.law.cornell.edu/uscode/text/15/261
CST = timezone(timedelta(hours=-6))
CENTRAL_PREVAILING = "America/Chicago"

# ASSUMPTION: hours with missing or non-positive ERCO demand are filled by linear
# interpolation for gaps of up to 3 hours and by the same hour of the previous day
# for longer gaps.
MAX_INTERPOLATED_GAP_HOURS = 3
HOURS_PER_DAY = 24

# ASSUMPTION: the ratio of sum(T_t) to EIA-861 residential plus commercial sales is
# flagged when it lies outside 0.85 to 1.15. The band only flags the ratio for review;
# nothing is rescaled.
CONSISTENCY_BAND = (0.85, 1.15)


def hour_index(year: int) -> pd.DataFrame:
    """Hour-ending stamps of calendar year in Central Standard Time.

    Columns utc_hour_ending (UTC) and cst_hour_ending (UTC minus 6 hours).
    """
    first = pd.Timestamp(year=year, month=1, day=1, hour=1, tz=CST)
    last = pd.Timestamp(year=year + 1, month=1, day=1, hour=0, tz=CST)
    cst = pd.date_range(first, last, freq="h")
    return pd.DataFrame({"utc_hour_ending": cst.tz_convert("UTC"), "cst_hour_ending": cst})


def align_to_year(utc_hour_ending: pd.Series, values: pd.Series, year: int) -> pd.Series:
    """Values on the hour index of year, NaN where an hour has no row.

    Raises ValueError if any UTC hour appears more than once.
    """
    dup = utc_hour_ending.duplicated(keep=False)
    if dup.any():
        hours = sorted({t.isoformat() for t in utc_hour_ending[dup]})
        raise ValueError(f"duplicate UTC hours after conversion: {hours}")
    s = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(utc_hour_ending))
    return s.reindex(pd.DatetimeIndex(hour_index(year)["utc_hour_ending"]))


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Start and end positions (inclusive) of the runs of True in mask."""
    runs, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        if not m and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def fill_gaps(demand: pd.Series) -> tuple[pd.Series, pd.Series, list[dict]]:
    """Apply the gap rule (ASSUMPTION at MAX_INTERPOLATED_GAP_HOURS).

    A run of missing or non-positive hours of length up to
    MAX_INTERPOLATED_GAP_HOURS, with a valid hour on each side, is filled by
    linear interpolation between those two hours. Any other run is filled hour by
    hour, in time order, with the value of the same hour of the previous day
    (HOURS_PER_DAY positions earlier), which may itself be a filled value.
    Returns the filled series, a boolean mask of filled hours and the list of runs.
    """
    v = demand.to_numpy(dtype=float).copy()
    bad = ~(v > 0)
    gaps = []
    for start, end in _runs(bad):
        n = end - start + 1
        if n <= MAX_INTERPOLATED_GAP_HOURS and start > 0 and end < len(v) - 1:
            a, b = v[start - 1], v[end + 1]
            for k in range(n):
                v[start + k] = a + (b - a) * (k + 1) / (n + 1)
            method = "linear interpolation"
        else:
            for i in range(start, end + 1):
                if i - HOURS_PER_DAY < 0:
                    raise ValueError(f"no previous day to fill hour position {i}")
                v[i] = v[i - HOURS_PER_DAY]
            method = "same hour of previous day"
        gaps.append(
            {
                "first_utc_hour_ending": demand.index[start].isoformat(),
                "last_utc_hour_ending": demand.index[end].isoformat(),
                "hours": n,
                "method": method,
            }
        )
    return pd.Series(v, index=demand.index), pd.Series(bad, index=demand.index), gaps


def loss_share(estimated_losses: float, total_disposition: float, direct_use: float) -> float:
    """lambda = L / (S - U)."""
    return estimated_losses / (total_disposition - direct_use)


def target(demand: pd.Series, lam: float, removed_mwh: float) -> pd.Series:
    """T_t = D_t * (1 - lambda) - X / H, with H the number of hours of demand."""
    return demand * (1.0 - lam) - removed_mwh / len(demand)


def describe(series: pd.Series, cst: pd.Series) -> dict:
    """Annual energy, peak and minimum with their hours (UTC and CST)."""
    s = series.reset_index(drop=True)
    i_max, i_min = int(s.idxmax()), int(s.idxmin())
    utc = series.index
    return {
        "energy_mwh": float(s.sum()),
        "peak_mw": float(s[i_max]),
        "peak_utc_hour_ending": utc[i_max].isoformat(),
        "peak_cst_hour_ending": cst.iloc[i_max].isoformat(),
        "min_mw": float(s[i_min]),
        "min_utc_hour_ending": utc[i_min].isoformat(),
        "min_cst_hour_ending": cst.iloc[i_min].isoformat(),
    }


def local_time_check(table: pd.DataFrame) -> dict:
    """Compare EIA's local-time column with UTC converted to Central prevailing time."""
    expected = table["utc_hour_ending"].dt.tz_convert(CENTRAL_PREVAILING).dt.tz_localize(None)
    offset = (table["utc_hour_ending"].dt.tz_localize(None) - table["local_hour_ending"])
    return {
        "rows": int(len(table)),
        "rows_matching_central_prevailing_time": int((expected == table["local_hour_ending"]).sum()),
        "rows_by_utc_minus_local_hours": {
            str(int(k / pd.Timedelta(hours=1))): int(v) for k, v in offset.value_counts().items()
        },
    }


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(local_config: Path, log=print) -> dict:
    """Build the fit target and write it under <data_root>/derived. Returns the summary."""
    local = _load_yaml(local_config)
    settings = _load_yaml(REPO_ROOT / "configs" / "fit_target.yaml")
    sources = load_sources()
    data_root = Path(local["data_root"])
    eia_dir, out_dir = data_root / "eia", data_root / "derived"
    ba, year, state = settings["balancing_area"], int(settings["year"]), settings["state"]

    # Inputs
    p930 = eia930.download(eia_dir, year, sources, log=log)
    p861 = eia861.download(eia_dir, year, sources, log=log)
    psep = eia_state_profile.download(eia_dir, state, sources, log=log)
    url930 = [sources[k]["url"] for k in eia930.source_keys(year)]
    url861 = sources[f"eia861_{year}"]["url"]
    urlsep = sources[f"eia_state_profile_{state.lower()}"]["url"]

    table = eia930.read_balance(p930, ba)
    raw_demand, demand_choice = eia930.choose_demand(table)
    lt_check = local_time_check(table)
    idx = hour_index(year)
    aligned = align_to_year(table["utc_hour_ending"], raw_demand, year)
    rows_outside_year = int(len(table) - table["utc_hour_ending"].isin(idx["utc_hour_ending"]).sum())
    demand, filled, gaps = fill_gaps(aligned)

    t10 = eia_state_profile.read_table10(psep, year)
    lam = loss_share(t10["estimated_losses_mwh"], t10["total_disposition_mwh"], t10["direct_use_mwh"])

    sales = eia861.read_sales(p861, year)
    by_class = eia861.sales_by_class(sales, state, ba)
    removed_mwh = float(by_class.loc[settings["classes_removed"], "mwh"].sum())
    kept_mwh = float(by_class.loc[settings["classes_kept"], "mwh"].sum())
    hours = len(demand)
    removed_mw = removed_mwh / hours

    tgt = target(demand, lam, removed_mwh)
    cst = idx["cst_hour_ending"]

    # Checks
    d_stats, t_stats = describe(demand, cst), describe(tgt, cst)
    ratio = t_stats["energy_mwh"] / kept_mwh
    negative = int((tgt < 0).sum())
    pub = settings["ercot_published"]
    pub_peak_utc = pd.Timestamp(pub["peak_local_hour_ending"]).tz_convert("UTC")
    ercot_cmp = {
        "source": pub["source"],
        "source_page": pub["source_page"],
        "published_peak_mw": pub["peak_mw"],
        "published_peak_utc_hour_ending": pub_peak_utc.isoformat(),
        "demand_at_published_peak_hour_mw": float(demand.loc[pub_peak_utc]),
        "peak_difference_mw": d_stats["peak_mw"] - pub["peak_mw"],
        "published_annual_energy_mwh": pub["annual_energy_mwh"],
        "published_annual_energy_text": pub["annual_energy_text"],
        "energy_difference_mwh": d_stats["energy_mwh"] - pub["annual_energy_mwh"],
    }
    fmt = {"removed_mw": removed_mw, "loss_share": lam, "year": year, "balancing_area": ba, "hours": hours}
    assumptions = {k: v.format(**fmt) for k, v in settings["assumptions"].items()}

    summary = {
        "balancing_area": ba,
        "year": year,
        "state": state,
        "hours": hours,
        "inputs": {
            "eia930_demand": {"source": url930, "demand_column": demand_choice},
            "estimated_losses_mwh": {"source": urlsep, "value": t10["estimated_losses_mwh"]},
            "total_disposition_mwh": {"source": urlsep, "value": t10["total_disposition_mwh"]},
            "direct_use_mwh": {"source": urlsep, "value": t10["direct_use_mwh"]},
            "eia861_mwh_by_class": {
                "source": url861,
                "service_types_kept": list(eia861.SERVICE_TYPES_KEPT),
                "value": by_class["mwh"].to_dict(),
                "with_delivery_rows_report_only": by_class["mwh_with_delivery"].to_dict(),
                "rows_by_service_type": eia861.service_type_counts(sales, state, ba),
            },
        },
        "derived": {
            "loss_share": {"formula": "L / (S - U)", "value": lam},
            "removed_mwh": {"formula": "I + Tr", "value": removed_mwh},
            "removed_mw": {"formula": "X / H", "value": removed_mw},
            "kept_mwh": {"formula": "residential + commercial EIA-861 MWh", "value": kept_mwh},
        },
        "filled_hours": {
            "count": int(filled.sum()),
            "gaps": gaps,
            "utc_hours": [t.isoformat() for t in demand.index[filled.to_numpy()]],
        },
        "eia930_rows_outside_year": rows_outside_year,
        "local_time_check": lt_check,
        "checks": {
            "demand": d_stats,
            "target": t_stats,
            "consistency_ratio": ratio,
            "consistency_band": list(CONSISTENCY_BAND),
            "consistency_within_band": CONSISTENCY_BAND[0] <= ratio <= CONSISTENCY_BAND[1],
            "target_negative_hours": negative,
            "ercot_published": ercot_cmp,
        },
        "assumptions": assumptions,
    }

    _print_checks(summary, by_class, log)
    if negative:
        raise ValueError(f"target is negative in {negative} hours; nothing written")

    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "utc_hour_ending": idx["utc_hour_ending"].to_numpy(),
            "cst_hour_ending": cst,
            "demand_mw": demand.to_numpy(),
            "filled": filled.to_numpy(),
            "loss_share": lam,
            "removed_mw": removed_mw,
            "target_mw": tgt.to_numpy(),
        }
    )
    stem = f"{ba}_{year}_fit_target"
    frame.to_parquet(out_dir / f"{stem}.parquet", index=False)
    frame.to_csv(out_dir / f"{stem}.csv", index=False)
    with open(out_dir / f"{stem}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    log(f"wrote {out_dir / (stem + '.parquet')}, .csv and _summary.json")
    return summary


def _print_checks(s: dict, by_class: pd.DataFrame, log) -> None:
    c = s["checks"]
    log("1. annual energy, peak and minimum")
    for name in ("demand", "target"):
        x = c[name]
        log(
            f"   {name}: {x['energy_mwh']:,.0f} MWh; peak {x['peak_mw']:,.1f} MW at "
            f"{x['peak_cst_hour_ending']} CST; min {x['min_mw']:,.1f} MW at {x['min_cst_hour_ending']} CST"
        )
    i, d = s["inputs"], s["derived"]
    log(
        f"2. loss share {d['loss_share']['value']:.4%} = {i['estimated_losses_mwh']['value']:,.0f} / "
        f"({i['total_disposition_mwh']['value']:,.0f} - {i['direct_use_mwh']['value']:,.0f})"
    )
    log("3. EIA-861 MWh by class (Delivery dropped | Delivery included)")
    for k, row in by_class.iterrows():
        log(f"   {k}: {row['mwh']:,.0f} | {row['mwh_with_delivery']:,.0f}")
    log(f"4. consistency sum(T)/(R+C) = {c['consistency_ratio']:.4f} (within band: {c['consistency_within_band']})")
    log(f"5. minimum target {c['target']['min_mw']:,.1f} MW; negative hours {c['target_negative_hours']}")
    e = c["ercot_published"]
    log(
        f"6. ERCOT published peak {e['published_peak_mw']:,} MW, D_t peak differs by {e['peak_difference_mw']:,.1f} MW; "
        f"published energy {e['published_annual_energy_mwh']:,} MWh, D_t differs by {e['energy_difference_mwh']:,.0f} MWh"
    )
    log(f"   demand column: {i['eia930_demand']['demand_column']['column']}; filled hours: {s['filled_hours']['count']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the hourly fit target.")
    parser.add_argument("--config", default=str(REPO_ROOT / "configs" / "local.yaml"))
    args = parser.parse_args(argv)
    run(Path(args.config))


if __name__ == "__main__":
    main()
