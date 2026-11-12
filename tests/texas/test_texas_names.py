"""Digit-only load shape names, the zone code table, the name map and the four checks."""

import hashlib

import pytest

from dxsew.texas import names, stage

CODES = {"": "00", "Coast": "01", "North": "04", "North-Central": "05"}

LOADS = (
    "New Load.load_a_1 conn=wye bus1=a.1 kW=1.5 Phases=1 yearly=res_kw_9312-North_pu\n"
    "\n"
    "New Load.load_b conn=wye bus1=b.1.2.3 kW=20.25 Phases=3 yearly=com_kw_12_pu\n"
)
SHAPES = (
    "New Loadshape.res_kw_9312-North_pu npts= 35040 interval=0.25 "
    "mult = (file=../profiles/res_kw_9312-North_pu.csv) qmult = (file=../profiles/res_kvar_9312-North_pu.csv)\n"
    "New Loadshape.com_kw_12_pu npts= 35040 interval=0.25 "
    "mult = (file=../profiles/com_kw_12_pu.csv) qmult = (file=../profiles/com_kvar_12_pu.csv)\n"
)


def released_tree(root):
    feeder = root / "sub" / "sub--f1"
    feeder.mkdir(parents=True)
    (feeder / "Loads.dss").write_bytes(LOADS.encode())
    (feeder / "LoadShapes.dss").write_bytes(SHAPES.encode())
    (feeder / "Lines.dss").write_bytes(b"New Line.l1 bus1=a bus2=b length=1\n")
    (root / "sub" / "Intermediates.txt").write_bytes(b"res_kw_9312-North_pu stays as it is in a text file\n")
    return root / "sub"


def staged_setup(tmp_path):
    released = released_tree(tmp_path / "released")
    shapes, parquets, problems = names.scan_folder(released)
    assert not problems
    folder = tmp_path / "parquet_data"
    folder.mkdir()
    sha = {}
    for shape in shapes.values():
        if shape.qty == "kw":
            data = f"bytes of {shape.released}".encode()
            (folder / names.new_parquet(shape, CODES)).write_bytes(data)
            sha[names.parquet_of(shape)] = hashlib.sha256(data).hexdigest()
    rows = names.map_rows(shapes.values(), CODES, lambda n: f"profiles/{n}.csv", lambda n: f"load_data/{n}", sha)
    forward, _ = names.loadshape_maps(rows)
    staged = tmp_path / "staged" / "sub"
    stage.build_staged_tree(released, staged, forward)
    return released, staged, rows, folder


def test_forms_are_anchored_at_both_ends():
    shape = names.parse_loadshape("res_kw_9312-North_pu")
    assert (shape.type, shape.qty, shape.digits, shape.zone) == ("res", "kw", "9312", "North")
    assert names.parse_loadshape("com_kvar_12_pu").zone == ""
    assert names.parse_loadshape("res_kw_7-North-Central_pu").zone == "North-Central"
    for bad in ("res_kw_9312-North_pu_x", "xres_kw_1_pu", "res_kwh_1_pu", "res_kw_12a_pu", "res_kw_1-North_x_pu", "RES_KW_1_pu"):
        with pytest.raises(names.NameFormError):
            names.parse_loadshape(bad)
    assert names.PARQUET.match("res_9312-North.parquet")["zone"] == "North"
    assert names.PARQUET.match("res_9312-North.parquet.part") is None


def test_new_names_of_the_example():
    shape = names.parse_loadshape("res_kw_9312-North_pu")
    assert names.new_loadshape(shape, CODES) == "res_kw_931204_pu"
    assert names.new_parquet(shape, CODES) == "res_931204.parquet"
    assert names.parquet_of(shape) == "res_9312-North.parquet"
    kvar = names.parse_loadshape("res_kvar_9312-North_pu")
    assert names.new_loadshape(kvar, CODES) == "res_kvar_931204_pu"
    assert names.new_loadshape(names.parse_loadshape("com_kw_12_pu"), CODES) == "com_kw_1200_pu"
    with pytest.raises(names.NameFormError):
        names.new_loadshape(names.parse_loadshape("res_kw_1-West_pu"), CODES)


def test_zone_code_table_is_sorted_and_codes_never_move(tmp_path):
    keys = [
        "R/load_data/res_1-North.parquet",
        "R/load_data/com_2-Coast.parquet",
        "R/profiles/res_kvar_3-East_pu.csv",
        "R/load_data/res_4.parquet",
        "R/other/readme.txt",
    ]
    endings = names.endings_in_release(keys)
    assert endings == ["Coast", "East", "North"]
    table = names.extend_zone_codes({}, endings)
    assert table == {"": "00", "Coast": "01", "East": "02", "North": "03"}
    later = names.extend_zone_codes(table, ["Alpha", "Coast", "West"])
    assert later == {**table, "Alpha": "04", "West": "05"}
    path = tmp_path / "codes.csv"
    names.write_zone_codes(later, path)
    assert path.read_text(encoding="utf-8").splitlines()[:3] == ["zone_ending,code", ",00", "Coast,01"]
    assert names.read_zone_codes(path) == later


def test_zone_code_table_limits():
    with pytest.raises(names.NameFormError, match="more than 99"):
        names.extend_zone_codes({}, [f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(100)])
    with pytest.raises(names.NameFormError, match="underscore"):
        names.extend_zone_codes({}, ["North_East"])


def test_scan_reports_a_shape_like_token_of_no_form(tmp_path):
    folder = tmp_path / "sub" / "f"
    folder.mkdir(parents=True)
    (folder / "Loads.dss").write_bytes(b"New Load.x bus1=a kW=1 yearly=res_kw_1-North_pu\nNew Load.y bus1=a kW=1 yearly=res_kw_2_North_pu\n")
    shapes, parquets, problems = names.scan_folder(tmp_path / "sub")
    assert set(shapes) == {"res_kw_1-North_pu"}
    assert parquets == {"res_1-North.parquet"}
    assert problems == ["f/Loads.dss:2: res_kw_2_North_pu"]


def test_replacement_is_whole_token_only():
    mapping = {b"res_kw_1-North_pu": b"res_kw_104_pu"}
    data = b"New Loadshape.res_kw_1-North_pu mult=(file=p/res_kw_1-North_pu.csv) xres_kw_1-North_pu res_kw_1-North_pux\n"
    assert names.replace_tokens(data, mapping) == (
        b"New Loadshape.res_kw_104_pu mult=(file=p/res_kw_104_pu.csv) xres_kw_1-North_pu res_kw_1-North_pux\n"
    )


def test_all_four_checks_pass_on_a_clean_staging(tmp_path):
    released, staged, rows, folder = staged_setup(tmp_path)
    assert {r["kind"] for r in rows} == {"loadshape", "parquet"}
    assert len([r for r in rows if r["kind"] == "loadshape"]) == 4
    text = (staged / "sub--f1" / "LoadShapes.dss").read_text()
    assert "res_kvar_931204_pu.csv" in text and "North" not in text
    assert (staged / "Intermediates.txt").read_bytes() == (released / "Intermediates.txt").read_bytes()
    assert names.check_same_loads(released, staged) == []
    assert names.check_clean(released, staged, rows, folder) == []
    assert names.check_nothing_lost(released, staged, rows) == []
    assert names.staged_parquets(staged) == {"res_931204.parquet", "com_1200.parquet"}
    assert names.check_map(staged, rows, names.staged_parquets(staged)) == []


def test_check_same_loads_finds_a_changed_kw(tmp_path):
    released, staged, rows, folder = staged_setup(tmp_path)
    path = staged / "sub--f1" / "Loads.dss"
    path.write_bytes(path.read_bytes().replace(b"kW=20.25", b"kW=20.5"))
    assert names.check_same_loads(released, staged) == [
        "sub--f1/Loads.dss: released 2 loads, 21.75 kW; staged 2 loads, 22.0 kW"
    ]


def test_check_clean_finds_another_change_and_a_parquet(tmp_path):
    released, staged, rows, folder = staged_setup(tmp_path)
    path = staged / "sub--f1" / "Lines.dss"
    path.write_bytes(b"New Line.l1 bus1=a bus2=b length=2\n")
    (folder / "com_1200.parquet").write_bytes(b"other bytes")
    failures = names.check_clean(released, staged, rows, folder)
    assert failures == [
        "sub--f1/Lines.dss:1: forward map does not give the staged file",
        "com_1200.parquet: SHA-256 differs from released com_12.parquet",
    ]


def test_check_nothing_lost_finds_a_name_that_cannot_go_back(tmp_path):
    released, staged, rows, folder = staged_setup(tmp_path)
    path = staged / "sub--f1" / "Loads.dss"
    path.write_bytes(path.read_bytes().replace(b"yearly=com_kw_1200_pu", b"yearly=com_kw_1201_pu"))
    assert names.check_nothing_lost(released, staged, rows) == [
        "sub--f1/Loads.dss:3: backward map does not give the released file"
    ]


def test_check_map_finds_duplicates_leftovers_and_strangers(tmp_path):
    released, staged, rows, folder = staged_setup(tmp_path)
    path = staged / "sub--f1" / "Loads.dss"
    path.write_bytes(path.read_bytes() + b"New Load.c bus1=c kW=1 yearly=res_kw_5-Coast_pu\nNew Load.d bus1=c kW=1 yearly=res_kw_77_pu\n")
    doubled = rows + [{**rows[0], "released_name": "res_kw_1-Coast_pu"}]
    failures = names.check_map(staged, doubled, names.staged_parquets(staged))
    assert "name map: a loadshape new_name appears twice" in failures
    assert "sub--f1/Loads.dss:4: res_kw_5-Coast_pu still has a zone ending" in failures
    assert "sub--f1/Loads.dss:5: res_kw_77_pu is not in the name map" in failures
    assert "res_77.parquet: referenced parquet not in the name map" in failures


def test_stage_parquet_renames_in_place_and_stops_on_a_conflict(tmp_path):
    data = b"released bytes"
    (tmp_path / "res_1-North.parquet").write_bytes(data)
    guard = stage.DiskGuard(tmp_path, float("inf"), 0)
    sha, action = stage.stage_parquet("res_1-North.parquet", "res_104.parquet", len(data), "k", tmp_path, {}, guard, "", None)
    assert action == "renamed in place" and sha == hashlib.sha256(data).hexdigest()
    assert not (tmp_path / "res_1-North.parquet").exists() and (tmp_path / "res_104.parquet").read_bytes() == data
    again = stage.stage_parquet("res_1-North.parquet", "res_104.parquet", len(data), "k", tmp_path,
                                {"res_1-North.parquet": sha}, guard, "", None)
    assert again == (sha, "already renamed")
    (tmp_path / "res_2-North.parquet").write_bytes(b"other bytes of 2")
    (tmp_path / "res_204.parquet").write_bytes(b"different bytes!")
    with pytest.raises(stage.ParquetConflict):
        stage.stage_parquet("res_2-North.parquet", "res_204.parquet", 16, "k", tmp_path, {}, guard, "", None)
