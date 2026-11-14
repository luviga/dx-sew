"""EIA-861 veto of a county (decision K57), on small hand-made sheets."""

import zipfile

import pandas as pd
import pytest
from shapely.geometry import box

from dxsew.texas import ba, geo
from dxsew.texas.config import load_sample_settings
from dxsew.texas.sample import geography_columns, veto_counties, veto_for, veto_removed

SOURCE = {
    "state": "ZZ",
    "rto": "MAIN",
    "veto": True,
    "veto_ignore_marks": ["Other"],
    "territory_file": "territory.xlsx",
    "territory_sheet": "Counties_States",
    "utility_file": "utility.xlsx",
    "utility_sheet": "States",
    "utility_header_row": 1,
    "rto_group": "Operating in these RTOs",
}

# Utilities of state ZZ and their marks in the RTO group (MAIN, NEXT, FAR, Other).
UTILITIES = {
    1: ("Next One", ["NEXT"]),
    2: ("Next Two", ["NEXT"]),
    3: ("Main Coop", ["MAIN"]),
    4: ("No Row City", None),  # no utility data row in ZZ
    5: ("Blank Muni", []),
    6: ("Other Only", ["Other"]),
    7: ("Next And Other", ["NEXT", "Other"]),
    8: ("Far Power", ["FAR"]),
}

# County: utilities serving it.
SERVED = {
    "Alpha": [1, 2],  # vetoed: every utility marks NEXT only
    "Beta": [1, 3],  # one utility marks MAIN
    "Gamma": [1, 4],  # one utility has no utility data row
    "Delta": [1, 5],  # one utility marks no RTO
    "Epsilon": [1, 6],  # one utility marks only Other
    "Zeta": [7, 8],  # vetoed: NEXT beside Other counts as NEXT
    "Eta": [3, 4, 5, 6],  # first reason that applies: reports_rto
    "Theta": [4, 5],  # first reason that applies: missing_row
    "Iota": [3],  # MAIN only
}


@pytest.fixture
def eia_zip(tmp_path):
    rows = [(2018, number, UTILITIES[number][0], "ZZ", county) for county, numbers in SERVED.items() for number in numbers]
    rows.append((2018, 1, "Next One", "YY", "Iota"))  # another state, left out
    territory = pd.DataFrame(rows, columns=["Data Year", "Utility Number", "Utility Name", "State", "County"])
    marks = ["MAIN", "NEXT", "FAR", "Other"]
    group = ["Utility Characteristics", None, None, None, "Operating in these RTOs", None, None, None, "Other Activities"]
    names = ["Data Year", "Utility Number", "Utility Name", "State", *marks, "Bundled"]
    utility_rows = [group, names]
    for number, (name, found) in UTILITIES.items():
        if found is None:
            utility_rows.append([2018, number, name, "YY", "Y", None, None, None, "Y"])
            continue
        utility_rows.append([2018, number, name, "ZZ", *["Y" if m in found else "N" for m in marks], "Y"])
    territory.to_excel(tmp_path / "territory.xlsx", sheet_name="Counties_States", index=False)
    pd.DataFrame(utility_rows).to_excel(tmp_path / "utility.xlsx", sheet_name="States", index=False, header=False)
    path = tmp_path / "eia.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(tmp_path / "territory.xlsx", "territory.xlsx")
        archive.write(tmp_path / "utility.xlsx", "utility.xlsx")
    return path


def reasons(table: pd.DataFrame) -> dict:
    return dict(zip(table["county_eia"], table["reason"]))


def test_veto_reasons_per_county(eia_zip):
    table = geo.county_veto(eia_zip, SOURCE)
    assert reasons(table) == {
        "Alpha": geo.VETOED,
        "Beta": geo.REPORTS_RTO,
        "Delta": geo.NO_RTO_MARKED,
        "Epsilon": geo.IGNORED_MARK_ONLY,
        "Eta": geo.REPORTS_RTO,
        "Gamma": geo.MISSING_ROW,
        "Iota": geo.REPORTS_RTO,
        "Theta": geo.MISSING_ROW,
        "Zeta": geo.VETOED,
    }
    vetoed = dict(zip(table["county_eia"], table["vetoed"]))
    assert [county for county, value in vetoed.items() if value] == ["Alpha", "Zeta"]


def test_veto_table_lists_utilities_and_marks(eia_zip):
    table = geo.county_veto(eia_zip, SOURCE).set_index("county_eia")
    assert table.loc["Gamma", "utility_marks"] == "1 Next One [NEXT]; 4 No Row City [no utility data row]"
    assert table.loc["Delta", "utility_marks"] == "1 Next One [NEXT]; 5 Blank Muni [no RTO marked]"
    assert table.loc["Zeta", "utility_marks"] == "7 Next And Other [NEXT, Other]; 8 Far Power [FAR]"
    assert table.loc["Gamma", ["utilities", "utilities_with_row"]].tolist() == [2, 1]
    assert table.loc["Iota", "utilities"] == 1  # the row of state YY is left out


def test_veto_follows_the_rto_setting(eia_zip):
    table = geo.county_veto(eia_zip, {**SOURCE, "rto": "NEXT"})
    found = reasons(table)
    assert found["Alpha"] == geo.REPORTS_RTO
    assert found["Zeta"] == geo.REPORTS_RTO
    assert found["Iota"] == geo.VETOED  # MAIN only, another RTO than NEXT
    assert found["Epsilon"] == geo.REPORTS_RTO


def test_veto_follows_the_ignore_setting(eia_zip):
    found = reasons(geo.county_veto(eia_zip, {**SOURCE, "veto_ignore_marks": []}))
    assert found["Epsilon"] == geo.VETOED  # Other now counts as another RTO
    assert found["Delta"] == geo.NO_RTO_MARKED


def test_veto_leaves_membership_and_rtos_unchanged(eia_zip):
    rtos = geo.utility_rtos(eia_zip, SOURCE)
    assert rtos[7] == frozenset({"NEXT", "Other"})
    assert 4 not in rtos.index
    table, counts = geo.membership(eia_zip, SOURCE)
    status = dict(zip(table["county_eia"], table["status"]))
    assert status["Alpha"] == geo.OUTSIDE
    assert status["Beta"] == geo.PARTIAL
    assert status["Theta"] == geo.OUTSIDE  # the utility with no row is left out, as before
    assert counts["left_out_utilities"] == ["4 No Row City"]


def test_veto_reason_rule_directly():
    no_row = None
    assert geo.veto_reason([frozenset({"B"}), frozenset({"C", "Other"})], "A", ["Other"]) == geo.VETOED
    assert geo.veto_reason([frozenset({"A", "B"}), frozenset({"B"})], "A", ["Other"]) == geo.REPORTS_RTO
    assert geo.veto_reason([frozenset({"A"}), no_row], "A", ["Other"]) == geo.REPORTS_RTO
    assert geo.veto_reason([frozenset({"B"}), no_row], "A", ["Other"]) == geo.MISSING_ROW
    assert geo.veto_reason([frozenset(), frozenset({"Other"})], "A", ["Other"]) == geo.NO_RTO_MARKED
    assert geo.veto_reason([frozenset({"Other"})], "A", ["Other"]) == geo.IGNORED_MARK_ONLY


# Counties of a made-up state, side one degree; Kappa has no EIA-861 rows.
COUNTIES = [
    geo.County("99001", "Alpha", box(0, 0, 1, 1)),
    geo.County("99003", "Beta", box(1, 0, 2, 1)),
    geo.County("99005", "Kappa", box(2, 0, 3, 1)),
]
ZONES = {"99001": "Cold", "99003": "Cold", "99005": "Hot"}
# MAIN-X over all three counties, OTHER beyond them.
AREAS = [ba.Area("MAIN-X", box(0, 0, 3, 1)), ba.Area("OTHER", box(3, 0, 5, 1))]


def hand_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "subregion": ["P1R", "P1R", "P2U", "P2U", "P3R"],
            "kind": ["R", "R", "U", "U", "R"],
            "substation": ["a", "b", "c", "d", "e"],
            "centroid_x": [0.5, 0.6, 1.5, 2.5, 4.0],
            "centroid_y": [0.5, 0.5, 0.5, 0.5, 0.5],
            "feeders": [2, 3, 4, 5, 6],
            "loads": [10, 20, 30, 40, 50],
        }
    )


def test_frame_column_and_membership(eia_zip):
    status, _ = geo.membership(eia_zip, SOURCE)
    veto = veto_for(SOURCE, eia_zip, COUNTIES, status)
    result = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], AREAS, "MAIN", veto)
    # a and b lie in vetoed Alpha, c in Beta, d in Kappa (no EIA-861 rows), e in no county.
    assert result["county_veto_eia861"].tolist() == [True, True, False, False, None]
    assert result["in_frame"].tolist() == [False, False, True, True, False]
    assert result["stratum"].isna().tolist() == [True, True, False, False, True]
    assert list(result.columns).index("county_veto_eia861") == list(result.columns).index("county_status_eia861") + 1

    removed = veto_removed(result, "MAIN")
    assert removed[["substation", "county", "ba_label", "stratum", "feeders", "loads"]].values.tolist() == [
        ["a", "Alpha", "MAIN-X", "R|Cold", 2, 10],
        ["b", "Alpha", "MAIN-X", "R|Cold", 3, 20],
    ]
    table = veto_counties(result, veto, "MAIN").set_index("county_eia")
    assert table.loc["Alpha", ["substations_in_ba", "feeders_in_ba", "loads_in_ba", "frame_substations_removed"]].tolist() == [2, 5, 30, 2]
    assert table.loc["Beta", ["substations_in_ba", "frame_substations_removed"]].tolist() == [1, 0]
    assert table.loc["Alpha", "county_status_eia861"] == geo.OUTSIDE
    assert table.loc["Alpha", "county_geoid"] == "99001"
    assert pd.isna(table.loc["Gamma", "county_geoid"])  # no Census county of that name


def test_veto_only_removes(eia_zip):
    """A vetoed county outside the map polygons stays outside; nothing is added."""
    status, _ = geo.membership(eia_zip, SOURCE)
    veto = veto_for(SOURCE, eia_zip, COUNTIES, status)
    areas = [ba.Area("OTHER", box(0, 0, 5, 1))]
    without = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], areas, "MAIN")
    with_veto = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], areas, "MAIN", veto)
    assert not without["in_frame"].any()
    assert not with_veto["in_frame"].any()
    assert veto_removed(with_veto, "MAIN").empty


def test_veto_off_leaves_the_frame_unchanged(eia_zip):
    status, _ = geo.membership(eia_zip, SOURCE)
    assert veto_for({**SOURCE, "veto": False}, eia_zip, COUNTIES, status) is None
    assert veto_for({key: value for key, value in SOURCE.items() if key != "veto"}, eia_zip, COUNTIES, status) is None
    before = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], AREAS, "MAIN")
    off = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], AREAS, "MAIN", None)
    pd.testing.assert_frame_equal(before, off)
    assert "county_veto_eia861" not in off
    assert off["in_frame"].tolist() == [True, True, True, True, False]
    assert veto_removed(off, "MAIN").empty


def test_frame_with_no_county_vetoed_keeps_membership(eia_zip):
    status, _ = geo.membership(eia_zip, SOURCE)
    veto = veto_for(SOURCE, eia_zip, COUNTIES, status).assign(vetoed=False)
    before = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], AREAS, "MAIN")
    kept = geography_columns(hand_frame(), COUNTIES, status, ZONES, [], AREAS, "MAIN", veto)
    pd.testing.assert_frame_equal(before, kept.drop(columns="county_veto_eia861"))


def test_settings_turn_the_veto_on_with_other_ignored():
    balancing = load_sample_settings()["geography"]["balancing_area"]
    assert balancing["veto"] is True
    assert balancing["veto_ignore_marks"] == ["Other"]
    assert balancing["rto"] == "ERCOT"
