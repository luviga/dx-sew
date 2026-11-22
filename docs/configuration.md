# Configuration

Every setting dx-sew reads, file by file, with its meaning. Values and their sources or ASSUMPTION sentences are in the files themselves; this page says what each key does.

## Files and how they are found

`dxsew.config` finds the project root by walking up from its own file to the folder that holds `pyproject.toml`, and reads everything under `config/` and `inputs/` from there.

- `config/setups/registry.json`: schema `dxsew-setup-registry-v1`. Keys `schema`, `default` (the setup id used when no `--setup` is given) and `setups` (a map of setup id to `project:<path from the project root>`). A setup id not in the map stops the run, and so does a path without the `project:` prefix.
- `config/setups/<setup>.yaml`: one balancing area and year. It is checked on load: an unknown block or key, a missing block, or a disagreement with its area in `config/areas.yaml` (`rto`, `ba_code`, `ba_prefix`, state) stops the run.
- `config/areas.yaml`, `config/units.yaml`, `config/runtime.yaml`, `config/scan.yaml`: shared by every setup.
- `config/local.yaml`: the paths of one machine, not tracked.

### Sentences beside values

A key whose name ends in `_assumption` (the suffix `names.note_suffix` of `runtime.yaml`) holds the ASSUMPTION sentence of the key it extends, or of the step it names, for example `groups.cap_assumption` beside `groups.cap` and `fit.hour_day_assumption` for the day of an hour. These keys are allowed in any block. Keys named `assumptions` (`names.texts_key`) hold the ASSUMPTION texts a run fills with its computed values and writes to its summary file. The code never names either word itself, because the scan test bans it from code.

## The setup (`config/setups/ercot_2018.yaml`)

### Top level

| Key | Meaning |
|---|---|
| `setup_id` | Id of the setup; must equal its registry id |
| `area` | Name of its area in `config/areas.yaml` |
| `label` | Lower-case area name in output column and key names (`<label>_peak_on_day_mw` of `common_days.csv`, `<label>_published` of the fit target summary) |
| `year` | Year of the release, building files, EIA-861 and EIA-930 |
| `output_folder` | Folder under `data_root` that receives the outputs |

### `inputs`

| Key | Meaning |
|---|---|
| `zone_codes` | Path of the zone code table from the project root |
| `state_fips` | Path of the state FIPS table from the project root, with its source |

### `release`

| Key | Meaning |
|---|---|
| `bucket_url`, `prefix` | OEDI bucket and the prefix of the SMART-DS release |
| `description` | Name of the release in the descriptions of the coupling's topology registry |
| `opendss_tree` | Tree of one sub-region that holds one folder per substation |
| `not_substations` | Folders of that tree that are not substations |
| `substation_file_parts`, `feeder_file_parts` | Parts of an object key below the tree for a file of a substation folder and of a feeder folder |
| `opendss_default_windings` | Windings of a transformer that names none (the OpenDSS default) |
| `load_data_folder` | Folder of the load-data parquets |
| `listing_file` | Name of the saved listing under `<data_root>/oedi` |

### `geography`

| Key | Meaning |
|---|---|
| `counties.url`, `counties.state_fips` | Census county file and the state code of the setup's state; `coordinates_assumption` gives how bus coordinates are read |
| `balancing_area.url` | EIA-861 zip of the year |
| `balancing_area.state`, `rto`, `ba_code` | State, RTO column and balancing authority code of the area |
| `balancing_area.veto`, `veto_ignore_marks`, `veto_lookups` | The EIA-861 veto (K57), the marks that never count alone, and its two lookups (K58) |
| `balancing_area.territory_file`, `territory_sheet`, `utility_file`, `utility_sheet`, `utility_header_row`, `rto_group` | Layout of the service territory and utility data files |
| `balancing_area_map.ba_shapes`, `companions`, `folder` | URL of the map's `.shp`, the files beside it and the folder under `<data_root>/geo` |
| `balancing_area_map.ba_prefix` | Start of the names of the area's polygons; `coordinates_assumption` as above |
| `climate_zone.metadata_file`, `county_column`, `zone_column` | ResStock metadata under `eulp_metadata_root` and its two columns |

### `eia861`

| Key | Meaning |
|---|---|
| `year` | Year of the EIA-861 files; the veto lookups and the fit read the sales file of it |
| `sales_file`, `sales_sheet` | Name template (`{year}`) and sheet of the sales to ultimate customers |
| `sales_group_row`, `sales_measure_row`, `sales_names_row` | Header rows of that sheet (first row is 0); data start on the row after the names |
| `short_form_file`, `short_form_sheet` | Name template and sheet of the short form file |

### `sample`

| Key | Meaning |
|---|---|
| `selection_rule` | ASSUMPTION text of the systematic selection |
| `seed` | Seed of the random starts |
| `per_stratum` | Substations drawn per stratum |
| `sizes` | Sizes of the size table |
| `minutes_per_feeder` | Run times (`minutes`, with an ASSUMPTION `label`) for the hours of the size table |

### `staging`

| Key | Meaning |
|---|---|
| `folder` | Folder under `smart_ds_base` that receives the sample |
| `parquet_folder` | Flat folder of the load parquets inside it |
| `opendss_relpath` | Layout of one staged substation below the folder |

### `coupling`

| Key | Meaning |
|---|---|
| `topology_state`, `donor_id`, `status` | Values written into the coupling's settings |

### `groups`

| Key | Meaning |
|---|---|
| `feeder_network_files` | Files of a feeder folder read for the network features |
| `line_units_default` | Unit of a line that names none |
| `features` | Grouping features in order; `log(x)` is the natural log of x plus 1 |
| `regulator_max_low_kv` | A transformer with a winding under this voltage is a service transformer |
| `primary_kv_rule`, `primary_kv_decimals`, `primary_kv_fallback` | Rule, rounding and fallback of `primary_kv` |
| `missing_transformers_file` | How a feeder without `Transformers.dss` counts |
| `name_kv` | Voltage of each last part of a substation name |
| `substation_check_rel_tolerance` | Relative tolerance of the check of feeder kW sums against the frame |
| `scaling` | Scaling of the features (`frame_z`) |
| `cap` | Largest number of groups of a stratum |
| `small_share_report` | Customer shares at which small groups are counted |
| `dial_targets`, `dial_choice` | Explained targets of the dial and the one simulated |
| `max_frame_customer_share` | Largest share of all customers one group may hold (null turns it off) |
| `starting_weight` | Starting weight of a group (`customer_share`) |
| `gate_zero_tolerance`, `gate_weight_sum_tolerance` | Tolerances of the gates of the grouping |
| `download_max_gb`, `download_min_free_gb` | Guards of the download of the network files |

Each method key has its sentence beside it (`<key>_assumption`), and `extent_km_assumption` gives the bound of `extent_km`.

### `runs`

| Key | Meaning |
|---|---|
| `staging_folder` | Folder under `smart_ds_base` for the staged substations of the run feeders |
| `alone_folder` | Folder under `smart_ds_base` with one tree per run feeder |
| `parallel` | Coupling processes at once for phase 5b |
| `pipeline_override` | `pipeline_config.yaml` of the runs (the ResStock release of the weather year) |

### `curves`

| Key | Meaning |
|---|---|
| `recipe_file`, `peak_days_file` | The coupling's recipe and peak days files at its pin |
| `seasons` | Months of each season, in output order |
| `coupling_start` | First point of the coupling's calendar |
| `check_rel_tolerance` | Tolerance of the check of the rebuilt peaks (K66) |
| `top_end_uses` | End uses listed at each common day's peak |
| `timestamp_source`, `timestamp_convention` | Source and ASSUMPTION text written to `timestamps.csv` |

`end_uses_assumption` gives the rule for the end-use columns.

### `fit_target`

| Key | Meaning |
|---|---|
| `balancing_area`, `year`, `state` | EIA-930 balancing area, year and state of the target |
| `classes_kept`, `classes_removed` | EIA-861 classes kept and taken out |
| `standard_utc_offset_hours` | Offset of the area's standard time from UTC |
| `prevailing_time_zone` | IANA name of the local time of EIA's local-time column |
| `max_interpolated_gap_hours` | Longest gap filled by linear interpolation |
| `consistency_band` | Band that flags the ratio of the target to EIA-861 sales |
| `assumptions` | The two ASSUMPTION texts the run fills |
| `published` | The operator's own published figures, for comparison only; written as `<label>_published` |

### `fit`

| Key | Meaning |
|---|---|
| `last_fit_day` | Days 1 to this of each month are fit hours, the rest test hours |
| `end_use_tolerance`, `starting_tolerance` | Tolerances of the two checks of the sector split |
| `product_tolerance` | Tolerance of the check e x m x c = M |
| `excluded_end_uses` | End uses of the homes curve left out of its sum (the PV column); the rounding gap left is shared in proportion between homes and businesses |
| `assumptions` | The three ASSUMPTION texts of K71 the run fills |
| `sources` | Source texts of E, N_model and N_frame, written to the outputs |

### `trust`

| Key | Meaning |
|---|---|
| `edge_km` | Distance that marks a substation as near another area's edge |
| `puct_served_share` | Share of a served county's substations below which check 2 lists it |

### `veto_report`

| Key | Meaning |
|---|---|
| `eia861`, `counties`, `map` | Dotted paths of the setup's blocks of the three source files |
| `output_folder` | Folder under `data_root` that receives the report |
| `share_threshold` | Share of a county's area in the map that counts as in |
| `share_decimals` | Decimals of `share_in_area` |

## `config/areas.yaml`

`areas`: a list of areas, each with `name` (in output file names), `states` (postal codes), `rto` (column of the RTO group of the EIA-861 utility data file), `ba_code` (balancing authority code on the EIA-861 sales rows), `ba_prefix` (start of the area's polygon names on the map) and `veto_ignore_marks`.

## `config/units.yaml`

| Key | Meaning |
|---|---|
| `length_km_per_unit` | Kilometres per OpenDSS length unit |
| `bytes_per_gb` | Bytes per decimal gigabyte |
| `kw_per_mw` | Kilowatts per megawatt |
| `earth_radius_km` | Mean radius of the Earth (GRS 80 R1), with its ASSUMPTION sentence |
| `degrees_per_half_turn` | 180 degrees in pi radians |
| `minutes_per_hour`, `hours_per_day` | Calendar facts |
| `minutes_per_point`, `points_per_hour` | 15-minute points of the building files; checked against `minutes_per_hour` |
| `wye_voltage_ratio_squared` | Square of the line-to-line over line-to-neutral voltage of a wye |
| `fips_state_digits` | Digits of a state FIPS code |

## `config/runtime.yaml`

| Key | Meaning |
|---|---|
| `download.*` | Tries, timeouts, pauses and chunk size of single downloads |
| `oedi.*` | Timeout, retries, back-off, chunk size and progress steps of the OEDI client |
| `hash_block_bytes` | Block size when a SHA-256 is computed |
| `workers.*` | Default thread and process counts of the commands |
| `chunksize.*` | Tasks per worker call of the readers |
| `relative_floor` | Floor of a denominator in relative differences |
| `nearest_slack_degrees` | Slack when the nearest polygon is chosen |
| `print_width`, `print_rows`, `detail_items`, `pin_status_lines`, `print_paths` | Console and table cell lengths |
| `json_indent`, `yaml_width` | Format of the JSON and YAML files written |
| `names.note_suffix`, `names.texts_key` | Key names that hold the word the scan bans in code |

## `config/scan.yaml`

`scan.forbidden_terms`: region, state and balancing-area names that may not appear as a whole word in any string of the package. The scan test checks that the list holds every name, state, RTO, code and prefix of `config/areas.yaml`.

## `config/local.yaml`

| Key | Meaning |
|---|---|
| `data_root` | The workspace: downloads and generated files, outside the repository |
| `smart_ds_base` | SMART-DS root of the machine, as in the coupling |
| `eulp_metadata_root` | Root of the EULP building metadata |
| `coupling_repo` | Clone of the coupling at its pin |
| `coupling_python` | Interpreter that runs the coupling |

## `config/sources.yaml` and `config/coupling_pin.yaml`

`sources.yaml` lists each downloaded URL under a key (`url`, `found_on`, `found_via`, `documentation`, `retrieved`). `coupling_pin.yaml` holds `repository` and `commit` of the coupling; see [coupling.md](coupling.md).
