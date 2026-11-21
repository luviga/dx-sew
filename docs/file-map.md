# File map

Every tracked file of the repository and every kind of generated file, with what writes it and what reads it. Generated files live in the workspace, the folder `data_root` of `config/local.yaml`, and under `smart_ds_base` for the staged circuits; none is tracked. `<out>` below is `<data_root>/<output_folder>`, with `output_folder` taken from the setup (`texas` for `ercot_2018`).

## Tracked files

### Root

| File | What it is | Read by |
|---|---|---|
| `README.md` | Overview, layout, commands | people |
| `AGENTS.md` | Rules for coding agents | coding agents |
| `CONTRIBUTING.md` | Branches, commits, tests | people |
| `CITATION.cff` | How to cite the software | GitHub, citation tools |
| `LICENSE` | Apache License 2.0 | people |
| `pyproject.toml` | Package metadata, dependencies, build backend (`module-root = ""`) | uv; `dxsew.config.project_root` looks for this file |
| `uv.lock` | Locked dependency versions | uv |
| `.gitignore` | Files git leaves out (`config/local.yaml`, environments, data, parquet and zip files) | git |

### config/

| File | What it is | Read by |
|---|---|---|
| `config/setups/registry.json` | Setup ids, the file of each, the default id | `dxsew.config` |
| `config/setups/ercot_2018.yaml` | Every value of ERCOT in 2018, with sources and ASSUMPTION sentences | every entry point, through `dxsew.config` |
| `config/areas.yaml` | Names, states, RTO, balancing authority code and map prefix of each area | `dxsew.config` (setup check), `dxsew.frame.veto` |
| `config/units.yaml` | Unit conversions and calendar facts | `dxsew.config.load_units` and the modules that convert |
| `config/runtime.yaml` | Retries, timeouts, chunk sizes, worker counts, print widths, key names | `dxsew.config.load_runtime` |
| `config/scan.yaml` | Terms the scan test bans from strings in code | `tests/regression/test_no_data_in_code.py` |
| `config/sources.yaml` | Every URL the code downloads, with where it was found | `dxsew.sources` |
| `config/coupling_pin.yaml` | Repository and commit of DeltaE/dss-eulp-coupling | `dxsew.runs.coupling` |
| `config/local.yaml.example` | Template of `config/local.yaml` | people |

### inputs/

| File | What it is | Written by | Read by |
|---|---|---|---|
| `inputs/smartds_zone_codes.csv` | Two-digit code of each zone ending of the SMART-DS load shape names | `dxsew.sources.smartds_names` (main) | `dxsew.runs.stage`, `dxsew.runs.cases`, `dxsew.frame.trust` |
| `inputs/state_fips.csv` | State FIPS code by postal code (U.S. Census Bureau) | by hand from the source named in the setup | `dxsew.frame.veto` (report) |

### dxsew/

| Module | Role | Writes |
|---|---|---|
| `dxsew/__init__.py` | Version | |
| `dxsew/config.py` | Project root, registry, setups, shared settings, local paths, `--setup` | |
| `dxsew/sources/__init__.py` | Download of one source file | `<data_root>/eia/` |
| `dxsew/sources/eia861.py` | EIA-861 sales and customers by class | |
| `dxsew/sources/eia930.py` | EIA-930 hourly demand | |
| `dxsew/sources/eia_state_profile.py` | EIA State Electricity Profile, Table 10 | |
| `dxsew/sources/oedi.py` | Listing and download of the OEDI data lake | `<data_root>/oedi/` |
| `dxsew/sources/smartds_dss.py` | Readers of the SMART-DS OpenDSS text files | |
| `dxsew/sources/smartds_names.py` | Digit-only load shape names, zone codes, name map and checks | `inputs/smartds_zone_codes.csv` (main) |
| `dxsew/frame/veto.py` | EIA-861 veto of any area and its report | `<out>/veto_area_<area>_<stamp>.csv`, `veto_lookups_<area>_<stamp>.csv` |
| `dxsew/frame/ba_map.py` | Balancing area of a point on the map | `<data_root>/geo/` (map files) |
| `dxsew/frame/geo.py` | Counties, RTO status, climate zones | `<data_root>/census/` |
| `dxsew/frame/frame.py` | Frame of whole substations | `<data_root>/oedi/files/` |
| `dxsew/frame/sample.py` | Frame, geography, veto and the stratified sample | see generated files |
| `dxsew/frame/trust.py` | Four checks of the map | `<out>/trust_*_<stamp>.csv` |
| `dxsew/frame/features.py` | Features of every frame feeder | `<out>/features_<stamp>.parquet` |
| `dxsew/frame/groups.py` | Groups, dial, typical feeders | see generated files |
| `dxsew/curves/year.py` | Year curves of the run feeders and common days | `<out>/curves_<stamp>/` |
| `dxsew/fit/target.py` | Hourly fit target | `<data_root>/derived/` |
| `dxsew/fit/weights.py` | Fit of the weights and sector factors | `<out>/fit_<stamp>/` |
| `dxsew/runs/stage.py` | Staging with digit-only names | `<smart_ds_base>/<staging.folder>/`, `<out>/` |
| `dxsew/runs/settings.py` | Settings folder and cases of staged substations | `<out>/coupling_settings_<stamp>/`, `<out>/cases_<stamp>/` |
| `dxsew/runs/cases.py` | Run feeders alone: staging, alone trees, settings, cases, dry run | `<smart_ds_base>/<runs.*_folder>/`, `<out>/` |
| `dxsew/runs/coupling.py` | Pin check and runs of the coupling | the log file given |
| `dxsew/frame/__init__.py`, `dxsew/curves/__init__.py`, `dxsew/fit/__init__.py`, `dxsew/runs/__init__.py` | Package markers | |

### docs/

| File | What it is |
|---|---|
| `docs/file-map.md` | This page |
| `docs/configuration.md` | Every key of the setup and the shared settings |
| `docs/adapting.md` | How to run another balancing area |
| `docs/glossary.md` | Terms used across the code and notes |
| `docs/frame_procedure.md` | Frame procedure for any balancing area |
| `docs/texas_sample.md` | Sample, trust checks, staging and coupling settings of the ERCOT setup |
| `docs/groups.md` | Features, groups, dial, typical feeders |
| `docs/runs.md` | Run feeders alone through the coupling |
| `docs/coupling.md` | The coupling pin and how it is run |
| `docs/curves.md` | Year curves and common days |
| `docs/fit_target.md` | Fit target |
| `docs/fit.md` | Fit of the weights and sector factors |
| `docs/problem_log.csv` | Problems found while running the coupling or building its inputs |

### tests/regression/

One test file per module or step, run with `uv run pytest`: `test_ba.py`, `test_config.py`, `test_coupling.py`, `test_curves.py`, `test_dss.py`, `test_features.py`, `test_fit.py`, `test_fit_target.py`, `test_frame_veto.py`, `test_geo.py`, `test_groups.py`, `test_import.py`, `test_names.py`, `test_runs.py`, `test_sample.py`, `test_settings.py`, `test_trust.py`, `test_veto.py`, and `test_no_data_in_code.py`, the scan that fails if any `.py` file of `dxsew/` holds data.

## Generated files

Every name carries the UTC stamp of its run (`<stamp>`, `YYYYMMDDTHHMMSSZ`) except the fit target, whose name is fixed.

| File kind | Written by | Read by |
|---|---|---|
| `<data_root>/oedi/<release.listing_file>` | `dxsew.frame.sample` | `sample`, `features`, `trust`, `stage`, `cases`, `smartds_names` |
| `<data_root>/oedi/files/<key>` (release objects) | `frame`, `features` | `sample`, `features`, `trust` |
| `<data_root>/eia/`, `census/`, `geo/` (source files) | `dxsew.sources`, `sample`, `fit.target` | `sample`, `veto`, `trust`, `fit.target`, `fit.weights` |
| `<out>/listing_by_top_folder_<stamp>.csv`, `listing_by_subregion_<stamp>.csv` | `sample` | people |
| `<out>/frame_<stamp>.parquet`, `references_<stamp>.parquet` | `sample` | `features`, `trust` |
| `<out>/counties_outside_frame_<stamp>.csv`, `ba_summary_<stamp>.csv`, `ba_none_overlap_<stamp>.csv` | `sample` | people |
| `<out>/veto_counties_<stamp>.csv`, `veto_lookups_<stamp>.csv`, `veto_frame_removed_<stamp>.csv` | `sample` | people |
| `<out>/sample_<stamp>.csv`, `sizes_<stamp>.csv` | `sample` | `stage`, `settings`, `groups` |
| `<out>/veto_area_<area>_<stamp>.csv`, `veto_lookups_<area>_<stamp>.csv` | `veto` | people |
| `<out>/trust_zone_agreement_<stamp>.csv`, `trust_zone_table_`, `trust_county_shares_`, `trust_puct_counties_`, `trust_eia861_`, `trust_edges_` | `trust` | people |
| `<out>/released_<stamp>/` (untouched copies) | `stage`, `cases` | `stage` checks |
| `<smart_ds_base>/<staging.folder>/` (staged tree, parquets, `name_map.csv`) | `stage` | the coupling |
| `<out>/name_map_<stamp>.csv`, `staging_<stamp>.csv`, `checks_<stamp>.csv`, `left_sample_<stamp>.csv`, `parquets_shared_with_left_<stamp>.csv` | `stage` | `settings`, people |
| `<out>/coupling_settings_<stamp>/`, `<out>/cases_<stamp>/` | `settings` | `runs.coupling` |
| `<out>/features_<stamp>.parquet`, `missing_network_files_<stamp>.csv` | `features` | `groups`, `cases`, `curves.year`, `fit.weights` |
| `<out>/groups_<stamp>.parquet`, `group_table_`, `explained_`, `dial_`, `small_groups_`, `typical_feeders_`, `check_set_candidates_` | `groups` | `cases`, `curves.year`, `fit.weights` (typical feeders) |
| `<smart_ds_base>/<runs.staging_folder>/`, `<smart_ds_base>/<runs.alone_folder>/` | `cases` | the coupling |
| `<out>/staging_runs_<stamp>.csv`, `checks_runs_`, `name_map_runs_`, `runs_`, `dry_run_runs_<stamp>.txt` | `cases` | people |
| `<out>/coupling_settings_runs_<stamp>/`, `<out>/cases_runs_<stamp>/` | `cases` | `runs.coupling`, `curves.year` (copied into each run folder) |
| runs folder of the coupling (`runs_root`) | the coupling | `curves.year` |
| `<out>/curves_<stamp>/curves_check.csv`, `timestamps.csv`, `feeder_curves.parquet`, `strata.csv`, `stratum_curves.parquet`, `feeder_peaks.csv`, `common_days.csv`, `given_days_A_<stamp>.csv`, `given_days_B_<stamp>.csv` | `curves.year` | `fit.weights` (curves), the coupling (given days) |
| `<data_root>/derived/<balancing_area>_<year>_fit_target.parquet`, `.csv`, `_summary.json` | `fit.target` | `curves.year`, `fit.weights` |
| `<out>/fit_<stamp>/hourly.parquet`, `weights.csv`, `errors.csv`, `months.csv`, `hours_left_out.csv`, `sector_factors.csv`, `summary.json` | `fit.weights` | people, later jobs |
