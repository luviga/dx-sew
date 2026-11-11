# Stratified Texas sample of whole substations

This note describes how dx-sew draws a stratified sample of whole SMART-DS substations in Texas, stages each sampled substation for the coupling, and writes the coupling settings. The code is in `src/dxsew/texas/`, its settings are in `configs/texas_sample.yaml`, and its tests are in `tests/texas/`.

Nothing the code produces is stored in the repository. Listings, source files, frames, samples, size tables and coupling settings are written under `data_root`; the staged circuits and parquets are written under `smart_ds_base`.

## Commands

Run from the repository, in this order:

```
uv run python -m dxsew.texas.sample --minutes-per-feeder <minutes>
uv run python -m dxsew.texas.stage --stamp <stamp> --max-staging-gb <GB> --min-free-gb <GB>
uv run python -m dxsew.texas.settings --stamp <stamp>
```

- `sample` lists the release (or reuses the saved listing), builds the frame, assigns the geography, draws the sample and writes the size table. `--stamp` sets the stamp of the output names (default: the current UTC time). `--minutes-per-feeder` may be repeated; each value adds one run-hours column to the size table. `--refresh-listing` lists the release again.
- `stage` downloads the sampled substations and the load-data parquets they refer to. The two disk limits are arguments because they belong to the machine.
- `settings` writes the coupling settings of the substations that were staged.

`configs/local.yaml` (not tracked) must define `data_root`, `smart_ds_base` and `eulp_metadata_root`.

## Sources

| Input | Source |
|---|---|
| Circuits and load data | SMART-DS v0.9 Full Texas, year 2018, on the Open Energy Data Initiative data lake: `https://oedi-data-lake.s3.amazonaws.com`, prefix `SMART-DS/v0.9/2018/Full_Texas/`. Browser view: https://data.openei.org/s3_viewer?bucket=oedi-data-lake&prefix=SMART-DS%2Fv0.9%2F2018%2FFull_Texas%2F |
| Listing method | Amazon S3 ListObjectsV2, called without sign-in: https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html |
| County boundaries | U.S. Census Bureau, TIGER/Line 2018 counties: https://www2.census.gov/geo/tiger/TIGER2018/COUNTY/tl_2018_us_county.zip |
| Utilities per county and their balancing authority | U.S. Energy Information Administration, Form EIA-861 detailed data files, 2018: https://www.eia.gov/electricity/data/eia861/ (file https://www.eia.gov/electricity/data/eia861/archive/zip/f8612018.zip) |
| Climate zone per county | ResStock 2024 release 2, Texas baseline metadata, columns `in.county` and `in.building_america_climate_zone`: https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/resstock_tmy3_release_2/ |
| County code form of the metadata | IPUMS NHGIS, GISJOIN identifiers: https://www.nhgis.org/geographic-crosswalks |
| Coupling inputs | DeltaE/dss-eulp-coupling: https://github.com/DeltaE/dss-eulp-coupling (`run_case.py`, `configs/topology_registry.yaml`, `configs/cases/`, `0_download_smartds/`, `7_circuit_instantiation/instantiate_circuits.py`) |

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
- Balancing-area status of a county, from EIA-861 2018. The sheet `Counties_States` of `Service_Territory_2018.xlsx` lists the counties each utility serves. The sheet `States` of `Sales_Ult_Cust_2018.xlsx` gives the balancing authority code (`BA Code`) of each utility in Texas. A county is `ERCOT` when every utility that serves it has only the code `ERCO`, `partial` when some have it, and `outside` when none has it. A utility with no sales row in Texas is left out; a county left with no utility is `no_sales_row`. County names of the two sources are joined on their letters, lower case.
- Climate zone. The Building America climate zone of the county, read from the ResStock metadata. The county code of the metadata is the NHGIS GISJOIN code: the letter `G`, the state code and a zero, the county code and a zero. A county with no ResStock row takes the zone of the county with a row whose polygon centroid is nearest by great-circle angle.

### 4. Strata, selection and sizes

- Frame: the substations whose county has the status `ERCOT`. Substations of `partial`, `outside` and `no_sales_row` counties, and substations with no county, stay out and are counted.
- Strata: `U` or `R` crossed with the climate zone.
- Selection: ASSUMPTION: within each stratum, substations are ordered by load count and drawn by systematic selection from a random start with seed 555, so the sample spreads over substation sizes. A stratum with fewer substations than the target is taken whole.
- Detail of the selection. Ties in load count are ordered by sub-region and substation name. For a stratum of `N` substations and a target of `n`, the step is `N / n` and unit `i` of the sample is at position `floor((u + i) * N / n)` of the ordered stratum, `i = 0 ... n - 1`. The start `u` is one draw in `[0, 1)` per stratum, taken in stratum name order from `numpy.random.default_rng(555)`. Seed 555 is `random_seed` of `pipeline_config.yaml` of the coupling.
- The sample that is written and staged uses `sample.per_stratum` substations per stratum. The size table repeats the draw for each size in `sample.sizes`.
- Size table, one row per size: strata, substations, feeders, loads, customers, `opendss_gb` (bytes of the sampled substation folders), `parquets` and `parquet_gb` (distinct referenced parquets and their bytes in the listing), and for each `--minutes-per-feeder` value `hours = feeders * minutes / 60`. A GB is 10^9 bytes.

### 5. Staging

For each sampled substation, in sample order:

1. Its `opendss` substation folder is downloaded to `<smart_ds_base>/<staging.folder>/<sub-region>/base_timeseries/opendss/<substation>/`.
2. The load-data parquets its loads refer to are read from the staged `Loads.dss` files and downloaded, flat, into `<smart_ds_base>/<staging.folder>/parquet_data/`. A file already there with the listed size is skipped.
3. Before each download the disk guard checks the two limits given on the command line. If the staging folder would pass `--max-staging-gb`, or free space on its drive would fall below `--min-free-gb`, staging stops; `staging_<stamp>.csv` marks each substation as staged or not and says where it stopped.

The profile CSV files of the release (`profiles/`) are not staged. No phase of the coupling reads them: phase 7b rewrites every `New Loadshape` line of a feeder to the day profiles that phases 5c and 6 built (`7_circuit_instantiation/instantiate_circuits.py`).

### 6. Coupling settings

`settings` writes, under `<data_root>/texas/coupling_settings/`:

- `topology_registry.yaml`, one entry per staged substation with the keys of the coupling registry: `topology_state`, `geography` (the county), `urbanicity` (the sub-region), `feeder_id` and `circuit_folder` (the substation), `smart_ds_relpath`, `parquet_relpath`, `status` and `description`.
- `cases/<case_id>.yaml`, one case per staged substation, with the donor of `coupling.donor_id` and `max_feeders` equal to the feeder count of the substation.

The coupling resolves the circuit root as `<smart_ds_base>/<smart_ds_relpath>/<circuit_folder>` and the parquet root as `<smart_ds_base>/<parquet_relpath>` (`run_case.py`).

## Outputs

Under `<data_root>/texas/`, with the stamp of the run:

| File | Content |
|---|---|
| `listing_by_top_folder_<stamp>.csv`, `listing_by_subregion_<stamp>.csv` | object counts and bytes of the release |
| `frame_<stamp>.parquet` | one row per substation with the values of step 2 and the geography of step 3 |
| `references_<stamp>.parquet` | one row per substation and referenced parquet |
| `sample_<stamp>.csv` | the drawn substations |
| `sizes_<stamp>.csv` | the size table |
| `staging_<stamp>.csv` | staged or not, per sampled substation |
| `coupling_settings/` | registry and case files |

## Known limits

- The frame keeps whole counties. A substation of a `partial` county is left out even when its own utility is in ERCOT.
- The EIA-861 sales sheet has no Texas row for a utility that only delivers power for retail providers. Such a utility is left out of the status of its counties.
- The climate zone and the county of a substation are those of the centroid of its load buses; a substation that spans two counties is counted in one.
