"""The setup registry and loader: data only, closed on an unknown id or key (decision K72)."""

import json

import pytest

from dxsew import config


def test_registry_default_and_its_setup():
    registry = config.read_registry()
    assert registry["schema"] == config.REGISTRY_SCHEMA
    assert config.default_setup_id() in registry["setups"]
    setup = config.load_setup()
    assert setup["setup_id"] == config.default_setup_id()
    assert config.project_root().joinpath("pyproject.toml").is_file()


def test_unknown_setup_id_stops():
    with pytest.raises(config.SetupError, match="unknown setup"):
        config.load_setup("no_such_setup")
    with pytest.raises(config.SetupError):
        config.use_setup("no_such_setup")


def test_unknown_or_missing_key_stops():
    setup = config.load_setup()
    config.check_setup(setup)
    for broken in ({**setup, "extra_block": 1},
                   {**setup, "fit": {**setup["fit"], "last_fit_dya": 21}},
                   {k: v for k, v in setup.items() if k != "curves"}):
        with pytest.raises(config.SetupError):
            config.check_setup(broken)
    # A sentence beside a value is allowed in any block.
    note = config.load_runtime()["names"]["note_suffix"]
    config.check_setup({**setup, "fit": {**setup["fit"], f"anything{note}": "text"}})


def test_setup_must_agree_with_its_area():
    setup = config.load_setup()
    setup["geography"]["balancing_area"]["ba_code"] = "XXXX"
    with pytest.raises(config.SetupError, match="ba_code"):
        config.check_setup(setup)


def test_registry_with_another_schema_or_path_stops(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    folder = tmp_path / "config" / "setups"
    folder.mkdir(parents=True)
    path = folder / "registry.json"
    path.write_text(json.dumps({"schema": "other", "default": "a", "setups": {"a": "project:x.yaml"}}), encoding="utf-8")
    with pytest.raises(config.SetupError, match="schema"):
        config.read_registry(tmp_path)
    path.write_text(json.dumps({"schema": config.REGISTRY_SCHEMA, "default": "a", "setups": {"a": "x.yaml"}}),
                    encoding="utf-8")
    with pytest.raises(config.SetupError, match="project:"):
        config.setup_path("a", tmp_path)


def test_tolerances_and_units_are_numbers():
    setup, units = config.load_setup(), config.load_units()
    for value in (setup["fit"]["end_use_tolerance"], setup["fit"]["starting_tolerance"],
                  setup["fit"]["product_tolerance"], setup["curves"]["check_rel_tolerance"],
                  setup["groups"]["substation_check_rel_tolerance"], setup["groups"]["gate_zero_tolerance"],
                  setup["groups"]["gate_weight_sum_tolerance"], units["bytes_per_gb"],
                  config.load_runtime()["relative_floor"], config.load_runtime()["nearest_slack_degrees"]):
        assert isinstance(value, float)
    assert units["minutes_per_point"] * units["points_per_hour"] == units["minutes_per_hour"]


def test_load_setup_returns_a_copy():
    first = config.load_setup()
    first["year"] = 0
    assert config.load_setup()["year"] != 0
