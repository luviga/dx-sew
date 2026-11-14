"""Coupling settings and the staging helpers, on small hand-made inputs."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from dxsew.texas import settings as coupling_settings
from dxsew.texas import stage

SETTINGS = {
    "release": {"prefix": "REL/2018/State/", "load_data_folder": "load_data"},
    "staging": {"folder": "STAGED", "parquet_folder": "parquet_data", "opendss_relpath": "base/opendss"},
    "coupling": {"topology_state": "ZZ", "donor_id": "ZZ", "status": "ready"},
}
UNIT = {
    "subregion": "P9U",
    "substation": "p9uhs1_1247",
    "county": "Alpha",
    "zone": "Hot-Humid",
    "feeders": 3,
    "loads": 120,
}
# Keys of an entry of configs/topology_registry.yaml of DeltaE/dss-eulp-coupling,
# with parquet_relpath, which run_case.py reads when present.
COUPLING_KEYS = {
    "topology_state",
    "geography",
    "urbanicity",
    "feeder_id",
    "smart_ds_relpath",
    "circuit_folder",
    "parquet_relpath",
    "status",
    "description",
}


def test_registry_entry_has_the_keys_of_the_coupling():
    entry = coupling_settings.registry_entry(UNIT, SETTINGS)
    assert set(entry) == COUPLING_KEYS
    assert entry["smart_ds_relpath"] == "STAGED/P9U/base/opendss"
    assert entry["circuit_folder"] == "p9uhs1_1247"
    assert entry["parquet_relpath"] == "STAGED/parquet_data"
    assert entry["topology_state"] == "ZZ" and entry["status"] == "ready"
    assert "3 feeders" in entry["description"] and "120 loads" in entry["description"]


def test_case_file_runs_every_feeder_of_the_substation():
    case = coupling_settings.case_file(UNIT, SETTINGS)
    assert case == {
        "case_id": "ZZ_P9U_p9uhs1_1247__ZZ",
        "topology_id": "ZZ_P9U_p9uhs1_1247",
        "donor_id": "ZZ",
        "execution": {"max_feeders": 3},
    }


def test_settings_folder_for_staged_substations_only(tmp_path):
    sample = pd.DataFrame([UNIT, {**UNIT, "substation": "p9uhs2_1247", "feeders": 1}])
    staging = pd.DataFrame(
        {"subregion": ["P9U", "P9U"], "substation": ["p9uhs1_1247", "p9uhs2_1247"], "staged": [True, False]}
    )
    units = coupling_settings.staged_units(sample, staging)
    local = coupling_settings.local_settings(
        {"smart_ds_base": "X:/base", "runs_root": "runs"}, {"evwatts_extract_dir": "X:/ev"}, tmp_path / "runs"
    )
    days = coupling_settings.given_days(
        pd.DataFrame({"Season": ["summer", "winter"], "Month": [7, 1], "Day": [15, 15]}), ["f2", "f1"]
    )
    folder = tmp_path / "settings"
    written = coupling_settings.write_settings_folder(units, SETTINGS, folder, local, days)
    assert sorted(p.name for p in folder.iterdir()) == [
        "given_days.csv", "local.yaml", "pipeline_config.yaml", "topology_registry.yaml"
    ]
    registry = yaml.safe_load((folder / "topology_registry.yaml").read_text(encoding="utf-8"))
    assert list(registry["topologies"]) == ["ZZ_P9U_p9uhs1_1247"]
    assert set(registry["topologies"]["ZZ_P9U_p9uhs1_1247"]) == COUPLING_KEYS
    assert yaml.safe_load((folder / "pipeline_config.yaml").read_text(encoding="utf-8")) == {"peak_day_rule": "given"}
    loaded = yaml.safe_load((folder / "local.yaml").read_text(encoding="utf-8"))
    assert loaded == {"smart_ds_base": "X:/base", "runs_root": (tmp_path / "runs").as_posix(), "evwatts_extract_dir": "X:/ev"}
    assert (folder / "given_days.csv").read_text(encoding="utf-8").splitlines() == [
        "Feeder,Season,Month,Day", "f1,summer,7,15", "f2,summer,7,15", "f1,winter,1,15", "f2,winter,1,15",
    ]
    cases = coupling_settings.write_cases(units, SETTINGS, tmp_path / "cases")
    assert [path.name for path in cases] == ["ZZ_P9U_p9uhs1_1247__ZZ.yaml"]
    case = yaml.safe_load(cases[0].read_text(encoding="utf-8"))
    assert case["topology_id"] in registry["topologies"] and case["execution"] == {"max_feeders": 3}
    assert written[0] == folder / "local.yaml"


def test_runs_root_must_be_absolute_and_days_unique():
    with pytest.raises(ValueError):
        coupling_settings.local_settings({}, {}, Path("runs"))
    with pytest.raises(ValueError):
        coupling_settings.given_days(
            pd.DataFrame({"Feeder": ["f1", "f1"], "Season": ["summer", "summer"], "Month": [7, 7], "Day": [1, 2]}), []
        )


def test_feeders_are_folders_with_loads_and_loadshapes(tmp_path):
    for name, files in (("s--f1", ("Loads.dss", "LoadShapes.dss")), ("s--f2", ("Loads.dss",)), ("s--f0", ("LoadShapes.dss", "Loads.dss"))):
        (tmp_path / name).mkdir()
        for file in files:
            (tmp_path / name / file).write_text("", encoding="utf-8")
    (tmp_path / "Master.dss").write_text("", encoding="utf-8")
    assert coupling_settings.feeders_of(tmp_path) == ["s--f0", "s--f1"]


def test_staged_references_read_every_feeder(tmp_path):
    for feeder, shape in (("f1", "res_kw_1-Coast_pu"), ("f2", "com_kw_2-Coast_pu")):
        folder = tmp_path / "sub" / feeder
        folder.mkdir(parents=True)
        (folder / "Loads.dss").write_text(
            f"New Load.load_{feeder} bus1=b.1 kW=1 Phases=1 yearly={shape}\n", encoding="utf-8"
        )
    assert stage.staged_references(tmp_path / "sub") == {"res_1-Coast.parquet", "com_2-Coast.parquet"}


def test_disk_guard_stops_at_the_staging_limit(tmp_path):
    (tmp_path / "old.bin").write_bytes(b"x" * 600)
    guard = stage.DiskGuard(tmp_path, max_staging_bytes=1000, min_free_bytes=0)
    assert guard.staged_bytes == 600
    assert guard.allows(300)
    assert not guard.allows(200)
    assert "staging folder" in guard.reason


def test_disk_guard_stops_at_the_free_space_limit(tmp_path):
    guard = stage.DiskGuard(tmp_path, max_staging_bytes=float("inf"), min_free_bytes=float("inf"))
    assert not guard.allows(1)
    assert "free space" in guard.reason
