"""Representative choice and the scout of decision K77, on small made-up tables."""

import numpy as np
import pandas as pd
import pytest

from dxsew import config
from dxsew.frame import representatives as rep
from dxsew.frame import representatives_scout as scout

NINE = [
    "log(customers)", "log(load_kw)", "log(kw_per_customer)", "com_kw_share", "log(line_km)", "log(extent_km)",
    "log(service_transformers)", "log(kva_per_kw)", "primary_kv",
]
GROUPS_RULE = {"features": NINE, "scaling": "frame_z", "cap": 40}
AVERAGE = {
    "mean": ["customers", "load_kw", "line_km", "extent_km", "service_transformers", "regulators", "primary_kv"],
    "ratio": {"kw_per_customer": ["load_kw", "customers"], "kva_per_kw": ["service_kva", "load_kw"]},
    "weighted_mean": {"com_kw_share": "load_kw"},
}
BLOCK = {
    "order_rules": ["ward_drop", "load_weighted_drop"],
    "eligible": "all",
    "weight": "load_kw_share",
    "representative_choice": "centre",
    "max_representatives": 300,
    "check_quantities": ["customers", "loads", "line_km", "regulators"],
    "head_start_curves_stamp": None,
    "head_start_weights": ["load_kw", "customers"],
    "report_steps": [3, 5, 8],
    "gate_weight_tolerance": 1.0e-9,
    "gate_zero_tolerance": 1.0e-12,
}


def table(seed=11, sizes=(("A", 14), ("B", 9), ("C", 3), ("D", 1))):
    """A made-up feature table with every column the nine features and the average feeder read."""
    rng = np.random.default_rng(seed)
    rows = []
    for stratum, count in sizes:
        for i in range(count):
            customers = int(rng.integers(5, 3000))
            load_kw = float(customers * rng.uniform(1.0, 9.0))
            service_kva = float(load_kw * rng.uniform(1.0, 4.0))
            rows.append(
                {
                    "stratum": stratum,
                    "feeder": f"{stratum.lower()}s{i % 3}--f{i:02d}",
                    "customers": customers,
                    "loads": customers + int(rng.integers(0, 50)),
                    "load_kw": load_kw,
                    "kw_per_customer": load_kw / customers,
                    "com_kw_share": float(rng.uniform(0.0, 1.0)),
                    "line_km": float(rng.uniform(1.0, 80.0)),
                    "extent_km": float(rng.uniform(0.5, 20.0)),
                    "service_transformers": int(rng.integers(1, 400)),
                    "service_kva": service_kva,
                    "kva_per_kw": service_kva / load_kw,
                    "regulators": int(rng.integers(0, 4)),
                    "primary_kv": float(rng.choice([4.0, 12.47, 25.0])),
                    "runnable_alone": bool(rng.integers(0, 2)),
                }
            )
    return pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)


def partitions(result):
    """Per step n: the set of groups, each a frozenset of feeders."""
    found = {}
    for step, labels in zip(result["steps"], result["labels"]):
        groups = {}
        for name, stratum, label in zip(result["names"], result["strata"], labels):
            groups.setdefault((stratum, int(label)), set()).add(name)
        found[step["n"]] = {frozenset(value) for value in groups.values()}
    return found


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_centre_gives_the_rows_of_today(rule):
    strata, _, _ = rep.prepare(table(), GROUPS_RULE, "all")
    before = rep.sequence(strata, rule, 300)
    after = rep.sequence(strata, rule, 300, "centre", AVERAGE)
    assert rep.group_table(before["rows"]).equals(rep.group_table(after["rows"]))
    assert rep.step_table(before["steps"]).equals(rep.step_table(after["steps"]))
    assert all(np.array_equal(a, b) for a, b in zip(before["labels"], after["labels"]))


def test_average_feeder_target_by_hand():
    features = table()
    strata, _, _ = rep.prepare(features, GROUPS_RULE, "all")
    stratum = next(s for s in strata if s.name == "C")
    assert stratum.count == 3
    rows = features[features["stratum"] == "C"]
    load, customers = rows["load_kw"].sum(), rows["customers"].sum()
    by_hand = {
        "customers": customers / 3,
        "load_kw": load / 3,
        "kw_per_customer": load / customers,
        "com_kw_share": (rows["com_kw_share"] * rows["load_kw"]).sum() / load,
        "line_km": rows["line_km"].sum() / 3,
        "extent_km": rows["extent_km"].sum() / 3,
        "service_transformers": rows["service_transformers"].sum() / 3,
        "kva_per_kw": rows["service_kva"].sum() / load,
        "primary_kv": rows["primary_kv"].sum() / 3,
    }
    expected = []
    for spec in NINE:
        column = spec[len("log("):-len(")")] if spec.startswith("log(") else spec
        frame = features[column].to_numpy(dtype="float64")
        value = by_hand[column]
        if spec.startswith("log("):
            frame, value = np.log1p(frame), np.log1p(value)
        expected.append((value - frame.mean()) / frame.std())
    found = rep.target_point(stratum, np.arange(3), "average_feeder", AVERAGE)
    assert found == pytest.approx(np.array(expected), rel=1e-12, abs=1e-12)


def test_group_of_one_feeder_has_its_own_point():
    strata, _, _ = rep.prepare(table(), GROUPS_RULE, "all")
    single = next(s for s in strata if s.count == 1)
    for stratum in (single, strata[0]):
        for choice in rep.REPRESENTATIVE_CHOICES:
            target = rep.target_point(stratum, np.array([0]), choice, AVERAGE)
            assert np.array_equal(target, stratum.points[0])


@pytest.mark.parametrize("rule", rep.ORDER_RULES)
def test_choice_moves_representatives_never_splits(rule):
    strata, _, _ = rep.prepare(table(), GROUPS_RULE, "all")
    found = {choice: rep.sequence(strata, rule, 300, choice, AVERAGE) for choice in rep.REPRESENTATIVE_CHOICES}
    first = partitions(found["centre"])
    for choice, result in found.items():
        assert partitions(result) == first, choice
        codes = scout.partition_codes(result)
        assert all(np.array_equal(a, b) for a, b in zip(codes, scout.partition_codes(found["centre"])))
    chosen = {
        choice: rep.group_table(result["rows"]).groupby("n")["representative"].agg(set)
        for choice, result in found.items()
    }
    assert any((chosen[choice] != chosen["centre"]).any() for choice in chosen)


@pytest.mark.parametrize("choice", rep.REPRESENTATIVE_CHOICES)
def test_weights_nesting_and_zero_errors_for_every_choice(choice):
    features = table()
    built = rep.build(features, GROUPS_RULE, BLOCK, "AREA", choice, AVERAGE)
    rows = built["representatives"]
    product = (rows["weight"] * rows["representative_load_kw"]).groupby([rows["rule"], rows["n"], rows["stratum"]]).sum()
    load = built["in_grouping"].groupby("stratum")["load_kw"].sum()
    for (_, _, stratum), value in product.items():
        assert value == pytest.approx(load[stratum], rel=1e-12)
    for _, part in rows.groupby("rule"):
        sets = [set(at["representative"].dropna()) for _, at in part.groupby("n", sort=True)]
        assert all(a <= b for a, b in zip(sets, sets[1:]))
    new = rows[rows["since_n"] == rows["n"]]
    assert (new["representative"] == new["nearest_feeder"]).all()
    errors = built["errors"]
    last = errors["n"] == built["counts"]["in_grouping"]
    assert last.any() and (errors.loc[last, "error"].abs() < 1e-12).all()
    assert all(rep.gates(built, GROUPS_RULE, BLOCK).values())


def test_unknown_choice_stops_and_extra_features_reach_prepare():
    with pytest.raises(ValueError):
        rep.check_settings({**BLOCK, "representative_choice": "biggest"})
    rep.check_settings(BLOCK)
    strata, _, _ = rep.prepare(table(), GROUPS_RULE, "all")
    with pytest.raises(ValueError):
        rep.sequence(strata, "ward_drop", 300, "biggest")
    features = table()
    plain, _, _ = rep.prepare(features, GROUPS_RULE, "all")
    extended, _, _ = rep.prepare(features, GROUPS_RULE, "all", extra_features=["log(regulators)"])
    assert extended[0].scaling.specs == NINE + ["log(regulators)"]
    assert plain[0].points.shape[1] == len(NINE) and extended[0].points.shape[1] == len(NINE) + 1
    column = np.log1p(features.sort_values("feeder")["regulators"].to_numpy(dtype="float64"))
    scaled = (column - column.mean()) / column.std()
    own = features.sort_values("feeder").reset_index(drop=True)
    inside = own["stratum"] == extended[0].name
    assert extended[0].points[:, -1] == pytest.approx(scaled[inside.to_numpy()], rel=1e-12)
    assert np.array_equal(extended[0].points[:, :-1], plain[0].points)


def test_average_rule_must_cover_every_feature_column():
    with pytest.raises(ValueError):
        rep.check_average_rule({**AVERAGE, "mean": ["customers"]}, NINE)
    with pytest.raises(ValueError):
        rep.check_average_rule({**AVERAGE, "median": []}, NINE)
    rep.check_average_rule(AVERAGE, NINE + ["log(regulators)"])


def test_scout_settings_of_the_setup_are_known():
    setup = config.load_setup()
    scout.check_scout(setup["representatives_scout"], setup["representatives"], setup["groups"])
    # The choice of record is the load-weighted centre (decision K78); the code default stays that of K76.
    assert setup["representatives"]["representative_choice"] == "load_weighted_centre"
    assert rep.DEFAULT_CHOICE == "centre"
    with pytest.raises(ValueError):
        scout.check_scout({**setup["representatives_scout"], "choices": ["biggest"]}, setup["representatives"],
                          setup["groups"])


def test_step_rows_count_weights_and_strata_with_one_group():
    features = table()
    built = rep.build(features, GROUPS_RULE, BLOCK, "AREA")
    rule = "ward_drop"
    part = built["representatives"][built["representatives"]["rule"] == rule]
    errors = built["errors"][built["errors"]["rule"] == rule]
    steps = built["steps"][built["steps"]["rule"] == rule]
    reference = {int(n): set(at["representative"].dropna()) for n, at in part.groupby("n")}
    found = scout.step_rows(part, steps, errors, len(built["in_grouping"]), "AREA", BLOCK["check_quantities"], reference)
    first = found.iloc[0]
    assert first["n"] == 4 and first["strata_one_group"] == 4
    assert first["strata_one_group_names"] == "A;B;C;D"
    assert (found["shared_with_reference"] == found["n"]).all()
    expected = part.groupby("n")["weight"].sum() / len(built["in_grouping"])
    assert found.set_index("n")["weights_over_feeders"].to_numpy() == pytest.approx(expected.to_numpy())
    assert found["strata_one_group"].iloc[-1] == 1
    repick = scout.repick_rows(part, errors, built["in_grouping"], "AREA", BLOCK["check_quantities"], [5, 8])
    assert list(repick["n"]) == [5, 8]
    assert (repick["kept_not_target"] >= 0).all()
