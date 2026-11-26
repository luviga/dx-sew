# The power-flow trial

The trial (decision K73) runs a small set of cases through DeltaE/dss-eulp-coupling (https://github.com/DeltaE/dss-eulp-coupling) at the pin, in full: every phase from `pre` to `8`, both seasons and the three mixes of the coupling's mixes file. It proves the run code for the electrification mixes and the substation rebuild before the run of the whole library, and measures run time on the heavy phases. The code is in `dxsew/runs/trial.py`, `dxsew/runs/batch.py`, `dxsew/curves/mix.py` and `dxsew/runs/results.py`; the tests are in `tests/regression/test_trial.py` and `tests/regression/test_batch.py`.

## Cases

- **Run feeders.** Per stratum, the run feeder of the largest group (`trial.trial_feeders`): the group with the most customers among the groups that name a typical feeder of their own. A placeholder group (decision K62) borrows another group's run feeder, so it is not a candidate. Each run feeder runs alone, on its alone tree ([runs.md](runs.md)). Before any file is written, each alone tree is checked against its staged folder with `cases.build_alone_tree`; a missing tree, or one that holds other files, stops that feeder and nothing is copied over it.
- **Check set.** The substations of the setup key `trial.check_set`, run whole. A check-set substation is taken from the staging of the runs (`runs.staging_folder`) when its staging table marks it staged, else from the staging of the sample (`staging.folder`). Its feeders are the folders `settings.feeders_of` finds.
- **Timing set.** The `trial.timing_cases` run-feeder cases whose loads are nearest the median of the run-feeder cases; a tie goes to name order.

## Passes and settings

The setup key `trial.passes` holds one entry per pass:

- `pass_1`, `peak_day_rule: per_circuit`: each mix runs on the feeder's own worst day of that mix (the seasonal peak of the feeder's building curve under the mix; the baseline mix on the feeder's baseline peak day).
- `pass_2`, `peak_day_rule: given`, `given_day: B`: every mix, the baseline mix included, runs on day B ([curves.md](curves.md)).

Phases `pre` to `6_variants` read no peak-day rule, so they do the same work in both passes; `mix_days` and the phases after it differ.

```
uv run python -m dxsew.runs.trial --stamp <stamp> --typical <typical_feeders csv> --features <features parquet> \
    --given-days <given_days_B csv> --runs-root pass_1=<folder> --runs-root pass_2=<folder> \
    --base-local <coupling local.yaml> --runs-staging <staging_runs csv> \
    --check-sample <sample csv> --check-staging <staging csv>
```

For each pass it writes `<data_root>/texas/coupling_settings_trial_<pass>_<stamp>/`, merged by `run_case.py --settings` over the coupling's own files (coupling README, "Settings from a caller"):

- `local.yaml`: every key of the coupling `local.yaml` given, with `runs_root` set to the pass's folder;
- `topology_registry.yaml`: one entry per run feeder from `cases.registry_entry` on its alone tree, and one per check-set substation from `settings.registry_entry` on the staging chosen above;
- `pipeline_config.yaml`: the setup's `runs.pipeline_override` (the residential release of decision K22 and `horizon_days`) with the pass's `peak_day_rule`; nothing else;
- for a pass that names a `given_day`, `given_days.csv` (`Feeder, Season, Month, Day`, feeders named `<substation>--<feeder>`): the run feeders' rows of the given days file, and the same date in each season for every check-set feeder.

and one case per run feeder and per check-set substation in `cases_trial_<pass>_<stamp>/`, then `trial_cases_<stamp>.csv` (pass, rule, case, kind, feeders, loads, stratum, timing, case file, settings folder, runs root). A dry run (`run_case.py --phase pre --dry-run`, pin checked before and after) of each pass's first case and of each check-set case is saved as `dry_run_trial_<pass>_<case>_<stamp>.txt`.

## Schedule C

`dxsew.runs.batch` runs one case, one season and one phase per call, through `coupling.run_case(..., phase=<id>, check=False, quiet=True)`. It reads the phase order and the seasons from the coupling clone (`configs/defaults.yaml`) when it runs and checks the pin once before and once after the batch.

```
uv run python -m dxsew.runs.batch --stamp <stamp> --name <pass> --settings <settings folder> \
    --case <case.yaml> [--case ...] --log-dir <folder> --min-free-gb <GB> [--one-at-a-time <case id> ...]
```

1. **Serial part.** Every case, in the order of the `--case` arguments, runs each season and each phase up to and including `runs.serial_through_phase` (`5a`), one call at a time. Phase 5a is the only phase that downloads building files; it writes each file under its final name and skips a name that exists, so two downloads at once could leave a partial file that later runs skip (problem log P7). A case-season does not start while the drive of the runs has less than `--min-free-gb` free.
2. **One at a time.** The cases named with `--one-at-a-time` run their later phases one call at a time, with no other call of the batch running. In the first pass these are the timing cases.
3. **Parallel part.** The other cases, at most `runs.parallel` at once. A worker takes one case and runs its later phases season by season in the coupling's order.

A call that exits other than 0 ends its case-season: its later phases are skipped and recorded, and the batch goes on (decision K61).

The first pass runs the serial part for every case, then the timing cases one at a time, then the other cases at `runs.parallel` at once. The second pass runs the serial part, then every case at `runs.parallel` at once. The speed-up of a phase from 5b to 6_variants is the timing cases' seconds in the first pass over their seconds in the second, since that work is the same under both rules; the same ratio for `mix_days` to `8` compares unlike work, because under `given` the baseline mix is also cut and run.

### The batch table

`batch_<name>_<stamp>.csv` has one row per call or skipped phase: `case`, `season`, `phase`, `part` (`serial`, `one_at_a_time`, `parallel`), `start_utc`, `end_utc`, `seconds`, `exit_code`, `status` (`ok`, `failed`, `skipped`), `running_at_start` (calls of the batch running when it started, itself included), `free_gb` (free space on the drive of the runs), `free_memory_gb` and `note`. It is rewritten after every call. `batch_<name>_<stamp>_machine.csv` holds, per part, its UTC start and end, wall seconds, the machine's logical processors, the lowest free memory seen at the start or end of any call, and the most calls at once; the last two are taken over the whole batch, so every part's row carries the same values. The per-call output lines, with the UTC time of each, are in `<log dir>/<name>/<case>__<season>__<phase>.log`.

## Curves of the trial

```
uv run python -m dxsew.curves.mix --stamp <stamp> --trial-cases <trial_cases csv> --curves <curves folder>
```

1. **Baseline reproduction.** Each feeder of the first pass's workspaces in the coupling's first season is rebuilt with the K66 rebuild of [curves.md](curves.md) and checked against the workspace's `TX_peak_days.csv`; a run feeder's curve is set against `feeder_curves.parquet` of the curves folder given, point by point (`curves_repro.csv`, and the curves in `baseline_curves.parquet`).
2. **Mix curves.** For each feeder and mix of the weights file `TX_circuit_weights.csv` (setup key `curves.circuit_weights_file`; columns `Feeder, Mix, Group, Parquet_File, Weight`), per heating group in the order of `curves.mix_group_lists` and per row of that group's required-parquet list in file order: `out.electricity.total.energy_consumption` x 4 x `Weight`, the weight looked up by group and `Parquet_File`; a row without a weight adds nothing. The weight is used as a Python float, as the coupling uses it, so a float32 building column stays float32. The same is summed per sector and end use. The curve keeps the building files' own timestamps. An unreadable or missing building file, or timestamps other than the first file's, stops that feeder and mix with a message; there is no fallback. This is the formula of `run_mix`, steps 2 to 4, in https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/5b_profile_generation/find_max_day_curve_impl.py ; dx-sew repeats the formula, not the code.
3. **Checks** (`mix_checks.csv`). Placed on the coupling's position calendar (point i at `curves.coupling_start` plus 15 minutes times i), each curve of the first pass must peak in each season on the month and day of that season workspace's `TX_circuit_days.csv`, at its `Peak_kW`, within `curves.check_rel_tolerance`. In the second pass each row must carry the given day, and its `Peak_kW` must be the curve's highest point on that day within the same tolerance. The column `weights_equal_to_first_season` says whether the season workspace's weights file equals the first season's byte for byte. A pass whose weights and lists equal the first pass's byte for byte reuses the first pass's curves.
4. **Mix days** (`mix_days.csv`): per feeder, mix and season, the first pass's own worst day and peak, the time of that peak, the curve's highest point on day B, its ratio to the own peak, and the five largest end uses at the own peak, as `dxsew.curves.year` lists them.

`mix_curves.parquet` holds `feeder, mix, timestamp, kw` (float32) of the first pass.

The building curves of a mix carry heating electrification only; EVs, PV and storage are added by the coupling's phase 7 on the power-flow day. A mix's worst day is therefore chosen without them.

## Feeder results

```
uv run python -m dxsew.runs.results --stamp <stamp> --trial-cases <trial_cases csv> --batch pass_1=<csv> --batch pass_2=<csv>
```

`trial_feeders_<stamp>.csv` has one row per pass, case, feeder, season and mix. Circuit folders of phase 7 are mapped to feeders and mixes by the columns `substation`, `feeder` (`circuit_<id>`) and `scenario` (the mix) of `circuit_summary_combined.csv`, and each circuit id to its feeder folder by `reverse_circuit_map` of the workspace's `feeder_registry.json`. Each row holds the day and building-curve `Peak_kW` of `TX_circuit_days.csv`; from `aggregate_m2_combined.csv` (rows `row_type` data) the highest `P_3ph (kW)`, the highest `S_3ph (kVA)` and P over S at the step of highest S; `feeder_head_kva` (the `normamps` of the line monitor m1 watches, times the circuit `basekv`, times the square root of 3) and the highest S over it; `v_pu_min`, `v_pu_max`, `n_loads_below`, `n_loads_above`, `n_load_steps_outside` (band 0.95 to 1.05 pu); and `n_loads`, `n_evs`, `n_pv`, `n_storage`. Active power is never read from `aggregate_m1` (problem log P6). A case-season with a failed call in its batch gets one row with its failing phase.

## Outputs

| File | Written by |
|---|---|
| `coupling_settings_trial_<pass>_<stamp>/`, `cases_trial_<pass>_<stamp>/`, `trial_cases_<stamp>.csv`, `dry_run_trial_<pass>_<case>_<stamp>.txt` | `dxsew.runs.trial` |
| `batch_<pass>_<stamp>.csv`, `batch_<pass>_<stamp>_machine.csv` | `dxsew.runs.batch` |
| `trial_<stamp>/curves_repro.csv`, `baseline_curves.parquet`, `mix_curves.parquet`, `mix_checks.csv`, `mix_days.csv` | `dxsew.curves.mix` |
| `trial_feeders_<stamp>.csv` | `dxsew.runs.results` |

The substation side of the trial, the baseline rebuild of every frame substation and the check set, is in [assembly.md](assembly.md).
