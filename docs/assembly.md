# Substations at baseline: ratings, rebuild, loading and the check set

`dxsew.assembly.substations` rebuilds every frame substation at baseline from the group curves, rates it from its own transformers, and states its loading (decisions K60, K73 and K74). No count, energy or sector factor reaches any curve, feeder or substation (K74); results are stated for the frame. The code is in `dxsew/assembly/substations.py` and its tests are in `tests/regression/test_trial.py`.

## Ratings

```
uv run python -m dxsew.assembly.substations ratings --stamp <stamp> --frame <frame parquet>
```

The substation folder's `Transformers.dss` of every in-frame substation, the release copy the frame read under `<data_root>/oedi/files/`, is parsed with `smartds_dss.parse_transformers` (both OpenDSS forms, `wdg=` blocks and `buses=`/`kvs=` arrays; https://opendss.epri.com/Properties16.html). Each transformer is classed:

- **unit**: its windings carry two or more distinct kV values;
- **regulator**: every winding carries the same kV.

The high-side bus of a unit is the bus of its winding with the highest kV, the low-side bus that of the lowest. Units with the same high-side and low-side buses are **parallel**.

The **rating** of a substation is the sum of its units' kVA, the first winding's `kva` (or the first value of `kvas`) as the frame reads it; regulators are left out. A substation with no unit has an empty rating and is listed. A substation whose units feed two or more distinct low-side buses is marked `split_buses`; its loading is also reported against its largest unit, since each low-side bus is then served by fewer units than the whole rating. The rating class used in the counts is `no_unit`, `one_unit`, `parallel_units` (every unit in a parallel set), `split_buses` or `several_units`.

The frame's own `transformer_kva` (`dxsew.frame.frame.read_substation`) adds every `New Transformer.` line of the file, regulators and second units included. The gate: for a substation with one transformer that is a unit, the rating equals the frame's `transformer_kva` exactly.

Outputs: `substation_transformers_<stamp>.csv` (one row per transformer: subregion, substation, name, phases, windings, kVA, class, high-side and low-side bus, and `bus_<n>`, `kv_<n>` of each winding), `substation_ratings_<stamp>.csv` (one row per in-frame substation: transformers listed, units, regulators, units without kVA, parallel sets, units in parallel sets, low-side buses, `split_buses`, `rating_kva`, `largest_unit_kva`, the frame's `transformers` and `transformer_kva`, `ratings_equal`, `rating_class`) and `substation_rating_counts_<stamp>.csv` (substations and customers by number of units, with regulators, with parallel sets, `split_buses`, and by rating class).

## Rebuild at baseline

```
uv run python -m dxsew.assembly.substations assemble --stamp <stamp> --frame <frame parquet> --features <features parquet> \
    --typical <typical_feeders csv> --groups <groups parquet> --curves <curves folder> --ratings <substation_ratings csv> \
    --county-coverage <county coverage csv> --tract-coverage <tract coverage csv> \
    [--trial <trial folder> --trial-feeders <trial_feeders csv>]
```

Every frame feeder takes its group (at the typical table's number of groups for its stratum) and the group's run-feeder curve of `feeder_curves.parquet` times its own customers over the run feeder's customers (K60, the representation already used by step 4 of `dxsew.curves.year`). No other multiplier touches any curve. A substation's curve is the sum over its feeders.

Gate: the sum of all substation curves equals the sum of the stratum `kw` curves of `stratum_curves.parquet` at every point, within `assembly.sum_rel_tolerance` (relative, the denominator floored at `relative_floor` of `config/runtime.yaml`).

## Loading

`substations_<stamp>.csv` has one row per frame substation:

- identity: subregion, substation, county, county code, stratum, feeders, customers;
- the rating and its class columns from the ratings step;
- the peak kW of its year curve, the start of that 15-minute interval and the season whose months hold it (empty outside the seasons of `curves.seasons`);
- `loading_kw_over_kva_lower_bound`: peak kW over the rating;
- `loading_vs_largest_unit_lower_bound`, for `split_buses` substations: peak kW over the largest unit's kVA;
- per season, the highest kW on day B and on its stratum's day A ([curves.md](curves.md)), and each over the rating;
- `coincidence`: its peak over the sum of its feeders' own year peaks;
- `sum_of_feeder_peaks_kw`.

Loading is a lower bound of the apparent loading. A transformer's rating is in kVA, apparent power S; the curves are active power P in kW. Since S is at least P whatever the power factor, a loading in kW over kVA is never higher than the loading in kVA over kVA, and no power factor is assumed to close the gap. The power-flow trial's measured P over S at the feeder head, at the step of highest S ([trial.md](trial.md)), is reported beside the counts as context only and is applied to nothing.

`overload_counts_<stamp>.csv` counts substations and customers with loading above 1, at the peak and on day B of each season, by stratum and rating class, with the totals of each; the columns `context_head_p_over_s_*` give the range, median and row count of the trial's head P over S for the baseline mix in both passes.

## Apartment bands

Decision K74 keeps the curves as they are; this table shows how loading varies with the share of housing in large buildings, which the building stock models may represent unevenly. For every frame substation `loading_bands_<stamp>.csv` holds:

- its county's share of housing units in buildings of 5 or more units, from ACS 2018 table B25024 in the county coverage file (columns `assembly.units_5plus_columns` over `assembly.units_total_column`);
- for a substation whose load centroid (frame `centroid_x`, `centroid_y`) lies in a census tract of the tract coverage file, that tract's share by the same rule, and whether the tract lies in the county the frame gives (the tract code starts with the county code).

The tract of a centroid is found as `dxsew.frame.geo.assign_counties` assigns counties (a point in polygon test on the tract polygons, which carry the same `STATEFP`, `GEOID` and `NAME` fields), on the 2018 cartographic boundary file of the state's tracts (setup key `assembly.tracts_url`, in `config/sources.yaml`), read from `<data_root>/census/` or downloaded there. If the file cannot be had, the tract columns stay empty and the summary says so.

`loading_band_summary_<stamp>.csv` gives, per band of share (edges `assembly.share_bands`, which group the report only), county-based and tract-based: the substations, the quartiles and median of loading, the counties or tracts in the band and their frame homes over ACS occupied units. County ratios are taken over the counties that hold frame substations.

## Check set

For each substation of `trial.check_set`, `check_set_<stamp>.csv` holds:

- `kind` baseline: its own curve from its pass-1 run (the reproduction of [trial.md](trial.md), its feeders summed) against its rebuild from group curves above: the peak and its time, the energy, the CV(RMSE) of the rebuild against its own curve over the year (root mean square difference over the mean of its own curve), the ratios of peak and energy, and the load on day B of each season;
- `kind` mix `<mix>`, per season: its mix curves summed over its feeders: the coincident peak and its time, the sum of the feeders' own seasonal mix peaks, their ratio, and the load on day B.

This tests the assumption of K47 that a group curve scaled by customers stands for a feeder inside its substation. It is report-only.

## Outputs

| File | Command |
|---|---|
| `substation_transformers_<stamp>.csv`, `substation_ratings_<stamp>.csv`, `substation_rating_counts_<stamp>.csv` | `ratings` |
| `substations_<stamp>.csv`, `overload_counts_<stamp>.csv`, `loading_bands_<stamp>.csv`, `loading_band_summary_<stamp>.csv`, `check_set_<stamp>.csv` | `assemble` |
