"""Error curve, bootstrap band and stop rule (decision K80), on small made-up curves."""

import numpy as np
import pandas as pd
import pytest

from dxsew import config
from dxsew.frame import error_curve as ec
from dxsew.frame import representatives as rep
from dxsew.frame import sample_order as so

BLOCK = {
    "order_rule": "ward_drop",
    "sample_per_stratum": 3,
    "sample_seed": 5,
    "stop_gain": 0.01,
    "power_flow_cap": 10,
    "report_gains": [0.02, 0.01, 0.005],
    "bootstrap_draws": 200,
    "band": [0.05, 0.95],
}
ZERO = 1e-12
ERRORS = ["cv_rmse", "error_at_true_peak", "error_of_peak", "error_energy"]


def assigned_table(rows):
    """``assignments`` rows from tuples ``(n, feeder, stratum, representative, representative_load_kw)``."""
    return pd.DataFrame(rows, columns=["n", "feeder", "stratum", "representative", "representative_load_kw"])


def shares_of(loads):
    table = pd.DataFrame(
        [{"rule": "ward_drop", "n": len(loads), "stratum": name, "load_kw": value} for name, value in loads.items()]
    )
    return ec.stratum_shares(table, "ward_drop")


def build(assigned, load, curves, shares, block=BLOCK, max_n=300):
    names = sorted(curves)
    matrix = np.array([curves[name] for name in names], dtype="float64")
    return ec.build_curve(assigned, pd.Series(load, dtype="float64"), names, matrix, shares, block, max_n, 7)


def two_strata():
    """Strata A and B, one sample feeder each, whose representative is another feeder (hand-computed below)."""
    curves = {
        "a1": [1.0, 2.0, 3.0, 4.0],
        "a2": [2.0, 2.0, 2.0, 2.0],
        "b1": [4.0, 4.0, 4.0, 4.0],
        "b2": [1.0, 3.0, 1.0, 3.0],
    }
    load = {"a1": 10.0, "a2": 20.0, "b1": 5.0, "b2": 5.0}
    assigned = assigned_table([(2, "a1", "A", "a2", 20.0), (2, "b1", "B", "b2", 5.0)])
    return assigned, load, curves


def test_every_feeder_its_own_group_gives_zero_errors():
    rng = np.random.default_rng(3)
    names = [f"f{i}" for i in range(6)]
    curves = {name: rng.uniform(1.0, 50.0, size=24) for name in names}
    load = {name: float(rng.uniform(10.0, 90.0)) for name in names}
    strata = {name: ("A" if i < 4 else "B") for i, name in enumerate(names)}
    assigned = assigned_table([(n, name, strata[name], name, load[name]) for n in (2, 3) for name in names])
    built = build(assigned, load, curves, shares_of({"A": 3.0, "B": 1.0}))
    curve = built["curve"]
    assert set(curve["scope"]) == {"A", "B", ec.AREA_SCOPE}
    assert (curve[ERRORS].abs().fillna(0.0) <= ZERO).all().all()
    assert (curve.loc[curve["scope"] == ec.AREA_SCOPE, ec.BAND_COLUMNS].abs() <= ZERO).all().all()


def test_two_feeders_proportional_to_load_give_zero_errors():
    shape = np.array([1.0, 4.0, 2.0, 7.0, 3.0])
    curves = {"x": 30.0 * shape, "y": 12.0 * shape}
    load = {"x": 30.0, "y": 12.0}
    assigned = assigned_table([(1, "x", "A", "x", 30.0), (1, "y", "A", "x", 30.0)])
    curve = build(assigned, load, curves, shares_of({"A": 42.0}))["curve"]
    assert (curve[ERRORS].abs().fillna(0.0) <= ZERO).all().all()


def test_area_is_the_load_share_average_of_the_strata():
    assigned, load, curves = two_strata()
    curve = build(assigned, load, curves, shares_of({"A": 25.0, "B": 75.0}))["curve"].set_index("scope")
    a = {"cv_rmse": np.sqrt(3.5) / 2.5, "error_at_true_peak": 0.25 - 1.0, "error_of_peak": 0.25 - 1.0,
         "error_energy": 0.4 - 1.0}
    # B's true curve is flat, so its peak step is the first, where the estimate is 1.
    b = {"cv_rmse": np.sqrt(5.0) / 4.0, "error_at_true_peak": 0.25 - 1.0, "error_of_peak": 0.75 - 1.0,
         "error_energy": 0.5 - 1.0}
    for name, hand in (("A", a), ("B", b)):
        for column, value in hand.items():
            assert curve.loc[name, column] == pytest.approx(value)
    for column in ec.AREA_FIGURES:
        assert curve.loc[ec.AREA_SCOPE, column] == pytest.approx(0.25 * a[column] + 0.75 * b[column])
    assert np.isnan(curve.loc[ec.AREA_SCOPE, "error_at_true_peak"])
    assert curve.loc[ec.AREA_SCOPE, "sample_feeders"] == 2
    assert list(curve.index) == [ec.AREA_SCOPE, "A", "B"]


def area(cv, peak, first=3):
    return pd.DataFrame({"n": np.arange(first, first + len(cv)), "cv_rmse": cv, "error_of_peak": peak})


def test_stop_rule_waits_for_a_later_drop():
    # Flat from n 4 to 7, then a drop at 8: a rule that stops at the first flat step would stop at 4.
    curve = area([0.30, 0.20, 0.20, 0.20, 0.20, 0.05], [0.0] * 6)
    assert ec.stop_point(curve, 0.01, 10)["n"] == 8
    assert ec.stop_point(curve, 0.01, 7)["n"] == 4
    assert ec.stop_point(curve, 0.2, 10)["n"] == 4


def test_stop_rule_binds_on_cv_rmse_or_on_the_peak():
    by_cv = area([0.30, 0.10, 0.08, 0.055, 0.045, 0.04], [0.0] * 6)
    assert ec.stop_point(by_cv, 0.01, 10)["n"] == 7
    assert ec.stop_point(by_cv, 0.02, 10)["n"] == 6
    by_peak = area([0.1] * 6, [0.2, -0.15, 0.10, 0.02, 0.015, 0.012])
    found = ec.stop_point(by_peak, 0.01, 10)
    assert found["n"] == 6 and found["error_of_peak"] == pytest.approx(0.02)
    both = area([0.30, 0.10, 0.095, 0.09, 0.09, 0.09], [0.2, 0.2, 0.2, 0.05, 0.05, 0.05])
    assert ec.stop_point(both, 0.01, 10)["n"] == 6
    table = ec.stop_table(both.assign(scope=ec.AREA_SCOPE), BLOCK, 10, 8)
    assert list(table["gain"]) == [0.01, 0.02, 0.005]
    assert list(table["n"]) == [6, 6, 6] and set(table["last_n_with_curves"]) == {8}


def noisy_stratum(seed=11, count=20):
    """One stratum: the sample feeders are their representative's shape, scaled by load, with a shared bias and noise."""
    rng = np.random.default_rng(seed)
    steps = np.arange(96)
    shape = 10.0 + 5.0 * np.sin(steps / 8.0)
    curves = {"rep": 40.0 * shape}
    load = {"rep": 40.0}
    rows = []
    for i in range(count):
        name = f"s{i:02d}"
        load[name] = float(rng.uniform(5.0, 60.0))
        curves[name] = load[name] * shape * (1.1 + 0.05 * np.cos(steps / 5.0)) * rng.normal(1.0, 0.01, size=len(steps))
        rows.extend((n, name, "A", "rep", 40.0) for n in (1, 2))
    return assigned_table(rows), load, curves


def test_bootstrap_repeats_under_the_seed_and_holds_the_point():
    assigned, load, curves = noisy_stratum()
    first = build(assigned, load, curves, shares_of({"A": 1.0}))
    second = build(assigned, load, curves, shares_of({"A": 1.0}))
    pd.testing.assert_frame_equal(first["curve"], second["curve"])
    other = build(assigned, load, curves, shares_of({"A": 1.0}), {**BLOCK, "sample_seed": 6})
    area_rows = first["curve"][first["curve"]["scope"] == ec.AREA_SCOPE]
    assert not np.allclose(area_rows["cv_rmse_low"], other["curve"].loc[area_rows.index, "cv_rmse_low"])
    for _, row in area_rows.iterrows():
        assert row["cv_rmse_low"] <= row["cv_rmse"] <= row["cv_rmse_high"]
        assert row["peak_low"] <= row["error_of_peak"] <= row["peak_high"]


def test_band_does_not_depend_on_the_block_held_in_memory():
    assigned, load, curves = noisy_stratum()
    names = sorted(curves)
    matrix = np.array([curves[name] for name in names])
    found = [ec.build_curve(assigned, pd.Series(load), names, matrix, shares_of({"A": 1.0}), BLOCK, 300, size)
             for size in (1, 33, BLOCK["bootstrap_draws"])]
    for other in found[1:]:
        np.testing.assert_allclose(other["bootstrap"]["cv_rmse"], found[0]["bootstrap"]["cv_rmse"], rtol=1e-12)


def test_missing_sample_curve_is_left_out_and_counted():
    assigned, load, curves = two_strata()
    extra = assigned_table([(2, "a9", "A", "a2", 20.0)])
    with_missing = pd.concat([assigned, extra], ignore_index=True)
    built = build(with_missing, {**load, "a9": 3.0}, curves, shares_of({"A": 25.0, "B": 75.0}))
    plain = build(assigned, load, curves, shares_of({"A": 25.0, "B": 75.0}))
    assert built["counts"]["without_curve"] == 1 and built["counts"]["without_curve_by_stratum"] == {"A": 1}
    pd.testing.assert_frame_equal(built["curve"], plain["curve"])


def test_missing_representative_curve_ends_the_curve():
    assigned, load, curves = two_strata()
    later = assigned_table([(3, "a1", "A", "a2", 20.0), (3, "b1", "B", "b2", 5.0),
                            (4, "a1", "A", "zz", 8.0), (4, "b1", "B", "b2", 5.0),
                            (5, "a1", "A", "a1", 10.0), (5, "b1", "B", "b1", 5.0)])
    built = build(pd.concat([assigned, later], ignore_index=True), load, curves, shares_of({"A": 1.0, "B": 1.0}))
    assert built["end"] == {"last_n": 3, "ended_by": "zz", "needed_at": 4}
    assert sorted(built["curve"]["n"].unique()) == [2, 3]
    capped = build(pd.concat([assigned, later], ignore_index=True), load, curves, shares_of({"A": 1.0, "B": 1.0}),
                   max_n=2)
    assert sorted(capped["curve"]["n"].unique()) == [2]


def test_groups_on_curves_with_every_feeder_sampled_end_at_zero():
    rng = np.random.default_rng(2)
    rows = []
    for stratum, count in (("A", 7), ("B", 5)):
        for i in range(count):
            customers = int(rng.integers(5, 3000))
            rows.append({"stratum": stratum, "feeder": f"{stratum.lower()}--f{i}", "customers": customers,
                         "load_kw": float(customers * rng.uniform(1.0, 9.0)), "com_kw_share": float(rng.uniform()),
                         "runnable_alone": True})
    features = pd.DataFrame(rows)
    groups_rule = {"features": ["log(customers)", "log(load_kw)", "com_kw_share"], "scaling": "frame_z"}
    block = {"eligible": "all", "max_representatives": 300, "representative_choice": "load_weighted_centre"}
    names = sorted(features["feeder"])
    table, labels = ec.groups_on_curves(features, names, groups_rule, block, "ward_drop")
    assigned = ec.assignments(labels, table, "ward_drop", names)
    curves = {name: rng.uniform(1.0, 9.0, size=48) for name in names}
    load = features.set_index("feeder")["load_kw"].to_dict()
    built = build(assigned, load, curves, ec.stratum_shares(table, "ward_drop"))
    curve = built["curve"]
    assert curve["n"].min() == 2 and curve["n"].max() == len(names)
    last = curve[curve["n"] == len(names)]
    assert (last[ERRORS].abs().fillna(0.0) <= ZERO).all().all()
    assert (curve.loc[(curve["n"] == 2) & (curve["scope"] == ec.AREA_SCOPE), "cv_rmse"] > 0).all()
    shares = ec.stratum_shares(table, "ward_drop")
    assert shares.sum() == pytest.approx(1.0)


def test_read_sample_keeps_sample_rows_of_a_match_list(tmp_path):
    path = tmp_path / "match.csv"
    pd.DataFrame({"feeder": ["c1", "s1", "b1", "s1"], "role": ["candidate", "sample", "both", "sample"]}).to_csv(
        path, index=False)
    assert ec.read_sample(path) == ["s1", "b1"]
    plain = tmp_path / "plain.csv"
    pd.DataFrame({"feeder": ["x", "y"]}).to_csv(plain, index=False)
    assert ec.read_sample(plain) == ["x", "y"]


def test_settings_of_the_setup_are_known():
    setup = config.load_setup()
    block, representatives_block = setup["error_curve"], setup["representatives"]
    so.check_block(block, representatives_block)
    assert representatives_block["representative_choice"] in rep.REPRESENTATIVE_CHOICES
    for key, bad in (("order_rule", "other"), ("band", [0.9, 0.1]), ("sample_per_stratum", 0),
                     ("stop_gain", -0.01)):
        with pytest.raises(ValueError):
            so.check_block({**block, key: bad}, representatives_block)
