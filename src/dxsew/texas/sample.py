"""Stratified sample of whole substations of the SMART-DS Full Texas release.

Run from the repository::

    uv run python -m dxsew.texas.sample --minutes-per-feeder <minutes>

Steps: listing of the release (reused when the listing file exists), frame of
substations, county, balancing area by location (decision K56), county
status from EIA-861 (decision K52, kept for checking) and climate zone of each
substation, strata, the sample at the size of ``sample.per_stratum`` and the
size table for every size in ``sample.sizes``. All outputs go to the data
folder; nothing is written into the repository.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import frame as frame_module
from . import ba, geo, oedi
from .config import Paths, load_local, load_sample_settings

BYTES_PER_GB = 1e9  # decimal gigabyte, the unit of every GB column
ORDER_COLUMNS = ["loads", "subregion", "substation"]
SAMPLE_COLUMNS = [
    "stratum",
    "kind",
    "zone",
    "subregion",
    "substation",
    "feeders",
    "loads",
    "customers",
    "load_kw",
    "transformers",
    "transformer_kva",
    "county",
    "county_geoid",
    "ba",
    "ba_label",
    "county_status_eia861",
    "centroid_x",
    "centroid_y",
    "opendss_bytes",
    "parquets_referenced",
    "stratum_size",
    "position_in_stratum",
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_parquet(table: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.part")
    table.to_parquet(temporary, index=False)
    temporary.replace(path)


def write_csv(table: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.part")
    table.to_csv(temporary, index=False, lineterminator="\n")
    temporary.replace(path)


def get_listing(settings: dict, paths: Paths, refresh: bool) -> pd.DataFrame:
    """The saved listing of the release, or a new one from the data lake."""
    if paths.listing.is_file() and not refresh:
        return pd.read_parquet(paths.listing)
    release = settings["release"]
    listing = oedi.list_prefix(release["bucket_url"], release["prefix"])
    write_parquet(listing, paths.listing)
    return listing


def listing_counts(listing: pd.DataFrame, settings: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Object counts and bytes by top folder, and by sub-region with its substations."""
    prefix = settings["release"]["prefix"]
    top = listing["key"].str[len(prefix):].str.split("/").str[0]
    by_top = (
        listing.assign(top_folder=top)
        .groupby("top_folder")
        .agg(objects=("size", "size"), bytes=("size", "sum"))
        .reset_index()
    )
    objects = frame_module.opendss_objects(listing, settings)
    substations = objects.groupby("subregion")["substation"].nunique().rename("substations")
    by_subregion = by_top.merge(
        substations, left_on="top_folder", right_index=True, how="inner"
    ).rename(columns={"top_folder": "subregion"})
    by_subregion.insert(1, "kind", by_subregion["subregion"].str[-1])
    return by_top, by_subregion


def fetch_source(url: str, folder: Path) -> Path:
    """Download a source file into ``folder`` under its own name, or reuse it."""
    target = folder / url.rsplit("/", 1)[-1]
    with requests.Session() as session:
        oedi.fetch(session, url, target)
    return target


def geography_columns(
    frame: pd.DataFrame,
    counties: list[geo.County],
    status: pd.DataFrame,
    zone_of: dict[str, str],
    zone_filled: list[str],
    areas: list[ba.Area],
    prefix: str,
) -> pd.DataFrame:
    """Frame with county, balancing area, county status, zone, frame membership and stratum.

    ``status`` is the county table of ``geo.membership``; ``zone_of`` and
    ``zone_filled`` come from ``geo.county_zones``; ``areas`` is the balancing
    area map. A substation is in the frame when its load-bus centroid lies in
    one map polygon whose name starts with ``prefix`` (decision K56,
    ``ba.assign``) and its county has a zone; its stratum is its kind and the
    zone of its county. The county status from EIA-861 (decision K52) is kept
    as ``county_status_eia861`` for checking and plays no part in the frame.
    """
    frame = frame.copy()
    found = geo.assign_counties(frame["centroid_x"], frame["centroid_y"], counties)
    frame["county_geoid"] = [county.geoid if county else None for county in found]
    frame["county"] = [county.name if county else None for county in found]
    keys = {geo.name_key(county.name): county.geoid for county in counties}
    by_geoid = dict(zip(status["county_key"].map(keys), status["status"]))
    has_county = frame["county"].notna()
    frame["county_status_eia861"] = frame["county_geoid"].map(by_geoid).where(has_county)
    frame.loc[has_county & frame["county_status_eia861"].isna(), "county_status_eia861"] = geo.NO_DATA
    frame["zone"] = frame["county_geoid"].map(zone_of)
    frame["zone_from_nearest_county"] = frame["county_geoid"].isin(zone_filled)
    frame = ba.assign(frame, areas, prefix)
    frame["in_frame"] = frame["in_ba"] & frame["zone"].notna()
    frame["stratum"] = (frame["kind"] + "|" + frame["zone"]).where(frame["in_frame"])
    return frame.drop(columns="in_ba")


def add_geography(frame: pd.DataFrame, settings: dict, paths: Paths) -> tuple[pd.DataFrame, dict]:
    """Add county, RTO status, climate zone and stratum to the frame.

    When the centroids do not read as longitudes and latitudes inside the
    state, no geography is assigned and the strata are the kinds alone.
    """
    geography = settings["geography"]
    counties = geo.read_counties(
        fetch_source(geography["counties"]["url"], paths.census), geography["counties"]["state_fips"]
    )
    located = frame["centroid_x"].notna()
    notes: dict = {"substations_without_load_coordinates": int((~located).sum())}
    box = geo.bounds_of(counties)
    notes["state_bounds"] = box
    notes["coordinates_inside_state"] = geo.coordinates_inside(
        frame.loc[located, "centroid_x"], frame.loc[located, "centroid_y"], box
    )
    if not notes["coordinates_inside_state"]:
        frame = frame.copy()
        frame["in_frame"] = located
        frame["stratum"] = frame["kind"].where(located)
        return frame, notes

    balancing = geography["balancing_area"]
    status, counts = geo.membership(fetch_source(balancing["url"], paths.eia), balancing)
    notes.update(counts)
    keys = {geo.name_key(county.name) for county in counties}
    notes["eia_counties_not_in_census"] = sorted(
        status.loc[~status["county_key"].isin(keys), "county_eia"]
    )
    notes["census_counties_not_in_eia"] = sorted(
        county.name for county in counties if geo.name_key(county.name) not in set(status["county_key"])
    )
    notes["counties_by_status"] = status["status"].value_counts().to_dict()

    climate = geography["climate_zone"]
    zones, several = geo.climate_zones(paths.eulp_metadata_root / climate["metadata_file"], climate)
    zone_of, filled = geo.county_zones(counties, zones)
    notes["counties_with_several_zones"] = several
    notes["counties_zone_from_nearest"] = [c.name for c in counties if c.geoid in set(filled)]

    source = geography["balancing_area_map"]
    areas = ba.read_areas(ba.fetch_map(source, paths.geo, oedi.fetch))
    notes["map_records"] = len(areas)
    frame = geography_columns(frame, counties, status, zone_of, filled, areas, source["ba_prefix"])
    notes["substations_outside_every_county"] = int(located.sum() - frame["county"].notna().sum())
    notes["substations_in_prefix_without_zone"] = int(
        (frame["ba_label"].map(lambda v: ba.in_prefix(v, source["ba_prefix"])) & frame["zone"].isna()).sum()
    )
    return frame, notes


def systematic_positions(size: int, target: int, start: float) -> list[int]:
    """Positions drawn from an ordered stratum of ``size`` units.

    A stratum no larger than the target is taken whole. Otherwise the step is
    ``size / target`` and unit ``i`` of the sample sits at
    ``floor((start + i) * step)``, with ``start`` in ``[0, 1)``.
    """
    if size <= target:
        return list(range(size))
    step = size / target
    return [math.floor((start + i) * step) for i in range(target)]


def draw_sample(frame: pd.DataFrame, per_stratum: int, seed: int) -> pd.DataFrame:
    """Systematic selection inside every stratum of the frame.

    Substations of a stratum are ordered by load count (ties by sub-region and
    substation name). One random start per stratum is drawn, in stratum name
    order, from ``numpy.random.default_rng(seed)``.
    """
    generator = np.random.default_rng(seed)
    members = frame[frame["in_frame"]]
    chosen = []
    for _, group in members.groupby("stratum", sort=True):
        start = float(generator.random())
        ordered = group.sort_values(ORDER_COLUMNS, kind="mergesort")
        positions = systematic_positions(len(ordered), per_stratum, start)
        picked = ordered.iloc[positions].copy()
        picked["stratum_size"] = len(ordered)
        picked["position_in_stratum"] = positions
        chosen.append(picked)
    if not chosen:
        return pd.DataFrame(columns=list(frame.columns) + ["stratum_size", "position_in_stratum"])
    return pd.concat(chosen, ignore_index=True)


def size_row(
    sample: pd.DataFrame, references: pd.DataFrame, per_stratum: int, minutes: list[float]
) -> dict:
    """Totals of one sample: counts, bytes to stage, and run hours per time given."""
    keys = sample[["subregion", "substation"]]
    used = references.merge(keys, on=["subregion", "substation"]).drop_duplicates("parquet")
    feeders = int(sample["feeders"].sum())
    row = {
        "per_stratum": per_stratum,
        "strata": int(sample["stratum"].nunique()),
        "substations": len(sample),
        "feeders": feeders,
        "loads": int(sample["loads"].sum()),
        "customers": int(sample["customers"].sum()),
        "opendss_gb": sample["opendss_bytes"].sum() / BYTES_PER_GB,
        "parquets": len(used),
        "parquets_not_in_release": int(used["bytes"].isna().sum()),
        "parquet_gb": float(used["bytes"].sum()) / BYTES_PER_GB,
    }
    for value in minutes:
        row[f"hours_at_{value:g}_min_per_feeder"] = feeders * value / 60
    return row


def stratum_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Substations, feeders and loads of the frame by stratum."""
    return (
        frame[frame["in_frame"]]
        .groupby("stratum")
        .agg(substations=("substation", "size"), feeders=("feeders", "sum"), loads=("loads", "sum"))
        .reset_index()
    )


def county_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Substations, feeders and loads by county and balancing area label, outside the frame."""
    if "ba_label" not in frame:
        return pd.DataFrame()
    out = frame[~frame["in_frame"] & frame["county"].notna()]
    return (
        out.groupby(["ba_label", "county", "county_status_eia861"])
        .agg(substations=("substation", "size"), feeders=("feeders", "sum"), loads=("loads", "sum"))
        .reset_index()
        .sort_values(["ba_label", "substations", "county"], ascending=[True, False, True], kind="mergesort")
        .reset_index(drop=True)
    )


def status_summary(frame: pd.DataFrame, column: str = "county_status_eia861") -> pd.DataFrame:
    """Substations, feeders and loads by a status column (missing: no county)."""
    if column not in frame:
        return pd.DataFrame()
    return (
        frame.assign(**{column: frame[column].fillna("no_county")})
        .groupby(column)
        .agg(substations=("substation", "size"), feeders=("feeders", "sum"), loads=("loads", "sum"))
        .reset_index()
    )


def ba_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Substations, feeders and loads by balancing area label, for the frame and for all substations."""
    if "ba_label" not in frame:
        return pd.DataFrame()
    parts = []
    for scope, rows in (("frame", frame[frame["in_frame"]]), ("all", frame)):
        parts.append(
            rows.groupby("ba_label")
            .agg(substations=("substation", "size"), feeders=("feeders", "sum"), loads=("loads", "sum"))
            .reset_index()
            .assign(scope=scope)
        )
    table = pd.concat(parts, ignore_index=True)
    return table[["scope", "ba_label", "substations", "feeders", "loads"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", default=None, help="UTC stamp of the output names; default now")
    parser.add_argument(
        "--minutes-per-feeder",
        type=float,
        action="append",
        default=[],
        help="run time of one feeder in minutes, for the size table; may be repeated",
    )
    parser.add_argument("--refresh-listing", action="store_true", help="list the release again")
    parser.add_argument("--workers", type=int, default=32, help="download threads")
    parser.add_argument("--processes", type=int, default=os.cpu_count() or 1, help="reader processes")
    args = parser.parse_args(argv)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    stamp = args.stamp or utc_stamp()
    out = paths.texas

    listing = get_listing(settings, paths, args.refresh_listing)
    by_top, by_subregion = listing_counts(listing, settings)
    write_csv(by_top, out / f"listing_by_top_folder_{stamp}.csv")
    write_csv(by_subregion, out / f"listing_by_subregion_{stamp}.csv")
    print(f"listing: {len(listing)} objects, {listing['size'].sum() / BYTES_PER_GB:.2f} GB")
    print(by_subregion.groupby("kind").agg(subregions=("subregion", "size"), substations=("substations", "sum")))

    frame, references = frame_module.build_frame(listing, settings, paths, args.workers, args.processes)
    frame, notes = add_geography(frame, settings, paths)
    write_parquet(frame, out / f"frame_{stamp}.parquet")
    write_parquet(references, out / f"references_{stamp}.parquet")
    for name, value in notes.items():
        print(f"{name}: {value}")
    print(status_summary(frame).to_string(index=False))
    write_csv(county_summary(frame), out / f"counties_outside_frame_{stamp}.csv")
    if "ba_label" in frame:
        by_ba = ba_summary(frame)
        write_csv(by_ba, out / f"ba_summary_{stamp}.csv")
        print(by_ba.to_string(index=False))
        areas = ba.read_areas(ba.map_files(settings["geography"]["balancing_area_map"], paths.geo)[0])
        outside = ba.outside_list(frame, areas)
        write_csv(outside, out / f"ba_none_overlap_{stamp}.csv")
        print(outside.to_string(index=False))
    print(stratum_summary(frame).to_string(index=False))

    rule = settings["sample"]
    sizes = []
    for per_stratum in rule["sizes"]:
        drawn = draw_sample(frame, per_stratum, rule["seed"])
        sizes.append(size_row(drawn, references, per_stratum, args.minutes_per_feeder))
        if per_stratum == rule["per_stratum"]:
            write_csv(drawn.reindex(columns=SAMPLE_COLUMNS), out / f"sample_{stamp}.csv")
    write_csv(pd.DataFrame(sizes), out / f"sizes_{stamp}.csv")
    print(pd.DataFrame(sizes).to_string(index=False))
    print(f"written to {out} with stamp {stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
