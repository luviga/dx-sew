# Error curve, sample order and stop rule

On 24 November 2026 the maintainer fixed how the number of representatives that get power flow is read (decision K80, after decisions K76 and K78). The weighted representatives of [representatives.md](representatives.md) are scored against the EULP year curves of a random sample of feeders, at every number of representatives, and a stop rule reads from that error curve where one more power-flow run stops paying.

Two commands build everything that does not need the coupling: `dxsew.frame.sample_order` draws the sample order and writes the list of feeders to match, and `dxsew.frame.error_curve` scores the curve, its bootstrap band and the stop rule. The EULP curves of the sample feeders and of the candidate representatives come from the coupling's matching phases at its v1.0.0 tag; a later job (J9b) matches the feeders of the match list, builds their year curves with `dxsew.curves.year` and runs `dxsew.frame.error_curve` on them.

The groups and representatives are those of `dxsew.frame.representatives` under the choice of record, `representative_choice: load_weighted_centre` (decision K78). The order rule is `error_curve.order_rule`. No curve is multiplied by any factor other than the weight of decision K76, a load kW over a representative's load kW.

## Commands

From the repository, with `config/local.yaml` in place:

```
uv run python -m dxsew.frame.sample_order --stamp <stamp> --features-stamp <features stamp> --representatives-stamp <stamp of a run of dxsew.frame.representatives> [--setup <id>]
uv run python -m dxsew.frame.error_curve --stamp <stamp> --features-stamp <features stamp> --representatives-stamp <stamp> --curves <feeder_curves.parquet> --sample <csv with a feeder column> [--cap <n>] [--on-curves] [--setup <id>]
```

`--sample` takes any CSV with a `feeder` column. When the CSV also has a `role` column, as `match_list_<stamp>.csv` has, only its rows of role `sample` or `both` are sample feeders, so the match list can be passed as it is. `--cap` replaces `error_curve.power_flow_cap` for the stop rule of that run. `--on-curves` takes the groups from the method on the feeders of `--curves` alone (below); the representatives stamp is then not read.

## Sample order and match list

The feeders in grouping are those of the first step of rule `error_curve.order_rule` in `representatives_groups_<stamp>.parquet`. For each stratum, its feeders in grouping are sorted by name and then put in a random order by `numpy.random.default_rng(seed).permutation`, where the seed is `error_curve.sample_seed` plus the stratum's position, from 0, in the sorted list of strata. A stratum's order depends only on its own feeders and that position, so changing the feeders of another stratum, or adding a stratum that sorts after it, never moves it.

The sample of a stratum is the first `error_curve.sample_per_stratum` feeders of its order, or all of them when the stratum has fewer. Because the order is fixed, a larger sample later only adds feeders to the one drawn now.

The match list holds the feeders to match, in this order: the representatives of rule `error_curve.order_rule` at n = `representatives.max_representatives` (or at the last step, when the order stops before it), by `since_n` and then name, role `candidate`; then, per stratum in name order, the sample feeders by rank, role `sample`. A feeder in both lists appears once, among the candidates, with role `both`. The command prints, per stratum, how many sample feeders and candidates are `runnable_alone` at today's pin.

## The error curve

For every n from the first step (the number of strata) to the last step of the curve:

1. Per stratum s, over its sample feeders S_s that are in grouping and have a curve: `true` is the sum of their own curves; `estimate` is the sum over f in S_s of the curve of f's group representative at step n times f's `load_kw` over that representative's `load_kw`. A sample feeder that is its own group's representative adds its own curve, so when every sample feeder is a representative the error is 0.
2. Both are scored with `curve_errors` of `dxsew.frame.representatives`: `cv_rmse` (root mean square error over the mean of `true`), `error_at_true_peak` (estimate over true at the step of the true peak, minus 1), `error_of_peak` (the estimate's own peak over the true peak, minus 1) and `error_energy` (estimate's sum over true's sum, minus 1).
3. The area figure of `cv_rmse`, `error_of_peak` and `error_energy` is the strata's values averaged with weights equal to each stratum's share of the `load_kw` of the group rows, over the strata that have sample feeders. No curve is scaled to build it; `error_at_true_peak` has no area figure.

Groups. Normally the groups are those of a run of `dxsew.frame.representatives` (`representatives_<stamp>.parquet` and `representatives_groups_<stamp>.parquet`), rule `error_curve.order_rule`. With `--on-curves` they come from the method on the feeders with curves alone, as the head start does: `prepare` with `keep` set to those feeders, then `sequence` with the rule and `representatives.representative_choice`. This is the dry run on the run feeders that already have year curves: no other frame representative has a curve yet. Those feeders were chosen as group centres and not at random, so a dry run checks the code and not the error of the method.

## Bootstrap band

`error_curve.bootstrap_draws` resamples are drawn up front from `numpy.random.default_rng(error_curve.sample_seed)`, strata in name order: each resample draws the sample feeders of each stratum with replacement, as many as the stratum has. A feeder drawn twice counts twice in both `true` and `estimate`. The area `cv_rmse` and `error_of_peak` of each resample at each n give the band at the fractions `error_curve.band` (`numpy.quantile`, linear). The band shows how much the curve depends on which feeders were drawn.

## Stop rule

The stop point for a gain g is the smallest n, from the first step to the cap (`error_curve.power_flow_cap`, or `--cap`), such that for every larger m up to the cap both

- `cv_rmse(n) - cv_rmse(m)` is at most g, and
- `abs(error_of_peak(n)) - abs(error_of_peak(m))` is at most g,

on the area figures. A curve that is flat for a while and then drops does not stop on the flat part: the rule looks at every larger n up to the cap, not at the next one. The stop point is reported for `error_curve.stop_gain` and for each other gain of `error_curve.report_gains`.

## Missing curves

A sample feeder without a curve, or not in grouping, is left out and counted; the run prints the counts. The curve runs from the first step to the last step at which every representative that a sample feeder with a curve needs has a curve, and to at most `representatives.max_representatives`; the run prints the representative that ended it and the step that needed it, and `error_stop_<stamp>.csv` records the last step in `last_n_with_curves`. The stop rule then looks only up to that step.

## Settings

All in the block `error_curve` of `config/setups/ercot_2018.yaml`.

| Setting | Value | ASSUMPTION or source |
|---|---|---|
| `order_rule` | `ward_drop` | decision K78; one of `representatives.order_rules` |
| `sample_per_stratum` | 50 | ASSUMPTION: 50 feeders per stratum, drawn at random, measure the error of the weighted representatives well enough to read a gain of one percentage point; the bootstrap band of every run shows whether that holds. |
| `sample_seed` | 20261124 | ASSUMPTION: any fixed seed serves; it is recorded so the same sample is drawn again, and the bootstrap band shows how much one draw matters. |
| `stop_gain` | 0.01 | ASSUMPTION: one more power-flow run is worth it while some larger number of representatives, up to the cap, still lowers the CV(RMSE) or the peak error by more than one percentage point. |
| `power_flow_cap` | 150 | ASSUMPTION: 150 representatives is the most power flow one machine can run for the study, about 26 to 33 hours per pass at four at once at the trial's measured rate. |
| `report_gains` | 0.02, 0.01, 0.005 | ASSUMPTION: the stop point is also reported for half and twice the chosen gain, so a reader sees how much it depends on that setting. |
| `bootstrap_draws` | 1000 | ASSUMPTION: 1,000 resamples of the sample feeders within each stratum give a stable band. |
| `band` | 0.05, 0.95 | ASSUMPTION: the band runs from the 5th to the 95th percentile of the resampled errors. |

The number of candidate representatives is `representatives.max_representatives`. `bootstrap_block` of `config/runtime.yaml` sets how many resamples are held in memory at a time; it changes no figure.

## Output files

All under `<data_root>/<output_folder>/`, named with the stamp of the run.

- `sample_order_<stamp>.csv`: `stratum`, `rank` (from 1), `feeder`, `load_kw`, `customers`, `runnable_alone`; every feeder in grouping once.
- `match_list_<stamp>.csv`: `feeder`, `role` (`candidate`, `sample` or `both`), `stratum`, `rank` (empty for candidates), `since_n` (empty for sample feeders), `runnable_alone`.
- `error_curve_<stamp>.csv`: one row per n and scope: `n`, `scope` (`area` or the stratum), `sample_feeders`, `cv_rmse`, `error_at_true_peak`, `error_of_peak`, `error_energy`, and for `area` the band `cv_rmse_low`, `cv_rmse_high`, `peak_low`, `peak_high` (of `error_of_peak`).
- `error_stop_<stamp>.csv`: one row per gain: `gain`, `n`, `cv_rmse`, `error_of_peak`, `cap`, `last_n_with_curves`.
- `error_curve_assumptions_<stamp>.csv`: `block`, `key`, `sentence` for every ASSUMPTION key of the blocks `representatives` and `error_curve`, word for word.

## Known limits

- Adding a stratum that sorts before another stratum moves that stratum's position, and so its seed and its order.
- A dry run with `--on-curves` scores the run feeders, which were chosen as group centres and are not a random sample.
- The bootstrap resamples the sample feeders and keeps the groups and representatives fixed, so the band does not show how much the representatives themselves depend on the frame.
