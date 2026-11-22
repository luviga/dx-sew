"""Substation transformers, ratings and the baseline assembly of the frame (decisions K60, K73, K74).

Run from the repository::

    uv run python -m dxsew.assembly.substations ratings --stamp <stamp> --frame <frame parquet> [--setup <id>]
    uv run python -m dxsew.assembly.substations assemble --stamp <stamp> --frame <frame parquet> \\
        --features <parquet> --typical <csv> --groups <parquet> --curves <curves folder> \\
        --ratings <substation_ratings csv> --county-coverage <csv> --tract-coverage <csv> \\
        [--trial <trial folder> --trial-feeders <csv>] [--setup <id>]

``ratings`` (step 1 of the trial). The substation folder's ``Transformers.dss`` of
every in-frame substation, the release copy the frame read under
``<data_root>/oedi/files/``, is parsed with ``smartds_dss.parse_transformers``.
``substation_transformers_<stamp>.csv`` holds one row per transformer:
subregion, substation, name, phases, each winding's bus and kV, kVA (the first
winding's, as the frame reads it) and its class:

- unit: the windings carry two or more distinct kV values;
- regulator: every winding carries the same kV (or none is given).

Units with the same high-side bus (the bus of the winding with the highest kV)
and the same low-side bus (that of the lowest kV) are parallel. The rating of a
substation is the sum of its units' kVA, regulators left out; with no unit the
rating is empty. ``substation_ratings_<stamp>.csv`` holds one row per in-frame
substation: units, regulators, parallel sets, distinct low-side buses, the
rating, the largest unit's kVA, the frame's ``transformer_kva`` and whether the
two are equal, and ``split_buses`` when the units feed two or more distinct
low-side buses.

``assemble`` (step 7) is described in docs/assembly.md and in ``assemble``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from dxsew import config
from dxsew.config import Paths, load_local, load_runtime, load_sample_settings, load_units
from dxsew.curves import year
from dxsew.curves.mix import baseline_mix
from dxsew.frame import geo
from dxsew.frame import frame as frame_module
from dxsew.frame.sample import get_listing, write_csv
from dxsew.sources import download
from dxsew.sources import smartds_dss as dss

UNIT, REGULATOR = "unit", "regulator"
TRANSFORMERS_FILE = "transformers.dss"  # lower case, as frame.SUBSTATION_FILES names it
QUARTILES = ("25%", "75%")  # labels of pandas describe()
DAY_A = "A"  # option name of the stratum's own seasonal peak day in common_days.csv (dxsew.curves.year)


def transformer_class(transformer: dss.Transformer) -> str:
    """``unit`` when the windings carry two or more distinct kV values, else ``regulator``."""
    values = {winding.kv for winding in transformer.windings if winding.kv is not None}
    return UNIT if len(values) > 1 else REGULATOR


def sides(transformer: dss.Transformer) -> tuple[str, str]:
    """Bus of the winding with the highest kV and bus of the winding with the lowest kV (first on a tie)."""
    rated = [winding for winding in transformer.windings if winding.kv is not None]
    if not rated:
        return "", ""
    high = max(rated, key=lambda winding: winding.kv)
    low = min(rated, key=lambda winding: winding.kv)
    return high.bus, low.bus


def transformer_rows(subregion: str, substation: str, transformers: list[dss.Transformer]) -> list[dict]:
    """One row per transformer of a substation folder's ``Transformers.dss``."""
    rows = []
    for transformer in transformers:
        high, low = sides(transformer)
        row = {"subregion": subregion, "substation": substation, "name": transformer.name,
               "phases": transformer.phases, "windings": len(transformer.windings), "kva": transformer.kva,
               "class": transformer_class(transformer), "high_bus": high, "low_bus": low}
        for number, winding in enumerate(transformer.windings, start=1):
            row[f"bus_{number}"] = winding.bus
            row[f"kv_{number}"] = winding.kv
        rows.append(row)
    return rows


def substation_rating(rows: pd.DataFrame) -> dict:
    """Rating and class columns of one substation from its transformer rows (possibly none)."""
    units = rows[rows["class"] == UNIT] if len(rows) else rows
    pairs = units.groupby(["high_bus", "low_bus"]).size() if len(units) else pd.Series(dtype=int)
    low_buses = int(units["low_bus"].nunique()) if len(units) else 0
    rated = units["kva"].dropna() if len(units) else pd.Series(dtype=float)
    return {
        "transformers_listed": int(len(rows)),
        "units": int(len(units)),
        "regulators": int((rows["class"] == REGULATOR).sum()) if len(rows) else 0,
        "units_without_kva": int(len(units) - len(rated)),
        "parallel_sets": int((pairs > 1).sum()),
        "units_in_parallel_sets": int(pairs[pairs > 1].sum()),
        "low_buses": low_buses,
        "split_buses": low_buses > 1,
        "rating_kva": float(rated.sum()) if len(rated) else np.nan,
        "largest_unit_kva": float(rated.max()) if len(rated) else np.nan,
    }


def rating_class(row: dict | pd.Series) -> str:
    """Class of a substation's rating for the counts: no_unit, one_unit, parallel_units, split_buses or several_units."""
    if row["units"] == 0:
        return "no_unit"
    if row["units"] == 1:
        return "one_unit"
    if row["split_buses"]:
        return "split_buses"
    if row["units_in_parallel_sets"] == row["units"]:
        return "parallel_units"
    return "several_units"


def ratings(frame: pd.DataFrame, files: dict[tuple[str, str], Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The transformer table and the rating table of the in-frame substations.

    ``files`` maps (subregion, substation) to the local copy of its substation
    folder's ``Transformers.dss``; a substation without one has no transformer row
    and an empty rating.
    """
    inside = frame[frame["in_frame"].astype(bool)]
    rows = []
    for subregion, substation in inside[["subregion", "substation"]].itertuples(index=False):
        path = files.get((subregion, substation))
        if path is None or not Path(path).is_file():
            continue
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        rows += transformer_rows(subregion, substation, dss.parse_transformers(text))
    table = pd.DataFrame(rows)
    if table.empty:
        table = pd.DataFrame(columns=["subregion", "substation", "name", "phases", "windings", "kva", "class",
                                      "high_bus", "low_bus"])
    grouped = dict(iter(table.groupby(["subregion", "substation"]))) if len(table) else {}
    out = []
    for item in inside.itertuples(index=False):
        key = (item.subregion, item.substation)
        mine = grouped.get(key, table.iloc[:0])
        row = {"subregion": item.subregion, "substation": item.substation,
               "transformers_file": key in files and Path(files[key]).is_file()}
        row.update(substation_rating(mine))
        row["frame_transformers"] = item.transformers
        row["frame_transformer_kva"] = item.transformer_kva
        row["ratings_equal"] = bool(row["rating_kva"] == item.transformer_kva)
        row["rating_class"] = rating_class(row)
        out.append(row)
    return table, pd.DataFrame(out)


def transformer_files(listing: pd.DataFrame, settings: dict, paths: Paths) -> dict[tuple[str, str], Path]:
    """Local copy of each substation folder's ``Transformers.dss``, as the frame reads it."""
    objects = frame_module.opendss_objects(listing, settings)
    mine = objects[(objects["feeder"] == "") & (objects["file"] == TRANSFORMERS_FILE)]
    return {(row.subregion, row.substation): paths.release_file(row.key) for row in mine.itertuples(index=False)}


def rating_counts(table: pd.DataFrame, customers: pd.Series) -> pd.DataFrame:
    """Substations and customers by number of units, by flag (regulators, parallel sets, split buses) and by rating class."""
    data = table.assign(customers=customers.to_numpy())
    masks = [(f"{int(n)} units", data["units"] == n) for n in sorted(data["units"].unique())]
    masks += [("with regulators", data["regulators"] > 0), ("with parallel sets", data["parallel_sets"] > 0),
              ("split_buses", data["split_buses"].astype(bool))]
    masks += [(f"class {name}", data["rating_class"] == name) for name in sorted(data["rating_class"].unique())]
    masks.append(("all", data["units"] >= 0))
    return pd.DataFrame([{"count": label, "substations": int(mask.sum()),
                          "customers": int(data.loc[mask, "customers"].sum())} for label, mask in masks])


def main_ratings(args, settings: dict, paths: Paths) -> int:
    frame = pd.read_parquet(args.frame)
    listing = get_listing(settings, paths, refresh=False)
    table, rated = ratings(frame, transformer_files(listing, settings, paths))
    write_csv(table, paths.output / f"substation_transformers_{args.stamp}.csv")
    write_csv(rated, paths.output / f"substation_ratings_{args.stamp}.csv")
    inside = frame[frame["in_frame"].astype(bool)].reset_index(drop=True)
    counts = rating_counts(rated, inside["customers"])
    write_csv(counts, paths.output / f"substation_rating_counts_{args.stamp}.csv")
    one = rated[(rated["transformers_listed"] == 1) & (rated["units"] == 1)]
    print(f"in-frame substations {len(rated)}; transformer rows {len(table)} "
          f"({int((table['class'] == UNIT).sum())} units, {int((table['class'] == REGULATOR).sum())} regulators)")
    print(f"gate, one transformer that is a unit: {int(one['ratings_equal'].sum())} of {len(one)} equal")
    print(f"no Transformers.dss: {int((~rated['transformers_file']).sum())}; no unit: {int((rated['units'] == 0).sum())}")
    print(f"ratings equal to the frame's: {int(rated['ratings_equal'].sum())} of {len(rated)}")
    print(counts.to_string(index=False))
    print(rated["rating_class"].value_counts().to_string())
    return 0


# ---------------------------------------------------------------------------
# Step 7: the baseline assembly (``assemble``).


def feeder_factors(features: pd.DataFrame, groups: pd.DataFrame, typical: pd.DataFrame) -> pd.DataFrame:
    """Each frame feeder with its group, run feeder and factor customers / run feeder customers (K60).

    ``groups`` holds every k; the k of each stratum is the typical table's.
    """
    k_of = typical.groupby("stratum")["k"].first()
    chosen = groups[groups["k"] == groups["stratum"].map(k_of)][["stratum", "feeder", "group"]]
    table = features[["subregion", "substation", "feeder", "stratum", "county", "customers"]].merge(
        chosen, on=["stratum", "feeder"], how="left", validate="one_to_one")
    table = table.merge(typical[["stratum", "group", "run_feeder", "run_feeder_customers"]],
                        on=["stratum", "group"], how="left", validate="many_to_one")
    if table["run_feeder"].isna().any():
        raise ValueError(f"{int(table['run_feeder'].isna().sum())} frame feeders have no group or run feeder")
    table["factor"] = table["customers"] / table["run_feeder_customers"]
    return table


def substation_curves(factors: pd.DataFrame, curves: dict[str, np.ndarray]) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Substation curves as the sum of their feeders' scaled run-feeder curves (K60).

    Returns the substations (subregion, substation, feeders, customers, sum of
    the feeders' own peaks), their curves (one row each, float64) and the run
    feeder order of the weight matrix.
    """
    run_feeders = np.array(sorted(curves))
    position = {name: index for index, name in enumerate(run_feeders)}
    matrix = np.vstack([np.asarray(curves[name], dtype=float) for name in run_feeders])
    peaks = matrix.max(axis=1)
    keys = factors[["subregion", "substation"]].drop_duplicates().sort_values(["subregion", "substation"])
    keys = keys.reset_index(drop=True)
    row_of = {(a, b): i for i, (a, b) in enumerate(keys.itertuples(index=False, name=None))}
    weights = np.zeros((len(keys), len(run_feeders)))
    own_peaks = np.zeros(len(keys))
    for item in factors.itertuples(index=False):
        i, j = row_of[(item.subregion, item.substation)], position[item.run_feeder]
        weights[i, j] += item.factor
        own_peaks[i] += item.factor * peaks[j]
    summary = factors.groupby(["subregion", "substation"]).agg(
        feeders=("feeder", "size"), customers=("customers", "sum"), stratum=("stratum", "first"),
        county=("county", "first")).reset_index()
    keys = keys.merge(summary, on=["subregion", "substation"], how="left")
    keys["sum_of_feeder_peaks_kw"] = own_peaks
    return keys, weights @ matrix, run_feeders


def season_of(month: int, seasons: dict[str, list[int]]) -> str:
    """The season whose months hold the month, or empty."""
    return next((name for name, months in seasons.items() if month in months), "")


def day_high(curves: np.ndarray, dates: pd.DatetimeIndex, date: pd.Timestamp) -> np.ndarray:
    """Each curve's highest point on one date."""
    mask = np.asarray(dates == date)
    return curves[:, mask].max(axis=1)


def band_of(shares: pd.Series, edges: list[float]) -> pd.Series:
    """Band label of each share: [0, e1), [e1, e2), ..., [e_last, and above)."""
    bins = [0.0, *edges, np.inf]
    labels = [f"[{low:g}, {high:g})" for low, high in zip(bins[:-1], bins[1:])]
    return pd.cut(shares, bins=bins, labels=labels, right=False, include_lowest=True)


def share_5plus(table: pd.DataFrame, settings: dict) -> pd.Series:
    """Share of housing units in buildings of 5 or more units (setup keys ``assembly.units_*``)."""
    block = settings["assembly"]
    total = table[block["units_total_column"]].astype(float)
    return table[list(block["units_5plus_columns"])].astype(float).sum(axis=1) / total.where(total > 0)


def band_summary(values: pd.DataFrame, share: str, value: str, places: pd.DataFrame, place_share: str,
                 edges: list[float], settings: dict, label: str) -> pd.DataFrame:
    """Per band: count, quartiles and median of ``value``, and homes over ACS occupied units of the places in the band."""
    block = settings["assembly"]
    data = values.assign(band=band_of(values[share], edges)).dropna(subset=["band"])
    places = places.assign(band=band_of(places[place_share], edges)).dropna(subset=["band"])
    rows = []
    for band in data["band"].cat.categories:
        part = data.loc[data["band"] == band, value].dropna()
        mine = places[places["band"] == band]
        occupied = float(mine[block["occupied_column"]].sum())
        rows.append({"basis": label, "band": band, "substations": int(len(part)),
                     "loading_q1": float(part.describe()[QUARTILES[0]]) if len(part) else np.nan,
                     "loading_median": float(part.median()) if len(part) else np.nan,
                     "loading_q3": float(part.describe()[QUARTILES[-1]]) if len(part) else np.nan,
                     "places": int(len(mine)),
                     "homes_over_occupied": float(mine[block["homes_column"]].sum()) / occupied if occupied else np.nan})
    return pd.DataFrame(rows)


def curve_measures(own: np.ndarray, other: np.ndarray, dates: pd.DatetimeIndex, starts: pd.DatetimeIndex,
                   day_b: dict[str, pd.Timestamp], hours_per_point: float) -> dict:
    """Peak, its time, energy and load on day B of both curves, and CV(RMSE) of ``other`` against ``own``."""
    row = {}
    for label, curve in (("own", own), ("rebuilt", other)):
        index = int(np.argmax(curve))
        row[f"{label}_peak_kw"] = float(curve[index])
        row[f"{label}_peak_time"] = pd.Timestamp(starts[index]).isoformat()
        row[f"{label}_energy_kwh"] = float(curve.sum() * hours_per_point)
        for season, date in day_b.items():
            row[f"{label}_day_b_{season}_kw"] = float(curve[np.asarray(dates == date)].max())
    row["cv_rmse"] = float(np.sqrt(np.mean((other - own) ** 2)) / np.mean(own))
    row["peak_ratio_rebuilt_over_own"] = row["rebuilt_peak_kw"] / row["own_peak_kw"]
    row["energy_ratio_rebuilt_over_own"] = row["rebuilt_energy_kwh"] / row["own_energy_kwh"]
    return row


def count_over(table: pd.DataFrame, measures: dict[str, str]) -> pd.DataFrame:
    """Substations and customers with loading above 1 by stratum and rating class, with the totals of each."""
    parts = [*table.groupby(["stratum", "rating_class"]),
             *[((stratum, "all"), part) for stratum, part in table.groupby("stratum")],
             *[(("all", klass), part) for klass, part in table.groupby("rating_class")],
             (("all", "all"), table)]
    rows = []
    for (stratum, klass), part in parts:
        row = {"stratum": stratum, "rating_class": klass, "substations": len(part),
               "customers": int(part["customers"].sum())}
        for name, column in measures.items():
            over = part[column] > 1
            row[f"over_1_{name}_substations"] = int(over.sum())
            row[f"over_1_{name}_customers"] = int(part.loc[over, "customers"].sum())
            row[f"over_1_{name}_share"] = float(over.mean()) if len(part) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def main_assemble(args, settings: dict, paths: Paths) -> int:
    """Step 7: rebuild, per-substation table, counts, apartment bands and the check set (docs/assembly.md)."""
    block, rule = settings["assembly"], settings["curves"]
    seasons = rule["seasons"]
    units = load_units()
    hours_per_point = units["minutes_per_point"] / units["minutes_per_hour"]
    frame = pd.read_parquet(args.frame)
    inside = frame[frame["in_frame"].astype(bool)]
    features = pd.read_parquet(args.features)
    typical = pd.read_csv(args.typical)
    factors = feeder_factors(features, pd.read_parquet(args.groups), typical)
    long = pd.read_parquet(args.curves / "feeder_curves.parquet")
    timestamps = pd.DatetimeIndex(long[long["feeder"] == long["feeder"].iloc[0]]["timestamp"])
    curves = {name: part["kw"].to_numpy() for name, part in long.groupby("feeder", sort=True)}
    subs, sub_curves, _ = substation_curves(factors, curves)
    dates, starts = year.point_dates(timestamps), year.interval_start(timestamps)

    # 7.1 Gate: the sum of the substation curves against the sum of the stratum curves.
    strata = pd.read_parquet(args.curves / "stratum_curves.parquet")
    stratum_sum = strata.groupby("timestamp", sort=True)["kw"].sum().reindex(timestamps).to_numpy(dtype=float)
    total = sub_curves.sum(axis=0)
    relative = np.abs(total - stratum_sum) / np.maximum(np.abs(stratum_sum), load_runtime()["relative_floor"])
    gate = bool(relative.max() <= block["sum_rel_tolerance"])
    print(f"rebuild: {len(subs)} substations, {len(factors)} feeders, {int(factors['customers'].sum())} customers")
    print(f"sum gate: largest relative difference {relative.max():.3e} (tolerance {block['sum_rel_tolerance']}): "
          f"{'passed' if gate else 'FAILED'}")

    # 7.2 Per substation.
    rated = pd.read_csv(args.ratings)
    common = pd.read_csv(args.curves / "common_days.csv")
    given_letter = next(spec["given_day"] for spec in settings["trial"]["passes"].values() if "given_day" in spec)
    day_b = {s: pd.Timestamp(common[(common["option"] == given_letter) & (common["season"] == s)]["date"].iloc[0])
             for s in seasons}
    peak_index = sub_curves.argmax(axis=1)
    table = subs.merge(inside[["subregion", "substation", "county_geoid", "centroid_x", "centroid_y"]],
                       on=["subregion", "substation"], how="left")
    table = table.merge(rated, on=["subregion", "substation"], how="left")
    table["peak_kw"] = sub_curves[np.arange(len(subs)), peak_index]
    table["peak_time"] = [pd.Timestamp(starts[i]).isoformat() for i in peak_index]
    table["peak_season"] = [season_of(int(starts[i].month), seasons) for i in peak_index]
    table["loading_kw_over_kva_lower_bound"] = table["peak_kw"] / table["rating_kva"]
    table["loading_vs_largest_unit_lower_bound"] = np.where(table["split_buses"].astype(bool),
                                                            table["peak_kw"] / table["largest_unit_kva"], np.nan)
    for season in seasons:
        high = day_high(sub_curves, dates, day_b[season])
        table[f"day_b_{season}_kw"] = high
        table[f"day_b_{season}_loading"] = high / table["rating_kva"]
        a_days = common[(common["option"] == DAY_A) & (common["season"] == season)].set_index("stratum")["date"]
        a_high = np.full(len(subs), np.nan)
        for stratum, date in a_days.items():
            mask = (table["stratum"] == stratum).to_numpy()
            if mask.any():
                a_high[mask] = day_high(sub_curves[mask], dates, pd.Timestamp(date))
        table[f"day_a_{season}_kw"] = a_high
        table[f"day_a_{season}_loading"] = a_high / table["rating_kva"]
    table["coincidence"] = table["peak_kw"] / table["sum_of_feeder_peaks_kw"]
    write_csv(table.drop(columns=["centroid_x", "centroid_y"]), paths.output / f"substations_{args.stamp}.csv")

    # 7.3 Counts above loading 1, by stratum and rating class; the head P over S beside them as context.
    measures = {"peak": "loading_kw_over_kva_lower_bound", **{f"day_b_{s}": f"day_b_{s}_loading" for s in seasons}}
    counts = count_over(table, measures)
    if args.trial_feeders is not None and Path(args.trial_feeders).is_file():
        trial = pd.read_csv(args.trial_feeders)
        base = trial[trial["mix"] == baseline_mix(rule)] if "mix" in trial else trial.iloc[:0]
        ratio = base["p_over_s_at_max_s"].dropna() if "p_over_s_at_max_s" in base else pd.Series(dtype=float)
        counts["context_head_p_over_s_min"] = float(ratio.min()) if len(ratio) else np.nan
        counts["context_head_p_over_s_median"] = float(ratio.median()) if len(ratio) else np.nan
        counts["context_head_p_over_s_max"] = float(ratio.max()) if len(ratio) else np.nan
        counts["context_head_p_over_s_rows"] = int(len(ratio))
    write_csv(counts, paths.output / f"overload_counts_{args.stamp}.csv")
    print(counts[counts["stratum"] == "all"].to_string(index=False))

    # 7.4 Apartment bands.
    counties = pd.read_csv(args.county_coverage)
    counties["share_5plus"] = share_5plus(counties, settings)
    tracts = pd.read_csv(args.tract_coverage, dtype={"tract": str})
    tracts["share_5plus_rule"] = share_5plus(tracts, settings)
    bands = table[["subregion", "substation", "stratum", "county", "county_geoid", "customers", "rating_kva",
                   "peak_kw", "loading_kw_over_kva_lower_bound", "centroid_x", "centroid_y"]].copy()
    # County codes are compared as numbers: the frame holds them as text, the coverage file as numbers.
    bands["county_geoid"] = pd.to_numeric(bands["county_geoid"]).astype("Int64")
    counties["county_geoid"] = pd.to_numeric(counties["county_geoid"]).astype("Int64")
    bands = bands.merge(counties[["county_geoid", "share_5plus"]].rename(columns={"share_5plus": "county_share_5plus"}),
                        on="county_geoid", how="left")
    bands["county_band"] = band_of(bands["county_share_5plus"], block["share_bands"])
    note = ""
    try:
        tract_file = paths.census / Path(block["tracts_url"]).name
        if not tract_file.is_file():
            tract_file = download(block["tracts_url"], paths.census)
        wanted = set(tracts["tract"])
        polygons = [t for t in geo.read_counties(tract_file, settings["geography"]["counties"]["state_fips"])
                    if t.geoid in wanted]
        found = geo.assign_counties(bands["centroid_x"].tolist(), bands["centroid_y"].tolist(), polygons)
        bands["tract"] = [t.geoid if t is not None else None for t in found]
    except (OSError, RuntimeError, ValueError) as error:
        note = f"tract boundary file not available: {error}"
        bands["tract"] = None
        print(note)
    bands = bands.merge(tracts[["tract", "share_5plus_rule"]].rename(columns={"share_5plus_rule": "tract_share_5plus"}),
                        on="tract", how="left")
    bands["tract_band"] = band_of(bands["tract_share_5plus"], block["share_bands"])
    has_tract = bands["tract"].notna()
    county_digits = bands["county_geoid"].astype("Int64").astype(str)
    bands["tract_county_agrees"] = pd.Series(
        [tract.startswith(county) if isinstance(tract, str) else None
         for tract, county in zip(bands["tract"], county_digits)], dtype=object)
    write_csv(bands.drop(columns=["centroid_x", "centroid_y"]), paths.output / f"loading_bands_{args.stamp}.csv")
    summary = pd.concat([
        band_summary(bands, "county_share_5plus", "loading_kw_over_kva_lower_bound",
                     counties[counties["county_geoid"].isin(set(bands["county_geoid"].dropna()))], "share_5plus",
                     block["share_bands"], settings, "county"),
        band_summary(bands[has_tract], "tract_share_5plus", "loading_kw_over_kva_lower_bound", tracts,
                     "share_5plus_rule", block["share_bands"], settings, "tract")], ignore_index=True)
    summary["note"] = note
    summary["band_edges_note"] = block["share_bands" + load_runtime()["names"]["note_suffix"]]
    write_csv(summary, paths.output / f"loading_band_summary_{args.stamp}.csv")
    disagree = int((bands["tract_county_agrees"] == False).sum())  # noqa: E712 - an object column
    print(f"substations in a tract of the tract file: {int(has_tract.sum())}; tract outside the frame county: {disagree}")
    print(summary.drop(columns=["band_edges_note"]).to_string(index=False))

    # 7.5 Check set.
    rows = []
    if args.trial is not None:
        own_curves = pd.read_parquet(args.trial / "baseline_curves.parquet")
        mix_curves = pd.read_parquet(args.trial / "mix_curves.parquet")
        for item in settings["trial"]["check_set"]:
            subregion, substation = item.split("/", 1)
            feeders = sorted(factors[(factors["subregion"] == subregion)
                                     & (factors["substation"] == substation)]["feeder"])
            index = subs.index[(subs["subregion"] == subregion) & (subs["substation"] == substation)][0]
            mine = own_curves[own_curves["feeder"].isin(feeders)]
            own = mine.groupby("timestamp", sort=True)["kw"].sum().reindex(timestamps).to_numpy(dtype=float)
            row = {"substation": item, "kind": "baseline", "season": "", "feeders_in_own_run": int(mine["feeder"].nunique()),
                   "feeders_in_frame": len(feeders)}
            row.update(curve_measures(own, sub_curves[index], dates, starts, day_b, hours_per_point))
            rows.append(row)
            for mix, part in mix_curves[mix_curves["feeder"].isin(feeders)].groupby("mix", sort=False):
                pivot = part.pivot(index="timestamp", columns="feeder", values="kw").reindex(timestamps)
                values = pivot.to_numpy(dtype=float)
                summed = values.sum(axis=1)
                for season in seasons:
                    mask = year.season_mask(dates, season)
                    at = np.flatnonzero(mask)[int(np.argmax(summed[mask]))]
                    own_peaks = float(values[mask].max(axis=0).sum())
                    rows.append({"substation": item, "kind": f"mix {mix}", "season": season,
                                 "feeders_in_own_run": int(pivot.shape[1]), "feeders_in_frame": len(feeders),
                                 "coincident_peak_kw": float(summed[at]),
                                 "coincident_peak_time": pd.Timestamp(starts[at]).isoformat(),
                                 "sum_of_feeder_peaks_kw": own_peaks,
                                 "coincidence": float(summed[at]) / own_peaks if own_peaks else np.nan,
                                 "day_b_kw": float(summed[np.asarray(dates == day_b[season])].max())})
    check = pd.DataFrame(rows)
    write_csv(check, paths.output / f"check_set_{args.stamp}.csv")
    print(check.to_string(index=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("ratings", help="transformer table and ratings (step 1)")
    one.add_argument("--stamp", required=True)
    one.add_argument("--frame", type=Path, required=True, help="frame_<stamp>.parquet")
    config.add_setup_argument(one)
    two = sub.add_parser("assemble", help="baseline assembly, loading, apartment bands and check set (step 7)")
    two.add_argument("--stamp", required=True)
    two.add_argument("--frame", type=Path, required=True, help="frame_<stamp>.parquet")
    two.add_argument("--features", type=Path, required=True, help="features_<stamp>.parquet")
    two.add_argument("--typical", type=Path, required=True, help="typical_feeders_<stamp>.csv")
    two.add_argument("--groups", type=Path, required=True, help="groups_<stamp>.parquet")
    two.add_argument("--curves", type=Path, required=True, help="curves folder of dxsew.curves.year")
    two.add_argument("--ratings", type=Path, required=True, help="substation_ratings_<stamp>.csv of the ratings step")
    two.add_argument("--county-coverage", type=Path, required=True, help="county coverage file with table B25024")
    two.add_argument("--tract-coverage", type=Path, required=True, help="tract coverage file with table B25024")
    two.add_argument("--trial", type=Path, default=None, help="trial_<stamp> folder of dxsew.curves.mix")
    two.add_argument("--trial-feeders", type=Path, default=None, help="trial_feeders_<stamp>.csv of dxsew.runs.results")
    config.add_setup_argument(two)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    if args.command == "ratings":
        return main_ratings(args, settings, paths)
    if args.command == "assemble":
        return main_assemble(args, settings, paths)
    return 1


if __name__ == "__main__":
    sys.exit(main())
