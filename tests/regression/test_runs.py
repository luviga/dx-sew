"""Run feeders alone: alone trees, registry paths, settings files, on a small made-up tree."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from dxsew.config import Paths
from dxsew.runs import cases as runs

SETTINGS = {
    "release": {"prefix": "REL/2018/State/", "load_data_folder": "load_data", "listing_file": "listing.parquet",
                "description": "REL v0 State"},
    "staging": {"folder": "J3E_STAGED", "parquet_folder": "parquet_data", "opendss_relpath": "base/opendss"},
    "coupling": {"topology_state": "ZZ", "donor_id": "ZZ", "status": "ready"},
    "runs": {"staging_folder": "RUNS_STAGED", "alone_folder": "ALONE", "parallel": 4,
             "pipeline_override": {"eulp_release": {"residential": "resstock_amy2018_release_2"}}},
    "output_folder": "out",
}
TYPICAL = pd.DataFrame({
    "stratum": ["U|Hot-Humid", "U|Hot-Humid", "R|Hot-Dry"],
    "k": [3, 3, 2],
    "group": [1, 2, 1],
    "customers": [100, 50, 30],
    "run_feeder": ["s1_1247--f1", "s1_1247--f2", "s1_1247--f1"],
    "run_is_placeholder": [False, False, True],
    "run_feeder_loads": [12, 5, 12],
    "run_feeder_customers": [6, 3, 6],
})
FEATURES = pd.DataFrame({
    "subregion": ["P9U", "P9U", "P9U"],
    "substation": ["s1_1247", "s1_1247", "s1_1247"],
    "feeder": ["s1_1247--f1", "s1_1247--f2", "s1_1247--f3"],
    "county": ["Alpha", "Alpha", "Alpha"],
    "stratum": ["U|Hot-Humid", "U|Hot-Humid", "U|Hot-Humid"],
    "loads": [12, 5, 40],
})


@pytest.fixture
def tree(tmp_path):
    """A staged substation with three feeders under the runs staging root, and a J3e root beside it."""
    base = tmp_path / "base"
    local = {"data_root": str(tmp_path / "data"), "smart_ds_base": str(base), "eulp_metadata_root": str(tmp_path / "m")}
    settings = runs.runs_settings(SETTINGS)
    paths = Paths(local, settings)
    sub = paths.staging / "P9U" / "base/opendss" / "s1_1247"
    for feeder in ("s1_1247--f1", "s1_1247--f2", "s1_1247--f3"):
        (sub / feeder).mkdir(parents=True)
        (sub / feeder / "Loads.dss").write_text(f"New Load.{feeder} bus1=b.1 kW=1 yearly=res_kw_1_pu\n", encoding="utf-8")
        (sub / feeder / "LoadShapes.dss").write_text("New Loadshape.res_kw_1_pu npts=1\n", encoding="utf-8")
    (sub / "Master.dss").write_text("Redirect s1_1247--f1/Loads.dss\n", encoding="utf-8")
    (base / "J3E_STAGED").mkdir()
    return local, settings, paths


def test_runs_settings_is_a_copy_with_its_own_staging_root():
    copied = runs.runs_settings(SETTINGS)
    assert copied["staging"]["folder"] == "RUNS_STAGED"
    assert SETTINGS["staging"]["folder"] == "J3E_STAGED"
    with pytest.raises(ValueError):
        runs.runs_settings({**SETTINGS, "runs": {**SETTINGS["runs"], "staging_folder": "J3E_STAGED"}})
    with pytest.raises(KeyError):
        runs.runs_settings({**SETTINGS, "runs": {"staging_folder": "X"}})


def test_run_units_are_the_distinct_run_feeders_by_loads():
    units = runs.run_units(TYPICAL, FEATURES)
    assert units["feeder"].tolist() == ["s1_1247--f2", "s1_1247--f1"]
    f1 = units.set_index("feeder").loc["s1_1247--f1"]
    assert f1["groups"] == 2 and f1["group_customers"] == 130 and f1["strata"] == "R|Hot-Dry; U|Hot-Humid"
    assert f1["zone"] == "Hot-Humid" and f1["subregion"] == "P9U"
    with pytest.raises(ValueError):
        runs.run_units(TYPICAL.assign(run_feeder="nowhere"), FEATURES)


def test_alone_tree_holds_only_its_feeder(tree):
    local, settings, paths = tree
    units = runs.run_units(TYPICAL, FEATURES).to_dict("records")
    for unit in units:
        target = runs.alone_folder(paths, settings, unit)
        files, size = runs.build_alone_tree(runs.staged_feeder_folder(paths, settings, unit), target)
        assert files == 2 and size > 0
        substation = target.parent
        assert [p.name for p in substation.iterdir()] == [unit["feeder"]]
        assert target == paths.smart_ds_base / "ALONE" / unit["feeder"] / "P9U" / "base/opendss" / "s1_1247" / unit["feeder"]
    # Same files again: kept. Other files: refused.
    unit = units[0]
    assert runs.build_alone_tree(runs.staged_feeder_folder(paths, settings, unit), runs.alone_folder(paths, settings, unit))[0] == 2
    (runs.alone_folder(paths, settings, unit) / "extra.dss").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        runs.build_alone_tree(runs.staged_feeder_folder(paths, settings, unit), runs.alone_folder(paths, settings, unit))


def test_registry_paths_point_to_the_alone_tree_and_the_runs_parquets(tree):
    local, settings, paths = tree
    unit = runs.run_units(TYPICAL, FEATURES).to_dict("records")[1]
    entry = runs.registry_entry(unit, settings)
    assert entry["smart_ds_relpath"] == "ALONE/s1_1247--f1/P9U/base/opendss"
    assert entry["circuit_folder"] == "s1_1247"
    assert entry["parquet_relpath"] == "RUNS_STAGED/parquet_data"
    assert "s1_1247--f1" in entry["description"] and "1 feeder" in entry["description"]
    # The coupling resolves the circuit root as smart_ds_base / smart_ds_relpath / circuit_folder.
    root = paths.smart_ds_base / entry["smart_ds_relpath"] / entry["circuit_folder"]
    assert root == runs.alone_folder(paths, settings, unit).parent
    assert runs.topology_id(settings, unit) == "ZZ_P9U_s1_1247_f1"
    case = runs.case_file(unit, settings)
    assert case == {"case_id": "ZZ_P9U_s1_1247_f1__ZZ", "topology_id": "ZZ_P9U_s1_1247_f1", "donor_id": "ZZ",
                    "execution": {"max_feeders": 1}}


def test_settings_files(tree, tmp_path):
    local, settings, paths = tree
    units = runs.run_units(TYPICAL, FEATURES).to_dict("records")
    coupling_local = {"smart_ds_base": "X:/base", "eulp_metadata_root": "X:/m", "runs_root": str(tmp_path / "runs")}
    folder = tmp_path / "settings"
    written = runs.write_settings_folder(units, settings, folder, coupling_local)
    assert sorted(p.name for p in folder.iterdir()) == ["local.yaml", "pipeline_config.yaml", "topology_registry.yaml"]
    assert yaml.safe_load((folder / "pipeline_config.yaml").read_text(encoding="utf-8")) == {
        "eulp_release": {"residential": "resstock_amy2018_release_2"}}
    registry = yaml.safe_load((folder / "topology_registry.yaml").read_text(encoding="utf-8"))["topologies"]
    assert sorted(registry) == ["ZZ_P9U_s1_1247_f1", "ZZ_P9U_s1_1247_f2"]
    assert yaml.safe_load((folder / "local.yaml").read_text(encoding="utf-8")) == coupling_local
    cases = runs.write_cases(units, settings, tmp_path / "cases")
    for path in cases:
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert case["topology_id"] in registry and case["execution"] == {"max_feeders": 1}
    assert written[0] == folder / "local.yaml"
    with pytest.raises(FileExistsError):
        runs.write_settings_folder(units, settings, folder, coupling_local)


def test_the_j3e_staging_root_is_never_written(tree, tmp_path):
    local, settings, paths = tree
    j3e = Paths(local, SETTINGS)
    before = sorted(p for p in j3e.staging.rglob("*"))
    units = runs.run_units(TYPICAL, FEATURES).to_dict("records")
    for unit in units:
        runs.build_alone_tree(runs.staged_feeder_folder(paths, settings, unit), runs.alone_folder(paths, settings, unit))
        entry = runs.registry_entry(unit, settings)
        assert "J3E_STAGED" not in yaml.safe_dump(entry)
    runs.write_settings_folder(units, settings, tmp_path / "s", {"runs_root": str(tmp_path / "r")})
    runs.write_cases(units, settings, tmp_path / "c")
    assert sorted(p for p in j3e.staging.rglob("*")) == before == []
    assert paths.staging != j3e.staging and paths.staging_parquet.parent == paths.staging
    assert "J3E_STAGED" not in "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "s").iterdir())


def test_unknown_zone_endings_are_read_from_the_staging_notes():
    result = pd.DataFrame({"note": ["", "zone endings with no code: Panhandle South-West", None, "parquets: x"]})
    assert runs.unknown_endings(result) == {"Panhandle", "South-West"}


def test_short_feeder():
    assert runs.short_feeder("p14uhs14_1247--p14udt20832") == "p14udt20832"
    assert runs.short_feeder("plain") == "plain"
