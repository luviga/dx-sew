"""EIA State Electricity Profile, Table 10, Supply and disposition of electricity.

Source: the full data tables XLSX of the state profile, for Texas linked from
https://www.eia.gov/electricity/state/texas/ (Full data tables). EIA computes the
loss share as estimated losses divided by (total disposition minus direct use),
https://www.eia.gov/tools/faqs/faq.php?id=105. The URL is in configs/sources.yaml
under eia_state_profile_<state in lower case>.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from dxsew.sources import download as _download
from dxsew.sources import load_sources

ROWS = {
    "Estimated losses": "estimated_losses_mwh",
    "Total disposition": "total_disposition_mwh",
    "Direct use": "direct_use_mwh",
}


def download(folder: Path | str, state: str, sources: dict | None = None, log=print) -> Path:
    sources = sources or load_sources()
    return _download(sources[f"eia_state_profile_{state.lower()}"]["url"], folder, log=log)


def parse_table10(rows: list[tuple], year: int) -> dict:
    """Estimated losses, total disposition and direct use of year, in MWh.

    rows are the cell values of worksheet 10, one tuple per row. The header row
    starts with "Category" and holds "Year <yyyy>" labels.
    """
    header = next(r for r in rows if r and r[0] is not None and str(r[0]).strip() == "Category")
    labels = [" ".join(str(c).split()) if c is not None else "" for c in header]
    col = labels.index(f"Year {year}")
    out = {}
    for r in rows:
        name = str(r[0]).strip() if r and r[0] is not None else ""
        if name in ROWS:
            out[ROWS[name]] = float(r[col])
    missing = set(ROWS.values()) - set(out)
    if missing:
        raise ValueError(f"Table 10 lacks rows: {sorted(missing)}")
    return out


def read_table10(path: Path | str, year: int) -> dict:
    """Read worksheet 10 of the state profile workbook and return parse_table10."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = next(n for n in wb.sheetnames if n.split(".")[0].strip() == "10")
        rows = list(wb[sheet].iter_rows(values_only=True))
    finally:
        wb.close()
    return parse_table10(rows, year)
