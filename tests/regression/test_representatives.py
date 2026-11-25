"""Order of the representatives, groups and weights by load kW (decision K76), on small made-up tables."""

import numpy as np
import pandas as pd
import pytest
from scipy.cluster.hierarchy import fcluster

from dxsew import config
from dxsew.frame import representatives as rep

GROUPS_RULE = {"features": ["log(customers)", "log(load_kw)", "com_kw_share"], "scaling": "frame_z", "cap": 40}
BLOCK = {
    "order_rules": ["ward_drop", "load_weighted_drop"],
    "eligible": "all",
    "weight": "load_kw_share",
    "max_representatives": 300,
    "check_quantities": ["customers", "loads", "line_km"],
    "head_start_curves_stamp": None,
    "head_start_weights": ["load_kw", "customers"],
    "report_steps": [3, 5, 8],
    "gate_weight_tolerance": 1.0e-9,
    "gate_zero_tolerance": 1.0e-12,
}
QUANTITIES = BLOCK["check_quantities"]


def table(seed=7, sizes=(("A", 13), ("B", 9), ("C", 1))):
    """A made-up feature table with three strata, one of them a single feeder."""
    rng = np.random.default_rng(seed)
    rows = []
    for stratum, count in sizes:
        for i in range(count):
            customers = int(rng.integers(5, 3000))
            rows.append(
                {
                    "stratum": stratum,
                    "feeder": f"{stratum.lower()}s{i % 3}--f{i:02d}",
                    "customers": customers,
                    "loads": customers + int(rng.integers(0, 50)),
                    "load_kw": float(customers * rng.uniform(1.0, 9.0)),
                    "com_kw_share": float(rng.uniform(0.0, 1.0)),
                    "line_km": float(rng.uniform(1.0, 80.0)),
                    "runnable_alone": bool(rng.integers(0, 2)),
                }
            )
    return pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)


def run(rule, features=None, cap=300, eligible="all"):
    strata, in_grouping, _ = rep.prepare(table() if features is None else features, GROUPS_RULE, eligible)
    return strata, in_grouping, rep.sequence(strata, rule, cap)


def partitions(result):
    """Per step n: {(stratum, group): frozenset of feeders}."""
    found = {}
    for step, labels in zip(result["steps"], result["labels"]):
        groups = {}
        for name, stratum, label in zip(result["names"], result["strata"], labels):
            groups.setdefault((stratum, int(label)), set()).add(name)
        found[step["n"]] = {key: frozenset(value) for key, value in groups.items()}
    return found


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_order_is_deterministic(rule):
    first, second = run(rule)[2], run(rule)[2]
    assert rep.step_table(first["steps"]).equals(rep.step_table(second["steps"]))
    assert rep.group_table(first["rows"]).equals(rep.group_table(second["rows"]))
    shuffled = run(rule, table().sample(frac=1.0, random_state=3))[2]
    assert rep.group_table(first["rows"]).equals(rep.group_table(shuffled["rows"]))


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_first_step_and_stop(rule):
    strata, in_grouping, result = run(rule)
    steps = rep.step_table(result["steps"])
    assert steps["n"].iloc[0] == len(strata) == 3
    assert list(steps["n"]) == list(range(3, len(in_grouping) + 1))
    capped = run(rule, cap=6)[2]
    assert [step["n"] for step in capped["steps"]] == [3, 4, 5, 6]


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_parent_split_before_children_and_partitions_nest(rule):
    _, _, result = run(rule)
    found = partitions(result)
    for step in result["steps"][1:]:
        n, stratum = step["n"], step["stratum"]
        before, after = found[n - 1], found[n]
        parent = before[(stratum, step["split_group"])]
        assert (stratum, step["new_group"]) not in before
        assert after[(stratum, step["split_group"])] | after[(stratum, step["new_group"])] == parent
        assert not after[(stratum, step["split_group"])] & after[(stratum, step["new_group"])]
        unchanged = {key: value for key, value in before.items() if key != (stratum, step["split_group"])}
        assert all(after[key] == value for key, value in unchanged.items())


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_representatives_are_kept_and_numbers_follow_rule_three(rule):
    _, _, result = run(rule)
    rows = rep.group_table(result["rows"])
    by_n = {n: part.set_index(["stratum", "group"]) for n, part in rows.groupby("n")}
    steps = result["steps"]
    for step in steps[1:]:
        n, stratum = step["n"], step["stratum"]
        before, after = by_n[n - 1], by_n[n]
        assert set(before["representative"]) <= set(after["representative"])
        assert len(set(after["representative"])) == n
        kept = after.loc[(stratum, step["split_group"])]
        assert kept["representative"] == before.loc[(stratum, step["split_group"]), "representative"]
        assert kept["since_n"] == before.loc[(stratum, step["split_group"]), "since_n"]
        new = after.loc[(stratum, step["new_group"])]
        assert step["new_group"] == before.loc[stratum].index.max() + 1
        assert new["representative"] == step["new_representative"]
        assert new["representative"] == new["nearest_feeder"]
        assert new["since_n"] == n


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_weights_times_representative_load_add_to_the_load(rule):
    _, in_grouping, result = run(rule)
    rows = rep.group_table(result["rows"])
    product = (rows["weight"] * rows["representative_load_kw"]).groupby([rows["n"], rows["stratum"]]).sum()
    load = in_grouping.groupby("stratum")["load_kw"].sum()
    for (n, stratum), value in product.items():
        assert value == pytest.approx(load[stratum], rel=1e-12)
    assert rows.groupby("n")["load_share"].sum().to_numpy() == pytest.approx(1.0, rel=1e-12)


def test_ward_partition_after_each_split_equals_fcluster():
    strata, _, result = run("ward_drop")
    assert rep.ward_matches_fcluster(result, strata, 40) == {stratum.name: True for stratum in strata}
    found = partitions(result)
    for stratum in strata:
        if stratum.tree is None:
            continue
        splits = [step["n"] for step in result["steps"][1:] if step["stratum"] == stratum.name]
        for j, n in enumerate(splits, start=1):
            labels = fcluster(stratum.tree, t=j + 1, criterion="maxclust")
            theirs = {frozenset(np.array(stratum.names)[labels == label]) for label in np.unique(labels)}
            mine = {value for (name, _), value in found[n].items() if name == stratum.name}
            assert mine == theirs


def test_explained_starts_at_zero_and_never_falls():
    for rule in rep.ORDER_RULES:
        steps = rep.step_table(run(rule)[2]["steps"])
        assert steps["explained"].iloc[0] == 0.0 and steps["explained_load_weighted"].iloc[0] == 0.0
        assert (np.diff(steps["explained"]) >= -1e-12).all()
        assert (np.diff(steps["explained_load_weighted"]) >= -1e-12).all()
        assert steps["explained"].iloc[-1] == pytest.approx(1.0)


def test_every_feeder_its_own_group_gives_zero_errors():
    built = rep.build(table(), GROUPS_RULE, BLOCK, "AREA")
    last = built["errors"]["n"] == built["errors"]["n"].max()
    assert built["errors"]["n"].max() == built["counts"]["in_grouping"]
    assert (built["errors"].loc[last, "error"].abs() < 1e-12).all()
    assert set(built["errors"]["scope"]) == {"AREA", "A", "B", "C"}
    gates = rep.gates(built, GROUPS_RULE, BLOCK)
    assert all(gates.values()), gates


def test_eligible_rules_and_zero_load():
    features = table()
    features.loc[features["feeder"] == "as0--f00", "load_kw"] = 0.0
    strata, _, counts = rep.prepare(features, GROUPS_RULE, "runnable_alone")
    assert counts["zero_load_kw"] == 1
    for stratum in strata:
        assert (stratum.eligible == (stratum.runnable & (stratum.load_kw > 0))).all()
    rows = rep.group_table(run("ward_drop", features)[2]["rows"])
    assert "as0--f00" not in set(rows["representative"])
    # Only a group of the zero-load feeder alone has no representative.
    empty = rows[rows["representative"].isna()]
    assert len(empty) and (empty["feeders"] == 1).all() and empty["nearest_feeder"].isna().all()
    assert rows.loc[rows["feeders"] > 1, "representative"].notna().all()


def test_pin_table_counts_flagged_representatives():
    built = rep.build(table(), GROUPS_RULE, BLOCK, "AREA")
    pin = built["pin"]
    assert list(pin["n"].unique()) == [3, 5, 8]
    for _, row in pin.iterrows():
        at = built["representatives"].query("rule == @row.rule and n == @row.n")
        flagged = ~at["runnable_at_pin"].astype(bool)
        assert row["not_runnable_at_pin"] == flagged.sum()
        assert row["not_runnable_load_share"] == pytest.approx(at.loc[flagged, "load_kw"].sum() / at["load_kw"].sum())


def test_head_start_is_exact_with_every_feeder():
    features = table()
    names = sorted(features["feeder"])[:10]
    stamps = pd.date_range("2018-01-01 00:15", periods=8, freq="15min")
    rng = np.random.default_rng(1)
    curves = pd.DataFrame(
        [{"feeder": name, "timestamp": stamp, "kw": float(rng.uniform(1, 100))} for name in names for stamp in stamps]
    )
    started = rep.head_start(features, curves, GROUPS_RULE, BLOCK)
    assert started["hours"] == 0.25
    feeders = started["feeders"].set_index("feeder")
    first = curves[curves["feeder"] == names[0]]["kw"]
    assert feeders.loc[names[0], "energy_kwh"] == pytest.approx(first.sum() * 0.25)
    assert feeders.loc[names[0], "peak_kw"] == pytest.approx(first.max())
    errors = started["errors"]
    assert set(errors["weight"]) == {"load_kw", "customers"}
    assert rep.head_start_gate(errors, 10, 1e-12)
    summary = started["summary"]
    assert set(summary["statistic"]) == {"median", "q1", "q3", "cv", "spearman"}


def test_head_start_follows_the_representative_choice():
    features = table()
    names = sorted(features["feeder"])[:10]
    stamps = pd.date_range("2018-01-01 00:15", periods=8, freq="15min")
    rng = np.random.default_rng(1)
    curves = pd.DataFrame(
        [{"feeder": name, "timestamp": stamp, "kw": float(rng.uniform(1, 100))} for name in names for stamp in stamps]
    )
    curve_names, matrix, _ = rep.curve_matrix(curves)
    weighted = rep.method_on_curves(features, curve_names, matrix, GROUPS_RULE, BLOCK, choice="load_weighted_centre")
    centre = rep.method_on_curves(features, curve_names, matrix, GROUPS_RULE, BLOCK, choice="centre")
    chosen = rep.head_start(features, curves, GROUPS_RULE, {**BLOCK, "representative_choice": "load_weighted_centre"})
    default = rep.head_start(features, curves, GROUPS_RULE, BLOCK)
    pd.testing.assert_frame_equal(chosen["errors"], weighted["errors"], check_exact=True)
    pd.testing.assert_frame_equal(default["errors"], centre["errors"], check_exact=True)
    assert not weighted["errors"].equals(centre["errors"])


def test_curve_errors():
    true = np.array([1.0, 2.0, 4.0, 1.0])
    found = rep.curve_errors(true * 1.1, true)
    assert found["error_energy"] == pytest.approx(0.1)
    assert found["error_at_true_peak"] == pytest.approx(0.1)
    assert found["error_of_peak"] == pytest.approx(0.1)
    assert found["cv_rmse"] == pytest.approx(0.1 * np.sqrt((true ** 2).mean()) / true.mean())


def test_settings_of_the_setup_are_known():
    block = config.load_setup()["representatives"]
    rep.check_settings(block)
    for key, bad in (("order_rules", ["other"]), ("eligible", "some"), ("weight", "customers"),
                     ("head_start_weights", ["kwh"])):
        with pytest.raises(ValueError):
            rep.check_settings({**block, key: bad})
    assert isinstance(block["gate_weight_tolerance"], float) and isinstance(block["gate_zero_tolerance"], float)


def test_weighted_sum_of_squares():
    points = np.array([[0.0], [2.0], [10.0]])
    assert rep.weighted_sum_of_squares(points, np.array([1.0, 1.0, 0.0])) == pytest.approx(2.0)
    assert rep.weighted_sum_of_squares(points, np.zeros(3)) == 0.0
