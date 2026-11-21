# Rules for coding agents

These rules hold for every change to dx-sew, by a person or by an agent.

## The standard of values

- Every value the code uses is externally sourced, computed every run by a stated rule from sourced inputs, or labelled ASSUMPTION. Internal files are never a source. A source is a retrievable URL, DOI or public repository permalink.
- No `.py` file of `dxsew/` holds a number other than 0, 1 and -1, a region or balancing-area name, a year, the file name of a data source, a tolerance, or ASSUMPTION text. Each such value lives in a YAML or CSV file under `config/` or `inputs/`, next to its source or its ASSUMPTION sentence (a key `<key>_assumption`). The run copies the ASSUMPTION sentences into its outputs.
- An ASSUMPTION sentence is moved word for word. Rewording one changes the outputs of record and needs a recorded decision.
- `tests/regression/test_no_data_in_code.py` enforces the rule with `ast`: numbers (the exponent of `**` excepted), strings holding ASSUMPTION, a year, a data file name or a term of `config/scan.yaml`, and docstrings holding ASSUMPTION. There is no exemption per line and no `noqa`. Comments do not hold the word ASSUMPTION either.
- Writing in tracked files: plain English, no em dashes, no machine paths, no figures from model results, no hash values.

## Layout

- `dxsew/` is region-neutral; no folder or module name holds a region. Readers of public data are in `dxsew/sources/`, the frame and groups in `dxsew/frame/`, curves in `dxsew/curves/`, the fit in `dxsew/fit/`, staging and the coupling in `dxsew/runs/`.
- One setup per balancing area and year in `config/setups/`, named in `config/setups/registry.json`. Shared settings in `config/areas.yaml`, `config/units.yaml`, `config/runtime.yaml`, `config/scan.yaml`. Small sourced tables in `inputs/`.
- Generated files go to the workspace (`data_root` of `config/local.yaml`), never into the repository. [docs/file-map.md](docs/file-map.md) lists every file and what writes and reads it; [docs/configuration.md](docs/configuration.md) every key.

## Commands

```
uv sync
uv run pytest
uv run python -m dxsew.<subpackage>.<module> --help    # every command takes --setup <id>
```

## Git

- Never commit data: no parquet, zip, xlsx, workspace file or `config/local.yaml`.
- Never commit to `main`. Work on a branch; pull requests are opened and merged by the maintainer.
- Commit author and committer are the maintainer. No AI co-author line, no tool line and no AI name in commit messages, pull request texts or outputs.
- Moves and renames go in their own commit, with nothing else changed.
