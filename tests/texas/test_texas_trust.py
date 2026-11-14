"""Trust checks of the balancing area map, on small hand-made inputs."""

from collections import Counter

import pandas as pd
import pytest
from shapely.geometry import box

from dxsew.texas import ba, trust

ENDINGS = ["", "Coast", "East", "Far-West", "North", "North-Central"]
AREAS = [
    ba.Area("AREA-C", box(0, 0, 1, 1)),
    ba.Area("AREA-NC", box(1, 0, 2, 1)),
    ba.Area("AREA-FW", box(2, 0, 3, 1)),
    ba.Area("OTHER", box(3.1, 0, 4, 1)),
]


def frame_of(rows):
    columns = ["subregion", "substation", "county", "county_geoid", "ba_label", "loads", "feeders",
               "centroid_x", "centroid_y", "in_frame", "county_status_eia861"]
    return pd.DataFrame(rows, columns=columns)


FRAME = frame_of([
    ("P1U", "a", "Alpha", "99001", "AREA-C", 10, 1, 0.5, 0.5, True, "AREA"),
    ("P1U", "b", "Alpha", "99001", "AREA-NC", 20, 2, 1.5, 0.5, True, "AREA"),
    ("P2R", "c", "Beta", "99003", "AREA-FW", 30, 3, 2.99, 0.5, True, "partial"),
    ("P2R", "d", "Beta", "99003", "OTHER", 40, 1, 3.5, 0.5, False, "partial"),
    ("P3U", "e", "Gamma", "99005", "none", 50, 1, 9.0, 0.5, False, "outside"),
])


def test_initials_and_pairs():
    assert trust.initials("North-Central") == "NC"
    assert trust.initials("Far-West") == "FW"
    pairs = trust.pair_zones([a.ba for a in AREAS], ENDINGS, "AREA")
    assert pairs == {"AREA-C": "Coast", "AREA-FW": "Far-West", "AREA-NC": "North-Central"}


def test_pairs_stop_on_a_polygon_with_no_ending():
    with pytest.raises(ValueError, match="AREA-X"):
        trust.pair_zones(["AREA-X"], ENDINGS, "AREA")
    with pytest.raises(ValueError, match="2 zone endings"):
        trust.pair_zones(["AREA-N"], ENDINGS + ["Nowhere"], "AREA")


def test_zone_counts_and_most_common():
    counts = trust.zone_counts(["res_kw_1-Coast_pu", "com_kw_2-Coast_pu", "res_kw_3_pu", "odd", None])
    assert counts == Counter({"Coast": 2, trust.NO_ENDING: 1, None: 2})
    assert trust.most_common(counts) == "Coast"
    assert trust.most_common(Counter({"East": 1, "Coast": 1})) == "Coast"
    assert trust.most_common(Counter({None: 3})) is None


def test_zone_agreement_table_and_summary():
    pairs = trust.pair_zones([a.ba for a in AREAS], ENDINGS, "AREA")
    counts = {
        ("P1U", "a"): Counter({"Coast": 9, "East": 1}),
        ("P1U", "b"): Counter({"North": 15, "North-Central": 5}),
        ("P2R", "c"): Counter({"Far-West": 30}),
    }
    agreement = trust.zone_agreement(FRAME, counts, pairs)
    assert agreement["substation"].tolist() == ["a", "b", "c"]
    assert agreement["agree"].tolist() == [True, False, True]
    assert agreement["loads_in_polygon_zone"].tolist() == [9, 5, 30]
    summary = trust.zone_summary(agreement)
    assert summary["share_substations_agree"] == pytest.approx(2 / 3)
    assert summary["share_loads_agree"] == pytest.approx(40 / 60)
    assert summary["share_loads_own_ending_in_polygon_zone"] == pytest.approx(44 / 60)
    table = trust.zone_table(agreement, ENDINGS, pairs)
    assert table.shape == (len(ENDINGS), 1 + len(pairs))
    assert table["most_common_ending"].tolist()[0] == trust.NO_ENDING
    assert table.set_index("most_common_ending").loc["North", "North-Central"] == 1
    assert table.drop(columns="most_common_ending").to_numpy().sum() == 3


def test_county_shares_and_puct():
    shares = trust.county_shares(FRAME, "AREA")
    assert shares.set_index("county")["inside"].to_dict() == {"Alpha": 2, "Beta": 1, "Gamma": 0}
    assert shares.set_index("county")["share_inside"]["Beta"] == pytest.approx(0.5)
    puct = pd.DataFrame({"county": ["Alpha", "Beta", "Gamma"], "status": ["not served", "served", "served"]})
    result = trust.puct_compare(shares, puct).set_index("county")
    # Alpha: not served with substations inside; Gamma: served with none inside;
    # Beta: served with exactly half inside, not listed.
    assert result["listed"].to_dict() == {"Alpha": True, "Beta": False, "Gamma": True}


def test_eia861_groups():
    shares = trust.county_shares(FRAME, "AREA")
    territory = pd.DataFrame({
        "Utility Number": [1, 1, 2, 3],
        "Utility Name": ["Inside Coop", "Inside Coop", "Outside Power", "Blank Muni"],
        "County": ["Alpha", "Beta", "Gamma", "Gamma"],
    })
    rtos = pd.Series({1: frozenset({"AREA"}), 2: frozenset({"ELSE"}), 3: frozenset()}, dtype=object)
    table = trust.eia861_check(shares, territory, rtos, "AREA")
    assert table[["group", "county", "substations", "inside", "pulls_away"]].values.tolist() == [
        ["only_AREA", "Alpha", 2, 2, False],
        ["only_AREA", "Beta", 2, 1, True],
        ["only_other", "Gamma", 1, 0, False],
    ]
    summary = trust.eia861_summary(table).set_index("group")
    assert summary.loc["only_AREA", "share_inside"] == pytest.approx(3 / 4)
    assert summary.loc["only_other", "share_inside"] == 0


def test_edges():
    result = trust.edges(FRAME, AREAS, "AREA", limit_km=15.0)
    assert result["substation"].tolist() == ["a", "b", "c"]
    # c sits 0.11 degree of longitude west of OTHER, about 12 km at latitude 0.5.
    assert result["nearest_other_ba"].tolist() == ["OTHER", "OTHER", "OTHER"]
    assert result["km_to_other_ba"].iloc[2] == pytest.approx(0.11 * ba.KM_PER_DEGREE, rel=1e-3)
    assert result["near_edge"].tolist() == [False, False, True]
    assert trust.EDGE_KM == 10.0
