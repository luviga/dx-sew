# Frame procedure for any balancing area

This note gives the frame procedure of dx-sew so it can be applied to a balancing area other than ERCOT. The frame is the list of SMART-DS substations from which a sample is drawn. The procedure is written once; an area enters it through three names and a few settings. The Texas sample applies it with the ERCOT names; [texas_sample.md](texas_sample.md) has the details of every Texas step, and the per-area settings are in `configs/veto_areas.yaml`.

## Steps

### 1. Locate each substation

Each substation is placed at the centroid of its load buses: the mean longitude and the mean latitude of the distinct buses that carry a load. The county of the substation is the Census county whose polygon holds the centroid. A substation outside every county of its state gets no county and stays out of the frame. The coordinates of the release are read as longitude and latitude, with the ASSUMPTION written in [texas_sample.md](texas_sample.md), step 3.

### 2. Place it by the map (decision K56)

A substation may enter the frame when its centroid lies inside exactly one polygon of the balancing area map shipped by PyPSA-USA whose name starts with the polygon prefix of the area. The prefix is a setting (`ba_prefix`), so another area uses the same rule with another prefix. A centroid in no polygon, or in two, stays out. The map is the first and main step: the steps below can only take substations out again.

### 3. The EIA-861 veto with its two lookups (decisions K57 and K58)

The veto reads three files of one Form EIA-861 year: the service territory file (the utilities serving each county), the utility data file (the column group "Operating in these RTOs", where each utility marks the ISOs and RTOs it operates in, one row per utility and state), and the sales to ultimate customers file with the short form file (the balancing authority code of each utility's rows, column `BA Code`). The area enters through `rto`, the column name of the RTO group, and `ba_code`, the code on the sales rows.

Answers. Each utility serving a county gets one answer:

1. A utility with a row in the state that marks at least one RTO keeps its marks.
2. Lookup 1: a utility with no row in the state takes the union of the marks of its rows in other states. If those rows mark nothing, it goes on to lookup 2.
3. Lookup 2: a utility that still has no mark takes the balancing authority codes of its own sales and short form rows, in the state first and in any state if it has none there. A code equal to `ba_code` counts as marking `rto`. Any other code counts as another area, written `BA:<code>`, whether or not that area is an RTO.
4. Otherwise the answer is unknown.

Rule. A county is vetoed, and its substations leave the frame, when every utility serving it has an answer, every answer marks at least one area, none marks `rto`, and every answer marks at least one area that is neither `rto` nor an ignored mark (`veto_ignore_marks`, `Other` by default, since the column `Other` names no RTO). A county that is not vetoed gets the first reason that applies: `reports_rto`, `missing_row`, `no_rto_marked`, `ignored_mark_only`. A vetoed county gets `other_rto_only`.

What never removes: an unknown answer, an ignored mark alone, and a mixed county where some utility marks `rto`. A mixed county keeps the answer of the map.

What never adds: the veto only takes substations out of the frame. A substation the map leaves out stays out whatever the veto reads, and the lookups never change the answer of a utility whose row in the state marks an RTO.

Settings. `veto` turns the step on. `veto_lookups` turns the two lookups on; with it off, the veto is the K57 rule alone, on the rows of the state. `ba_code` is required when the lookups are on.

Standard outputs, written before anything is applied: the county table (every county with its utilities, the answer of each and where it came from, the reason and veto with and without the lookups) and the list of looked-up utilities (every utility of the state a lookup touched, with the lookup, the rows or codes it found, its answer and the counties it serves). The review of a new area starts from these two lists.

The veto can also be run alone, with no SMART-DS data, for the counties of an area's states:

```
uv run python -m dxsew.veto --areas configs/veto_areas.yaml --area <name> --stamp <stamp>
```

It writes `veto_area_<name>_<stamp>.csv` (one row per county: Census code, the share of the county inside polygons of the prefix, the veto with and without the lookups, the utility marks and a class) and `veto_lookups_<name>_<stamp>.csv`. The class sorts counties by the map and the veto: `map_in_veto_out`, `map_in_kept`, `map_out_utilities_in` and `map_out`. A county counts as in the map when at least half of its area lies inside the polygons of the prefix; that threshold is an ASSUMPTION of the report only, and shares are computed on longitude and latitude degrees (ASSUMPTION: within one county this differs from an equal-area share by a negligible amount). The report changes no frame.

### 4. Strata

The strata are the kind of sub-region (urban or rural, the last letter of its name) crossed with the Building America climate zone of the substation's county, from the ResStock metadata of the state. A substation with no climate zone stays out of the frame.

### 5. Systematic draw

Within each stratum the substations are ordered by load count and drawn by systematic selection from a random start, with the seed of the settings (ASSUMPTION, labelled in the settings). A stratum smaller than the target is taken whole.

### 6. Staging and runs at the coupling pin

The sampled substations are staged with digit-only load shape names, checked, and run with DeltaE/dss-eulp-coupling at the one commit recorded in `configs/coupling_pin.yaml`. See [texas_sample.md](texas_sample.md), steps 6 to 8, and [coupling.md](coupling.md).

## The three names per area

| Area | `rto` (utility data column) | `ba_code` (sales rows) | `ba_prefix` (map polygons) | States |
|---|---|---|---|---|
| ERCOT | `ERCOT` | `ERCO` | `ERCO` | TX |
| CAISO | `CAISO` | `CISO` | `CISO` | CA |
| NYISO | `NYISO` | `NYIS` | `NYISO` | NY |

Each `ba_code` was checked against the sales rows of the area's states before use.

## What an area needs beyond the three names

The three names are enough for an RTO whose column, code and polygons all exist. A run over the whole country needs more:

- The list of states of each area. An RTO can serve parts of several states, and the veto reads the counties state by state.
- A prefix for each RTO column of the utility data file. The map names the polygons of most RTOs with a prefix, but the column `Other` names no RTO and has no polygons.
- A rule for areas that are not RTOs. Many map polygons are single balancing authorities named by their code (for example `LDWP`, `BANC`, `TVA`), with no column in the RTO group; for them the veto could only read the codes of the sales rows.
- A table for the regional polygons of the map (`Arizona`, `Carolina`, `Florida`), which merge several balancing authorities under one name that is not a code of the sales rows.
- A decision for the codes of the sales rows that have no polygon on the map, including those of Alaska and Hawaii, where utilities are left unknown after both lookups.
- A check of lookup 1 for utilities with rows in many states, whose joined marks can name RTOs that do not serve the county.
- The same reading for another EIA-861 year, if the frame moves from 2018: the file names, the sheet layout and the codes can change between years.

## Sources

| Input | Source |
|---|---|
| Utilities per county, RTO marks, balancing authority codes | U.S. Energy Information Administration, Form EIA-861 detailed data files, 2018: https://www.eia.gov/electricity/data/eia861/ (file https://www.eia.gov/electricity/data/eia861/archive/zip/f8612018.zip): `Service_Territory_2018.xlsx` (sheet `Counties_States`), `Utility_Data_2018.xlsx` (sheet `States`, column group "Operating in these RTOs"), `Sales_Ult_Cust_2018.xlsx` (sheet `States`, column `BA Code`), `Short_Form_2018.xlsx` (sheet `861S`, column `BA Code`), `Balancing_Authority_2018.xlsx` (codes and names). PUDL publishes the RTO marks as `core_eia861__yearly_utility_data_rto`: https://data.catalyst.coop/preview/pudl/core_eia861__yearly_utility_data_rto |
| Balancing area map | PyPSA-USA, `workflow/repo_data/geospatial/BA_shapes_new/Modified_BE_BA_Shapes` (field `BA`): https://github.com/PyPSA/pypsa-usa/tree/3d67e2061e02a5b983ed9807a1b2668e7fdf6fd4/workflow/repo_data/geospatial/BA_shapes_new . License MIT: https://github.com/PyPSA/pypsa-usa/blob/3d67e2061e02a5b983ed9807a1b2668e7fdf6fd4/LICENSE.md . Cite as K. Tehranchi, T. Barnes, M. Frysztacki, I. M. L. Azevedo, "Costs and benefits of transmission investments in the United States under different policy scenarios", Environmental Research Letters 21 (2026) 194039, https://doi.org/10.1088/1748-9326/aea586 ; software "PyPSA-USA: An Open-Source Energy System Optimization Model for the United States", https://doi.org/10.5281/zenodo.10815964 |
| County boundaries | U.S. Census Bureau, TIGER/Line 2018 counties: https://www2.census.gov/geo/tiger/TIGER2018/COUNTY/tl_2018_us_county.zip |
| State codes of the county file | U.S. Census Bureau, State FIPS codes: https://www2.census.gov/geo/docs/reference/state.txt |
| Climate zone per county | ResStock 2024 release 2 metadata, columns `in.county` and `in.building_america_climate_zone`: https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/resstock_tmy3_release_2/ |
| Circuits | SMART-DS v0.9, year 2018, on the Open Energy Data Initiative data lake: https://data.openei.org/s3_viewer?bucket=oedi-data-lake&prefix=SMART-DS%2Fv0.9%2F2018%2F |
| Coupling | DeltaE/dss-eulp-coupling: https://github.com/DeltaE/dss-eulp-coupling |
