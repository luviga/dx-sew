# Stratified Texas sample of whole substations

This note describes how dx-sew draws a stratified sample of whole SMART-DS substations in Texas, stages each sampled substation for the coupling with digit-only load shape names, and writes the coupling settings. The code is in `src/dxsew/texas/`, its settings are in `configs/texas_sample.yaml` and `configs/texas_zone_codes.csv`, and its tests are in `tests/texas/`. How dx-sew runs the coupling is in [coupling.md](coupling.md).

Nothing the code produces is stored in the repository. Listings, source files, frames, samples, size tables, released copies, name maps and coupling settings are written under `data_root`; the staged circuits and parquets are written under `smart_ds_base`.

## Commands

Run from the repository, in this order:

```
uv run python -m dxsew.texas.sample --stamp <stamp> [--minutes-per-feeder <minutes>]
uv run python -m dxsew.texas.trust --stamp <stamp> [--puct-counties <csv>]
uv run python -m dxsew.texas.names
uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB> [--previous-sample <csv>]
uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB> --check-only
uv run python -m dxsew.texas.settings --stamp <stamp> --runs-root <folder> --base-local <file> \
    [--extra-local <file> --extra-key <key>] --days <csv> [--substation <subregion>/<substation>]
```

- `sample` lists the release (or reuses the saved listing), builds the frame, assigns the geography, draws the sample and writes the size table. `--stamp` sets the stamp of the output names (default: the current UTC time). Each value of `sample.minutes_per_feeder` in the settings adds one run-hours column to the size table; `--minutes-per-feeder`, which may be repeated, replaces them. `--refresh-listing` lists the release again.
- `trust` runs the four trust checks of the balancing area map on the frame of a stamp (section "Trust checks of the map"). They report and change nothing. `--puct-counties` gives the county list of the Public Utility Commission of Texas as a CSV with the columns `county` and `status` (`served`, `partly` or `not served`); without it the second check does not run.
- `names` writes `configs/texas_zone_codes.csv` from the saved listing, or extends it with endings it lacks. Codes already in the table keep their meaning.
- `stage` places the released copies, builds the staged tree with digit-only names, renames the parquets, writes the name map and runs the four checks. The two disk limits are arguments because they belong to the machine. `--previous-sample` names the sample CSV of an earlier staging; its substations that left the sample are listed, with the parquets they share with this one. `--check-only` runs the four checks again on the staged tree and changes nothing; it exits with 1 when a check fails.
- `settings` writes the settings folder and the case files of the staged substations (section "Coupling settings").

`configs/local.yaml` (not tracked) must define `data_root`, `smart_ds_base` and `eulp_metadata_root`; `coupling_repo` and `coupling_python` are read by `dxsew.coupling`.

## Sources

| Input | Source |
|---|---|
| Circuits and load data | SMART-DS v0.9 Full Texas, year 2018, on the Open Energy Data Initiative data lake: `https://oedi-data-lake.s3.amazonaws.com`, prefix `SMART-DS/v0.9/2018/Full_Texas/`. Browser view: https://data.openei.org/s3_viewer?bucket=oedi-data-lake&prefix=SMART-DS%2Fv0.9%2F2018%2FFull_Texas%2F |
| Listing method | Amazon S3 ListObjectsV2, called without sign-in: https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html |
| Balancing area map (decision K56) | PyPSA-USA, `workflow/repo_data/geospatial/BA_shapes_new/Modified_BE_BA_Shapes` (`.shp`, `.shx`, `.dbf`, `.prj`; NAD83 geographic; field `BA`), the onshore shapes of PyPSA-USA (`workflow/rules/build_electricity.smk`): https://github.com/PyPSA/pypsa-usa/tree/3d67e2061e02a5b983ed9807a1b2668e7fdf6fd4/workflow/repo_data/geospatial/BA_shapes_new . License MIT: https://github.com/PyPSA/pypsa-usa/blob/3d67e2061e02a5b983ed9807a1b2668e7fdf6fd4/LICENSE.md . Cite as K. Tehranchi, T. Barnes, M. Frysztacki, I. M. L. Azevedo, "Costs and benefits of transmission investments in the United States under different policy scenarios", Environmental Research Letters 21 (2026) 194039, https://doi.org/10.1088/1748-9326/aea586 ; software DOI https://doi.org/10.5281/zenodo.10815964 |
| Mean radius of the Earth, for distances in km | 6,371.0088 km, the mean radius `R1` of the GRS 80 ellipsoid: H. Moritz, "Geodetic Reference System 1980", Journal of Geodesy 74 (2000) 128-133, https://doi.org/10.1007/s001900050278 |
| Roughness of balancing area shapes | U.S. Energy Information Administration, Balancing Authorities layer, derived from the HIFLD control areas; its shapes overlap, have gaps and are not strict boundaries: https://services7.arcgis.com/FGr1D95XCGALKXqM/ArcGIS/rest/services/Balancing_Authorities/FeatureServer |
| Counties served by ERCOT (trust check 2) | Public Utility Commission of Texas, Texas Electricity Supply Chain Map, section "Public Map Layers", layer "Counties Served by ERCOT": https://www.puc.texas.gov/industry/maps/supplychain/Default.aspx |
| County boundaries | U.S. Census Bureau, TIGER/Line 2018 counties: https://www2.census.gov/geo/tiger/TIGER2018/COUNTY/tl_2018_us_county.zip |
| Utilities per county | U.S. Energy Information Administration, Form EIA-861 detailed data files, 2018, file `Service_Territory_2018.xlsx`, sheet `Counties_States`: https://www.eia.gov/electricity/data/eia861/ (file https://www.eia.gov/electricity/data/eia861/archive/zip/f8612018.zip) |
| ISOs and RTOs each utility operates in | The same EIA-861 2018 zip, file `Utility_Data_2018.xlsx`, sheet `States`, column group "Operating in these RTOs". PUDL publishes the same data as table `core_eia861__yearly_utility_data_rto`: https://data.catalyst.coop/preview/pudl/core_eia861__yearly_utility_data_rto |
| Climate zone per county | ResStock 2024 release 2, Texas baseline metadata, columns `in.county` and `in.building_america_climate_zone`: https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/resstock_tmy3_release_2/ |
| County code form of the metadata | IPUMS NHGIS, GISJOIN identifiers: https://www.nhgis.org/geographic-crosswalks |
| Coupling inputs | DeltaE/dss-eulp-coupling: https://github.com/DeltaE/dss-eulp-coupling (`run_case.py`, `configs/topology_registry.yaml`, `0_download_smartds/`, `7_circuit_instantiation/instantiate_circuits.py`, `7_circuit_instantiation/power_flow_sim_daily_EV_STO_DG_deploy.py`, README sections "Settings from a caller" and "Peak-day rules") |

## Steps

### 1. Listing

`sample` lists every object under the release prefix and saves the key and size of each to `<data_root>/oedi/<listing_file>`. Object counts and bytes by top folder and by sub-region are written beside the frame.

### 2. Frame of substations

The frame has one row per substation folder of the tree `<sub-region>/scenarios/base_timeseries/opendss/`. The folder `subtransmission` of that tree is not a substation and is left out.

- A feeder is a folder below the substation folder that holds both `Loads.dss` and `LoadShapes.dss`. This is the rule of `0_download_smartds/stage_circuits_plain.py` of the coupling.
- For each substation the code downloads the `Loads.dss` of every feeder and the `Buscoords.dss` and `Transformers.dss` of the substation folder. These are the smallest files that give the loads, their buses, the coordinates of the buses and the substation transformers. No circuit is compiled.
- Values per substation, with their inputs and formulas:

| Column | Inputs | Formula |
|---|---|---|
| `kind` | sub-region name | its last letter, `U` or `R` |
| `feeders` | listing | number of feeder folders |
| `loads` | `Loads.dss` of the feeders | number of `New Load.` lines |
| `customers` | `Loads.dss` | number of distinct load names after removing a final `_1` or `_2` (SMART-DS writes a split-phase customer as two load objects) |
| `load_kw` | `Loads.dss` | sum of `kW` over the load lines |
| `centroid_x`, `centroid_y` | `Loads.dss`, `Buscoords.dss` | mean longitude and mean latitude of the distinct buses that carry a load (`bus1` without its phase nodes) |
| `transformers`, `transformer_kva` | `Transformers.dss` of the substation folder | number of transformers, and the sum of the `kva` of their first windings |
| `opendss_files`, `opendss_bytes` | listing | objects and bytes of the substation folder |
| `parquets_referenced` | `Loads.dss` | number of distinct load-data parquets the loads refer to |

- A load refers to a parquet through its `yearly=` load shape: the shape name without its `kw` part and its `_pu` ending is the file name in `load_data/` (`res_kw_<id>_pu` gives `res_<id>.parquet`). This is the `Parquet_Name` rule of `0_download_smartds/generate_loads_pivot.py` of the coupling. The references of every substation are written to `references_<stamp>.parquet` with the size of each parquet in the listing.

### 3. County, balancing area and climate zone

- Coordinate check. The centroids must read as longitude and latitude inside the bounding box of the Texas counties of the Census file. If one does not, no geography is assigned and the strata are `U` and `R` alone.
- County. Point in polygon of the centroid against the Texas counties (`STATEFP` 48) of the Census file. A centroid inside no Texas county gets no county and stays out of the frame.
- ASSUMPTION: the bus coordinates of the release are longitude and latitude in WGS84 degrees, and they are set against the NAD83 county boundaries with no datum shift. The release states no coordinate system. Positions in the two datums differ by up to about two metres in the United States (NOAA National Geodetic Survey, https://www.ngs.noaa.gov/2010Summit/Improving_the_NSRS.pdf), far below the size of a county.
- Balancing area by location (decision K56). Every polygon of the balancing area map that holds the centroid is found by point in polygon; a point on an edge counts as inside. Two records of the map with the same `BA` name count as one area. The ASSUMPTION on the coordinates above applies here too: the map is NAD83 geographic and the centroids are set against it with no datum shift.
  - `ba`: the names of every area holding the centroid, joined by `+`, empty when none does.
  - `ba_label`: the name of the one area holding the centroid; `none` when no area holds it; `overlap` when two or more do.
  - A substation labelled `none` or `overlap` stays out of the frame. `ba_none_overlap_<stamp>.csv` lists each with its sub-region, county, nearest area and the distance to it in km (0 for an `overlap`).
  - Distance in km: the polygon is set on a plane tangent at the point, with east-west degrees shortened by the cosine of the latitude, and one degree is `6371.0088 * pi / 180` km (mean radius of the Earth, see Sources). The nearest area is searched among the areas whose distance in degrees is at most the smallest one divided by the cosine of the latitude, since no other can be nearer in km.
- County status from EIA-861 (decision K52), kept as the column `county_status_eia861` for checking. It plays no part in the frame. The RTO is the setting `geography.balancing_area.rto` (`ERCOT` for Texas).
  1. A utility operates in the RTO when its row of the utility data sheet, read by utility number and state, holds `Y` in that RTO's column of the group "Operating in these RTOs". `N` and a blank cell both count as not operating there. A utility with several rows in the state operates in every RTO any of its rows reports.
  2. The service territory sheet lists the utilities serving each county of the state.
  3. A county is `ERCOT` (the RTO name) when every utility serving it operates in the RTO, `partial` when some do, and `outside` when none do.
  4. A utility with no utility data row in the state is left out of its counties and counted. A county left with no utility is `no_utility_row`.
  5. County names of the two sources are joined on their letters, lower case.
- Climate zone. The Building America climate zone of the county, read from the ResStock metadata. The county code of the metadata is the NHGIS GISJOIN code: the letter `G`, the state code and a zero, the county code and a zero. A county with no ResStock row takes the zone of the county with a row whose polygon centroid is nearest by great-circle angle.

### 4. Strata, selection and sizes

- Frame: the substations whose `ba_label` starts with the setting `geography.balancing_area_map.ba_prefix` (`ERCO` for Texas, whose map polygons are `ERCO-C`, `ERCO-E`, `ERCO-FW`, `ERCO-N`, `ERCO-NC`, `ERCO-S`, `ERCO-SC` and `ERCO-W`) and whose county has a climate zone. Another balancing area uses the same rule with another prefix. `ba_summary_<stamp>.csv` gives substations, feeders and loads by `ba_label` for the frame and for all substations; `counties_outside_frame_<stamp>.csv` gives them by label and county for the substations outside the frame.
- Strata: `U` or `R` crossed with the climate zone.
- Selection: ASSUMPTION: within each stratum, substations are ordered by load count and drawn by systematic selection from a random start with seed 555, so the sample spreads over substation sizes. A stratum with fewer substations than the target is taken whole.
- Detail of the selection. Ties in load count are ordered by sub-region and substation name. For a stratum of `N` substations and a target of `n`, the step is `N / n` and unit `i` of the sample is at position `floor((u + i) * N / n)` of the ordered stratum, `i = 0 ... n - 1`. The start `u` is one draw in `[0, 1)` per stratum, taken in stratum name order from `numpy.random.default_rng(555)`. Seed 555 is `random_seed` of `pipeline_config.yaml` of the coupling.
- The sample that is written and staged uses `sample.per_stratum` substations per stratum. The size table repeats the draw for each size in `sample.sizes`.
- Size table, one row per size: strata, substations, feeders, loads, customers, `opendss_gb` (bytes of the sampled substation folders), `parquets` and `parquet_gb` (distinct referenced parquets and their bytes in the listing), and for each run time per feeder `hours = feeders * minutes / 60`. A GB is 10^9 bytes. The run times are in the settings, `sample.minutes_per_feeder`, each with a label that says it was measured on one substation at the coupling pin.

### 5. Trust checks of the map

The balancing area shapes are rough: EIA says that shapes derived from the same HIFLD control areas overlap, have gaps and are not strict boundaries (see Sources). Four checks, functions of `src/dxsew/texas/trust.py` with their own tests, set the map against other sources. None changes the frame and none has a pass mark; each writes a CSV with the stamp.

1. Zone agreement (`trust_zone_agreement_<stamp>.csv`, `trust_zone_table_<stamp>.csv`). For each frame substation, the zone endings of the `yearly=` load shape names of the loads of its released `Loads.dss` files are counted, and the most common ending is kept (a tie goes to the first in sorted order). The polygon `ERCO-<code>` pairs with the ending of the zone code table whose word initials are `<code>`: `ERCO-C` Coast, `ERCO-E` East, `ERCO-FW` Far-West, `ERCO-N` North, `ERCO-NC` North-Central, `ERCO-S` South, `ERCO-SC` South-Central, `ERCO-W` West. Reported: the share of frame substations, and of their loads, whose polygon zone equals their most common ending; the share of loads whose own ending equals their polygon zone; a table of substations by most common ending (with "no ending") against polygon zone; and the largest disagreements by load count.
2. PUCT county list (`trust_county_shares_<stamp>.csv`, and `trust_puct_counties_<stamp>.csv` when the list is given). For each Texas county, the share of its substations inside polygons of the prefix. With the PUCT status of each county, the check lists every county the PUCT marks served where fewer than half its substations are inside, and every county it marks not served where any substation is inside. A county marked partly served is reported and not listed.
3. EIA-861 (`trust_eia861_<stamp>.csv`). Two groups of utilities, read from the utility data sheet as for the county status: those that mark the RTO and no other, and those that mark at least one RTO and not this one. For each group, the substations of the counties its utilities serve (each county once) and the share inside polygons of the prefix. A county pulls the first share away from 1 when any of its substations is outside, and the second away from 0 when any is inside.
4. Edges (`trust_edges_<stamp>.csv`). For each frame substation, the distance in km to the nearest polygon whose name does not start with the prefix, measured as in step 3. A coastline or a national border with no polygon beyond it is not such an edge. ASSUMPTION: 10 km marks a substation as near a balancing area edge, about the size of a large distribution substation's service area; the count shows how much of the frame depends on where the map draws its edges.

### 6. Released copies

For each sampled substation, `stage` places an untouched copy of its released `opendss` substation folder in `<data_root>/texas/released_<stamp>/<sub-region>/<substation>/`. A folder already staged under the release names is copied when its files and their sizes equal the listing. Otherwise the substation folder is listed with ListObjectsV2, the result is compared with the saved listing, and every object is downloaded. The copy must equal the listing in files and sizes. It is the reference of the checks and is never edited.

### 7. Staging with digit-only load shape names (decision K55)

Phase 7c of the coupling finds the building of a load by the digits of its load shape name and compares them as an integer with the `Yearly_Number` column of its building table, which `0_download_smartds/scan_feeders.py` fills with the third underscore field of the name. The release names carry a zone ending after the digits (`res_kw_9312-North_pu`), so that column reads as text and no row matches. Staging therefore gives every load shape a name of digits only.

Name forms, each parsed with a pattern anchored at both ends (`src/dxsew/texas/names.py`):

| Kind | Form | Example |
|---|---|---|
| Load shape of the release | `<type>_<qty>_<digits>[-<zone>]_pu`, `type` `res` or `com`, `qty` `kw` or `kvar`, `zone` letters and hyphens | `res_kw_9312-North_pu` |
| Parquet of the release | `<type>_<digits>[-<zone>].parquet` | `res_9312-North.parquet` |
| Staged load shape | `<type>_<qty>_<number>_pu` | `res_kw_931204_pu` |
| Staged parquet, as the coupling builds it | `<type>_<number>.parquet` | `res_931204.parquet` |

Any token of a `.dss` file that starts with `res_` or `com_` (any case) looks like a load shape name. A token of that kind that fits no form stops the staging of its substation, and the file and line are reported.

New name. The new number is the released digits followed by the two-digit code of the zone ending. With `North` coded `04`, `res_kw_9312-North_pu` becomes `res_kw_931204_pu` and its parquet `res_9312-North.parquet` becomes `res_931204.parquet`. The kw and kvar names of one building get the same number, because phase 7b finds the kvar profile by replacing `_kw_` with `_kvar_` in the kw name. The code has a fixed width, so two released names never get the same new number.

Zone code table, `configs/texas_zone_codes.csv`, columns `zone_ending,code`:

- Codes are labels standing only for their zone ending. A code once committed never changes meaning, and a new ending takes the next free code.
- The first version holds every distinct zone ending of the load-data parquets and profiles of the whole Full Texas 2018 release, sorted and coded `01`, `02` and onward. The empty ending, a name with no zone, has code `00`.
- `names` stops when an ending holds an underscore or when more than 99 endings would need a code. `stage` stops a substation that has an ending missing from the table.

Staged tree. For each sampled substation, `<smart_ds_base>/<staging.folder>/<sub-region>/base_timeseries/opendss/<substation>/` is rebuilt from the released copy. In every `.dss` file each load shape name is replaced as a whole token (a run of letters, digits, underscores and hyphens), including the profile paths of `LoadShapes.dss`; every other byte is kept. Other files are copied as they are. A substation staged earlier is rebuilt the same way. The release keeps the circuits under `<sub-region>/scenarios/base_timeseries/opendss/`; the staged path drops `scenarios`.

Parquets, in the flat folder `<smart_ds_base>/<staging.folder>/parquet_data/`. For each released parquet the staged loads refer to, the code computes the SHA-256 of the released file and gives the file its new name: renamed in place when the released name is present, downloaded first when it is absent. When the new name is already there, its SHA-256 must equal the released file's; otherwise the substation stops. The EULP donor folders in the same folder are not touched. A parquet that a substation outside the sample also refers to is renamed all the same, and `parquets_shared_with_left_<stamp>.csv` lists those cases.

The profile CSV files of the release (`profiles/`) are not staged. Phase 7b rewrites every `New Loadshape` line of a feeder to the day profiles that phases 5c and 6 built (`7_circuit_instantiation/instantiate_circuits.py`).

Name map, `<staging.folder>/name_map.csv`, copied to `<data_root>/texas/name_map_<stamp>.csv`. One row per name:

| Column | Content |
|---|---|
| `released_name`, `new_name` | the two names |
| `kind` | `loadshape` or `parquet` |
| `type`, `qty` | `res` or `com`; `kw` or `kvar` (empty for a parquet) |
| `number` | the released digits |
| `zone_ending`, `code` | the zone ending (empty for none) and its code |
| `source` | OEDI key of the released file: the profile CSV of a load shape, the load-data parquet of a parquet; empty when the release has no such profile |
| `sha256` | SHA-256 of the released parquet (parquets only) |

Checks, functions of `names` with their own tests, run on every sampled substation. A failed check stops the staging of that substation, removes its staged folder and reports the file and line.

1. Same loads: per feeder, the count of `New Load` lines and the sum of their kW in the staged `Loads.dss` equal the released copy.
2. Clean: applying the map forward to each released `.dss` file gives the staged file byte for byte, other files are equal as they are, and every staged parquet has the SHA-256 of its released file.
3. Nothing lost: applying the map backwards to each staged `.dss` file gives the released file byte for byte.
4. Map: the map is one to one; every load shape name and every parquet name the coupling builds from the staged loads is in it; no name with a zone ending remains in a staged `.dss` file.

Disk guard. Before each download the guard checks the two limits given on the command line. If the staging folder would pass `--max-staging-gb`, or free space on its drive would fall below `--min-free-gb`, staging stops and `staging_<stamp>.csv` says where. A download into the released copies counts against the free-space limit only.

### 8. Coupling settings

`settings` writes, for the staged substations (all of them, or those named with `--substation`):

- `<data_root>/texas/coupling_settings_<stamp>/`, the folder that `run_case.py --settings` merges over the coupling's own files:
  - `local.yaml`: every key of the coupling `local.yaml` given with `--base-local`, each `--extra-key` read from `--extra-local` (for example `evwatts_extract_dir`, which the EV cars step reads), and `runs_root` set to the absolute folder of `--runs-root`.
  - `topology_registry.yaml`: one entry per substation with the keys `topology_state`, `geography` (the county), `urbanicity` (the sub-region), `feeder_id` and `circuit_folder` (the substation), `smart_ds_relpath` (`<staging.folder>/<sub-region>/base_timeseries/opendss`), `parquet_relpath` (`<staging.folder>/parquet_data`), `status: ready` and `description`.
  - `pipeline_config.yaml`: `peak_day_rule: given`.
  - `given_days.csv`: columns `Feeder`, `Season`, `Month`, `Day`, one row per feeder and season, the feeder named as its SMART-DS feeder folder. `--days` gives either this table or the columns `Season`, `Month` and `Day` alone, which then apply to every feeder.
- `<data_root>/texas/cases_<stamp>/<case_id>.yaml`, one case per substation, with the donor of `coupling.donor_id` and `max_feeders` equal to the feeder count of the substation.

The coupling resolves the circuit root as `<smart_ds_base>/<smart_ds_relpath>/<circuit_folder>` and the parquet root as `<smart_ds_base>/<parquet_relpath>` (`run_case.py`, `resolve_case`). Machine paths go only into these generated files.

## Outputs

Under `<data_root>/texas/`, with the stamp of the run:

| File | Content |
|---|---|
| `listing_by_top_folder_<stamp>.csv`, `listing_by_subregion_<stamp>.csv` | object counts and bytes of the release |
| `frame_<stamp>.parquet` | one row per substation with the values of step 2 and the geography of step 3, including `ba`, `ba_label` and `county_status_eia861` |
| `references_<stamp>.parquet` | one row per substation and referenced parquet |
| `ba_summary_<stamp>.csv` | substations, feeders and loads by `ba_label`, for the frame and for all substations |
| `ba_none_overlap_<stamp>.csv` | substations labelled `none` or `overlap`, with the nearest area and its distance in km |
| `counties_outside_frame_<stamp>.csv` | substations, feeders and loads by `ba_label` and county for the substations outside the frame |
| `trust_zone_agreement_<stamp>.csv`, `trust_zone_table_<stamp>.csv`, `trust_county_shares_<stamp>.csv`, `trust_puct_counties_<stamp>.csv`, `trust_eia861_<stamp>.csv`, `trust_edges_<stamp>.csv` | the trust checks of the map |
| `sample_<stamp>.csv` | the drawn substations |
| `sizes_<stamp>.csv` | the size table |
| `released_<stamp>/` | the released copies |
| `staging_<stamp>.csv` | staged or not, per sampled substation, with the source of its released copy and its parquet actions |
| `checks_<stamp>.csv` | the four checks per substation |
| `name_map_<stamp>.csv` | the name map |
| `left_sample_<stamp>.csv`, `parquets_shared_with_left_<stamp>.csv` | substations of the earlier sample that left this one, and the parquets they share with it |
| `coupling_settings_<stamp>/`, `cases_<stamp>/` | settings folder and case files |

## Known limits

- The frame follows the polygons of the map, which are rough near their edges; the trust checks report how far it agrees with the zone endings of the release, the PUCT county list and EIA-861.
- A substation is placed by the centroid of its load buses; a substation whose loads span two balancing areas is counted in one.
- In the county status kept for checking, a utility whose utility data row reports no RTO counts as not operating in the RTO, and a utility whose row is filed under another state is left out.
- The climate zone and the county of a substation are those of the centroid of its load buses; a substation that spans two counties is counted in one.
- Staged parquets carry new names, so a substation staged earlier under the release names no longer finds a parquet it shares with the current sample.
