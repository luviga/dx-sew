"""Tests of dxsew.fit on made-up curves."""

import numpy as np
import pandas as pd
import pytest

from dxsew import fit


def ts(text):
    return pd.Timestamp(text)


def test_cst_hour_ending_of_est_points():
    est = pd.DatetimeIndex(["2018-01-01 00:15", "2018-01-01 01:00", "2018-01-01 01:15", "2018-01-01 02:00",
                            "2018-12-31 23:15", "2019-01-01 00:00"])
    got = fit.cst_hour_ending(est)
    want = pd.DatetimeIndex(["2018-01-01 00:00", "2018-01-01 00:00", "2018-01-01 01:00", "2018-01-01 01:00",
                             "2018-12-31 23:00", "2018-12-31 23:00"])
    assert got.equals(want)


def _curves(feeders, start, end, value):
    stamps = pd.date_range(start, end, freq="15min")
    rows = []
    for feeder in feeders:
        for i, t in enumerate(stamps):
            rows.append({"feeder": feeder, "timestamp": t, "kw": value(feeder, i, t)})
    return pd.DataFrame(rows)


def test_hourly_curves_year_end():
    # EST points 2018-12-31 21:00 to 2019-01-01 00:00; CST hours ending 21:00 to 2019-01-01 00:00.
    curves = _curves(["a", "b"], "2018-12-31 21:00", "2019-01-01 00:00",
                     lambda f, i, t: 1000.0 * i * (1 if f == "a" else 2))
    hours = pd.date_range("2018-12-31 21:00", "2019-01-01 00:00", freq="h")
    mw, left = fit.hourly_curves(curves, hours)
    # Hour ending 21:00 CST takes EST points 21:15..22:00, positions 1..4: mean 2.5 kW*1000 -> 2.5 MW.
    assert list(mw.index) == [ts("2018-12-31 21:00"), ts("2018-12-31 22:00"), ts("2018-12-31 23:00")]
    assert mw.loc[ts("2018-12-31 21:00"), "a"] == pytest.approx(2.5)
    assert mw.loc[ts("2018-12-31 22:00"), "a"] == pytest.approx(6.5)
    assert mw.loc[ts("2018-12-31 23:00"), "b"] == pytest.approx(2 * 10.5)
    in_target = left[left["in_target"]]
    assert list(in_target["cst_hour_ending"]) == [ts("2019-01-01 00:00")]
    assert in_target["max_points"].iloc[0] == 0
    outside = left[~left["in_target"]]
    assert list(outside["cst_hour_ending"]) == [ts("2018-12-31 20:00")]
    assert outside["max_points"].iloc[0] == 1


def test_fit_columns_merge_groups_sharing_a_feeder():
    hours = pd.date_range("2018-01-01 01:00", periods=3, freq="h")
    feeder_mw = pd.DataFrame({"f1": [1.0, 2.0, 3.0], "f2": [10.0, 10.0, 10.0]}, index=hours)
    typical = pd.DataFrame({
        "stratum": ["S", "S", "T"], "group": [1, 2, 1], "customers": [20, 10, 5],
        "starting_weight": [2 / 3, 1 / 3, 1.0], "run_feeder": ["f1", "f1", "f2"],
        "run_feeder_customers": [10, 10, 5], "run_is_placeholder": [False, True, False],
    })
    columns, groups = fit.fit_columns(typical, feeder_mw)
    assert list(columns.columns) == ["f1", "f2"]
    np.testing.assert_allclose(columns["f1"], [3.0, 6.0, 9.0])
    np.testing.assert_allclose(columns["f2"], [10.0, 10.0, 10.0])
    assert list(groups["column"]) == ["f1", "f1", "f2"]
    table = fit.weights_table(groups, pd.Series({"f1": 2.0, "f2": 0.5}))
    s = table[table["stratum"] == "S"]
    assert list(s["column_groups"]) == ["S#1;S#2", "S#1;S#2"]
    assert list(s["column_customers"]) == [30, 30]
    np.testing.assert_allclose(s["implied_weight"], [4 / 3, 2 / 3])
    np.testing.assert_allclose(s["fitted_share"], [2 / 3, 1 / 3])
    with pytest.raises(KeyError):
        fit.fit_columns(typical.assign(run_feeder=["f1", "f1", "f9"]), feeder_mw)


def test_split_by_day_of_month():
    hours = pd.DatetimeIndex(["2018-01-01 01:00", "2018-01-21 23:00", "2018-01-22 00:00", "2018-01-22 01:00",
                              "2018-02-01 00:00", "2018-02-28 12:00", "2018-03-01 00:00"])
    assert list(fit.split_of(hours)) == ["fit", "fit", "fit", "test", "test", "test", "test"]


def test_split_counts_on_a_year():
    hours = pd.date_range("2018-01-01 01:00", "2019-01-01 00:00", freq="h")
    split = fit.split_of(hours)
    assert (split == "fit").sum() == 21 * 24 * 12
    assert (split == "test").sum() == 8760 - 21 * 24 * 12


def test_nnls_recovers_known_weights():
    rng = np.random.default_rng(7)
    x = rng.uniform(0.5, 2.0, size=(200, 4))
    w = np.array([1.5, 0.0, 3.0, 0.25])
    got, residual = fit.fit_nnls(x, x @ w)
    np.testing.assert_allclose(got, w, atol=1e-9)
    assert residual == pytest.approx(0.0, abs=1e-8)


def test_nnls_clips_negative_weight_to_zero():
    x = np.array([[1.0, 1.0], [1.0, 2.0], [1.0, 3.0]])
    got, _ = fit.fit_nnls(x, x @ np.array([4.0, -1.0]))
    assert got[1] == 0.0
    assert got[0] == pytest.approx(2.0)


def test_common_scale():
    x = np.array([[1.0, 1.0], [2.0, 2.0]])
    assert fit.fit_common_scale(x, 3 * x.sum(axis=1)) == pytest.approx(3.0)
    assert fit.fit_common_scale(x, -x.sum(axis=1)) == 0.0


def _two_days():
    hours = pd.date_range("2018-01-01 01:00", periods=48, freq="h")
    target = pd.Series(100.0, index=hours)
    target.iloc[10] = 200.0   # day 1 range 100
    target.iloc[30] = 150.0   # day 2 range 50
    return hours, target


def test_energy_error():
    assert fit.energy_error([1.0, 2.0, 3.0], [2.0, 2.0, 2.0]) == pytest.approx(0.0)
    assert fit.energy_error([3.0, 3.0], [2.0, 2.0]) == pytest.approx(0.5)


def test_peak_errors_per_month():
    hours = pd.DatetimeIndex(["2018-01-10 05:00", "2018-01-11 05:00", "2018-02-10 05:00", "2018-02-11 05:00"])
    target = pd.Series([10.0, 20.0, 40.0, 30.0], index=hours)
    model = pd.Series([25.0, 1.0, 10.0, 20.0], index=hours)
    got = fit.peak_errors(model, target)
    assert got.loc[1] == pytest.approx(0.25)
    assert got.loc[2] == pytest.approx(-0.5)


def test_peak_month_uses_day_of_hour_start():
    hours = pd.DatetimeIndex(["2018-01-31 23:00", "2018-02-01 00:00", "2018-02-01 01:00"])
    target = pd.Series([1.0, 5.0, 2.0], index=hours)
    got = fit.peak_errors(target, target)
    assert list(got.index) == [1, 2]
    assert fit.peak_errors(target * 2, target).loc[1] == pytest.approx(1.0)


def test_range_errors_per_day():
    hours, target = _two_days()
    model = target.copy()
    model.iloc[10] = 150.0   # day 1 range 50: error -0.5
    model.iloc[30] = 175.0   # day 2 range 75: error +0.5
    got = fit.range_errors(model, target)
    np.testing.assert_allclose(got.to_numpy(), [-0.5, 0.5])
    # The hour ending 2018-01-02 00:00 belongs to 1 January.
    assert got.index[0] == ts("2018-01-01")


def test_cv_rmse():
    assert fit.cv_rmse([1.0, 3.0], [2.0, 2.0]) == pytest.approx(0.5)


def test_error_row_and_months():
    hours, target = _two_days()
    model = target * 1.1
    row = fit.error_row(model, target)
    assert row["energy_error"] == pytest.approx(0.1)
    assert row["peak_error_mean_abs"] == pytest.approx(0.1)
    assert row["range_error_mean_abs"] == pytest.approx(0.1)
    assert row["cv_rmse"] == pytest.approx(0.1 * np.sqrt(np.mean(target ** 2)) / target.mean())
    assert row["model_peak_hour"] == row["target_peak_hour"] == hours[10]
    assert row["peak_hour_gap_hours"] == 0.0
    assert row["days"] == 2
    months = fit.month_rows(model, target)
    assert list(months["month"]) == [1]
    assert months["energy_error"].iloc[0] == pytest.approx(0.1)


def test_run_end_to_end():
    # Two feeders on two CST days (21 January fit, 22 January test); target is an exact mix of the columns.
    est = pd.date_range("2018-01-21 00:15", "2018-01-23 00:00", freq="15min")
    rng = np.random.default_rng(3)
    a = rng.uniform(1000, 3000, len(est))
    b = rng.uniform(500, 1500, len(est))
    curves = pd.concat([pd.DataFrame({"feeder": "fa", "timestamp": est, "kw": a}),
                        pd.DataFrame({"feeder": "fb", "timestamp": est, "kw": b})], ignore_index=True)
    typical = pd.DataFrame({
        "stratum": ["S", "S", "T"], "group": [1, 2, 1], "customers": [10, 10, 4],
        "starting_weight": [0.5, 0.5, 1.0], "run_feeder": ["fa", "fa", "fb"],
        "run_feeder_customers": [10, 10, 2], "run_is_placeholder": [False, True, False],
    })
    cst = pd.date_range("2018-01-21 01:00", "2018-01-23 00:00", freq="h", tz="Etc/GMT+6")
    mw, _ = fit.hourly_curves(curves, cst.tz_localize(None))
    y = 0.7 * 2 * mw["fa"] + 1.3 * 2 * mw["fb"]
    target = pd.DataFrame({"utc_hour_ending": cst.tz_convert("UTC"), "cst_hour_ending": cst,
                           "target_mw": y.reindex(cst.tz_localize(None)).to_numpy()})
    out = fit.run(curves, typical, target)
    assert out["summary"]["hours_used"] == 47
    assert out["summary"]["columns"] == 2
    w = out["weights"].drop_duplicates("column").set_index("column")["fitted_weight"]
    assert w["fa"] == pytest.approx(0.7, rel=1e-9)
    assert w["fb"] == pytest.approx(1.3, rel=1e-9)
    fitted = out["errors"].set_index(["model", "split"]).loc[("fitted", "fit")]
    assert fitted["cv_rmse"] == pytest.approx(0.0, abs=1e-9)
    assert set(out["hourly"].columns) >= {"cst_hour_ending", "target_mw", "starting_mw", "common_scale_mw",
                                          "fitted_mw", "fa", "fb"}
    assert out["summary"]["fit_hours"] == 24
    assert out["summary"]["test_hours"] == 23
