"""Runs of DeltaE/dss-eulp-coupling at one pinned commit (decision K53).

dx-sew runs one commit of the coupling, recorded in the tracked file
``configs/coupling_pin.yaml``. ``check_pin()`` stops when the coupling clone
(``coupling_repo`` of ``configs/local.yaml``) is at another commit or has local
changes. ``run_case()`` checks the pin, runs the coupling's ``run_case.py`` with
``coupling_python`` from the clone, and checks the pin again after the run, which
shows that the run wrote nothing tracked or untracked in the clone.

Run from the repository::

    uv run python -m dxsew.coupling --case <case.yaml> --settings <folder> [--season summer|winter] [--log <file>]
    uv run python -m dxsew.coupling --check-only

Coupling repository: https://github.com/DeltaE/dss-eulp-coupling (``run_case.py``,
README sections "Settings from a caller" and "Peak-day rules").
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PIN_FILE = "coupling_pin.yaml"
SEASONS = ("summer", "winter")


class PinError(RuntimeError):
    """The coupling clone is not at the pinned commit, or it has local changes."""


def _configs() -> Path:
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        if (folder / "configs" / PIN_FILE).is_file():
            return folder / "configs"
    raise FileNotFoundError(f"configs/{PIN_FILE} not found in {here} or any folder above it.")


def read_pin(path: Path | None = None) -> dict:
    """``repository`` and ``commit`` of the pin file."""
    path = Path(path) if path else _configs() / PIN_FILE
    with path.open(encoding="utf-8") as handle:
        pin = yaml.safe_load(handle)
    if not isinstance(pin, dict) or not pin.get("repository") or not pin.get("commit"):
        raise PinError(f"{path} must define repository and commit.")
    pin["commit"] = str(pin["commit"]).strip().lower()
    return pin


def read_local(path: Path | None = None) -> dict:
    """``coupling_repo`` and ``coupling_python`` of ``configs/local.yaml``."""
    path = Path(path) if path else _configs() / "local.yaml"
    with path.open(encoding="utf-8") as handle:
        local = yaml.safe_load(handle) or {}
    missing = [key for key in ("coupling_repo", "coupling_python") if not local.get(key)]
    if missing:
        raise KeyError(f"{path} lacks the keys: {', '.join(missing)}.")
    return local


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise PinError(f"git {' '.join(args)} failed in {repo}: {done.stderr.strip()}")
    return done.stdout


def check_pin(repo: Path, pin: dict | None = None) -> str:
    """Stop unless the clone's ``HEAD`` is the pinned commit and it has no local change.

    A local change is any line of ``git status --porcelain --untracked-files=all``:
    a modified, added, deleted or untracked file. Returns the commit.
    """
    pin = pin or read_pin()
    head = _git(repo, "rev-parse", "HEAD").strip().lower()
    if head != pin["commit"]:
        raise PinError(
            f"The coupling clone {repo} is at {head}, not at the pinned commit {pin['commit']} "
            f"of {pin['repository']}. Check out the pinned commit, or move the pin by a recorded decision."
        )
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status.strip():
        lines = status.strip().splitlines()
        raise PinError(
            f"The coupling clone {repo} has local changes ({len(lines)} paths), for example:\n  "
            + "\n  ".join(lines[:10])
            + "\nRun the coupling only from a clean clone at the pinned commit."
        )
    return head


def command(case: Path, settings: Path, python: Path, seasons=SEASONS) -> list[str]:
    """``run_case.py`` command line for all phases of the case in the seasons given."""
    seasons = list(seasons)
    if not seasons or any(season not in SEASONS for season in seasons):
        raise ValueError(f"seasons must be taken from {SEASONS}")
    cmd = [str(python), "run_case.py", "--case", str(Path(case).resolve()), "--all",
           "--settings", str(Path(settings).resolve())]
    if len(set(seasons)) == 1:
        cmd += ["--season", seasons[0]]
    return cmd


def run_case(case: Path, settings: Path, seasons=SEASONS, repo: Path | None = None,
             python: Path | None = None, log: Path | None = None, pin: dict | None = None) -> int:
    """Check the pin, run the case with the coupling's ``run_case.py``, check the pin again.

    Every output line is printed and, when ``log`` is given, written there with
    the UTC time it arrived. Returns the exit code of ``run_case.py``; the second
    pin check raises ``PinError`` whatever the exit code.
    """
    if repo is None or python is None:
        local = read_local()
        repo = Path(repo or local["coupling_repo"])
        python = Path(python or local["coupling_python"])
    repo = Path(repo).resolve()
    check_pin(repo, pin)
    cmd = command(case, settings, python, seasons)
    handle = Path(log).open("a", encoding="utf-8") if log else None
    try:
        process = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in process.stdout:
            sys.stdout.write(line)
            if handle:
                handle.write(f"{datetime.now(timezone.utc).isoformat(timespec='milliseconds')} {line}")
                handle.flush()
        code = process.wait()
    finally:
        if handle:
            handle.close()
    check_pin(repo, pin)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one coupling case at the pinned commit.")
    parser.add_argument("--case", type=Path, help="case file of the coupling")
    parser.add_argument("--settings", type=Path, help="settings folder of the case")
    parser.add_argument("--season", choices=SEASONS, action="append", help="season; default both")
    parser.add_argument("--log", type=Path, default=None, help="file that receives the timed output lines")
    parser.add_argument("--check-only", action="store_true", help="check the pin and run nothing")
    args = parser.parse_args(argv)
    local = read_local()
    if args.check_only:
        print(f"pin ok: {check_pin(Path(local['coupling_repo']))}")
        return 0
    if not args.case or not args.settings:
        parser.error("--case and --settings are required unless --check-only is given")
    return run_case(args.case, args.settings, args.season or SEASONS, log=args.log)


if __name__ == "__main__":
    sys.exit(main())
