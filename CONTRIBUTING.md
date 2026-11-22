# Contributing

## Branches

Work on a branch named after the job or the change, made from `main` or from the open branch it builds on. Never commit to `main`; the maintainer opens and merges pull requests on GitHub.

## Commits

- One logical change per commit, with a message that says what changed and why, and the decision it follows (for example K72).
- Moves and renames of files go in their own commit, made with `git mv`, with only import lines and paths changed.
- The commit author and committer are Luis Victor, the maintainer. Messages carry no co-author or tool line.
- `git diff --check` is clean before each commit.
- Every value follows the standard in [AGENTS.md](AGENTS.md); the scan test enforces it.
- New problems found while running the coupling go to `docs/problem_log.csv` in the same commit.

## Tests

```
uv sync
uv run pytest
```

All tests pass before a push. No test touches the network.
