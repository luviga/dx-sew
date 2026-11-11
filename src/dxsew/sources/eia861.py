"""EIA-861 sales to ultimate customers by class.

Source: EIA-861 annual file (https://www.eia.gov/electricity/data/eia861/),
workbook Sales_Ult_Cust_<year>.xlsx, sheet States. Rows are reported by utility,
state, balancing authority code and service type: Bundled (energy and delivery),
Energy (energy only) or Delivery (delivery only). Summing Energy and Delivery
rows for the same customers counts their MWh twice (PUDL table
core_eia861__yearly_sales, https://data.catalyst.coop/preview/pudl/core_eia861__yearly_sales),
so Delivery rows are dropped. The URL is in configs/sources.yaml under eia861_<year>.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd

from dxsew.sources import download as _download
from dxsew.sources import load_sources

CLASSES = ("residential", "commercial", "industrial", "transportation")
SERVICE_TYPES_KEPT = ("Bundled", "Energy")
ID_COLUMNS = {
    "Data Year": "data_year",
    "Utility Number": "utility_number",
    "Utility Name": "utility_name",
    "Part": "part",
    "Service Type": "service_type",
    "State": "state",
    "Ownership": "ownership",
    "BA Code": "ba_code",
}


def download(folder: Path | str, year: int, sources: dict | None = None, log=print) -> Path:
    sources = sources or load_sources()
    return _download(sources[f"eia861_{year}"]["url"], folder, log=log)


def read_sales(zip_path: Path | str, year: int) -> pd.DataFrame:
    """Sheet States of Sales_Ult_Cust_<year>.xlsx as a tidy table.

    Columns: the identifiers of ID_COLUMNS and <class>_mwh for each class in
    CLASSES. Cells published as "." become missing. Footnote rows are dropped.
    """
    with zipfile.ZipFile(zip_path) as z:
        data = z.read(f"Sales_Ult_Cust_{year}.xlsx")
    raw = pd.read_excel(io.BytesIO(data), sheet_name="States", header=None)
    group = raw.iloc[0].ffill()
    measure = raw.iloc[1]
    names = raw.iloc[2].astype(str).str.split("\n").str[0].str.strip()
    columns = {}
    for i in range(raw.shape[1]):
        if names[i] in ID_COLUMNS:
            columns[i] = ID_COLUMNS[names[i]]
        elif str(measure[i]).strip() == "Sales" and str(group[i]).strip().lower() in CLASSES:
            columns[i] = f"{str(group[i]).strip().lower()}_mwh"
    body = raw.iloc[3:, list(columns)].rename(columns=columns)
    body = body[pd.to_numeric(body["data_year"], errors="coerce") == year].copy()
    for c in CLASSES:
        body[f"{c}_mwh"] = pd.to_numeric(body[f"{c}_mwh"], errors="coerce")
    return body.reset_index(drop=True)


def class_totals(
    sales: pd.DataFrame, state: str, ba_code: str, service_types: tuple[str, ...] | None
) -> pd.Series:
    """MWh by class for rows of state and ba_code; service_types None keeps all."""
    rows = sales[(sales["state"] == state) & (sales["ba_code"] == ba_code)]
    if service_types is not None:
        rows = rows[rows["service_type"].isin(service_types)]
    return pd.Series({c: float(rows[f"{c}_mwh"].sum()) for c in CLASSES}, name="mwh")


def sales_by_class(sales: pd.DataFrame, state: str, ba_code: str) -> pd.DataFrame:
    """MWh by class with Delivery rows dropped (used) and included (report only)."""
    return pd.DataFrame(
        {
            "mwh": class_totals(sales, state, ba_code, SERVICE_TYPES_KEPT),
            "mwh_with_delivery": class_totals(sales, state, ba_code, None),
        }
    )


def service_type_counts(sales: pd.DataFrame, state: str, ba_code: str) -> dict:
    """Number of rows per service type for state and ba_code."""
    rows = sales[(sales["state"] == state) & (sales["ba_code"] == ba_code)]
    return {str(k): int(v) for k, v in rows["service_type"].value_counts().items()}
