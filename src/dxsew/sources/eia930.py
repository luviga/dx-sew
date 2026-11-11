"""EIA-930 hourly demand from the six-month Balance files.

Source: EIA, Hourly Electric Grid Monitor, Six-Month Files
(https://www.eia.gov/electricity/930-content/about.html). Demand is metered net
generation inside the balancing area minus metered net interchange, hourly
integrated, published with the hour-ending convention. The URLs are in
configs/sources.yaml under eia930_balance_<year>_jan_jun and _jul_dec.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from dxsew.sources import download as _download
from dxsew.sources import load_sources

HALVES = ("jan_jun", "jul_dec")
COL_BA = "Balancing Authority"
COL_UTC = "UTC Time at End of Hour"
COL_LOCAL = "Local Time at End of Hour"
COL_REPORTED = "Demand (MW)"
COL_ADJUSTED = "Demand (MW) (Adjusted)"
TIME_FORMAT = "%m/%d/%Y %I:%M:%S %p"


def source_keys(year: int) -> list[str]:
    return [f"eia930_balance_{year}_{half}" for half in HALVES]


def download(folder: Path | str, year: int, sources: dict | None = None, log=print) -> list[Path]:
    """Download the two six-month Balance files of year into folder."""
    sources = sources or load_sources()
    return [_download(sources[k]["url"], folder, log=log) for k in source_keys(year)]


def tidy_balance(raw: pd.DataFrame, balancing_area: str) -> pd.DataFrame:
    """Rows of one balancing area as a tidy table.

    Columns: utc_hour_ending (UTC, tz-aware), local_hour_ending (naive, as
    published, kept for checking only), demand_reported_mw and, when the file
    has it, demand_adjusted_mw. Sorted by utc_hour_ending.
    """
    rows = raw[raw[COL_BA] == balancing_area]
    out = pd.DataFrame(
        {
            "utc_hour_ending": pd.to_datetime(rows[COL_UTC], format=TIME_FORMAT).dt.tz_localize("UTC"),
            "local_hour_ending": pd.to_datetime(rows[COL_LOCAL], format=TIME_FORMAT),
            "demand_reported_mw": pd.to_numeric(rows[COL_REPORTED], errors="coerce"),
        }
    )
    if COL_ADJUSTED in rows.columns:
        out["demand_adjusted_mw"] = pd.to_numeric(rows[COL_ADJUSTED], errors="coerce")
    return out.sort_values("utc_hour_ending").reset_index(drop=True)


def read_balance(paths: list[Path | str], balancing_area: str) -> pd.DataFrame:
    """Read the Balance CSV files and return tidy_balance of their concatenation."""
    raw = pd.concat(
        [pd.read_csv(p, thousands=",", low_memory=False) for p in paths], ignore_index=True
    )
    return tidy_balance(raw, balancing_area)


def choose_demand(table: pd.DataFrame) -> tuple[pd.Series, dict]:
    """Pick EIA's adjusted demand if present, otherwise the reported demand.

    Returns the chosen series and a record of the choice. When both columns
    exist, the record holds the hours where they differ (a value in one and not
    the other counts as a difference) and the total difference in MWh, adjusted
    minus reported, with a missing value counted as zero.
    """
    reported = table["demand_reported_mw"]
    if "demand_adjusted_mw" not in table.columns:
        return reported, {"column": COL_REPORTED, "adjusted_column_present": False}
    adjusted = table["demand_adjusted_mw"]
    differ = ~((adjusted == reported) | (adjusted.isna() & reported.isna()))
    record = {
        "column": COL_ADJUSTED,
        "adjusted_column_present": True,
        "hours_differ": int(differ.sum()),
        "hours_differ_reported_missing": int((differ & reported.isna()).sum()),
        "hours_differ_adjusted_missing": int((differ & adjusted.isna()).sum()),
        "difference_mwh": float((adjusted.fillna(0) - reported.fillna(0)).sum()),
        "differing_utc_hours": [t.isoformat() for t in table.loc[differ, "utc_hour_ending"]],
    }
    return adjusted, record
