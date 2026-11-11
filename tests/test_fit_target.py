"""Fit target tests on small hand-made tables. No test touches the network."""

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from dxsew import fit_target
from dxsew.sources import eia861, eia930, eia_state_profile


def _sales_zip(tmp_path):
    """A tiny Sales_Ult_Cust_2018.xlsx inside a zip, laid out like EIA-861."""
    top = ["Utility Characteristics"] + [None] * 8
    mid = [None] * 9
    names = ["Data Year", "Utility Number", "Utility Name", "Part", "Service Type",
             "Data Type\nO = Observed\nI = Imputed", "State", "Ownership", "BA Code"]
    for cls in ["RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL", "TRANSPORTATION", "TOTAL"]:
        top += [cls, None, None]
        mid += ["Revenues", "Sales", "Customers"]
        names += ["Thousand Dollars", "Megawatthours", "Count"]

    def row(service, part, state, ba, r, c, i, t):
        return [2018, 1, "U", part, service, "O", state, "Investor Owned", ba,
                0, r, 0, 0, c, 0, 0, i, 0, 0, t, 0, 0, r + c + i + t, 0]

    rows = [
        top, mid, names,
        row("Bundled", "A", "TX", "ERCO", 100, 50, 30, 1),
        row("Energy", "B", "TX", "ERCO", 10, 5, 3, 0),
        row("Delivery", "C", "TX", "ERCO", 10, 5, 3, 0),
        row("Bundled", "A", "TX", "SWPP", 999, 999, 999, 999),
        row("Bundled", "A", "OK", "ERCO", 999, 999, 999, 999),
        ["Footnote text"] + [None] * 23,
    ]
    rows[3][16] = "."  # industrial sales published as "." become missing
    xlsx = io.BytesIO()
    pd.DataFrame(rows).to_excel(xlsx, sheet_name="States", header=False, index=False)
    path = tmp_path / "f8612018.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Sales_Ult_Cust_2018.xlsx", xlsx.getvalue())
    return path


def test_delivery_rows_dropped_bundled_and_energy_kept(tmp_path):
    sales = eia861.read_sales(_sales_zip(tmp_path), 2018)
    assert len(sales) == 5
    table = eia861.sales_by_class(sales, "TX", "ERCO")
    assert table.loc["residential", "mwh"] == 110
    assert table.loc["commercial", "mwh"] == 55
    assert table.loc["industrial", "mwh"] == 3
    assert table.loc["transportation", "mwh"] == 1
    assert table.loc["residential", "mwh_with_delivery"] == 120
    assert table.loc["industrial", "mwh_with_delivery"] == 6


def test_loss_share_from_three_numbers():
    assert fit_target.loss_share(5.0, 120.0, 20.0) == pytest.approx(0.05)


def test_table10_parse():
    rows = [
        ("Table 10. Supply and disposition of electricity", None, None),
        ("Category", "Year\n2019", "Year\n2018"),
        ("Direct use", 7, 20),
        ("Estimated losses", 9, 5),
        ("Total disposition", 300, 120),
    ]
    out = eia_state_profile.parse_table10(rows, 2018)
    assert out == {"direct_use_mwh": 20.0, "estimated_losses_mwh": 5.0, "total_disposition_mwh": 120.0}


def test_target_short_series():
    demand = pd.Series([100.0, 200.0, 300.0, 400.0])
    out = fit_target.target(demand, 0.1, 40.0)
    assert out.tolist() == pytest.approx([80.0, 170.0, 260.0, 350.0])


def test_hour_index_2018():
    idx = fit_target.hour_index(2018)
    assert len(idx) == 8760
    assert idx["utc_hour_ending"].iloc[0] == pd.Timestamp("2018-01-01 07:00", tz="UTC")
    assert idx["utc_hour_ending"].iloc[-1] == pd.Timestamp("2019-01-01 06:00", tz="UTC")
    assert not idx["utc_hour_ending"].duplicated().any()
    naive_utc = idx["utc_hour_ending"].dt.tz_localize(None)
    naive_cst = idx["cst_hour_ending"].dt.tz_localize(None)
    assert ((naive_utc - naive_cst) == pd.Timedelta(hours=6)).all()
    assert idx["cst_hour_ending"].iloc[0].strftime("%Y-%m-%d %H:%M") == "2018-01-01 01:00"
    assert idx["cst_hour_ending"].iloc[-1].strftime("%Y-%m-%d %H:%M") == "2019-01-01 00:00"


def test_duplicate_hour_stops():
    stamps = pd.Series(pd.to_datetime(["2018-01-01 07:00", "2018-01-01 07:00"]).tz_localize("UTC"))
    with pytest.raises(ValueError, match="duplicate"):
        fit_target.align_to_year(stamps, pd.Series([1.0, 2.0]), 2018)


def _three_days():
    hours = pd.date_range("2018-01-01 07:00", periods=72, freq="h", tz="UTC")
    return pd.Series(np.arange(1, 73, dtype=float), index=hours)


def test_gap_of_two_hours_is_interpolated():
    s = _three_days()
    s.iloc[30] = np.nan
    s.iloc[31] = 0.0
    filled, mask, gaps = fit_target.fill_gaps(s)
    assert filled.iloc[30] == pytest.approx(31.0)
    assert filled.iloc[31] == pytest.approx(32.0)
    assert mask.sum() == 2
    assert gaps[0]["method"] == "linear interpolation" and gaps[0]["hours"] == 2


def test_gap_of_five_hours_takes_previous_day():
    s = _three_days()
    s.iloc[40:45] = np.nan
    s.iloc[42] = -5.0
    filled, mask, gaps = fit_target.fill_gaps(s)
    assert filled.iloc[40:45].tolist() == s.iloc[16:21].tolist()
    assert mask.sum() == 5
    assert gaps[0]["method"] == "same hour of previous day" and gaps[0]["hours"] == 5


def test_adjusted_demand_chosen_and_differences_counted():
    raw = pd.DataFrame(
        {
            "Balancing Authority": ["ERCO", "ERCO", "ERCO", "XYZ"],
            "UTC Time at End of Hour": ["01/01/2018 8:00:00 AM", "01/01/2018 7:00:00 AM",
                                        "01/01/2018 9:00:00 AM", "01/01/2018 7:00:00 AM"],
            "Local Time at End of Hour": ["01/01/2018 2:00:00 AM", "01/01/2018 1:00:00 AM",
                                          "01/01/2018 3:00:00 AM", "01/01/2018 1:00:00 AM"],
            "Demand (MW)": ["1,000", None, "1,200", "5"],
            "Demand (MW) (Adjusted)": ["1,000", "900", "1,150", "5"],
        }
    )
    raw["Demand (MW)"] = raw["Demand (MW)"].str.replace(",", "")
    raw["Demand (MW) (Adjusted)"] = raw["Demand (MW) (Adjusted)"].str.replace(",", "")
    table = eia930.tidy_balance(raw, "ERCO")
    assert table["utc_hour_ending"].is_monotonic_increasing and len(table) == 3
    demand, record = eia930.choose_demand(table)
    assert demand.tolist() == [900.0, 1000.0, 1150.0]
    assert record["column"] == "Demand (MW) (Adjusted)"
    assert record["hours_differ"] == 2
    assert record["difference_mwh"] == pytest.approx(850.0)
