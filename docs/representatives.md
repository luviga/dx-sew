# Representatives, their order and their weights by load kW

On 22 November 2026 the maintainer decided the method of dx-sew (decision K76). Feeders are grouped by a nested tree on the nine features read from circuit files ([groups.md](groups.md)). Each group has one real feeder as its representative. A representative stands for its group with one weight, the group's load kW over its own load kW, and its whole curve and its power-flow results are later multiplied by that weight as one piece. Representatives are added one at a time and earlier ones are kept. No curve per customer is used anywhere.

`dxsew.frame.representatives` builds the first part, which needs no coupling run: the order in which representatives are added, the representative and weight of every group at every step, and three checks that need no new building run. Later jobs put EULP buildings on the representatives and on a random sample of feeders and read the error of the method there. This module does not draw that sample and does not choose how many representatives are run; [error_curve.md](error_curve.md) does (decision K80).

The customer weights, the dial and the assembly of decision K60 ([groups.md](groups.md), [assembly.md](assembly.md)) are unchanged and stay until a later job.

## Command

From the repository, with `config/local.yaml` in place:

```
uv run python -m dxsew.frame.representatives --stamp <stamp> --features-stamp <features stamp> [--setup <id>]
```

It reads `features_<features stamp>.parquet` of `dxsew.frame.features`, the blocks `groups` and `representatives` of the setup (and `representatives_scout.average_feeder` when `representative_choice` is `average_feeder`), and, for the head start, `curves_<representatives.head_start_curves_stamp>/feeder_curves.parquet` of `dxsew.curves.year`. Every output goes to `<data_root>/<output_folder>/`; nothing is written into the repository. The run prints the ASSUMPTION sentences of the block, the counts, the pin table and the gates, and writes every ASSUMPTION sentence of the blocks `groups` and `representatives` to `representatives_assumptions_<stamp>.csv`.

## Features and trees

The scaled features and the Ward tree of each stratum are exactly those of `dxsew.frame.groups`, made with its own functions: the columns of `groups.features`, scaling `frame_z` over every frame feeder, and one tree per stratum (`scipy.cluster.hierarchy.linkage`, method `ward`, Euclidean; J. H. Ward, "Hierarchical grouping to optimize an objective function", Journal of the American Statistical Association 58 (1963) 236-244, https://doi.org/10.1080/01621459.1963.10500845). Feeders left out of grouping (an empty feature) stay out and are counted. A feeder whose `load_kw` is 0 cannot be a representative; such feeders are counted.

## The order of the representatives

For each rule of `representatives.order_rules` the module builds one sequence of steps for the whole balancing area.

1. The first step has one group per stratum, so it has as many representatives as there are strata. The representative of a group is the eligible feeder nearest the group's target point (`groups.nearest`; ties go to the first feeder in name order). Under `representative_choice: centre` the target point is the group's centre, the unweighted mean of the scaled features; the other choices, and the choice of record, `load_weighted_centre` (decision K78), are below.
2. At each next step one group is split into the two children its tree node has. Among all groups of all strata that can still be split, the one with the largest drop is split:
   - `ward_drop`: the within-group sum of squares of the parent minus those of its two children, on the scaled features (`groups._sum_of_squares`).
   - `load_weighted_drop`: the same with each feeder weighted by its `load_kw`, each sum taken around the load-weighted centre of its group.

   Ties go to the stratum first in name order, then to the lower group number.
3. The child that holds the parent's representative keeps that representative and the parent's group number. The other child takes the next free number of its stratum, and its representative is the eligible feeder nearest its own target point. No representative is ever dropped.
4. The sequence stops at `max_representatives` representatives, or when no group can be split.

Under `ward_drop` the drop of a node is the increase of the within-group sum of squares at that node's merge in the Ward tree, and merge heights rise from the leaves to the root, so the partition of a stratum after its j-th split is the cut of `fcluster(tree, j + 1, "maxclust")`; the run checks it. Under `load_weighted_drop` the tree is the same and only the order of the splits changes; a group is never split before its parent.

## The representative choice

On 23 November 2026 the maintainer decided to scout three ways of choosing a group's representative before choosing one (decision K77). The setting `representatives.representative_choice` names the rule. Each rule sets a target point on the scaled features of the run, and the representative is the eligible feeder nearest that point (`groups.nearest`, ties to the first name). Everything else of the order stays as above: the groups, the order of the splits and the rule that a kept representative stays.

- `centre` (the rule of decision K76, and the code's default when the setting is absent): the unweighted mean of the members' scaled points.
- `load_weighted_centre` (the choice of record, decision K78): the mean of the members' scaled points weighted by `load_kw`. When the members' `load_kw` add to 0 it is the unweighted mean.
- `average_feeder`: one raw row is built from the members' values in the feature table, as `representatives_scout.average_feeder` says: for a column under `mean`, the sum over the members over their number; for a column under `ratio` `[a, b]`, the sum of `a` over the sum of `b`; for a column under `weighted_mean` `w`, the sum of the value times `w` over the sum of `w`. Columns that no feature of the run reads are not used. The row is turned into features with `groups.feature_values` and the run's feature specs, and scaled with the per-column mean and standard deviation that `frame_z` used over the frame (kept from `prepare`, not computed again on the group).

A group of one feeder has that feeder's own point as its target under all three choices. The columns `distance`, `nearest_feeder` and `nearest_distance` of the group rows refer to the target point. The choice moves representatives and never changes a partition: within one order rule and one feature set, the groups at every step are the same under the three choices. Under `centre` every output of `dxsew.frame.representatives` is the same as before decision K77.

On 24 November 2026 the maintainer chose `load_weighted_centre` as the choice of record (decision K78), after the scout below, with the order rule `ward_drop` and the nine features of decision K60; the setup's `representative_choice` is now `load_weighted_centre`. Earlier representatives are kept as before.

## The scout command

```
uv run python -m dxsew.frame.representatives_scout --stamp <stamp> --features-stamp <features stamp> --reference-stamp <stamp of a run of dxsew.frame.representatives> [--setup <id>]
```

The scout measures and changes no default (decision K77). It runs one sequence, from the number of strata to `representatives.max_representatives`, for every combination of `representatives.order_rules`, `representatives_scout.choices` and `representatives_scout.feature_sets`; `eligible`, `weight`, `check_quantities` and `report_steps` come from the block `representatives`. A feature set adds its features to `groups.features` for its sequences only; the block `groups` is not changed, and the frame scaling covers the added features too. The head start of the run feeders with year curves runs for every combination with the weight `representatives_scout.head_start_weight` only.

It reads `features_<features stamp>.parquet`, the reference run's `representatives_<reference stamp>.parquet`, `representatives_errors_<reference stamp>.csv` and `head_start_errors_<reference stamp>.csv`, and `curves_<representatives.head_start_curves_stamp>/feeder_curves.parquet`. The reference sequence for the column `shared_with_reference` is rule `ward_drop` of the reference run. The run prints the ASSUMPTION sentences of the blocks `groups`, `representatives` and `representatives_scout` at its start, the gates of `dxsew.frame.representatives` for every sequence, whether the partitions are equal across the choices, whether the rows of choice `centre` with the first feature set equal the reference run's (values within `gate_zero_tolerance` relative), and its run time.

Outputs under `<data_root>/<output_folder>/`, each with the columns `rule`, `choice` and `feature_set` first:

- `scout_representatives_<stamp>.parquet`: the rows of `representatives_<stamp>.parquet` for every sequence.
- `scout_errors_<stamp>.csv`: the rows of `representatives_errors_<stamp>.csv` (every step, the balancing area and each stratum) for every sequence.
- `scout_steps_<stamp>.csv`: one row per sequence and step: `n`, `explained`, `explained_load_weighted`, `weights_over_feeders` (the sum of the groups' weights over the number of feeders in grouping), `strata_one_group` (strata with one group) and `strata_one_group_names` (joined by `;`), `shared_with_reference` (representatives at this n that are also representatives at this n of the reference sequence), `error_<quantity>` for every quantity of `check_quantities` for the balancing area, `max_abs_stratum_error` (the largest absolute error over the strata and quantities) and `max_abs_stratum_where` (stratum and quantity).
- `scout_head_start_<stamp>.csv`: the head start for every sequence: `n`, `cv_rmse`, `error_at_true_peak`, `error_of_peak`, `error_energy`, `note`.
- `scout_repick_<stamp>.csv`, a report only: for every sequence and every n of `report_steps`, `kept_not_target` (groups whose kept representative is not the eligible feeder nearest the group's target point) and `kept_not_target_load_share` (the share of `load_kw` those groups hold), and, for the representatives as kept and for a set where every group takes the feeder nearest its target, `kept_weights_over_feeders`, `repicked_weights_over_feeders` and the balancing-area error of each quantity (`kept_error_<quantity>`, `repicked_error_<quantity>`). Nothing else uses the repicked set.
- `scout_assumptions_<stamp>.csv`: `block`, `key` and `sentence` of every ASSUMPTION key of the blocks `groups`, `representatives` and `representatives_scout`, word for word.

## The weight

The weight of a group is the group's `load_kw` over its representative's `load_kw` (`weight: load_kw_share`, decision K76). Its whole curve and its power-flow results are later multiplied by this one number. The weights times the representatives' `load_kw` add up to each stratum's `load_kw`.

At every step and for every group the module writes the group's `feeders`, `customers`, `load_kw`, `load_share` (its `load_kw` over that of every feeder in grouping), the representative and its `load_kw`, the `weight`, the `distance` from the representative to the group's target point, the nearest eligible feeder to the target point and its distance, `runnable_at_pin` (the representative's `runnable_alone`, [groups.md](groups.md)) and `since_n`, the step at which this representative was added.

At every step and for the balancing area: `explained`, 1 minus the sum over every group of every stratum of the within-group sum of squares, over the sum over the strata of each stratum's total sum of squares, on the scaled features; and `explained_load_weighted`, the same with feeders weighted by `load_kw`, each sum around its load-weighted centre. Both are 0 at the first step, where the strata are the groups.

## Checks that need no building run

For every rule, step and quantity Q of `check_quantities`, for the balancing area and for each stratum: `true` is the sum of Q over the feeders in grouping, `estimate` is the sum over groups of weight times the representative's Q, and `error` is estimate over true minus 1 (0 when both are 0, empty when only `true` is). With every feeder its own group every error is 0.

The pin table counts, for each rule and each number of `report_steps` that the sequence reaches, the representatives with `runnable_at_pin` false and the share of load kW their groups hold.

## Head start on the run feeders that have year curves

The run feeders of the typical-feeder table ([runs.md](runs.md)) have EULP year curves (`head_start_curves_stamp`). They were chosen as group centres, so they are not a random sample; the head start is a first look and says so in a `note` column of each of its files.

- Sizing. For each of them: yearly EULP energy in kWh (the sum of `kw` times the hours of one step, the step taken from the timestamps), EULP peak kW, and from the feature table `load_kw`, `customers` and `stratum`, with four ratios: energy over `load_kw`, energy over `customers`, peak over `load_kw`, peak over `customers`. For all of them and per stratum: the count, and for each ratio its median, quartiles (pandas `describe`) and coefficient of variation (standard deviation, population, over the mean); and the Spearman rank correlation (`scipy.stats.spearmanr`) of EULP energy with `load_kw` and with `customers`, and of EULP peak with each.
- The method on these feeders alone. They are treated as if they were every feeder. Their features are scaled with the frame's mean and standard deviation (not their own), a Ward tree is built per stratum on them, and the order above runs with rule `ward_drop`, the setup's `representative_choice` and no cap other than their number. At every step, for each weight of `head_start_weights`, the estimate curve is the sum over groups of weight times the representative's curve, where the weight is the group's `load_kw` over the representative's (`load_kw`) or the group's customers over the representative's (`customers`). It is compared with the sum of their curves: CV(RMSE) over the year (root mean square error over the mean of the true sum), the error at the true sum's peak step, the error of the estimate's own peak, and the error in yearly energy. When every feeder is its own group every error is 0; the run checks it within `gate_zero_tolerance`.

## Gates

The run prints each gate as pass or FAIL:

- `ward_equals_fcluster`: under `ward_drop`, for every stratum and every k up to `groups.cap` that the sequence reaches, the groups equal those of `fcluster` at that k, as sets of feeders.
- `representatives_kept`: for both rules, the representatives of step n are among those of step n + 1.
- `weights_add_to_load`: for both rules and every step, the sum of weight times representative `load_kw` equals the stratum's `load_kw` within `gate_weight_tolerance` relative.
- `one_group_per_feeder`: every feeder in grouping is in exactly one group at every step.
- `explained_not_falling`: `explained` and `explained_load_weighted` never fall from one step to the next by more than `gate_zero_tolerance`.
- `head_start_zero_at_full`: every error of the head start is 0 within `gate_zero_tolerance` when every feeder is its own group.

## Settings

All in the block `representatives` of `config/setups/ercot_2018.yaml`.

| Setting | Value | ASSUMPTION or source |
|---|---|---|
| `order_rules` | `ward_drop`, `load_weighted_drop` | the two rules of the order above |
| `eligible` | `all` | ASSUMPTION: the coupling at its v1.0.0 tag runs every frame feeder alone, so any feeder can be a representative; the column runnable_at_pin marks those the pinned commit cannot run. Values: `all` (every feeder in grouping) or `runnable_alone` |
| `weight` | `load_kw_share` | decision K76 |
| `representative_choice` | `load_weighted_centre` | the choice of record (decision K78); `centre` (the rule of decision K76) and `average_feeder` are the other values (decision K77) |
| `max_representatives` | 300 | ASSUMPTION: 300 representatives are more than one machine can run in the time of the study, so the order is written far enough to see where more runs stop paying. |
| `check_quantities` | `customers`, `res_customers`, `com_customers`, `loads`, `line_km`, `service_transformers`, `service_kva`, `regulators` | ASSUMPTION: these are the columns of the feature table that add up over feeders and that the weighted representatives should reproduce. |
| `head_start_curves_stamp` | `20261118T212825Z` | the year curves of the run feeders; null skips the head start |
| `head_start_weights` | `load_kw`, `customers` | the two weights compared in the head start |
| `report_steps` | 25, 50, 86, 100, 200, 300 | ASSUMPTION: these numbers of representatives span the runs that can be afforded. |
| `gate_weight_tolerance`, `gate_zero_tolerance` | 1.0e-9, 1.0e-12 | tolerances of the gates (rounding only) |

The scout reads the block `representatives_scout` of the same file.

| Setting | Value | ASSUMPTION or source |
|---|---|---|
| `choices` | `centre`, `load_weighted_centre`, `average_feeder` | the three rules of the representative choice (decision K77) |
| `feature_sets` | `nine` (no added feature), `with_regulators` (`log(regulators)`) | ASSUMPTION: the number of regulators read from a feeder's circuit files, logged as the other counts are, marks feeders with voltage support; it is tried as a tenth feature to see whether groups that separate feeders with and without regulators reproduce the frame's regulators better. |
| `average_feeder` | `mean`: `customers`, `load_kw`, `line_km`, `extent_km`, `service_transformers`, `regulators`, `primary_kv`; `ratio`: `kw_per_customer` from `load_kw` over `customers`, `kva_per_kw` from `service_kva` over `load_kw`; `weighted_mean`: `com_kw_share` by `load_kw` | ASSUMPTION: a group's average feeder has, for each count, the group's total over its number of feeders; for each ratio, the group's ratio of totals; for the commercial kW share, the group's commercial kW over its kW; and for the primary voltage, the group's mean. Its copies, weighted by load kW, reproduce the group's totals. |
| `head_start_weight` | `load_kw` | the weight of the scout's head start, one of `head_start_weights` |

## Output files

All under `<data_root>/<output_folder>/`, named with the stamp of the run.

- `representatives_steps_<stamp>.csv`: one row per rule and step: `rule`, `n` (number of representatives), `stratum`, `split_group`, `new_group`, `drop`, `new_representative`, `explained`, `explained_load_weighted`. The first step of each rule has no split.
- `representatives_<stamp>.parquet`: one row per rule, step and group: `rule`, `n`, `stratum`, `group`, `feeders`, `customers`, `load_kw`, `load_share`, `representative`, `representative_load_kw`, `weight`, `distance`, `nearest_feeder`, `nearest_distance`, `runnable_at_pin`, `since_n`.
- `representatives_groups_<stamp>.parquet`: one row per rule, step and feeder in grouping: `rule`, `n`, `feeder`, `stratum`, `group`.
- `representatives_errors_<stamp>.csv`: `rule`, `n`, `scope` (the balancing area's name of the setup's `area`, or the stratum), `quantity`, `true`, `estimate`, `error`.
- `representatives_pin_<stamp>.csv`: `rule`, `n`, `representatives`, `not_runnable_at_pin`, `not_runnable_load_share`.
- `head_start_feeders_<stamp>.csv`: `feeder`, `stratum`, `load_kw`, `customers`, `energy_kwh`, `peak_kw`, `energy_per_load_kw`, `energy_per_customer`, `peak_per_load_kw`, `peak_per_customer`, `note`.
- `head_start_summary_<stamp>.csv`: `scope` (`all` or the stratum), `feeders`, `quantity` (a ratio, or `<curve measure>_vs_<size>` for a rank correlation), `statistic` (`median`, `q1`, `q3`, `cv` or `spearman`), `value`, `note`.
- `head_start_errors_<stamp>.csv`: `weight`, `n`, `cv_rmse`, `error_at_true_peak`, `error_of_peak`, `error_energy`, `note`.
- `representatives_assumptions_<stamp>.csv`: `block`, `key`, `sentence` for every ASSUMPTION key of the blocks `groups` and `representatives`, word for word.

The scout's six files are listed under [The scout command](#the-scout-command).

## Known limits

- A kept representative can sit far from its group's centre after several splits: it was the nearest eligible feeder to the centre of an ancestor group, and the column `nearest_feeder` shows the feeder that would be chosen now.
- The tree is unweighted while the weights are by load kW: `load_weighted_drop` changes only the order of the splits, never the groups a node can split into.
- The head start is not a random sample: its feeders were chosen as group centres, so its errors are a first look and not the error of the method.
- The weight scales a whole curve by load kW; a group whose feeders differ in load shape from its representative carries that difference into the estimate.
- A group whose feeders all have `load_kw` 0 (or, with `eligible: runnable_alone`, none runnable) has no representative and no weight, and its feeders count in no estimate.
- The representative choice acts only when a group is formed. A kept representative was chosen for an ancestor group, so after several splits it can be far from its own group's target point under any choice; `scout_repick_<stamp>.csv` counts such groups and shows what a fresh pick would give, and nothing uses it.
- The average feeder is a row built from sums; the representative is the real feeder nearest that row on the scaled log features, so its own `load_kw` need not be near the group's mean `load_kw`, and the weights need not add to the number of feeders.
- An average feeder with an empty feature (a ratio whose denominator adds to 0 over the group) stops the run. With the features of the setup this cannot happen, since a feeder with such a value is left out of grouping.
- The scout's partitions under one rule and one feature set are equal across the choices only while no two splits tie on their drop: ties go to the lower group number, and the choice can change which child keeps the parent's number. The scout checks the equality on every run.
