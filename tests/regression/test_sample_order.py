"""Sample order and match list (decision K80), on small made-up feeder tables."""

import numpy as np
import pandas as pd

from dxsew.frame import representatives as rep
from dxsew.frame import sample_order as so


def feeders(sizes=(("B", 9), ("A", 12), ("C", 4)), seed=4):
    rng = np.random.default_rng(seed)
    rows = [
        {"feeder": f"{stratum.lower()}--f{i:02d}", "stratum": stratum, "load_kw": float(rng.uniform(1.0, 9.0)),
         "customers": int(rng.integers(1, 99)), "runnable_alone": bool(rng.integers(0, 2))}
        for stratum, count in sizes
        for i in range(count)
    ]
    return pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)


def test_order_is_a_permutation_of_each_stratum():
    table = feeders()
    order = so.sample_order(table, 9)
    assert list(order.columns) == so.ORDER_COLUMNS
    for name, part in table.groupby("stratum"):
        mine = order[order["stratum"] == name]
        assert sorted(mine["feeder"]) == sorted(part["feeder"])
        assert list(mine["rank"]) == list(range(1, len(part) + 1))
    assert list(order["stratum"].unique()) == ["A", "B", "C"]


def test_order_follows_the_seed_plus_the_stratum_position():
    table = feeders()
    order = so.sample_order(table, 9)
    names = sorted(table.loc[table["stratum"] == "B", "feeder"])
    expected = [names[i] for i in np.random.default_rng(9 + 1).permutation(len(names))]
    assert list(order.loc[order["stratum"] == "B", "feeder"]) == expected


def test_order_repeats_and_another_stratum_does_not_move_it():
    table = feeders()
    first = so.sample_order(table, 9)
    pd.testing.assert_frame_equal(first, so.sample_order(table.sample(frac=1.0, random_state=1), 9))
    added = pd.concat([table, feeders(sizes=(("D", 6),), seed=8)], ignore_index=True)
    changed = pd.concat([table[table["stratum"] != "C"], feeders(sizes=(("C", 7),), seed=5)], ignore_index=True)
    for other in (added, changed):
        found = so.sample_order(other, 9)
        for name in ("A", "B"):
            mine = first[first["stratum"] == name].reset_index(drop=True)
            pd.testing.assert_frame_equal(found[found["stratum"] == name].reset_index(drop=True), mine)
    assert not so.sample_order(table, 10).equals(first)


def test_first_of_order_takes_all_of_a_small_stratum():
    order = so.sample_order(feeders(), 9)
    first = so.first_of_order(order, 5)
    assert first.groupby("stratum").size().to_dict() == {"A": 5, "B": 5, "C": 4}


def test_match_list_roles_and_order():
    chosen = pd.DataFrame({"feeder": ["r2", "r1", "s3"], "stratum": ["A", "B", "B"], "since_n": [2, 2, 4]})
    chosen = chosen.sort_values(["since_n", "feeder"]).reset_index(drop=True)
    sample = pd.DataFrame({"stratum": ["B", "A", "B"], "rank": [2, 1, 1], "feeder": ["s3", "s1", "s2"]})
    runnable = pd.Series({"r1": True, "r2": False, "s1": True, "s2": False, "s3": True})
    found = so.match_list(chosen, sample, runnable)
    assert list(found.columns) == so.MATCH_COLUMNS
    assert list(found["feeder"]) == ["r1", "r2", "s3", "s1", "s2"]
    assert list(found["role"]) == ["candidate", "candidate", "both", "sample", "sample"]
    assert found["rank"].isna().tolist() == [True, True, False, False, False]
    assert found["since_n"].isna().tolist() == [False, False, False, True, True]
    assert found.loc[2, "rank"] == 2 and found.loc[2, "since_n"] == 4
    assert list(found["runnable_alone"]) == [True, False, True, True, False]


def test_candidates_and_in_grouping_from_a_sequence():
    table = feeders()
    table["customers"] = table["customers"] * 10
    table["com_kw_share"] = np.linspace(0.0, 1.0, len(table))
    groups_rule = {"features": ["log(customers)", "log(load_kw)", "com_kw_share"], "scaling": "frame_z"}
    strata, _, _ = rep.prepare(table, groups_rule, "all")
    result = rep.sequence(strata, "ward_drop", 6, "load_weighted_centre")
    rows, labels = rep.group_table(result["rows"]), rep.label_table("ward_drop", result)
    inside = so.in_grouping_feeders(labels, "ward_drop")
    assert sorted(inside["feeder"]) == sorted(table["feeder"])
    chosen = so.candidates(rows, "ward_drop", 300)
    assert len(chosen) == 6
    assert list(chosen["since_n"]) == sorted(chosen["since_n"])
    assert chosen["since_n"].iloc[0] == 3 and (chosen["since_n"] == 3).sum() == 3
    assert len(so.candidates(rows, "ward_drop", 4)) == 4
