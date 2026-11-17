# Feeder features, nested groups and typical feeders

Paper 1 describes every feeder of the ERCOT frame with cheap features read from its circuit text files, groups the feeders inside each stratum with one nested tree, takes one real feeder per group as its typical feeder, and gives each group a starting weight (decision K60). Later jobs simulate only the typical feeders. The method runs at the current coupling pin; feeders the coupling cannot run are flagged and never chosen, and every problem goes to [problem_log.csv](problem_log.csv) (decision K61). A group with no runnable feeder takes the nearest group's typical feeder as a marked placeholder, so later jobs run end to end; dx-sew builds no workaround for the coupling's problems (decision K62).

The circuits are SMART-DS v0.9, Full Texas, 2018, on the OEDI data lake: https://data.openei.org/submissions/2981 . The frame is the one of [texas_sample.md](texas_sample.md) and [frame_procedure.md](frame_procedure.md). No circuit is compiled and no power flow runs: every file is read as text.

## Commands

From the repository, with `configs/local.yaml` in place:

```
uv run python -m dxsew.texas.features --stamp <stamp> --frame-stamp <frame stamp> [--workers 16] [--processes 8]
uv run python -m dxsew.texas.groups --stamp <stamp> --features-stamp <stamp> [--sample-stamp <sample stamp>]
```

`--workers` is the number of download threads and `--processes` the number of reader processes. `--sample-stamp` names `sample_<stamp>.csv` of the sample job, for the check-set candidates. Every output goes to `<data_root>/texas/`; nothing is written into the repository.

## Feeders and network files

A feeder is a folder of the `opendss` tree of a substation that holds both `loads.dss` and `loadshapes.dss` (`frame.FEEDER_FILES`). The feeders read are those of the substations with `in_frame` true in `frame_<frame stamp>.parquet`.

`features` first downloads the files named in `groups.feeder_network_files` (`lines.dss` and the feeder-level `transformers.dss`, names matched without regard to case) into the release folder, with the same downloader as the frame. Present files of the right size are reused. It stops before downloading when the plan is over `groups.download_max_gb`, or when the free space left would fall under `groups.download_min_free_gb`. A feeder without one of the two files is listed in `missing_network_files_<stamp>.csv`. A feeder without `lines.dss` keeps its line columns empty. A feeder without `transformers.dss` has no transformer: `service_transformers` 0, `regulators` 0, `service_kva` 0.0 and `kva_per_kw` 0.0 (setting `groups.missing_transformers_file: no_transformers`). ASSUMPTION: a feeder folder of the release with no Transformers.dss has no transformer of its own; its customers connect at primary voltage.

## Feature table

`features_<stamp>.parquet` holds one row per frame feeder.

| Column | Meaning |
|---|---|
| `subregion`, `substation`, `stratum`, `county` | from the frame |
| `feeder` | the feeder folder name, `<substation>--<feeder>` |
| `loads` | load objects of `Loads.dss` |
| `customers` | customers: the `_1` and `_2` objects of a split-phase customer are one customer (`dss.customer_count`) |
| `res_customers`, `com_customers` | customers whose load shape name starts with `res` or `com` before `_kw` (`dss.load_sector`, the rule of `dss.parquet_name`) |
| `load_kw` | sum of the released kW |
| `com_kw_share` | kW of commercial loads over `load_kw` |
| `kw_per_customer` | `load_kw` over `customers` |
| `lines` | line objects of `Lines.dss` |
| `line_km` | sum of line lengths in km, each converted by its `units` |
| `lines_without_units` | lines that name no unit OpenDSS lists, read in `groups.line_units_default` |
| `extent_km` | longest shortest-path distance between two buses of the line graph, regulators bridged |
| `graph_parts` | connected parts of the line graph, regulators bridged |
| `service_transformers` | service transformers of the feeder `Transformers.dss` (below) |
| `regulators` | the other transformers of the file: voltage regulators (below) |
| `service_kva` | sum of the service transformers' ratings (`dss.parse_transformer_kva`: `kva` of the first winding, or the first value of `kvas`) |
| `primary_kv` | the system voltage line to line, read from the service transformers, or `name_kv` when none gives one (below) |
| `name_kv` | the voltage in the last part of the substation name (below) |
| `kva_per_kw` | `service_kva` over `load_kw` |
| `no_res`, `no_com`, `shared_shapes`, `shared_pairs_total`, `runnable_alone` | flags, below |

Line lengths. OpenDSS lists the length units `none`, `mi`, `kft`, `km`, `m`, `ft`, `in` and `cm` (https://opendss.epri.com/Units.html). The factors to km follow from the exact NIST values of the foot (0.3048 m), inch (0.0254 m) and mile (1609.344 m), https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b9 . A line without `length` counts 0.

Transformers. `dss.parse_transformers` reads both forms of an OpenDSS transformer: `buses=[...]` with `kvs=(...)`, one item per winding, and blocks of `wdg=<n>` followed by `bus=` and `kv=` of winding n (`bus` and `kv` before any `wdg` belong to winding 1). For each transformer it keeps `phases` and, per winding, the bus without node numbers, the node list (`p1lv.1.0` gives 1 and 0) and `kv`. The properties are those of the OpenDSS Transformer object, https://opendss.epri.com/Properties16.html .

Regulators. The feeder `Transformers.dss` of SMART-DS also holds the transformers of the feeder's voltage regulators (names with `reg`, such as `trans_reg_...`). A transformer with a winding under `groups.regulator_max_low_kv` (1 kV) is a service transformer; any other is counted in `regulators` and left out of `service_transformers`, `service_kva` and `kva_per_kw`. ASSUMPTION: a service transformer has a winding under 1 kV; a transformer with no winding under this voltage is a voltage regulator or a primary-level transformer, not a service transformer. The transformer columns are filled for every feeder: a feeder whose file holds only regulators, or that has no `transformers.dss` (setting `groups.missing_transformers_file`, above), counts 0 service transformers. A feeder with zero service transformers is a medium-voltage customer feeder and is grouped like any other.

Extent. The line graph has one node per bus (phase nodes dropped) and one edge per line, weighted by its length in km; parallel lines keep the shortest length and a line from a bus to itself is left out. Each regulator adds one edge of 0 km between the buses of its first two windings, so the graph crosses the regulator; lines are read as before. In each connected part, a Dijkstra search from the part's first bus in name order finds the farthest bus, and a second search from that bus gives the part's extent. `extent_km` is the largest over the parts. ASSUMPTION: the two-sweep result is exact on a radial feeder and a lower bound where loops exist. Service transformers are not edges, so the buses behind them still form parts of their own. Where a regulator's two buses are already joined by a line path, the 0 km edge shortens that path and `extent_km` can fall.

Primary voltage. `primary_kv` is the system voltage line to line, read from the service transformers. For each service transformer, take its highest-voltage winding (the first on a tie). OpenDSS rates a 2- or 3-phase transformer phase to phase, and any other by its actual winding voltage (property `kV`, https://opendss.epri.com/Properties16.html). So when the transformer has `phases=1` and that winding's bus names one phase node (one phase number, with or without `.0`), the line-to-line value is `kv` times the square root of 3; otherwise it is `kv`. The square root of 3 is the ratio of line-to-line to line-to-neutral voltage in a balanced three-phase system. `primary_kv` is the largest such value on the feeder, rounded to `groups.primary_kv_decimals` (2) decimals. Setting `groups.primary_kv_rule: line_to_line`. ASSUMPTION: a single-phase transformer between one phase and neutral is rated line to neutral. Two decimals: ASSUMPTION: system voltages are named to two decimals (12.47 kV), so a voltage computed from a line-to-neutral rating and one written line to line give the same value.

When no service transformer of the feeder gives a voltage (the feeder has none, or none has a `kv`), `primary_kv` is the feeder's `name_kv` (below), as the table writes it (setting `groups.primary_kv_fallback: name_kv`). Regulators still give no voltage. When `name_kv` is empty too, `primary_kv` stays empty. ASSUMPTION: a feeder with no service transformer serves its customers at primary voltage, and the system voltage written in its substation name is that voltage.

Name voltage. `name_kv` is the voltage that `groups.name_kv` gives for the last part of the substation name, after the last underscore: `1247` gives 12.47, `25` gives 25.0 and `4` gives 4.0. Any other last part leaves it empty, and `features` prints those substations. ASSUMPTION: SMART-DS Texas substation names end in the system voltage line to line written without its decimal point. `name_kv` is a check of `primary_kv` and its fallback, not a grouping feature.

## Flags

The flags mark feeders the coupling cannot run at its pin (problem log rows P1 and P2).

- `no_res`: the feeder has no residential customer. A case needs at least one home and one commercial building (P1).
- `no_com`: the feeder has no commercial customer (P1).
- `shared_shapes`: load shapes of the feeder used by loads of more than one phase count. The coupling's loads list groups by feeder, phase count and load shape, so such a shape becomes two rows of one building and `mix_days` stops (P2).
- `shared_pairs_total`: rows of the feeder after grouping its loads by phase count and load shape, the count the coupling's map holds.
- `runnable_alone`: no flag at all, so the feeder could run as a case by itself.

## Scaling

The grouping features are those of `groups.features`, in order: `log(customers)`, `log(load_kw)`, `log(kw_per_customer)`, `com_kw_share`, `log(line_km)`, `log(extent_km)`, `log(service_transformers)`, `log(kva_per_kw)`, `primary_kv`. A log is the natural log of the value plus 1. `kw_per_customer` and `kva_per_kw` are logged because both have long tails that put single feeders tens of standard deviations out. With `groups.scaling: frame_z`, each feature becomes its value minus the frame mean, over the frame standard deviation (population), taken over every frame feeder that has the feature. Every feature then has the same spread, and a distance means the same in every stratum. A feeder with an empty feature is left out of grouping and printed with the empty features named. With the transformer columns filled for every feeder and the `primary_kv` fallback, only a feeder without `lines.dss`, or with no service transformer voltage and no `name_kv`, can have an empty feature.

## Nested groups

Each stratum gets one Ward tree on the scaled features, Euclidean (`scipy.cluster.hierarchy.linkage`, method `ward`). Ward's method joins, at each step, the two groups whose union adds least to the within-group sum of squares: J. H. Ward, "Hierarchical grouping to optimize an objective function", Journal of the American Statistical Association 58 (1963) 236-244, https://doi.org/10.1080/01621459.1963.10500845 . Ward is deterministic, so no seed is needed.

The tree is cut at every number of groups k from 1 to `groups.cap` or the stratum's feeder count, whichever is smaller (`fcluster`, criterion `maxclust`). Cuts of one tree are nested: every group at k+1 lies inside one group at k. Numbering is stable between k and k+1: when a group splits, its larger part (by feeders; on a tie, the part holding the first feeder in name order) keeps the number and the other part takes the next free number. A group keeps its number at every k until it splits.

For each stratum and k, `explained` is 1 minus the within-group sum of squares over the total sum of squares, on the scaled features. It is 0 at k = 1 and does not fall as k grows.

## Groups, typical feeders and weights

For each group at each k:

- `feeders` and `customers`, and `customer_share`, its customers over those of the stratum's feeders in grouping;
- the centre, the mean of its scaled features;
- `starting_weight`, equal to `customer_share` (setting `groups.starting_weight`); the weights of a stratum sum to 1 at every k;
- `flagged_customer_share`, the share of the group's customers on feeders that are not `runnable_alone`;
- the typical feeder, the `runnable_alone` feeder nearest the centre (Euclidean, scaled features), with its distance; and the nearest feeder of any kind with its distance. Ties go to the first feeder in name order. A group with no runnable feeder has no typical feeder.
- placeholders (decision K62), for a group with no typical feeder only: `placeholder_group`, the group of the same stratum and k that has a typical feeder and whose centre is nearest this group's centre (Euclidean, scaled features; ties to the lower group number); `placeholder_feeder`, that group's typical feeder; and `placeholder_distance`, the distance from this group's centre to that feeder. A group with its own typical feeder leaves the three empty. When no group of a stratum at some k has a typical feeder, the three stay empty and `groups` prints the stratum and k. ASSUMPTION: the typical feeder of the nearest group stands in for a group that cannot be run, until the coupling can run it; the placeholder columns mark every such group, so later jobs can run end to end and still tell the stand-ins apart.

## Small groups and dial table

Small groups are reported, never changed: for each stratum, k and share of `groups.small_share_report`, the number of groups under that customer share and the share of customers they hold.

The dial table gives, for each explained target of `groups.dial_targets`, the smallest k per stratum that reaches it (or the cap, marked not reached), with the typical feeders at that k, the groups at that k that use a placeholder (`placeholder_groups`) and the share of the stratum's customers in those groups (`placeholder_customer_share`), and a row `all` with the sums over the strata (its share is over the customers of every stratum).

## Check-set candidates

Two staged substations run in full in the coupling as the check set (K60). For each substation of the sample, `case_runnable` is true when the substation as one case holds at least one residential and one commercial customer and no feeder has `shared_shapes` above 0.

## Settings

All in the block `groups` of `configs/texas_sample.yaml`.

| Setting | Value | ASSUMPTION |
|---|---|---|
| `feeder_network_files` | `lines.dss`, `transformers.dss` | the files of a feeder folder read for the network features |
| `line_units_default` | `km` | SMART-DS writes `Units=km` on its lines, so a line without a unit is read in km; such lines are counted in `lines_without_units` |
| `features` | the nine features above | size (customers, kW), load mix (kW per customer, commercial kW share), length (line km, extent) and equipment (service transformers, kVA per kW, primary voltage) describe a feeder well enough to group it; logs keep the largest feeders from setting the scale alone, and kW per customer and kVA per kW are logged too because both have long tails that put single feeders tens of standard deviations out |
| `regulator_max_low_kv` | 1.0 | a service transformer has a winding under 1 kV; a transformer with no winding under this voltage is a voltage regulator or a primary-level transformer, not a service transformer |
| `primary_kv_rule` | `line_to_line` | a single-phase transformer between one phase and neutral is rated line to neutral |
| `primary_kv_decimals` | 2 | system voltages are named to two decimals (12.47 kV), so a voltage computed from a line-to-neutral rating and one written line to line give the same value |
| `primary_kv_fallback` | `name_kv` | a feeder with no service transformer serves its customers at primary voltage, and the system voltage written in its substation name is that voltage |
| `missing_transformers_file` | `no_transformers` | a feeder folder of the release with no Transformers.dss has no transformer of its own; its customers connect at primary voltage |
| `name_kv` | `1247`: 12.47, `25`: 25.0, `4`: 4.0 | SMART-DS Texas substation names end in the system voltage line to line written without its decimal point; the column is a check of `primary_kv`, not a grouping feature |
| `scaling` | `frame_z` | each feature becomes (value minus frame mean) over frame standard deviation, so every feature has the same spread and a distance means the same in every stratum |
| `cap` | 40 | enough groups per stratum to see where adding groups stops paying |
| `small_share_report` | 0.005, 0.01, 0.02 | shares that bracket a floor that may be set later; the report only counts |
| `dial_targets` | 0.5, 0.6, 0.7, 0.8, 0.9 | targets that show the cost in typical feeders of each step of fit |
| `starting_weight` | `customer_share` | a group starts at its share of the customers of its stratum; job J5 adjusts the weights against ERCOT |
| `download_max_gb`, `download_min_free_gb` | 40, 150 | a plan over 40 GB means a wrong selection; 150 GB left free protects the jobs that share the machine |

## Output files

All under `<data_root>/texas/`, named with the stamp of the run.

- `features_<stamp>.parquet`: the feature table above.
- `missing_network_files_<stamp>.csv` (only when a file is missing): `subregion`, `substation`, `feeder`, `stratum`, `county`, `file`.
- `groups_<stamp>.parquet`: `stratum`, `k`, `feeder`, `group`, one row per feeder in grouping and k.
- `group_table_<stamp>.csv`: `stratum`, `k`, `group`, `feeders`, `customers`, `customer_share`, `starting_weight`, `flagged_customer_share`, `typical_feeder`, `typical_distance`, `nearest_feeder`, `nearest_distance`, `placeholder_group`, `placeholder_feeder`, `placeholder_distance`, and one `centre_<feature>` column per feature (`log(x)` written `log_x`).
- `explained_<stamp>.csv`: `stratum`, `k`, `groups`, `explained`.
- `dial_<stamp>.csv`: `target`, `stratum`, `k`, `reached`, `explained`, `typical_feeders`, `placeholder_groups`, `placeholder_customer_share`.
- `small_groups_<stamp>.csv`: `stratum`, `k`, `share_below`, `groups_below`, `customer_share_below`.
- `check_set_candidates_<stamp>.csv`: `subregion`, `substation`, `stratum`, `feeders`, `customers`, `res_customers`, `com_customers`, `feeders_no_res`, `feeders_no_com`, `feeders_with_shared_shapes`, `shared_shapes`, `shared_pairs_total`, `feeders_runnable_alone`, `case_runnable`.

`groups` also prints the frame mean and standard deviation of each feature, the feeders left out of grouping, the dial table, the strata and k with no typical feeder in any group, and the gates: nesting, `explained` 0 at k = 1 and not falling, weights summing to 1, and one group per feeder at every k.
