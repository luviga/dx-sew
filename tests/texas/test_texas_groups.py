"""Nested groups, weights and typical feeders, on a small made-up feature table."""

import math

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage

from dxsew.texas import groups

RULE = {
    "features": ["log(customers)", "kw_per_customer"],
    "scaling": "frame_z",
    "cap": 5,
    "small_share_report": [0.05, 0.2],
    "dial_targets": [0.5, 0.99],
    "starting_weight": "customer_share",
}


def table(rows):
    """Feature table rows: (stratum, feeder, customers, kw_per_customer, runnable_alone)."""
    frame = pd.DataFrame(rows, columns=["stratum", "feeder", "customers", "kw_per_customer", "runnable_alone"])
    frame["subregion"] = "P9U"
    frame["substation"] = frame["feeder"].str.split("--").str[0]
    return frame


ROWS = [
    ("A", "s1--f01", 10, 1.0, True),
    ("A", "s1--f02", 11, 1.1, True),
    ("A", "s1--f03", 12, 0.9, False),
    ("A", "s2--f04", 500, 5.0, True),
    ("A", "s2--f05", 520, 5.2, False),
    ("A", "s2--f06", 480, 4.8, True),
    ("A", "s3--f07", 100, 20.0, False),
    ("A", "s3--f08", 110, 21.0, False),
    ("B", "s4--f09", 30, 2.0, True),
    ("B", "s4--f10", 40, 3.0, True),
    ("B", "s4--f11", 3000, 9.0, True),
]


def test_explained_is_zero_with_one_group():
    points = np.array([[0.0, 1.0], [2.0, 3.0], [5.0, -1.0]])
    assert groups.explained(points, np.array([1, 1, 1])) == 0.0
    assert math.isclose(groups.explained(points, np.array([1, 2, 3])), 1.0)


def test_labels_are_nested_and_stable():
    points = np.array([[0.0], [0.1], [5.0], [5.2], [10.0], [10.4], [30.0]])
    names = [f"f{i}" for i in range(len(points))]
    labels = groups.stable_labels(linkage(points, method="ward"), 5, names)
    assert groups.nesting_ok(labels)
    for k, label in labels.items():
        assert sorted(np.unique(label)) == list(range(1, k + 1))
    for k in range(1, 5):
        moved = labels[k] != labels[k + 1]
        # only the feeders of one split group change number, and all take the new number
        assert len(np.unique(labels[k][moved])) <= 1
        assert set(labels[k + 1][moved]) <= {k + 1}


def test_nesting_check_finds_a_group_that_crosses():
    assert not groups.nesting_ok({1: np.array([1, 1, 1]), 2: np.array([1, 2, 2]), 3: np.array([1, 3, 1])})


def test_weights_sum_to_one_and_every_feeder_has_one_group():
    features = table(ROWS)
    result = groups.build(features, RULE)
    sums = result["group_table"].groupby(["stratum", "k"])["starting_weight"].sum()
    assert np.allclose(sums, 1.0, atol=1e-12)
    gates = groups.gates(result, features)
    assert all(gates.values()), gates
    at_one = result["explained"].query("k == 1")["explained"]
    assert (at_one == 0).all()
    # stratum B has 3 feeders, so its cap is 3
    assert result["explained"].query("stratum == 'B'")["k"].max() == 3


def test_typical_feeder_skips_a_flagged_feeder():
    rows = [
        ("A", "s1--f1", 100, 1.0, True),
        ("A", "s1--f2", 101, 1.01, False),  # the centre itself, but flagged
        ("A", "s1--f3", 102, 1.02, True),
    ]
    points = np.array([[0.0], [1.0], [2.5]])
    part = table(rows)
    _, group_table, _, _ = groups.group_stratum("A", part, points, ["x"], 1)
    row = group_table.iloc[0]
    # centre 3.5 / 3: f2 is nearest but flagged; f1 is nearer than f3
    assert row["nearest_feeder"] == "s1--f2"
    assert row["typical_feeder"] == "s1--f1"
    points = np.array([[0.0], [1.0], [2.0]])
    _, group_table, _, _ = groups.group_stratum("A", part, points, ["x"], 1)
    row = group_table.iloc[0]
    assert row["nearest_feeder"] == "s1--f2"
    assert row["nearest_distance"] == 0.0
    assert row["typical_feeder"] == "s1--f1"  # f1 and f3 tie at 1.0; the first name wins
    assert row["typical_distance"] == 1.0
    assert math.isclose(row["flagged_customer_share"], 101 / 303)


def test_group_without_a_runnable_feeder_has_no_typical_feeder():
    result = groups.build(table(ROWS), RULE)
    group_table = result["group_table"]
    lonely = group_table[group_table["typical_feeder"].isna()]
    assert len(lonely) >= 1
    assert (lonely["flagged_customer_share"] == 1.0).all()
    assert lonely["nearest_feeder"].notna().all()


def test_a_feeder_with_an_empty_feature_is_left_out():
    rows = ROWS + [("A", "s5--f12", 50, float("nan"), True)]
    result = groups.build(table(rows), RULE)
    assert result["left_out"]["feeder"].tolist() == ["s5--f12"]
    assert "s5--f12" not in set(result["groups"]["feeder"])


def test_dial_picks_the_smallest_k_reaching_the_target():
    explained = pd.DataFrame(
        {"stratum": ["A"] * 3 + ["B"] * 2, "k": [1, 2, 3, 1, 2], "explained": [0.0, 0.6, 0.9, 0.0, 0.4]}
    )
    group_table = pd.DataFrame(
        {"stratum": ["A", "A", "B", "B"], "k": [2, 2, 2, 2], "typical_feeder": ["x", None, "y", "z"]}
    )
    dial = groups.dial(explained, group_table, [0.5])
    a = dial[dial["stratum"] == "A"].iloc[0]
    b = dial[dial["stratum"] == "B"].iloc[0]
    total = dial[dial["stratum"] == "all"].iloc[0]
    assert (a["k"], a["reached"], a["typical_feeders"]) == (2, True, 1)
    assert (b["k"], b["reached"], b["typical_feeders"]) == (2, False, 2)
    assert (total["k"], total["typical_feeders"]) == (4, 3)


def test_check_set_candidates():
    features = pd.DataFrame(
        {
            "subregion": ["P9U"] * 3,
            "substation": ["a", "b", "b"],
            "stratum": ["A"] * 3,
            "customers": [1, 5, 6],
            "res_customers": [0, 4, 5],
            "com_customers": [1, 1, 1],
            "no_res": [True, False, False],
            "no_com": [False, False, False],
            "shared_shapes": [0, 0, 0],
            "shared_pairs_total": [1, 2, 2],
            "runnable_alone": [False, True, True],
        }
    )
    sample = pd.DataFrame({"subregion": ["P9U", "P9U"], "substation": ["a", "b"]})
    out = groups.check_set_candidates(features, sample).set_index("substation")
    assert not out.loc["a", "case_runnable"]
    assert out.loc["b", "case_runnable"]
    assert out.loc["b", "feeders"] == 2
