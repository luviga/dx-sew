"""Settings of dx-sew: the setup registry, the setups and the shared files under ``config/``.

The project root is the folder that holds ``pyproject.toml``, found by walking up
from this file. Under it:

- ``config/setups/registry.json``: the setup ids and the file of each one, and
  the default id. A setup is read as data only; an unknown id or an unknown
  key stops the run.
- ``config/setups/<setup>.yaml``: every value of one balancing area and year.
- ``config/areas.yaml``, ``config/units.yaml``, ``config/runtime.yaml``,
  ``config/scan.yaml``: shared by every setup.
- ``config/local.yaml`` (not tracked): the paths of this machine, keys
  ``data_root``, ``smart_ds_base`` and ``eulp_metadata_root``.

Every key is described in docs/configuration.md.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_MARKER = "pyproject.toml"
CONFIG_FOLDER = "config"
REGISTRY_PATH = ("setups", "registry.json")
REGISTRY_SCHEMA = "dxsew-setup-registry-v1"
PROJECT_PREFIX = "project:"
LOCAL_SETTINGS_NAME = "local.yaml"
LOCAL_KEYS = ("data_root", "smart_ds_base", "eulp_metadata_root")

# Keys of a setup: the blocks and, for each block that is a mapping, its keys.
# A key ending in the suffix of runtime.yaml names.note_suffix holds an
# sentence that qualifies a value and is allowed in any block.
SETUP_KEYS = {
    "setup_id": None,
    "area": None,
    "label": None,
    "year": None,
    "output_folder": None,
    "inputs": {"zone_codes", "state_fips"},
    "release": {"bucket_url", "prefix", "description", "opendss_tree", "not_substations", "substation_file_parts",
                "feeder_file_parts", "opendss_default_windings", "load_data_folder", "listing_file"},
    "geography": {"counties", "balancing_area", "balancing_area_map", "climate_zone"},
    "eia861": {"year", "sales_file", "sales_sheet", "sales_group_row", "sales_measure_row", "sales_names_row",
               "short_form_file", "short_form_sheet"},
    "sample": {"selection_rule", "seed", "per_stratum", "sizes", "minutes_per_feeder"},
    "staging": {"folder", "parquet_folder", "opendss_relpath"},
    "coupling": {"topology_state", "donor_id", "status"},
    "groups": {"feeder_network_files", "line_units_default", "features", "regulator_max_low_kv", "primary_kv_rule",
               "primary_kv_decimals", "primary_kv_fallback", "missing_transformers_file", "name_kv",
               "substation_check_rel_tolerance", "scaling", "cap", "small_share_report", "dial_targets",
               "max_frame_customer_share", "dial_choice", "starting_weight", "gate_zero_tolerance",
               "gate_weight_sum_tolerance", "download_max_gb", "download_min_free_gb"},
    "runs": {"staging_folder", "alone_folder", "parallel", "serial_through_phase", "pipeline_override"},
    "curves": {"recipe_file", "peak_days_file", "seasons", "coupling_start", "check_rel_tolerance", "top_end_uses",
               "timestamp_source", "timestamp_convention", "mix_days_folder", "circuit_days_file",
               "circuit_weights_file", "mix_group_lists", "results_folder", "aggregate_file", "circuit_summary_file",
               "voltage_summary_file", "feeder_registry_file"},
    "fit_target": {"balancing_area", "year", "state", "classes_kept", "classes_removed", "standard_utc_offset_hours",
                   "prevailing_time_zone", "max_interpolated_gap_hours", "consistency_band", "published"},
    "fit": {"last_fit_day", "end_use_tolerance", "starting_tolerance", "product_tolerance", "excluded_end_uses",
            "sources"},
    "trust": {"edge_km", "puct_served_share"},
    "veto_report": {"eia861", "counties", "map", "output_folder", "share_threshold", "share_decimals"},
    "trial": {"check_set", "passes", "timing_cases"},
    "assembly": {"sum_rel_tolerance", "share_bands", "units_5plus_columns", "units_total_column", "homes_column",
                 "occupied_column", "tracts_url"},
}
# Blocks that also hold the texts a run fills (runtime.yaml names.texts_key).
BLOCKS_WITH_TEXTS = ("fit_target", "fit")


class SetupError(KeyError):
    """An unknown setup id, an unknown key, or a setup that disagrees with config/areas.yaml."""


def project_root(start: Path | None = None) -> Path:
    """The folder that holds ``pyproject.toml``, walking up from ``start`` (default: this file)."""
    here = Path(start or __file__).resolve()
    for folder in (here, *here.parents):
        if (folder / PROJECT_MARKER).is_file():
            return folder
    raise FileNotFoundError(f"{PROJECT_MARKER} not found in {here} or any folder above it.")


def find_configs(start: Path | None = None) -> Path:
    """The ``config`` folder of the project."""
    return project_root(start) / CONFIG_FOLDER


def read_yaml(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a mapping.")
    return data


@lru_cache(maxsize=None)
def _shared(name: str) -> dict:
    return read_yaml(find_configs() / name)


def load_units() -> dict:
    """``config/units.yaml``, checked: minutes_per_point x points_per_hour = minutes_per_hour."""
    units = _shared("units.yaml")
    if units["minutes_per_point"] * units["points_per_hour"] != units["minutes_per_hour"]:
        raise ValueError("config/units.yaml: minutes_per_point times points_per_hour is not minutes_per_hour")
    return units


def load_runtime() -> dict:
    """``config/runtime.yaml``."""
    return _shared("runtime.yaml")


def load_areas(path: Path | None = None) -> list[dict]:
    """The areas of ``config/areas.yaml`` (or the file given)."""
    return read_yaml(path or find_configs() / "areas.yaml")["areas"]


def load_scan() -> dict:
    """``config/scan.yaml``."""
    return _shared("scan.yaml")["scan"]


def read_registry(root: Path | None = None) -> dict:
    """``config/setups/registry.json``, checked for its schema and its default."""
    path = (root or project_root()).joinpath(CONFIG_FOLDER, *REGISTRY_PATH)
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("schema") != REGISTRY_SCHEMA:
        raise SetupError(f"{path}: schema is {registry.get('schema')!r}, not {REGISTRY_SCHEMA!r}.")
    if registry.get("default") not in registry.get("setups", {}):
        raise SetupError(f"{path}: the default {registry.get('default')!r} is not a setup of the registry.")
    return registry


def default_setup_id(root: Path | None = None) -> str:
    return read_registry(root)["default"]


def setup_path(setup_id: str | None = None, root: Path | None = None) -> Path:
    """File of a setup; an id not in the registry stops the run."""
    root = root or project_root()
    registry = read_registry(root)
    setup_id = setup_id or registry["default"]
    if setup_id not in registry["setups"]:
        raise SetupError(f"unknown setup {setup_id!r}; setups: {', '.join(sorted(registry['setups']))}.")
    reference = registry["setups"][setup_id]
    if not reference.startswith(PROJECT_PREFIX):
        raise SetupError(f"setup {setup_id!r}: {reference!r} does not start with {PROJECT_PREFIX!r}.")
    return root / reference[len(PROJECT_PREFIX):]


def check_setup(setup: dict, where: str = "setup") -> None:
    """Stop on a key the setup does not define (SETUP_KEYS) or a setup that disagrees with its area."""
    names = load_runtime()["names"]
    suffix, texts = names["note_suffix"], names["texts_key"]
    unknown = [f"{where}: {key}" for key in setup if key not in SETUP_KEYS and not key.endswith(suffix)]
    for block, keys in SETUP_KEYS.items():
        if block not in setup:
            unknown.append(f"{where}: missing {block}")
            continue
        if keys is None:
            continue
        allowed = set(keys) | ({texts} if block in BLOCKS_WITH_TEXTS else set())
        unknown += [f"{where}: {block}.{key}" for key in setup[block]
                    if key not in allowed and not str(key).endswith(suffix)]
    if unknown:
        raise SetupError("unknown or missing setup keys: " + "; ".join(unknown))
    areas = {area["name"]: area for area in load_areas()}
    if setup["area"] not in areas:
        raise SetupError(f"{where}: area {setup['area']!r} is not in config/areas.yaml.")
    area, balancing = areas[setup["area"]], setup["geography"]["balancing_area"]
    disagree = [name for name, mine in (("rto", balancing["rto"]), ("ba_code", balancing["ba_code"]),
                                        ("ba_prefix", setup["geography"]["balancing_area_map"]["ba_prefix"]))
                if area[name] != mine]
    if balancing["state"] not in area["states"]:
        disagree.append("states")
    if disagree:
        raise SetupError(f"{where}: {', '.join(disagree)} differ from area {setup['area']} of config/areas.yaml.")


@lru_cache(maxsize=None)
def _load_setup(setup_id: str, root: str) -> dict:
    path = setup_path(setup_id, Path(root))
    setup = read_yaml(path)
    check_setup(setup, path.name)
    if setup["setup_id"] != setup_id:
        raise SetupError(f"{path.name}: setup_id {setup['setup_id']!r} is not the registry id {setup_id!r}.")
    return setup


def load_setup(setup_id: str | None = None, root: Path | None = None) -> dict:
    """A copy of one setup (the registry default when ``setup_id`` is None), checked."""
    root = root or project_root()
    return copy.deepcopy(_load_setup(setup_id or default_setup_id(root), str(root)))


# The setup of this process: the registry default unless an entry point chose
# another with --setup (use_setup). Worker processes get the values they need in
# their task, never from here.
_CURRENT: dict[str, str | None] = {"id": None}


def use_setup(setup_id: str | None) -> str:
    """Make ``setup_id`` (default: the registry default) the setup of this process; returns the id."""
    chosen = setup_id or default_setup_id()
    setup_path(chosen)
    _CURRENT["id"] = chosen
    return chosen


def setup() -> dict:
    """The setup of this process (see ``use_setup``)."""
    return load_setup(_CURRENT["id"])


def add_setup_argument(parser) -> None:
    """``--setup <id>`` of every entry point, default the registry's default."""
    parser.add_argument("--setup", default=None,
                        help=f"setup id of config/setups/registry.json; default {default_setup_id()}")


def input_path(relative: str) -> Path:
    """A file of the project named by its path from the project root (the ``inputs`` keys of a setup)."""
    return project_root() / relative


def load_sample_settings(path: Path | None = None, setup_id: str | None = None) -> dict:
    """The setup of this process (or the id given), or the YAML file given."""
    if path is not None:
        return read_yaml(path)
    return load_setup(setup_id or _CURRENT["id"])


def load_local(path: Path | None = None) -> dict:
    """Read ``config/local.yaml`` and check its three keys."""
    path = path or find_configs() / LOCAL_SETTINGS_NAME
    if not Path(path).is_file():
        raise FileNotFoundError(
            f"{path} is missing. It must define: {', '.join(LOCAL_KEYS)}."
        )
    local = read_yaml(path)
    missing = [key for key in LOCAL_KEYS if not local.get(key)]
    if missing:
        raise KeyError(f"{path} lacks the keys: {', '.join(missing)}.")
    return local


class Paths:
    """Folders and files of a setup under the data root (the workspace)."""

    def __init__(self, local: dict, settings: dict):
        release = settings["release"]
        self.data_root = Path(local["data_root"])
        self.smart_ds_base = Path(local["smart_ds_base"])
        self.eulp_metadata_root = Path(local["eulp_metadata_root"])
        self.oedi = self.data_root / "oedi"
        self.listing = self.oedi / release["listing_file"]
        self.release_files = self.oedi / "files"
        self.census = self.data_root / "census"
        self.eia = self.data_root / "eia"
        self.geo = self.data_root / "geo"
        self.output = self.data_root / settings["output_folder"]
        self.coupling_settings = self.output / "coupling_settings"
        self.staging = self.smart_ds_base / settings["staging"]["folder"]
        self.staging_parquet = self.staging / settings["staging"]["parquet_folder"]

    def release_file(self, key: str) -> Path:
        """Local copy of one object of the release, at its full key."""
        return self.release_files / key
