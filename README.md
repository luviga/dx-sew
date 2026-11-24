# dx-sew

dx-sew builds the synthetic balancing area of Paper 1 by sewing together three public pieces: places, SMART-DS distribution circuits and EULP buildings. It is built one balancing area at a time, starting with ERCOT, and every value it uses is either sourced externally, computed by a rule from sourced inputs on every run, or labelled as an ASSUMPTION.

The layout follows [OSTRAM](https://github.com/Climate-Lead-Group/OSTRAM) (decision K72): a region-neutral package, one setup file per balancing area, small sourced tables in `inputs/`, generated files outside the code, and a test that fails if any `.py` file holds data.

## Folder layout

- `dxsew/`: the Python package, region-neutral. Subpackages `sources` (readers of the public data), `frame` (veto, map, sample, trust checks, features, groups, representatives and their scout), `curves` (year curves and common days), `fit` (fit target and weights), `runs` (staging, coupling settings, cases and the coupling at its pin) and `assembly` (substation transformers, ratings and the baseline assembly).
- `config/`: settings. `config/setups/registry.json` names the setups and the default one; `config/setups/ercot_2018.yaml` holds every value of ERCOT in 2018; `areas.yaml`, `units.yaml`, `runtime.yaml` and `scan.yaml` hold for every setup. Copy `config/local.yaml.example` to `config/local.yaml` and set the paths of your machine; `config/local.yaml` is not tracked.
- `inputs/`: small sourced tables (zone codes of the SMART-DS load shape names, state FIPS codes).
- `tests/regression/`: tests, run with pytest. No test touches the network.
- `docs/`: method notes and the reference pages: [file map](docs/file-map.md), [configuration](docs/configuration.md), [adapting to another balancing area](docs/adapting.md) and [glossary](docs/glossary.md). The frame procedure for any balancing area, with the EIA-861 veto and its lookups, is in [docs/frame_procedure.md](docs/frame_procedure.md).

Downloads and generated files go to the workspace, the `data_root` folder named in `config/local.yaml`, outside the repository. Nothing generated is committed.

Every command takes `--setup <id>`; without it the registry's default setup is used.

## Fit target

The circuit weights are fitted to ERCOT's observed 2018 hourly demand from EIA-930, after taking out industrial and transportation load (EIA-861) and transmission and distribution losses (EIA State Electricity Profile, Table 10). The result is residential and commercial load at the customer meter, the same quantity the bottom-up circuits produce. Run it with `uv run python -m dxsew.fit.target`. The method, sources and assumptions are in [docs/fit_target.md](docs/fit_target.md).

## Feeder groups

Every feeder of the ERCOT frame is described by features read from its circuit text files, the feeders of each stratum are grouped with one nested Ward tree, and each group gets a typical feeder and a starting weight. Feeders the coupling cannot run at its pin are flagged and never chosen. Run it with `uv run python -m dxsew.frame.features` and then `uv run python -m dxsew.frame.groups`. The method, settings and output files are in [docs/groups.md](docs/groups.md).

## Commands

| Step | Command | Method note |
|---|---|---|
| Frame and sample | `uv run python -m dxsew.frame.sample` | [texas_sample.md](docs/texas_sample.md) |
| Veto report of one area | `uv run python -m dxsew.frame.veto --area <name>` | [frame_procedure.md](docs/frame_procedure.md) |
| Trust checks of the map | `uv run python -m dxsew.frame.trust --stamp <frame stamp>` | [texas_sample.md](docs/texas_sample.md) |
| Zone code table | `uv run python -m dxsew.sources.smartds_names` | [texas_sample.md](docs/texas_sample.md) |
| Staging | `uv run python -m dxsew.runs.stage` | [texas_sample.md](docs/texas_sample.md) |
| Coupling settings | `uv run python -m dxsew.runs.settings` | [coupling.md](docs/coupling.md) |
| Features and groups | `uv run python -m dxsew.frame.features`, `uv run python -m dxsew.frame.groups` | [groups.md](docs/groups.md) |
| Representatives and their weights | `uv run python -m dxsew.frame.representatives --stamp <stamp> --features-stamp <stamp>` | [representatives.md](docs/representatives.md) |
| Scout of the representative choice | `uv run python -m dxsew.frame.representatives_scout --stamp <stamp> --features-stamp <stamp> --reference-stamp <stamp>` | [representatives.md](docs/representatives.md) |
| Substation ratings and assembly | `uv run python -m dxsew.assembly.substations ratings`, `uv run python -m dxsew.assembly.substations assemble` | [assembly.md](docs/assembly.md) |
| Run feeders | `uv run python -m dxsew.runs.cases` | [runs.md](docs/runs.md) |
| One coupling case | `uv run python -m dxsew.runs.coupling` | [coupling.md](docs/coupling.md) |
| Year curves and days | `uv run python -m dxsew.curves.year` | [curves.md](docs/curves.md) |
| Fit target | `uv run python -m dxsew.fit.target` | [fit_target.md](docs/fit_target.md) |
| Fit and sector factors | `uv run python -m dxsew.fit.weights` | [fit.md](docs/fit.md) |

## Problem log

[docs/problem_log.csv](docs/problem_log.csv) lists every problem found while running the coupling or building its inputs: where it is, the case that showed it, its symptom and cause, a candidate fix, its effect, how dx-sew handles it, its status and how to retest it. Every job appends its new problems to the file in the same commit as its other changes. When the coupling pin changes, the debugging job works from this file.

## Setup and tests

The environment is managed with [uv](https://docs.astral.sh/uv/) and needs Python 3.12 or later.

```
uv sync
cp config/local.yaml.example config/local.yaml   # then edit the paths
uv run pytest
```

Rules for contributors and coding agents are in [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).
