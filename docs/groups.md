# Feeder features, nested groups and typical feeders

Paper 1 describes every feeder of the ERCOT frame with cheap features read from its circuit text files, groups the feeders inside each stratum with one nested tree, takes one real feeder per group as its typical feeder, and gives each group a starting weight (decision K60). Later jobs simulate only the typical feeders. The method runs at the current coupling pin; feeders the coupling cannot run are flagged and never chosen, and every problem goes to [problem_log.csv](problem_log.csv) (decision K61).

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

`features` first downloads the files named in `groups.feeder_network_files` (`lines.dss` and the feeder-level `transformers.dss`, names matched without regard to case) into the release folder, with the same downloader as the frame. Present files of the right size are reused. It stops before downloading when the plan is over `groups.download_max_gb`, or when the free space left would fall under `groups.download_min_free_gb`. A feeder without one of the two files keeps the features of that file empty and is listed in `missing_network_files_<stamp>.csv`.

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
| `extent_km` | longest shortest-path distance between two buses of the line graph |
| `graph_parts` | connected parts of the line graph |
| `service_transformers` | transformer objects of the feeder `Transformers.dss`; the file also holds the transformers of the feeder's voltage regulators (names starting `trans_reg`), and they are counted with the rest |
| `service_kva` | sum of their ratings (`dss.parse_transformer_kva`: `kva` of the first winding, or the first value of `kvas`) |
| `primary_kv` | the highest `kv` or `kvs` value of any winding in the file |
| `kva_per_kw` | `service_kva` over `load_kw` |
| `no_res`, `no_com`, `shared_shapes`, `shared_pairs_total`, `runnable_alone` | flags, below |

Line lengths. OpenDSS lists the length units `none`, `mi`, `kft`, `km`, `m`, `ft`, `in` and `cm` (https://opendss.epri.com/Units.html). The factors to km follow from the exact NIST values of the foot (0.3048 m), inch (0.0254 m) and mile (1609.344 m), https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b9 . A line without `length` counts 0.

Extent. The line graph has one node per bus (phase nodes dropped) and one edge per line, weighted by its length in km; parallel lines keep the shortest length and a line from a bus to itself is left out. In each connected part, a Dijkstra search from the part's first bus in name order finds the farthest bus, and a second search from that bus gives the part's extent. `extent_km` is the largest over the parts. ASSUMPTION: the two-sweep result is exact on a radial feeder and a lower bound where loops exist. Service transformers and regulators are not lines, so the buses behind them form parts of their own.

## Flags

The flags mark feeders the coupling cannot run at its pin (problem log rows P1 and P2).

- `no_res`: the feeder has no residential customer. A case needs at least one home and one commercial building (P1).
- `no_com`: the feeder has no commercial customer (P1).
- `shared_shapes`: load shapes of the feeder used by loads of more than one phase count. The coupling's loads list groups by feeder, phase count and load shape, so such a shape becomes two rows of one building and `mix_days` stops (P2).
- `shared_pairs_total`: rows of the feeder after grouping its loads by phase count and load shape, the count the coupling's map holds.
- `runnable_alone`: no flag at all, so the feeder could run as a case by itself.

## Scaling

The grouping features are those of `groups.features`, in order: `log(customers)`, `log(load_kw)`, `kw_per_customer`, `com_kw_share`, `log(line_km)`, `log(extent_km)`, `log(service_transformers)`, `kva_per_kw`, `primary_kv`. A log is the natural log of the value plus 1. With `groups.scaling: frame_z`, each feature becomes its value minus the frame mean, over the frame standard deviation (population), taken over every frame feeder that has the feature. Every feature then has the same spread, and a distance means the same in every stratum. A feeder with an empty feature is left out of grouping and printed with the empty features named.

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

## Small groups and dial table

Small groups are reported, never changed: for each stratum, k and share of `groups.small_share_report`, the number of groups under that customer share and the share of customers they hold.

The dial table gives, for each explained target of `groups.dial_targets`, the smallest k per stratum that reaches it (or the cap, marked not reached), with the typical feeders at that k, and a row `all` with the sum over the strata.

## Check-set candidates

Two staged substations run in full in the coupling as the check set (K60). For each substation of the sample, `case_runnable` is true when the substation as one case holds at least one residential and one commercial customer and no feeder has `shared_shapes` above 0.

## Settings

All in the block `groups` of `configs/texas_sample.yaml`.

| Setting | Value | ASSUMPTION |
|---|---|---|
| `feeder_network_files` | `lines.dss`, `transformers.dss` | the files of a feeder folder read for the network features |
| `line_units_default` | `km` | SMART-DS writes `Units=km` on its lines, so a line without a unit is read in km; such lines are counted in `lines_without_units` |
| `features` | the nine features above | size (customers, kW), load mix (kW per customer, commercial kW share), length (line km, extent) and equipment (service transformers, kVA per kW, primary voltage) describe a feeder well enough to group it; logs keep the largest feeders from setting the scale alone |
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
- `group_table_<stamp>.csv`: `stratum`, `k`, `group`, `feeders`, `customers`, `customer_share`, `starting_weight`, `flagged_customer_share`, `typical_feeder`, `typical_distance`, `nearest_feeder`, `nearest_distance`, and one `centre_<feature>` column per feature (`log(x)` written `log_x`).
- `explained_<stamp>.csv`: `stratum`, `k`, `groups`, `explained`.
- `dial_<stamp>.csv`: `target`, `stratum`, `k`, `reached`, `explained`, `typical_feeders`.
- `small_groups_<stamp>.csv`: `stratum`, `k`, `share_below`, `groups_below`, `customer_share_below`.
- `check_set_candidates_<stamp>.csv`: `subregion`, `substation`, `stratum`, `feeders`, `customers`, `res_customers`, `com_customers`, `feeders_no_res`, `feeders_no_com`, `feeders_with_shared_shapes`, `shared_shapes`, `shared_pairs_total`, `feeders_runnable_alone`, `case_runnable`.

`groups` also prints the frame mean and standard deviation of each feature, the feeders left out of grouping, the dial table and the gates: nesting, `explained` 0 at k = 1 and not falling, weights summing to 1, and one group per feeder at every k.
