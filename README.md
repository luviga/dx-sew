# dx-sew

dx-sew builds the synthetic balancing area of Paper 1 by sewing together three public pieces: places, SMART-DS distribution circuits and EULP buildings. It is built one balancing area at a time, starting with ERCOT, and every value it uses is either sourced externally, computed by a rule from sourced inputs on every run, or labelled as an ASSUMPTION.

## Folder layout

- `src/dxsew/`: the Python package.
- `configs/`: configuration. Copy `configs/local.yaml.example` to `configs/local.yaml` and set the paths for your machine; `configs/local.yaml` is not tracked.
- `tests/`: tests, run with pytest. No test touches the network.
- `docs/`: method notes. The frame procedure for any balancing area, with the EIA-861 veto and its lookups, is in [docs/frame_procedure.md](docs/frame_procedure.md).

Downloads and derived files go to the `data_root` folder named in `configs/local.yaml`, outside the repository. Nothing derived is committed.

## Fit target

The circuit weights are fitted to ERCOT's observed 2018 hourly demand from EIA-930, after taking out industrial and transportation load (EIA-861) and transmission and distribution losses (EIA State Electricity Profile, Table 10). The result is residential and commercial load at the customer meter, the same quantity the bottom-up circuits produce. Run it with `uv run python -m dxsew.fit_target`. The method, sources and assumptions are in [docs/fit_target.md](docs/fit_target.md).

## Feeder groups

Every feeder of the ERCOT frame is described by features read from its circuit text files, the feeders of each stratum are grouped with one nested Ward tree, and each group gets a typical feeder and a starting weight. Feeders the coupling cannot run at its pin are flagged and never chosen. Run it with `uv run python -m dxsew.texas.features` and then `uv run python -m dxsew.texas.groups`. The method, settings and output files are in [docs/groups.md](docs/groups.md).

## Problem log

[docs/problem_log.csv](docs/problem_log.csv) lists every problem found while running the coupling or building its inputs: where it is, the case that showed it, its symptom and cause, a candidate fix, its effect, how dx-sew handles it, its status and how to retest it. Every job appends its new problems to the file in the same commit as its other changes. When the coupling pin changes, the debugging job works from this file.

## Setup and tests

The environment is managed with [uv](https://docs.astral.sh/uv/) and needs Python 3.12 or later.

```
uv sync
cp configs/local.yaml.example configs/local.yaml   # then edit the paths
uv run pytest
```
