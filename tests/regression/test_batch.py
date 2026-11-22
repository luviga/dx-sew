"""The batch runner of schedule C, with a fake run_case (no coupling)."""

import threading
import time

import pytest

from dxsew.runs import batch as batch_module

PHASES = ["pre", "1", "5a", "5b", "6", "8"]
SEASONS = ["summer", "winter"]


class Fake:
    """A run_case stand-in that records calls, how many run at once, and fails where told."""

    def __init__(self, fail=(), pause=0.01):
        self.calls, self.fail, self.pause = [], set(fail), pause
        self.now = self.most = 0
        self.lock = threading.Lock()
        self.overlaps = []

    def __call__(self, season, phase, case):
        with self.lock:
            self.now += 1
            self.most = max(self.most, self.now)
            self.overlaps.append((case, season, phase, self.now))
            self.calls.append((case, season, phase))
        time.sleep(self.pause)
        with self.lock:
            self.now -= 1
        return 1 if (case, season, phase) in self.fail else 0


def make(fake, free=1e12, minimum=0.0):
    return batch_module.Batch(runner=fake, free_bytes=lambda: free, memory_bytes=lambda: 1, min_free_bytes=minimum)


def cases(*names):
    return [(name, {"case": name}) for name in names]


def test_serial_part_runs_case_by_case_in_order_one_call_at_a_time():
    fake = Fake()
    batch = make(fake)
    batch_module.run_batch(cases("a", "b"), PHASES, SEASONS, "5a", 4, batch)
    serial = [call for call in fake.calls if call[2] in ("pre", "1", "5a")]
    assert serial == [(c, s, p) for c in ("a", "b") for s in SEASONS for p in ("pre", "1", "5a")]
    first_later = next(i for i, call in enumerate(fake.calls) if call[2] not in ("pre", "1", "5a"))
    assert all(call[2] in ("pre", "1", "5a") for call in fake.calls[:first_later])
    assert all(now == 1 for case, season, phase, now in fake.overlaps if phase in ("pre", "1", "5a"))
    assert {row["part"] for row in batch.rows if row["phase"] in ("pre", "1", "5a")} == {"serial"}


def test_parallel_part_respects_the_cap_and_keeps_each_case_season_in_order():
    fake = Fake(pause=0.03)
    batch = make(fake)
    names = [f"c{i}" for i in range(7)]
    batch_module.run_batch(cases(*names), PHASES, SEASONS, "5a", 3, batch)
    assert fake.most <= 3
    assert batch.most_running <= 3
    assert max(now for *_, now in fake.overlaps) >= 2
    for name in names:
        mine = [(s, p) for c, s, p in fake.calls if c == name and p not in ("pre", "1", "5a")]
        assert mine == [(s, p) for s in SEASONS for p in ("5b", "6", "8")]
    assert len(batch.rows) == len(names) * len(SEASONS) * len(PHASES)


def test_one_at_a_time_cases_run_alone_before_the_parallel_part():
    fake = Fake(pause=0.02)
    batch = make(fake)
    batch_module.run_batch(cases("a", "b", "c", "d"), PHASES, SEASONS, "5a", 4, batch, one_at_a_time=["c", "a"])
    later = [call for call in fake.calls if call[2] not in ("pre", "1", "5a")]
    assert [call[0] for call in later[:12]] == ["c"] * 6 + ["a"] * 6
    alone = [row for row in batch.rows if row["part"] == "one_at_a_time"]
    assert len(alone) == 12 and all(row["running_at_start"] == 1 for row in alone)


def test_a_failed_call_skips_the_rest_of_its_case_season_only():
    fake = Fake(fail={("a", "summer", "1"), ("b", "winter", "6")})
    batch = make(fake)
    batch_module.run_batch(cases("a", "b"), PHASES, SEASONS, "5a", 4, batch)
    assert ("a", "summer", "5a") not in fake.calls and ("a", "summer", "5b") not in fake.calls
    assert ("a", "winter", "8") in fake.calls
    assert ("b", "winter", "8") not in fake.calls and ("b", "summer", "8") in fake.calls
    skipped = {(r["case"], r["season"], r["phase"]) for r in batch.rows if r["status"] == "skipped"}
    assert skipped == {("a", "summer", p) for p in ("5a", "5b", "6", "8")} | {("b", "winter", "8")}
    failed = [r for r in batch.rows if r["status"] == "failed"]
    assert {(r["case"], r["season"], r["phase"]) for r in failed} == {("a", "summer", "1"), ("b", "winter", "6")}


def test_no_case_season_starts_under_the_free_space_limit():
    fake = Fake()
    batch = make(fake, free=10.0, minimum=100.0)
    batch_module.run_batch(cases("a"), PHASES, SEASONS, "5a", 4, batch)
    assert fake.calls == []
    assert all(row["status"] == "skipped" for row in batch.rows) and len(batch.rows) == len(SEASONS) * len(PHASES)


def test_split_phases_and_unknown_cases():
    assert batch_module.split_phases(PHASES, "5a") == (["pre", "1", "5a"], ["5b", "6", "8"])
    with pytest.raises(ValueError):
        batch_module.split_phases(PHASES, "9")
    with pytest.raises(ValueError):
        batch_module.run_batch(cases("a"), PHASES, SEASONS, "5a", 4, make(Fake()), one_at_a_time=["z"])


def test_coupling_order_reads_the_clone(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "defaults.yaml").write_text(
        'seasons:\n  - summer\n  - winter\nphase_order: [pre, "1", "5a", mix_days]\n', encoding="utf-8")
    assert batch_module.coupling_order(tmp_path) == (["pre", "1", "5a", "mix_days"], ["summer", "winter"])
