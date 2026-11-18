"""Year curves and common days, on made-up building files of a few hundred points."""

import numpy as np
import pandas as pd
import pytest

from dxsew.texas import curves

POINTS = 4 * 96  # four days at 15 minutes
STAMPS = pd.date_range("2018-01-01 00:15", periods=POINTS, freq="15min")
RES, COM = "parquet_residential_short_x_TX_2024_resstock_amy2018_release_2", "parquet_commercial_x_comm_TX"


def building(folder, name, cooling, lighting, stamps=STAMPS):
    folder.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame({
        "timestamp": stamps,
        "bldg_id": 1,
        "out.electricity.cooling.energy_consumption": cooling,
        "out.electricity.lighting.energy_consumption": lighting,
        "out.electricity.total.energy_consumption": np.asarray(cooling) + np.asarray(lighting),
        "out.electricity.net.energy_consumption": np.asarray(cooling) + np.asarray(lighting),
        "out.electricity.total.energy_consumption_intensity": 0.0,
    })
    table.to_parquet(folder / name, index=False)


@pytest.fixture
def root(tmp_path):
    rng = np.random.default_rng(1)
    building(tmp_path / RES, "1-0.parquet", rng.random(POINTS), rng.random(POINTS))
    building(tmp_path / COM, "2-0.parquet", rng.random(POINTS), rng.random(POINTS))
    return tmp_path


def recipe(rows):
    return pd.DataFrame(rows, columns=["Feeder", "Parquet_Folder", "Parquet_File", "REAL_LOAD_COUNT"])


def test_total_is_energy_times_four_times_count(root):
    made = recipe([("f", RES, "1-0.parquet", 2), ("f", COM, "2-0.parquet", 3)])
    result = curves.feeder_curve(made, root)
    res = pd.read_parquet(root / RES / "1-0.parquet")["out.electricity.total.energy_consumption"].to_numpy()
    com = pd.read_parquet(root / COM / "2-0.parquet")["out.electricity.total.energy_consumption"].to_numpy()
    assert np.allclose(result["total"], 4 * (2 * res + 3 * com), rtol=0, atol=1e-12)
    assert result["rows_read"] == 2 and len(result["total"]) == POINTS


def test_end_uses_add_to_the_total_by_sector(root):
    made = recipe([("f", RES, "1-0.parquet", 2), ("f", COM, "2-0.parquet", 3), ("f", RES, "1-0.parquet", 1)])
    result = curves.feeder_curve(made, root)
    assert set(result["end_uses"]) == {("res", "cooling"), ("res", "lighting"), ("com", "cooling"), ("com", "lighting")}
    assert np.allclose(sum(result["end_uses"].values()), result["total"])
    res = pd.read_parquet(root / RES / "1-0.parquet")["out.electricity.cooling.energy_consumption"].to_numpy()
    assert np.allclose(result["end_uses"][("res", "cooling")], 4 * 3 * res)


def test_an_unreadable_file_or_other_timestamps_stop_the_feeder(root):
    with pytest.raises(curves.FeederError, match="cannot read"):
        curves.feeder_curve(recipe([("f", RES, "1-0.parquet", 1), ("f", RES, "missing.parquet", 1)]), root)
    (root / COM / "3-0.parquet").write_bytes(b"partial")
    with pytest.raises(curves.FeederError, match="cannot read"):
        curves.feeder_curve(recipe([("f", COM, "3-0.parquet", 1)]), root)
    building(root / COM, "4-0.parquet", np.ones(POINTS), np.ones(POINTS), STAMPS - pd.Timedelta(minutes=15))
    with pytest.raises(curves.FeederError, match="timestamps differ"):
        curves.feeder_curve(recipe([("f", RES, "1-0.parquet", 1), ("f", COM, "4-0.parquet", 1)]), root)


def test_sector_of_the_folder():
    assert curves.sector_of(RES) == "res" and curves.sector_of(COM) == "com"
    with pytest.raises(curves.FeederError):
        curves.sector_of("parquet_other")


def test_coupling_calendar_check_passes_and_fails():
    total = np.ones(POINTS)
    total[96 * 2 + 40] = 9.0  # third day of the calendar, 2025-01-03
    peaks = curves.coupling_peaks(total)
    assert peaks == {"winter": (1, 3, 9.0)}
    ref = pd.DataFrame({"Feeder": ["f", "f"], "Season": ["winter", "summer"], "Month": [1, 7], "Day": [3, 1],
                        "Peak_kW": [9.0 * (1 + 5e-7), 5.0]})
    rows = {r["season"]: r for r in curves.check_feeder("f", peaks, ref, 2, 2)}
    assert rows["winter"]["passed"] and rows["winter"]["relative_difference"] < 1e-6
    assert not rows["summer"]["passed"] and rows["summer"]["note"] == "no rebuilt peak day"
    worse = ref.assign(Peak_kW=[9.0 * (1 + 2e-6), 5.0])
    assert not curves.check_feeder("f", peaks, worse, 2, 2)[0]["passed"]
    other_day = ref.assign(Day=[4, 1])
    assert not curves.check_feeder("f", peaks, other_day, 2, 2)[0]["passed"]
    assert not curves.check_feeder("f", peaks, ref, 1, 2)[0]["passed"]


def test_a_month_whose_peak_day_lacks_96_points_is_skipped():
    total = np.ones(96 * 31 + 50)  # January whole, then 50 points of February
    total[96 * 31 + 10] = 5.0  # highest point on an incomplete 1 February
    total[100] = 3.0
    peaks = curves.coupling_peaks(total)
    assert peaks["winter"] == (1, 2, 3.0)


def test_interval_start_date_rule():
    stamps = pd.DatetimeIndex(["2018-01-01 00:15", "2018-01-01 00:30", "2018-12-31 23:45", "2019-01-01 00:00"])
    dates = curves.point_dates(stamps)
    assert [d.date().isoformat() for d in dates] == ["2018-01-01", "2018-01-01", "2018-12-31", "2018-12-31"]
    beginning = pd.DatetimeIndex(["2018-01-01 00:00", "2018-01-01 00:15"])
    assert [d.date().isoformat() for d in curves.point_dates(beginning)] == ["2018-01-01", "2018-01-01"]


def test_stratum_scaling_by_customers():
    passed = {"f1": {"total": np.array([1.0, 2.0]), "end_uses": {("res", "c"): np.array([1.0, 2.0])}},
              "f2": {"total": np.array([10.0, 0.0]), "end_uses": {("com", "l"): np.array([10.0, 0.0])}}}
    rows = pd.DataFrame({"run_feeder": ["f1", "f2", "f1", "gone"], "customers": [100, 30, 50, 20],
                         "run_feeder_customers": [10, 15, 10, 4]})
    summed = curves.stratum_curve(rows, passed, 2, [("res", "c"), ("com", "l")])
    assert np.allclose(summed["total"], 10 * np.array([1, 2]) + 2 * np.array([10, 0]) + 5 * np.array([1, 2]))
    assert np.allclose(summed["end_uses"][("res", "c")] + summed["end_uses"][("com", "l")], summed["total"])
    assert summed["feeders"] == {"f1": 150.0, "f2": 30.0}
    assert summed["customers_left_out"] == 20 and summed["feeders_left_out"] == ["gone"]
    assert summed["groups"] == 4 and summed["groups_used"] == 3


def year():
    stamps = pd.date_range("2018-01-01 00:15", "2019-01-01 00:00", freq="15min")
    return stamps, curves.point_dates(stamps)


def test_day_a_is_the_seasonal_peak_of_the_stratum_curve():
    stamps, dates = year()
    curve = np.ones(len(stamps))
    curve[np.flatnonzero(dates == pd.Timestamp("2018-07-19"))[60]] = 8.0
    curve[np.flatnonzero(dates == pd.Timestamp("2018-12-31"))[-1]] = 7.0  # 2019-01-01 00:00 belongs to 31 December
    curve[np.flatnonzero(dates == pd.Timestamp("2018-05-01"))[0]] = 99.0  # outside both seasons
    assert curves.peak_date(curve, dates, "summer") == pd.Timestamp("2018-07-19")
    assert curves.peak_date(curve, dates, "winter") == pd.Timestamp("2018-12-31")


def test_day_b_uses_the_central_standard_time_hour_ending_date():
    hours = pd.date_range("2018-01-01 01:00", "2019-01-01 00:00", freq="h", tz="Etc/GMT+6")
    demand = np.full(len(hours), 100.0)
    demand[hours.get_loc(pd.Timestamp("2018-08-01 00:00", tz="Etc/GMT+6"))] = 500.0  # hour ending 00:00 is 31 July
    demand[hours.get_loc(pd.Timestamp("2018-01-17 08:00", tz="Etc/GMT+6"))] = 400.0
    ercot = curves.ercot_days(pd.DataFrame({"cst_hour_ending": hours, "demand_mw": demand}))
    assert curves.day_b(ercot, "summer") == pd.Timestamp("2018-07-31")
    assert curves.day_b(ercot, "winter") == pd.Timestamp("2018-01-17")


def test_day_c_counts_feeders_and_breaks_a_tie_on_the_stratum_curve():
    stamps, dates = year()
    curve = np.ones(len(stamps))
    curve[np.flatnonzero(dates == pd.Timestamp("2018-07-20"))[50]] = 3.0
    d1, d2 = pd.Timestamp("2018-07-19"), pd.Timestamp("2018-07-20")
    date, ties = curves.day_c({"a": d1, "b": d1, "c": d2}, curve, dates)
    assert date == d1 and ties == []
    date, ties = curves.day_c({"a": d1, "b": d2}, curve, dates)
    assert date == d2 and ties == [d1, d2]


def test_day_measures():
    stamps, dates = year()
    curve = np.ones(len(stamps))
    day = pd.Timestamp("2018-07-19")
    curve[np.flatnonzero(dates == day)[60]] = 4.0
    curve[np.flatnonzero(dates == pd.Timestamp("2018-08-02"))[60]] = 8.0
    uses = pd.DataFrame({"res:cooling": curve * 0.75, "com:lighting": curve * 0.25}, index=stamps)
    hours = pd.date_range("2018-01-01 01:00", "2019-01-01 00:00", freq="h")
    ercot = curves.ercot_days(pd.DataFrame({"cst_hour_ending": hours, "demand_mw": 50.0}))
    row = curves.day_measures(day, "summer", curve, uses, dates, {"a": day, "b": pd.Timestamp("2018-08-02")},
                              {"a": 30.0, "b": 10.0}, ercot)
    assert row["date"] == "2018-07-19" and row["weekday"] == "Thursday"
    assert row["stratum_share_of_seasonal_peak"] == pytest.approx(0.5)
    assert row["feeder_share_by_count"] == 0.5 and row["feeder_share_by_customers"] == 0.75
    assert row["ercot_share_of_seasonal_peak"] == 1.0
    assert row["end_use_1"] == "res:cooling" and row["end_use_1_kw"] == 3.0 and row["end_use_1_share"] == 0.75
    assert row["end_use_3"] == "" and np.isnan(row["end_use_3_kw"])
