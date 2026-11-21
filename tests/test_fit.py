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


# Sector factors (K71).

def _stratum_table(stamps, parts):
    """Long stratum table: parts is {stratum: (res1, res2, com1)} arrays in kW; kw is their sum."""
    rows = []
    for name, (r1, r2, c1) in parts.items():
        rows.append(pd.DataFrame({"stratum": name, "timestamp": stamps, "kw": r1 + r2 + c1,
                                  "com:cooling": c1, "res:cooling": r1, "res:pv": r2}))
    return pd.concat(rows, ignore_index=True)


def test_sector_curves_split_and_end_use_check():
    stamps = pd.date_range("2018-01-01 00:15", periods=8, freq="15min")
    one = np.arange(1.0, 9.0)
    stratum = _stratum_table(stamps, {"A": (100 * one, -one, 50 * one), "B": (10 * one, 0 * one, 30 * one)})
    curves, checks = fit.sector_curves(stratum)
    np.testing.assert_allclose(curves["res"], 110 * one - one)
    np.testing.assert_allclose(curves["com"], 80 * one)
    np.testing.assert_allclose(curves["kw"], curves["res"] + curves["com"])
    assert checks["max_gap_rel"].max() == pytest.approx(0.0, abs=1e-15)
    assert checks["points_over_tolerance"].sum() == 0
    # kw 0.5 % above the end uses in one point of stratum B: the check reports it.
    stratum.loc[(stratum["stratum"] == "B") & (stratum["timestamp"] == stamps[3]), "kw"] *= 1.005
    _, checks = fit.sector_curves(stratum)
    b = checks.set_index("stratum").loc["B"]
    assert b["max_gap_rel"] == pytest.approx(0.005 / 1.005)
    assert b["max_gap_rel_timestamp"] == stamps[3]
    assert b["points_over_tolerance"] == 1
    assert checks.set_index("stratum").loc["A", "points_over_tolerance"] == 0


def test_sector_hourly_uses_the_cst_rule():
    stamps = pd.date_range("2018-01-01 01:15", "2018-01-01 03:00", freq="15min")
    curves = pd.DataFrame({"res": 1000.0 * np.arange(8), "com": 2000.0}, index=stamps)
    hours = pd.DatetimeIndex(["2018-01-01 00:00", "2018-01-01 01:00", "2018-01-01 02:00"])
    mw = fit.sector_hourly(curves, hours)
    # CST hour ending 01:00 takes EST points 01:15 to 02:00 (positions 0 to 3); 00:00 has no points.
    assert list(mw.index) == list(hours[1:])
    np.testing.assert_allclose(mw["res"], [1.5, 5.5])
    np.testing.assert_allclose(mw["com"], [2.0, 2.0])


def test_sector_energy_and_customers():
    curves = pd.DataFrame({"res": [1000.0] * 4, "com": [4000.0] * 4})
    assert fit.sector_energy_mwh(curves) == pytest.approx({"res": 1.0, "com": 4.0})
    typical = pd.DataFrame({"customers": [30, 10, 6], "run_feeder_customers": [10, 10, 3],
                            "run_feeder": ["f1", "f1", "f2"]})
    features = pd.DataFrame({"feeder": ["f1", "f2", "f3"], "res_customers": [8, 1, 100], "com_customers": [2, 2, 7]})
    counts = fit.sector_customers(typical, features)
    # Groups: factors 3 and 1 on f1 (8 homes, 2 businesses), factor 2 on f2 (1 home, 2 businesses).
    assert counts["N_model"] == pytest.approx({"res": 4 * 8 + 2 * 1, "com": 4 * 2 + 2 * 2})
    assert counts["N_frame"] == {"res": 109.0, "com": 11.0}
    assert counts["frame_feeders"] == 3
    with pytest.raises(KeyError):
        fit.sector_customers(typical.assign(run_feeder=["f1", "f1", "f9"]), features)


def test_sector_factors_and_product():
    f = fit.sector_factors(S=120.0, N_eia=12.0, E=100.0, N_model=8.0, N_frame=10.0)
    assert f["M"] == pytest.approx(1.2)
    assert f["e"] == pytest.approx((120 / 12) / (100 / 8))
    assert f["m"] == pytest.approx(10 / 8)
    assert f["c"] == pytest.approx(12 / 10)
    assert f["e"] * f["m"] * f["c"] == pytest.approx(f["M"], rel=1e-12)
    assert f["product_gap"] <= fit.PRODUCT_TOLERANCE


def test_report_only_fits_known_answers():
    rng = np.random.default_rng(11)
    r = rng.uniform(10, 20, 300)
    b = rng.uniform(5, 30, 300)
    assert fit.fit_sector_scales(r, b, 1.3 * r + 0.7 * b) == pytest.approx((1.3, 0.7), rel=1e-9)
    s_res, s_com = fit.fit_sector_scales(r, b, 2.0 * r - 0.5 * b)
    assert min(s_res, s_com) >= 0.0
    assert fit.fit_scale_plus_constant(r, 1.1 * r + 50.0) == pytest.approx((1.1, 50.0), rel=1e-9)


def test_run_with_sectors_end_to_end():
    est = pd.date_range("2018-01-21 00:15", "2018-01-23 00:00", freq="15min")
    rng = np.random.default_rng(5)
    a = rng.uniform(1000, 3000, len(est))
    b = rng.uniform(500, 1500, len(est))
    curves = pd.concat([pd.DataFrame({"feeder": "fa", "timestamp": est, "kw": a}),
                        pd.DataFrame({"feeder": "fb", "timestamp": est, "kw": b})], ignore_index=True)
    typical = pd.DataFrame({
        "stratum": ["S", "S", "T"], "group": [1, 2, 1], "customers": [10, 10, 4],
        "starting_weight": [0.5, 0.5, 1.0], "run_feeder": ["fa", "fa", "fb"],
        "run_feeder_customers": [10, 10, 2], "run_is_placeholder": [False, True, False],
    })
    # Stratum curves at starting weights: S = 2 fa (60 % homes), T = 2 fb (30 % homes).
    stratum = _stratum_table(est, {"S": (1.2 * a, 0 * a, 0.8 * a), "T": (0.6 * b, 0 * b, 1.4 * b)})
    features = pd.DataFrame({"feeder": ["fa", "fb", "fz"], "res_customers": [8, 1, 5], "com_customers": [2, 1, 0]})
    eia = {"mwh": {"residential": 900.0, "commercial": 1100.0},
           "mwh_with_delivery": {"residential": 900.0, "commercial": 1100.0},
           "customers": {"residential": 40.0, "commercial": 9.0},
           "customers_with_delivery": {"residential": 40.0, "commercial": 9.0}, "rows_by_service_type": {}}
    sources = {"eia861": "u", "E": "e", "N_model": "n", "N_frame": "f"}
    cst = pd.date_range("2018-01-21 01:00", "2018-01-23 00:00", freq="h", tz="Etc/GMT+6")
    mw, _ = fit.hourly_curves(curves, cst.tz_localize(None))
    y = 1.4 * mw["fa"] + 2.5 * mw["fb"] + 3.0
    target = pd.DataFrame({"utc_hour_ending": cst.tz_convert("UTC"), "cst_hour_ending": cst,
                           "target_mw": y.reindex(cst.tz_localize(None)).to_numpy()})
    plain = fit.run(curves, typical, target)
    sector = {"stratum": stratum, "features": features, "eia": eia, "year": 2018, "sources": sources}
    out = fit.run(curves, typical, target, sector)
    # J5's outputs are unchanged by the sector models.
    pd.testing.assert_frame_equal(out["hourly"][plain["hourly"].columns], plain["hourly"])
    kept = out["errors"]["model"].isin(plain["errors"]["model"])
    pd.testing.assert_frame_equal(out["errors"][kept].reset_index(drop=True), plain["errors"])
    sec = out["summary"]["sector"]
    assert sec["check_end_uses"]["max_gap_rel"] == pytest.approx(0.0, abs=1e-12)
    assert sec["check_starting"]["max_gap_rel"] == pytest.approx(0.0, abs=1e-12)
    assert sec["sector_formula_year_gap"] == pytest.approx(0.0, abs=1e-12)
    # N_model: S groups factor 1 and 1 on fa (8, 2), T group factor 2 on fb (1, 1).
    assert sec["N_model"] == pytest.approx({"res": 18.0, "com": 6.0})
    assert sec["N_frame"] == {"res": 14.0, "com": 3.0}
    h = out["hourly"]
    np.testing.assert_allclose(h["res_mw"] + h["com_mw"], h["starting_mw"], rtol=1e-12)
    factors = sec["factors"]
    np.testing.assert_allclose(h["sector_formula_mw"],
                               factors["res"]["M"] * h["res_mw"] + factors["com"]["M"] * h["com_mw"])
    # a and b are the least squares of a X + b on the fit hours.
    rows = (h["split"] == "fit").to_numpy()
    design = np.column_stack([h.loc[rows, "starting_mw"], np.ones(rows.sum())])
    want = np.linalg.lstsq(design, h.loc[rows, "target_mw"], rcond=None)[0]
    assert (sec["a"], sec["b"]) == pytest.approx(tuple(want), rel=1e-9)
    assert set(out["errors"]["model"]) == set(fit.MODELS)
    assert set(out["months"]["model"]) == set(fit.MODELS)
    table = out["sector_factors"].set_index("sector")
    assert table.loc["res", "fitted_scale"] == pytest.approx(sec["s_res"])
    assert "14 homes and 3 businesses against 40 residential and 9 commercial" in sec["assumptions"]["customer"]
    assert "{:.4f}".format(factors["res"]["e"]) in sec["assumptions"]["energy_factor"]
