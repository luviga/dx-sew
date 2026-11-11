"""Coupling settings and the staging helpers, on small hand-made inputs."""

import pandas as pd
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


def test_settings_are_written_for_staged_substations_only(tmp_path):
    sample = pd.DataFrame([UNIT, {**UNIT, "substation": "p9uhs2_1247", "feeders": 1}])
    staging = pd.DataFrame(
        {"subregion": ["P9U", "P9U"], "substation": ["p9uhs1_1247", "p9uhs2_1247"], "staged": [True, False]}
    )
    units = coupling_settings.staged_units(sample, staging)
    written = coupling_settings.write_settings(units, SETTINGS, tmp_path)
    registry = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert list(registry["topologies"]) == ["ZZ_P9U_p9uhs1_1247"]
    assert set(registry["topologies"]["ZZ_P9U_p9uhs1_1247"]) == COUPLING_KEYS
    assert [path.name for path in written[1:]] == ["ZZ_P9U_p9uhs1_1247__ZZ.yaml"]
    case = yaml.safe_load(written[1].read_text(encoding="utf-8"))
    assert case["topology_id"] in registry["topologies"]


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
