# Stratified Texas sample of whole substations

This note describes how dx-sew draws a stratified sample of whole SMART-DS substations in Texas, stages each sampled substation for the coupling with digit-only load shape names, and writes the coupling settings. The code is in `src/dxsew/texas/`, its settings are in `configs/texas_sample.yaml` and `configs/texas_zone_codes.csv`, and its tests are in `tests/texas/`. How dx-sew runs the coupling is in [coupling.md](coupling.md).

Nothing the code produces is stored in the repository. Listings, source files, frames, samples, size tables, released copies, name maps and coupling settings are written under `data_root`; the staged circuits and parquets are written under `smart_ds_base`.

## Commands

Run from the repository, in this order:

```
uv run python -m dxsew.texas.sample --stamp <stamp> --minutes-per-feeder <minutes>
uv run python -m dxsew.texas.names
uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB> [--previous-sample <csv>]
uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB> --check-only
uv run python -m dxsew.texas.settings --stamp <stamp> --runs-root <folder> --base-local <file> \
    [--extra-local <file> --extra-key <key>] --days <csv> [--substation <subregion>/<substation>]
```

- `sample` lists the release (or reuses the saved listing), builds the frame, assigns the geography, draws the sample and writes the size table. `--stamp` sets the stamp of the output names (default: the current UTC time). `--minutes-per-feeder` may be repeated; each value adds one run-hours column to the size table. `--refresh-listing` lists the release again.
- `names` writes `configs/texas_zone_codes.csv` from the saved listing, or extends it with endings it lacks. Codes already in the table keep their meaning.
- `stage` places the released copies, builds the staged tree with digit-only names, renames the parquets, writes the name map and runs the four checks. The two disk limits are arguments because they belong to the machine. `--previous-sample` names the sample CSV of an earlier staging; its substations that left the sample are listed, with the parquets they share with this one. `--check-only` runs the four checks again on the staged tree and changes nothing; it exits with 1 when a check fails.
- `settings` writes the settings folder and the case files of the staged substations (section "Coupling settings").

`configs/local.yaml` (not tracked) must define `data_root`, `smart_ds_base` and `eulp_metadata_root`; `coupling_repo` and `coupling_python` are read by `dxsew.coupling`.

## Sources

| Input | Source |
|---|---|
| Circuits and load data | SMART-DS v0.9 Full Texas, year 2018, on the Open Energy Data Initiative data lake: `https://oedi-data-lake.s3.amazonaws.com`, prefix `SMART-DS/v0.9/2018/Full_Texas/`. Browser view: https://data.openei.org/s3_viewer?bucket=oedi-data-lake&prefix=SMART-DS%2Fv0.9%2F2018%2FFull_Texas%2F |
| Listing method | Amazon S3 ListObjectsV2, called without sign-in: https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html |
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

### 3. County, RTO and climate zone

- Coordinate check. The centroids must read as longitude and latitude inside the bounding box of the Texas counties of the Census file. If one does not, no geography is assigned and the strata are `U` and `R` alone.
- County. Point in polygon of the centroid against the Texas counties (`STATEFP` 48) of the Census file. A centroid inside no Texas county gets no county and stays out of the frame.
- ASSUMPTION: the bus coordinates of the release are longitude and latitude in WGS84 degrees, and they are set against the NAD83 county boundaries with no datum shift. The release states no coordinate system. Positions in the two datums differ by up to about two metres in the United States (NOAA National Geodetic Survey, https://www.ngs.noaa.gov/2010Summit/Improving_the_NSRS.pdf), far below the size of a county.
- RTO status of a county (decision K52). The RTO is the setting `geography.balancing_area.rto` (`ERCOT` for Texas); CAISO or NYISO use the same rule with another value.
  1. A utility operates in the RTO when its row of the utility data sheet, read by utility number and state, holds `Y` in that RTO's column of the group "Operating in these RTOs". `N` and a blank cell both count as not operating there. A utility with several rows in the state operates in every RTO any of its rows reports.
  2. The service territory sheet lists the utilities serving each county of the state.
  3. A county is `ERCOT` (the RTO name) when every utility serving it operates in the RTO, `partial` when some do, and `outside` when none do.
  4. A utility with no utility data row in the state is left out of its counties and counted. A county left with no utility is `no_utility_row`.
  5. County names of the two sources are joined on their letters, lower case.
- Climate zone. The Building America climate zone of the county, read from the ResStock metadata. The county code of the metadata is the NHGIS GISJOIN code: the letter `G`, the state code and a zero, the county code and a zero. A county with no ResStock row takes the zone of the county with a row whose polygon centroid is nearest by great-circle angle.

### 4. Strata, selection and sizes

- Frame: the substations whose county has the RTO status. Substations of `partial`, `outside` and `no_utility_row` counties, and substations with no county, stay out. `counties_outside_frame_<stamp>.csv` gives their substations, feeders and loads by county.
- Strata: `U` or `R` crossed with the climate zone.
- Selection: ASSUMPTION: within each stratum, substations are ordered by load count and drawn by systematic selection from a random start with seed 555, so the sample spreads over substation sizes. A stratum with fewer substations than the target is taken whole.
- Detail of the selection. Ties in load count are ordered by sub-region and substation name. For a stratum of `N` substations and a target of `n`, the step is `N / n` and unit `i` of the sample is at position `floor((u + i) * N / n)` of the ordered stratum, `i = 0 ... n - 1`. The start `u` is one draw in `[0, 1)` per stratum, taken in stratum name order from `numpy.random.default_rng(555)`. Seed 555 is `random_seed` of `pipeline_config.yaml` of the coupling.
- The sample that is written and staged uses `sample.per_stratum` substations per stratum. The size table repeats the draw for each size in `sample.sizes`.
- Size table, one row per size: strata, substations, feeders, loads, customers, `opendss_gb` (bytes of the sampled substation folders), `parquets` and `parquet_gb` (distinct referenced parquets and their bytes in the listing), and for each `--minutes-per-feeder` value `hours = feeders * minutes / 60`. A GB is 10^9 bytes.

### 5. Released copies

For each sampled substation, `stage` places an untouched copy of its released `opendss` substation folder in `<data_root>/texas/released_<stamp>/<sub-region>/<substation>/`. A folder already staged under the release names is copied when its files and their sizes equal the listing. Otherwise the substation folder is listed with ListObjectsV2, the result is compared with the saved listing, and every object is downloaded. The copy must equal the listing in files and sizes. It is the reference of the checks and is never edited.

### 6. Staging with digit-only load shape names (decision K55)

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

### 7. Coupling settings

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
| `frame_<stamp>.parquet` | one row per substation with the values of step 2 and the geography of step 3 |
| `references_<stamp>.parquet` | one row per substation and referenced parquet |
| `counties_outside_frame_<stamp>.csv` | substations, feeders and loads by county for `partial`, `outside` and `no_utility_row` counties |
| `sample_<stamp>.csv` | the drawn substations |
| `sizes_<stamp>.csv` | the size table |
| `released_<stamp>/` | the released copies |
| `staging_<stamp>.csv` | staged or not, per sampled substation, with the source of its released copy and its parquet actions |
| `checks_<stamp>.csv` | the four checks per substation |
| `name_map_<stamp>.csv` | the name map |
| `left_sample_<stamp>.csv`, `parquets_shared_with_left_<stamp>.csv` | substations of the earlier sample that left this one, and the parquets they share with it |
| `coupling_settings_<stamp>/`, `cases_<stamp>/` | settings folder and case files |

## Known limits

- The frame keeps whole counties. A substation of a `partial` county is left out even when its own utility operates in the RTO.
- A utility whose utility data row reports no RTO counts as not operating in the RTO, and a utility whose row is filed under another state is left out.
- The climate zone and the county of a substation are those of the centroid of its load buses; a substation that spans two counties is counted in one.
- Staged parquets carry new names, so a substation staged earlier under the release names no longer finds a parquet it shares with the current sample.
