"""Frame veto for any balancing area with the two EIA-861 lookups (decisions K57, K58), on hand-made sheets."""

import itertools
import zipfile

import pandas as pd
import pytest
import shapefile
import yaml
from shapely.geometry import box

from dxsew import veto
from dxsew.texas import ba, geo
from dxsew.texas.config import find_configs, load_sample_settings
from dxsew.texas.sample import geography_columns, veto_for, veto_lookups_for

RTOS = ["CAISO", "ERCOT", "NYISO", "SPP", "Other"]
BASE = {
    "state": "ZZ",
    "veto": True,
    "veto_ignore_marks": ["Other"],
    "territory_file": "territory.xlsx",
    "territory_sheet": "Counties_States",
    "utility_file": "utility.xlsx",
    "utility_sheet": "States",
    "utility_header_row": 1,
    "rto_group": "Operating in these RTOs",
}
# The three names of each area (the polygon prefix is not used by the veto).
AREAS = {
    "ERCOT": {"rto": "ERCOT", "ba_code": "ERCO"},
    "CAISO": {"rto": "CAISO", "ba_code": "CISO"},
    "NYISO": {"rto": "NYISO", "ba_code": "NYIS"},
}
ERCOT_ON = {**BASE, **AREAS["ERCOT"], "veto_lookups": True}
ERCOT_OFF = {**BASE, **AREAS["ERCOT"], "veto_lookups": False}

# Utility: name, utility data rows {state: marks}, sales rows [(state, service type, code)], short form [(state, code)].
UTILITIES = {
    1: ("Spp Row", {"ZZ": ["SPP"]}, [("ZZ", "Bundled", "SWPP")], []),
    2: ("Main Row", {"ZZ": ["ERCOT"]}, [], []),
    3: ("Other State", {"YY": ["SPP"]}, [("ZZ", "Bundled", "ERCO")], []),  # lookup 1 wins over sales
    4: ("Other State Blank", {"YY": []}, [("ZZ", "Delivery", "SWPP")], []),  # lookup 1 empty, then lookup 2
    5: ("Blank Target", {"ZZ": []}, [("ZZ", "Bundled", "ERCO"), ("ZZ", "Energy", ".")], []),
    6: ("Blank Nonrto", {"ZZ": []}, [("ZZ", "Bundled", "LDWP")], []),
    7: ("Blank Both", {"ZZ": []}, [("ZZ", "Bundled", "ERCO"), ("ZZ", "Energy", "SWPP")], []),
    8: ("Nothing", {}, [], []),  # no row anywhere, no sales row
    9: ("Blank Nothing", {"ZZ": []}, [("ZZ", "Bundled", None)], []),  # empty code only
    10: ("Sales Elsewhere", {"ZZ": []}, [("YY", "Bundled", "CISO")], []),
    11: ("Short Form", {}, [], [("ZZ", "NYIS")]),
    12: ("Other Only", {"ZZ": ["Other"]}, [("ZZ", "Bundled", "ERCO")], []),  # a row with a mark keeps it
}
SERVED = {
    "A": [1, 3],
    "B": [1, 4],
    "C": [1, 5],
    "D": [1, 6],
    "E": [1, 7],
    "F": [1, 8],
    "G": [1, 9],
    "H": [1, 10],
    "I": [1, 11],
    "J": [2],
    "K": [1, 12],
    "L": [1],
}
LOOKED_UP = [3, 4, 5, 6, 7, 8, 9, 10, 11]


def sales_sheet(rows) -> pd.DataFrame:
    classes = ["RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL", "TRANSPORTATION"]
    group = ["Utility Characteristics", *[None] * 7, *classes]
    measure = [*[None] * 8, *["Sales"] * 4]
    names = ["Data Year", "Utility Number", "Utility Name", "Part", "Service Type", "State", "Ownership", "BA Code"]
    body = [[2018, n, name, "A", service, state, "Cooperative", code, 1, 1, 1, 0] for n, name, state, service, code in rows]
    return pd.DataFrame([group, measure, names + ["Megawatthours"] * 4, *body])


def write_zip(path, utilities, served, state_name="ZZ"):
    """EIA-861 sheets of the utilities; the state ZZ of the tables is written as ``state_name``."""
    folder = path.parent
    name_of = {"ZZ": state_name}
    territory = pd.DataFrame(
        [(2018, n, utilities[n][0], state_name, county) for county, numbers in served.items() for n in numbers],
        columns=["Data Year", "Utility Number", "Utility Name", "State", "County"],
    )
    group = ["Utility Characteristics", None, None, None, "Operating in these RTOs", *[None] * (len(RTOS) - 1), "Other Activities"]
    names = ["Data Year", "Utility Number", "Utility Name", "State", *RTOS, "Bundled"]
    utility_rows, sales_rows, short_rows = [group, names], [], []
    for n, (name, rows, sales, short) in utilities.items():
        for state, marks in rows.items():
            utility_rows.append([2018, n, name, name_of.get(state, state), *["Y" if m in marks else "N" for m in RTOS], "Y"])
        sales_rows += [(n, name, name_of.get(state, state), service, code) for state, service, code in sales]
        short_rows += [(2018, n, name, name_of.get(state, state), code) for state, code in short]
    territory.to_excel(folder / "territory.xlsx", sheet_name="Counties_States", index=False)
    pd.DataFrame(utility_rows).to_excel(folder / "utility.xlsx", sheet_name="States", index=False, header=False)
    sales_sheet(sales_rows).to_excel(folder / "sales.xlsx", sheet_name="States", index=False, header=False)
    pd.DataFrame(short_rows, columns=["Data Year", "Utility Number", "Utility Name", "State", "BA Code"]).to_excel(
        folder / "short.xlsx", sheet_name="861S", index=False
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(folder / "territory.xlsx", "territory.xlsx")
        archive.write(folder / "utility.xlsx", "utility.xlsx")
        archive.write(folder / "sales.xlsx", "Sales_Ult_Cust_2018.xlsx")
        archive.write(folder / "short.xlsx", "Short_Form_2018.xlsx")
    return path


@pytest.fixture
def eia_zip(tmp_path):
    return write_zip(tmp_path / "eia.zip", UTILITIES, SERVED)


def column(table: pd.DataFrame, name: str) -> dict:
    return dict(zip(table["county_eia"], table[name]))


def test_lookups_off_is_k57_exactly(eia_zip):
    off = veto.county_veto(eia_zip, ERCOT_OFF, 2018)
    k57 = veto.veto_table(veto.territory_rows(eia_zip, ERCOT_OFF), veto.utility_rtos(eia_zip, ERCOT_OFF), "ERCOT", ["Other"])
    pd.testing.assert_frame_equal(off[k57.columns], k57)
    assert off["reason"].tolist() == off["reason_k57"].tolist()
    assert off["vetoed"].tolist() == off["vetoed_k57"].tolist()
    # The J3d-style cases: a missing row, a row with no mark, Other alone, SPP only.
    assert column(off, "reason") == {
        "A": veto.MISSING_ROW, "B": veto.MISSING_ROW, "C": veto.NO_RTO_MARKED, "D": veto.NO_RTO_MARKED,
        "E": veto.NO_RTO_MARKED, "F": veto.MISSING_ROW, "G": veto.NO_RTO_MARKED, "H": veto.NO_RTO_MARKED,
        "I": veto.MISSING_ROW, "J": veto.REPORTS_RTO, "K": veto.IGNORED_MARK_ONLY, "L": veto.VETOED,
    }
    # The default of veto_lookups is off, and the sales file is then not needed.
    without_key = {k: v for k, v in ERCOT_OFF.items() if k != "veto_lookups"}
    pd.testing.assert_frame_equal(veto.county_veto(eia_zip, without_key), off)


def test_geo_keeps_the_moved_names():
    assert geo.veto_reason is veto.veto_reason
    assert geo.veto_table is veto.veto_table
    assert geo.utility_rtos is veto.utility_rtos
    assert (geo.VETOED, geo.REPORTS_RTO, geo.MISSING_ROW, geo.NO_RTO_MARKED, geo.IGNORED_MARK_ONLY) == (
        veto.VETOED, veto.REPORTS_RTO, veto.MISSING_ROW, veto.NO_RTO_MARKED, veto.IGNORED_MARK_ONLY,
    )


def test_readers_of_marks_and_sales_codes(eia_zip):
    marks = veto.utility_marks_all_states(eia_zip, ERCOT_ON)
    assert marks[3] == {"YY": frozenset({"SPP"})}
    assert marks[5] == {"ZZ": frozenset()}
    assert 8 not in marks
    assert veto.utility_rtos(eia_zip, ERCOT_ON)[1] == frozenset({"SPP"})
    assert 3 not in veto.utility_rtos(eia_zip, ERCOT_ON).index
    codes = veto.sales_ba_codes(eia_zip, 2018)
    assert codes[4] == {"ZZ": frozenset({"SWPP"})}  # a Delivery row is kept
    assert codes[5] == {"ZZ": frozenset({"ERCO"})}  # the code "." is dropped
    assert 9 not in codes  # an empty code only
    assert codes[11] == {"ZZ": frozenset({"NYIS"})}  # from the short form file


def test_lookup_1_takes_another_state_row(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert column(on, "reason")["A"] == veto.VETOED
    assert "3 Other State [SPP via YY row]" in column(on, "utility_marks")["A"]
    answer = veto.resolve_marks([3], "ZZ", "ERCOT", "ERCO", {}, {3: {"YY": frozenset({"SPP"})}}, {3: {"ZZ": frozenset({"ERCO"})}}, True)[0]
    assert (answer.marks, answer.source, answer.lookup) == (frozenset({"SPP"}), "other_state_row:YY", "lookup_1")


def test_lookup_1_with_no_mark_goes_on_to_lookup_2(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert column(on, "reason")["B"] == veto.VETOED
    assert "4 Other State Blank [BA SWPP via sales rows]" in column(on, "utility_marks")["B"]


def test_lookup_2_with_the_target_code_counts_as_the_target(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert column(on, "reason")["C"] == veto.REPORTS_RTO
    assert "5 Blank Target [ERCOT via sales rows, BA ERCO]" in column(on, "utility_marks")["C"]


def test_lookup_2_with_a_non_rto_code_counts_as_another_area(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert column(on, "reason")["D"] == veto.VETOED
    assert "6 Blank Nonrto [BA LDWP via sales rows]" in column(on, "utility_marks")["D"]
    answer = veto.resolve_marks([6], "ZZ", "ERCOT", "ERCO", {6: frozenset()}, {}, {6: {"ZZ": frozenset({"LDWP"})}}, True)[0]
    assert answer.marks == frozenset({"BA:LDWP"})
    assert answer.source == "sales_ba_code:LDWP"


def test_lookup_2_with_the_target_and_another_code_counts_as_the_target(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert column(on, "reason")["E"] == veto.REPORTS_RTO


def test_lookup_2_reads_other_states_only_without_codes_in_the_state(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert "10 Sales Elsewhere [BA CISO via sales rows, other states]" in column(on, "utility_marks")["H"]
    in_state = veto.resolve_marks([1], "ZZ", "ERCOT", "ERCO", {1: frozenset()}, {},
                                  {1: {"ZZ": frozenset({"SWPP"}), "YY": frozenset({"ERCO"})}}, True)[0]
    assert in_state.marks == frozenset({"BA:SWPP"})


def test_unknown_utility_stays_unknown_and_blocks_the_veto(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    assert column(on, "reason")["F"] == veto.MISSING_ROW  # no row anywhere, no sales row
    assert column(on, "reason")["G"] == veto.NO_RTO_MARKED  # a row with no mark, an empty code only
    assert not column(on, "vetoed")["F"] and not column(on, "vetoed")["G"]
    assert "8 Nothing [no utility data row, nothing found by the lookups]" in column(on, "utility_marks")["F"]
    answers = veto.resolve_marks([8, 9], "ZZ", "ERCOT", "ERCO", {9: frozenset()}, {9: {"ZZ": frozenset()}}, {}, True)
    assert [(a.marks, a.source) for a in answers] == [(None, "unknown"), (frozenset(), "unknown")]


def test_county_with_an_unknown_utility_is_never_vetoed():
    choices = [None, frozenset(), frozenset({"SPP"}), frozenset({"BA:SWPP"}), frozenset({"Other"}), frozenset({"ERCOT"})]
    for size in (1, 2, 3):
        for marks in itertools.product(choices, repeat=size):
            if any(m is None or not m for m in marks):
                assert veto.veto_reason(list(marks), "ERCOT", ["Other"]) != veto.VETOED


def test_the_veto_never_adds(eia_zip):
    on = veto.county_veto(eia_zip, ERCOT_ON, 2018)
    # A county that reports the RTO without the lookups still does with them.
    for _, row in on.iterrows():
        if row["reason_k57"] == veto.REPORTS_RTO:
            assert row["reason"] == veto.REPORTS_RTO
    # In the frame, the lookups only take substations out.
    counties = [geo.County("99001", "A", box(0, 0, 1, 1)), geo.County("99003", "C", box(1, 0, 2, 1))]
    zones = {"99001": "Cold", "99003": "Cold"}
    areas = [ba.Area("ERCO-X", box(0, 0, 2, 1))]
    frame = pd.DataFrame(
        {"subregion": ["P1R", "P1R"], "kind": ["R", "R"], "substation": ["a", "c"], "centroid_x": [0.5, 1.5],
         "centroid_y": [0.5, 0.5], "feeders": [1, 1], "loads": [1, 1]}
    )
    status, _ = geo.membership(eia_zip, ERCOT_ON)
    with_off = geography_columns(frame, counties, status, zones, [], areas, "ERCO", veto_for(ERCOT_OFF, eia_zip, counties, status))
    with_on = geography_columns(frame, counties, status, zones, [], areas, "ERCO", veto_for(ERCOT_ON, eia_zip, counties, status))
    assert with_off["in_frame"].tolist() == [True, True]
    assert with_on["in_frame"].tolist() == [False, True]  # A is vetoed by lookup 1, C reads ERCOT by lookup 2
    assert not (with_on["in_frame"] & ~with_off["in_frame"]).any()


def test_the_three_area_settings_give_their_own_reasons(eia_zip):
    vetoed, reports = veto.VETOED, veto.REPORTS_RTO
    expected = {
        "ERCOT": {"C": reports, "E": reports, "H": vetoed, "I": vetoed, "J": reports},
        "CAISO": {"C": vetoed, "E": vetoed, "H": reports, "I": vetoed, "J": vetoed},
        "NYISO": {"C": vetoed, "E": vetoed, "H": vetoed, "I": reports, "J": vetoed},
    }
    same = {"A": vetoed, "B": vetoed, "D": vetoed, "F": veto.MISSING_ROW, "G": veto.NO_RTO_MARKED,
            "K": veto.IGNORED_MARK_ONLY, "L": vetoed}
    for name, names in AREAS.items():
        table = veto.county_veto(eia_zip, {**BASE, **names, "veto_lookups": True}, 2018)
        assert column(table, "reason") == {**same, **expected[name]}, name


def test_lookup_table_lists_every_looked_up_utility(eia_zip):
    table = veto.lookup_table(eia_zip, ERCOT_ON, 2018).set_index("utility_number")
    assert sorted(table.index) == LOOKED_UP
    assert table.loc[3, ["lookup", "source", "row_in_state", "other_state_rows"]].tolist() == [
        "lookup_1", "other_state_row:YY", "no row", "YY: SPP",
    ]
    assert table.loc[4, ["lookup", "source", "other_state_rows", "sales_codes"]].tolist() == [
        "lookup_2", "sales_ba_code:SWPP", "YY: none", "SWPP",
    ]
    assert table.loc[7, ["source", "answer"]].tolist() == ["sales_ba_code:ERCO,SWPP", "ERCOT via sales rows, BA ERCO"]
    assert table.loc[10, ["sales_codes_from", "answer"]].tolist() == ["any", "BA CISO via sales rows, other states"]
    assert table.loc[8, ["lookup", "answer", "source"]].tolist() == ["unknown", "unknown", "unknown"]
    assert table.loc[11, "source"] == "sales_ba_code:NYIS"
    assert table.loc[5, ["counties", "county_list"]].tolist() == [1, "C"]
    assert veto.lookup_table(eia_zip, ERCOT_OFF, 2018).empty


def test_sample_writes_the_lookup_list_through_the_settings(eia_zip):
    assert veto_lookups_for({**ERCOT_ON, "veto": False}, eia_zip) is None
    assert sorted(veto_lookups_for(ERCOT_ON, eia_zip)["utility_number"]) == LOOKED_UP
    assert veto_lookups_for(ERCOT_OFF, eia_zip).empty


def test_ba_code_is_required_with_the_lookups_on(eia_zip):
    with pytest.raises(KeyError, match="ba_code"):
        veto.county_veto(eia_zip, {k: v for k, v in ERCOT_ON.items() if k != "ba_code"}, 2018)
    veto.county_veto(eia_zip, {k: v for k, v in ERCOT_OFF.items() if k != "ba_code"})  # not needed when off


def test_settings_files_hold_the_three_names():
    balancing = load_sample_settings()["geography"]["balancing_area"]
    assert balancing["veto_lookups"] is True
    assert balancing["ba_code"] == "ERCO"
    with (find_configs() / "veto_areas.yaml").open(encoding="utf-8") as handle:
        areas = {a["name"]: a for a in yaml.safe_load(handle)["areas"]}
    assert {n: (a["states"], a["rto"], a["ba_code"], a["ba_prefix"], a["veto_ignore_marks"]) for n, a in areas.items()} == {
        "ERCOT": (["TX"], "ERCOT", "ERCO", "ERCO", ["Other"]),
        "CAISO": (["CA"], "CAISO", "CISO", "CISO", ["Other"]),
        "NYISO": (["NY"], "NYISO", "NYIS", "NYISO", ["Other"]),
    }


# Report-only run for one area: hand-made sheets, counties and two polygons.
REPORT_SERVED = {"A": [1, 3], "C": [1, 5], "H": [1, 10], "J": [2], "Q": [1]}
REPORT_COUNTIES = {  # Census name: square; Q has no Census county, Zed no EIA-861 row
    "A": box(0, 0, 1, 1),
    "C": box(1, 0, 2, 1),
    "H": box(2, 0, 3, 1),
    "J": box(3, 0, 4, 1),
    "Zed": box(4, 0, 5, 1),
}
# The area's polygon covers A and half of C; a non-RTO polygon covers the rest.
REPORT_POLYGONS = {"CISO-X": box(0, 0, 1.5, 1), "LDWP": box(1.5, 0, 5, 1)}
CAISO_AREA = {"name": "CAISO", "states": ["NV"], "rto": "CAISO", "ba_code": "CISO", "ba_prefix": "CISO",
              "veto_ignore_marks": ["Other"]}


def write_shapes(path, field, shapes, extra=None):
    with shapefile.Writer(str(path), shapeType=shapefile.POLYGON) as writer:
        for name in [field, *(extra or {})]:
            writer.field(name, "C", size=20)
        for key, geometry in shapes.items():
            writer.poly([list(geometry.exterior.coords)])
            writer.record(key, *[(value(key) if callable(value) else value) for value in (extra or {}).values()])


@pytest.fixture
def report_files(tmp_path):
    data, configs = tmp_path / "data", tmp_path / "configs"
    for folder in (data / "eia", data / "census", data / "geo" / "m", configs, tmp_path / "work"):
        folder.mkdir(parents=True)
    write_zip(tmp_path / "work" / "eia.zip", UTILITIES, REPORT_SERVED, state_name="NV")
    (tmp_path / "work" / "eia.zip").replace(data / "eia" / "eia.zip")
    geoids = {name: f"32{index:03d}" for index, name in enumerate(REPORT_COUNTIES, start=1)}
    write_shapes(tmp_path / "work" / "counties", "NAME", REPORT_COUNTIES,
                 {"STATEFP": "32", "GEOID": lambda name: geoids[name]})
    with zipfile.ZipFile(data / "census" / "counties.zip", "w") as archive:
        for ending in (".shp", ".shx", ".dbf"):
            archive.write(tmp_path / "work" / f"counties{ending}", f"counties{ending}")
    write_shapes(data / "geo" / "m" / "map", "BA", REPORT_POLYGONS)
    shared = {
        "geography": {
            "balancing_area": {**{k: v for k, v in BASE.items() if k not in ("state", "veto")},
                               "url": "https://example.org/eia.zip"},
            "counties": {"url": "https://example.org/counties.zip"},
            "balancing_area_map": {"ba_shapes": "https://example.org/map.shp", "folder": "m"},
        }
    }
    (configs / "shared.yaml").write_text(yaml.safe_dump(shared), encoding="utf-8")
    areas = {
        "sources": {"settings_file": "shared.yaml", "eia861": "geography.balancing_area",
                    "counties": "geography.counties", "map": "geography.balancing_area_map", "eia861_year": 2018},
        "output_folder": "out",
        "areas": [CAISO_AREA],
    }
    (configs / "veto_areas.yaml").write_text(yaml.safe_dump(areas), encoding="utf-8")
    (configs / "local.yaml").write_text(yaml.safe_dump({"data_root": str(data)}), encoding="utf-8")
    return configs / "veto_areas.yaml", data / "out"


def test_report_class_and_shares():
    assert veto.report_class(0.5, True, False) == "map_in_veto_out"
    assert veto.report_class(0.5, False, False) == "map_in_kept"
    assert veto.report_class(0.49, True, True) == "map_out_utilities_in"
    assert veto.report_class(0.0, False, False) == "map_out"
    counties = [({}, box(0, 0, 1, 1)), ({}, box(1, 0, 2, 1)), ({}, box(2, 0, 3, 1))]
    shares = veto.area_shares(counties, [box(0, 0, 1.5, 1)])
    assert shares == pytest.approx([1.0, 0.5, 0.0])
    assert veto.area_shares(counties, []) == [0.0, 0.0, 0.0]


def test_report_cli_writes_one_row_per_county_with_its_class(report_files, capsys):
    areas_path, out = report_files
    assert veto.main(["--areas", str(areas_path), "--area", "CAISO", "--stamp", "S1"]) == 0
    table = pd.read_csv(out / "veto_area_CAISO_S1.csv", dtype={"county_geoid": str})
    assert list(table.columns) == veto.REPORT_COLUMNS
    rows = table.set_index("county_eia")
    assert rows["class"].to_dict() == {
        "A": "map_in_veto_out",  # all inside, SPP via lookup 1
        "C": "map_in_veto_out",  # half inside, BA ERCO via lookup 2
        "H": "map_out_utilities_in",  # outside, CAISO via the sales rows of another state
        "J": "map_out",  # outside, ERCOT only
        "Q": "map_out",  # no Census county: no share
    }
    assert rows.loc["C", "share_in_area"] == pytest.approx(0.5)
    assert rows.loc["A", ["county", "county_geoid", "state"]].tolist() == ["A", "32001", "NV"]
    assert pd.isna(rows.loc["Q", "county_geoid"])
    assert rows.loc["A", ["reason_k57", "reason"]].tolist() == [veto.MISSING_ROW, veto.VETOED]
    assert rows.loc["H", "reason"] == veto.REPORTS_RTO
    printed = capsys.readouterr().out
    assert "eia_counties_not_in_census: ['NV Q']" in printed
    assert "census_counties_not_in_eia: ['NV Zed']" in printed
    looked_up = pd.read_csv(out / "veto_lookups_CAISO_S1.csv")
    assert sorted(looked_up["utility_number"]) == [3, 5, 10]
    assert set(looked_up["state"]) == {"NV"}


def test_report_cli_is_reproducible_and_stops_on_a_missing_file(report_files):
    areas_path, out = report_files
    veto.main(["--areas", str(areas_path), "--area", "CAISO", "--stamp", "S1"])
    veto.main(["--areas", str(areas_path), "--area", "CAISO", "--stamp", "S2"])
    for name in ("veto_area_CAISO", "veto_lookups_CAISO"):
        assert (out / f"{name}_S1.csv").read_bytes() == (out / f"{name}_S2.csv").read_bytes()
    with pytest.raises(KeyError, match="NOPE"):
        veto.main(["--areas", str(areas_path), "--area", "NOPE", "--stamp", "S3"])
    (areas_path.parent.parent / "data" / "eia" / "eia.zip").unlink()
    with pytest.raises(FileNotFoundError):
        veto.main(["--areas", str(areas_path), "--area", "CAISO", "--stamp", "S4"])
