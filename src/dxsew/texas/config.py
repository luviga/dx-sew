"""Settings of the Texas sample: the tracked method settings and the local paths.

Two files are read, both under ``configs/`` of the repository:

- ``texas_sample.yaml`` (tracked): sources, strata, the selection rule and sizes.
- ``local.yaml`` (not tracked): the paths of this machine, keys ``data_root``,
  ``smart_ds_base`` and ``eulp_metadata_root``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SAMPLE_SETTINGS_NAME = "texas_sample.yaml"
LOCAL_SETTINGS_NAME = "local.yaml"
LOCAL_KEYS = ("data_root", "smart_ds_base", "eulp_metadata_root")


def find_configs(start: Path | None = None) -> Path:
    """Return the ``configs`` folder that holds the tracked sample settings.

    The search goes from ``start`` (the current folder by default) up to the
    drive root, so the CLIs work from any folder inside the repository.
    """
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / "configs" / SAMPLE_SETTINGS_NAME
        if candidate.is_file():
            return candidate.parent
    raise FileNotFoundError(
        f"configs/{SAMPLE_SETTINGS_NAME} not found in {here} or any folder above it."
    )


def read_yaml(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a mapping.")
    return data


def load_sample_settings(path: Path | None = None) -> dict:
    """Read ``configs/texas_sample.yaml`` (or the file given)."""
    return read_yaml(path or find_configs() / SAMPLE_SETTINGS_NAME)


def load_local(path: Path | None = None) -> dict:
    """Read ``configs/local.yaml`` and check its three keys."""
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
    """Folders and files of the Texas sample under the data root."""

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
        self.texas = self.data_root / "texas"
        self.coupling_settings = self.texas / "coupling_settings"
        self.staging = self.smart_ds_base / settings["staging"]["folder"]
        self.staging_parquet = self.staging / settings["staging"]["parquet_folder"]

    def release_file(self, key: str) -> Path:
        """Local copy of one object of the release, at its full key."""
        return self.release_files / key
