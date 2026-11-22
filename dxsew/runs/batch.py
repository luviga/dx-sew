"""Batches of coupling calls, one case, one season and one phase per call (schedule C of decision K69).

Run from the repository::

    uv run python -m dxsew.runs.batch --stamp <stamp> --name <name> --settings <folder> \\
        --case <case.yaml> [--case <case.yaml> ...] --log-dir <folder> --min-free-gb <GB> \\
        [--one-at-a-time <case id> ...] [--setup <id>]

Each call runs ``dxsew.runs.coupling.run_case(case, settings, [season], phase=<id>,
check=False)``. The phases and seasons are read from the coupling clone
(``configs/defaults.yaml``, keys ``phase_order`` and ``seasons``) at run time. The
pin is checked once before and once after the batch.

1. Serial part: every case, in the order given, runs every season and every
   phase up to and including ``runs.serial_through_phase``, one call at a time.
   A case-season is not started while the drive of the runs has less than
   ``--min-free-gb`` free.
2. One at a time: the cases named with ``--one-at-a-time``, in the order named,
   run their later phases one call at a time with no other call of the batch
   running.
3. Parallel part: the other cases, at most ``runs.parallel`` at once. A worker
   takes one case and runs its later phases season by season in the coupling's
   order.

A call that exits other than 0 ends its case-season: the later phases of that
case-season are skipped and recorded, and the batch goes on (decision K61).

Writes ``<data_root>/<output_folder>/batch_<name>_<stamp>.csv`` (one row per
call or skipped phase: case, season, phase, part, UTC start and end, seconds,
exit code, status, calls of the batch running at its start counting itself,
free GB on the drive of the runs, free memory GB) after every call, and
``batch_<name>_<stamp>_machine.csv`` (logical processors, lowest free memory
seen, start, end and wall seconds of each part).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml

from dxsew import config
from dxsew.config import Paths, load_local, load_sample_settings
from dxsew.frame.sample import BYTES_PER_GB, write_csv
from dxsew.runs import coupling as coupling_module

DEFAULTS_FILE = ("configs", "defaults.yaml")
SERIAL, ONE_AT_A_TIME, PARALLEL = "serial", "one_at_a_time", "parallel"
OK, FAILED, SKIPPED = "ok", "failed", "skipped"


def coupling_order(repo: Path) -> tuple[list[str], list[str]]:
    """``phase_order`` and ``seasons`` of the coupling clone's ``configs/defaults.yaml``."""
    with Path(repo).joinpath(*DEFAULTS_FILE).open(encoding="utf-8") as handle:
        defaults = yaml.safe_load(handle)
    return [str(phase) for phase in defaults["phase_order"]], [str(season) for season in defaults["seasons"]]


def split_phases(phases: list[str], serial_through: str) -> tuple[list[str], list[str]]:
    """The phases up to and including ``serial_through``, and the phases after it."""
    if serial_through not in phases:
        raise ValueError(f"phase {serial_through} is not in the coupling's phase order")
    cut = phases.index(serial_through) + 1
    return phases[:cut], phases[cut:]


def free_memory_bytes() -> int | None:
    """Physical memory free now, in bytes; None where it cannot be read."""
    if os.name == "nt":
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]
        status = Status()
        status.length = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.avail_phys)
        return None
    try:
        return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Batch:
    """State of one batch: the calls made, the case-seasons ended, the calls running."""

    runner: Callable[..., int]
    free_bytes: Callable[[], float]
    memory_bytes: Callable[[], int | None]
    min_free_bytes: float
    written: Callable[[list[dict]], None] = lambda rows: None
    rows: list[dict] = field(default_factory=list)
    ended: dict[tuple[str, str], str] = field(default_factory=dict)
    running: int = 0
    most_running: int = 0
    lowest_memory: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _memory(self) -> int | None:
        value = self.memory_bytes()
        if value is not None and (self.lowest_memory is None or value < self.lowest_memory):
            self.lowest_memory = value
        return value

    def skip(self, case: str, season: str, phases: list[str], part: str, note: str) -> None:
        with self.lock:
            for phase in phases:
                self.rows.append({"case": case, "season": season, "phase": phase, "part": part, "start_utc": "",
                                  "end_utc": "", "seconds": None, "exit_code": None, "status": SKIPPED,
                                  "running_at_start": None, "free_gb": None, "free_memory_gb": None, "note": note})
            self.written(self.rows)

    def call(self, case: str, season: str, phase: str, part: str, args: dict) -> int:
        """Run one call and record it; returns its exit code (an exception counts as a failed call)."""
        with self.lock:
            self.running += 1
            self.most_running = max(self.most_running, self.running)
            running = self.running
            memory = self._memory()
            free = self.free_bytes()
        start = _now()
        note = ""
        try:
            code = int(self.runner(season=season, phase=phase, **args))
        except Exception as error:  # noqa: BLE001 - a call that raises ends its case-season like a failed exit
            code, note = -1, f"{type(error).__name__}: {error}"
        end = _now()
        with self.lock:
            self.running -= 1
            self._memory()
            self.rows.append({"case": case, "season": season, "phase": phase, "part": part,
                              "start_utc": start.isoformat(timespec="milliseconds"),
                              "end_utc": end.isoformat(timespec="milliseconds"),
                              "seconds": (end - start).total_seconds(), "exit_code": code,
                              "status": OK if code == 0 else FAILED, "running_at_start": running,
                              "free_gb": free / BYTES_PER_GB,
                              "free_memory_gb": memory / BYTES_PER_GB if memory is not None else None, "note": note})
            self.written(self.rows)
        return code

    def run_phases(self, case: str, season: str, phases: list[str], part: str, args: dict) -> bool:
        """Run the phases of one case-season in order; a failure ends it and skips the rest."""
        if (case, season) in self.ended:
            self.skip(case, season, phases, part, self.ended[(case, season)])
            return False
        for index, phase in enumerate(phases):
            if self.call(case, season, phase, part, args) != 0:
                note = f"phase {phase} failed"
                with self.lock:
                    self.ended[(case, season)] = note
                self.skip(case, season, phases[index + 1:], part, note)
                return False
        return True


def run_batch(cases: list[tuple[str, dict]], phases: list[str], seasons: list[str], serial_through: str,
              parallel: int, batch: Batch, one_at_a_time: list[str] = ()) -> dict[str, tuple[datetime, datetime]]:
    """Run the batch of schedule C; ``cases`` holds (case id, keyword arguments of the runner).

    Returns the UTC start and end of each part.
    """
    serial, later = split_phases(phases, serial_through)
    unknown = sorted(set(one_at_a_time) - {case for case, _ in cases})
    if unknown:
        raise ValueError(f"cases to run one at a time are not in the batch: {', '.join(unknown)}")
    times = {}
    start = _now()
    for case, kwargs in cases:
        for season in seasons:
            if batch.free_bytes() < batch.min_free_bytes:
                note = "not started: free space under the limit"
                batch.ended[(case, season)] = note
                batch.skip(case, season, serial, SERIAL, note)
                continue
            batch.run_phases(case, season, serial, SERIAL, kwargs)
    times[SERIAL] = (start, _now())
    by_id = dict(cases)
    start = _now()
    for case in one_at_a_time:
        for season in seasons:
            batch.run_phases(case, season, later, ONE_AT_A_TIME, by_id[case])
    times[ONE_AT_A_TIME] = (start, _now())
    rest = [(case, kwargs) for case, kwargs in cases if case not in set(one_at_a_time)]

    def worker(item: tuple[str, dict]) -> None:
        case, kwargs = item
        for season in seasons:
            batch.run_phases(case, season, later, PARALLEL, kwargs)

    start = _now()
    with ThreadPoolExecutor(max_workers=max(parallel, 1)) as pool:
        list(pool.map(worker, rest))
    times[PARALLEL] = (start, _now())
    return times


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--name", required=True, help="name of the batch in the output file names, such as pass_1")
    parser.add_argument("--settings", type=Path, required=True, help="settings folder of the cases")
    parser.add_argument("--case", type=Path, action="append", required=True,
                        help="case file; the serial part and the parallel part take the cases in this order")
    parser.add_argument("--one-at-a-time", action="append", default=[],
                        help="case id whose later phases run alone, after the serial part")
    parser.add_argument("--log-dir", type=Path, required=True, help="folder of the per-call logs")
    parser.add_argument("--min-free-gb", type=float, required=True,
                        help="no case-season starts while the drive of the runs has less free")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    clone = coupling_module.read_local()
    repo, python = Path(clone["coupling_repo"]), Path(clone["coupling_python"])
    with (args.settings / "local.yaml").open(encoding="utf-8") as handle:
        runs_root = Path(yaml.safe_load(handle)["runs_root"])
    runs_root.mkdir(parents=True, exist_ok=True)
    phases, seasons = coupling_order(repo)
    logs = args.log_dir / args.name
    logs.mkdir(parents=True, exist_ok=True)
    out = paths.output / f"batch_{args.name}_{args.stamp}.csv"

    def written(rows: list[dict]) -> None:
        write_csv(pd.DataFrame(rows), out)

    def runner(season: str, phase: str, case: Path) -> int:
        log = logs / f"{case.stem}__{season}__{phase}.log"
        return coupling_module.run_case(case, args.settings, [season], repo=repo, python=python, log=log,
                                        phase=phase, check=False, quiet=True)

    batch = Batch(runner=runner, free_bytes=lambda: shutil.disk_usage(runs_root).free,
                  memory_bytes=free_memory_bytes, min_free_bytes=args.min_free_gb * BYTES_PER_GB, written=written)
    cases = [(path.stem, {"case": path}) for path in args.case]
    print(f"pin ok: {coupling_module.check_pin(repo)}")
    print(f"{len(cases)} cases, seasons {', '.join(seasons)}, {len(phases)} phases, serial through "
          f"{settings['runs']['serial_through_phase']}, {settings['runs']['parallel']} at once; logs in {logs}")
    times = run_batch(cases, phases, seasons, settings["runs"]["serial_through_phase"], settings["runs"]["parallel"],
                      batch, args.one_at_a_time)
    written(batch.rows)
    machine = [{"part": part, "start_utc": begin.isoformat(timespec="milliseconds"),
                "end_utc": end.isoformat(timespec="milliseconds"), "wall_seconds": (end - begin).total_seconds(),
                "logical_processors": os.cpu_count(),
                "lowest_free_memory_gb": batch.lowest_memory / BYTES_PER_GB if batch.lowest_memory else None,
                "most_calls_at_once": batch.most_running} for part, (begin, end) in times.items()]
    write_csv(pd.DataFrame(machine), paths.output / f"batch_{args.name}_{args.stamp}_machine.csv")
    table = pd.DataFrame(batch.rows)
    print(table.groupby(["part", "status"]).size().to_string())
    print(f"pin ok: {coupling_module.check_pin(repo)}")
    print(f"written: {out}")
    return 0 if (table["status"] == OK).all() else 1


if __name__ == "__main__":
    sys.exit(main())
