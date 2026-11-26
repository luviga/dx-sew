# Running the coupling at a pinned commit

dx-sew builds its circuit results with DeltaE/dss-eulp-coupling (https://github.com/DeltaE/dss-eulp-coupling). It runs one commit of that repository, the pin, so that every result of dx-sew comes from known coupling code (decision K53). The code is in `dxsew/runs/coupling.py` and its tests are in `tests/regression/test_coupling.py`.

## The pin

`config/coupling_pin.yaml` is tracked and holds two keys:

- `repository`: the coupling repository URL;
- `commit`: the full commit hash dx-sew runs.

It is where dx-sew pins the coupling. `docs/problem_log.csv` cites the same commit inside the permalinks of its `where` column.

The coupling clone is named by `coupling_repo` in `config/local.yaml` (not tracked), and the interpreter that runs it by `coupling_python`. The coupling has its own environment, separate from dx-sew's, built from the clone's `requirements.txt`.

## check_pin()

`check_pin(repo)` reads the pin and stops with `PinError` when:

- `git rev-parse HEAD` of the clone differs from the pinned commit; or
- `git status --porcelain --untracked-files=all` of the clone prints any line, that is a modified, added, deleted or untracked file. Files the clone's `.gitignore` names, such as byte-code caches, do not count.

The message names the clone, the commit found or the first changed paths, and what to do. To check the clone alone:

```
uv run python -m dxsew.runs.coupling --check-only
```

## run_case()

`run_case(case, settings, seasons)`:

1. calls `check_pin()`;
2. runs `<coupling_python> run_case.py --case <case> --all --settings <folder>` with the clone as working folder, adding `--season <season>` when one season is asked for and leaving it out for both;
3. prints every output line and, when a log file is given, writes each line there with the UTC time it arrived, which gives the time of each phase;
4. calls `check_pin()` again. A run that left any tracked or untracked file in the clone stops here, whatever its exit code.

It returns the exit code of `run_case.py`. From the command line:

```
uv run python -m dxsew.runs.coupling --case <case.yaml> --settings <folder> [--season summer] [--log <file>] [--phase <id>] [--no-check]
```

Two keyword arguments change this, both off by default:

- `phase="<id>"` (`--phase <id>`) runs one phase of the coupling's phase order (`pre`, `1`, `2`, ..., `5b`, ...) with `run_case.py --phase <id>` in place of `--all`. `run_case.py` creates the case's workspace when it does not exist and runs the phase into it (`prepare_work_root` in https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/run_case.py#L323-L338), so the phases of one case can be run one call at a time, in the coupling's order.
- `quiet=True` writes the output lines to the log file only, not to the console, so cases run at once do not mix their output (used by `dxsew.runs.batch`, [trial.md](trial.md)).
- `check=False` (`--no-check`) runs neither pin check. `git status` takes the clone's index lock for a moment, so two pin checks at the same time can collide when several cases run at once. A caller that runs a batch in parallel checks the pin once before the batch and once after it instead.

## The settings folder

The coupling's `run_case.py --settings <folder>` merges files named `topology_registry.yaml`, `donor_registry.yaml`, `defaults.yaml`, `local.yaml` and `pipeline_config.yaml` over the repository's own files, so dx-sew never edits the clone (coupling README, "Settings from a caller"). The registries and `local.yaml` take new keys, and the clone needs no `config/local.yaml` of its own when the folder holds one. A file with another `.yaml` name stops the run. `<run_dir>/resolved_case.yaml` and `<run_dir>/settings/` record what the run used.

`dxsew.runs.settings` writes such a folder for staged Texas substations (see [texas_sample.md](texas_sample.md), section "Coupling settings"): `local.yaml` with an absolute `runs_root` outside the clone, `topology_registry.yaml`, `pipeline_config.yaml` with the setup's `runs.pipeline_override` (the residential release of decision K22 and `horizon_days`) and `peak_day_rule: given`, and `given_days.csv` with the day of each feeder and season (coupling README, "Peak-day rules"). The case files go beside it. Because `runs_root` is absolute, workspaces go to `<runs_root>/<case_id>/<season>/workspace` and nothing is written into the clone.

## Moving the pin

The pin moves only by a recorded decision. Before it moves, a reference substation is run at the old pin and at the new pin with the same settings folder, and the two results are compared. The decision records the old and new commits, the reference substation and the outcome of the comparison. Then `commit` in `config/coupling_pin.yaml` changes in its own commit, and the clone is checked out at the new commit.
