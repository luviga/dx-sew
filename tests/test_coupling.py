"""The coupling pin, on a small temporary git repository."""

import subprocess
import sys

import pytest

from dxsew import coupling


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "coupling"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "core.autocrlf", "false")
    (path / "run_case.py").write_text(
        "import sys\nprint('case', sys.argv[sys.argv.index('--case') + 1].endswith('case.yaml'))\n"
        "print('season', sys.argv[sys.argv.index('--season') + 1] if '--season' in sys.argv else 'both')\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "first")
    return path


def pin_of(repo):
    return {"repository": "https://example.org/coupling", "commit": git(repo, "rev-parse", "HEAD")}


def test_clean_repository_at_the_pin_passes(repo):
    pin = pin_of(repo)
    assert coupling.check_pin(repo, pin) == pin["commit"]


def test_another_commit_stops(repo):
    pin = pin_of(repo)
    (repo / "extra.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "extra.py")
    git(repo, "commit", "-q", "-m", "second")
    with pytest.raises(coupling.PinError, match="not at the pinned commit"):
        coupling.check_pin(repo, pin)


def test_an_untracked_file_stops(repo):
    pin = pin_of(repo)
    (repo / "notes.txt").write_text("local\n", encoding="utf-8")
    with pytest.raises(coupling.PinError, match="local changes"):
        coupling.check_pin(repo, pin)


def test_a_modified_file_stops(repo):
    pin = pin_of(repo)
    (repo / "run_case.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(coupling.PinError, match="local changes"):
        coupling.check_pin(repo, pin)


def test_run_case_checks_before_and_after(repo, tmp_path, capsys):
    pin = pin_of(repo)
    (tmp_path / "case.yaml").write_text("case_id: x\n", encoding="utf-8")
    log = tmp_path / "run.log"
    code = coupling.run_case(tmp_path / "case.yaml", tmp_path, ["winter"], repo=repo, python=sys.executable,
                             log=log, pin=pin)
    assert code == 0
    out = capsys.readouterr().out
    assert "case True" in out and "season winter" in out
    assert log.read_text(encoding="utf-8").splitlines()[0].endswith(" case True")
    (repo / "left_by_a_run.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(coupling.PinError):
        coupling.run_case(tmp_path / "case.yaml", tmp_path, repo=repo, python=sys.executable, pin=pin)


def test_command_runs_all_phases_with_settings(tmp_path):
    cmd = coupling.command(tmp_path / "c.yaml", tmp_path / "s", "py")
    assert cmd[:3] == ["py", "run_case.py", "--case"] and "--all" in cmd and "--season" not in cmd
    assert coupling.command(tmp_path / "c.yaml", tmp_path / "s", "py", ["summer"])[-2:] == ["--season", "summer"]
    with pytest.raises(ValueError):
        coupling.command(tmp_path / "c.yaml", tmp_path / "s", "py", ["spring"])


def test_pin_file_names_repository_and_commit(tmp_path):
    path = tmp_path / "pin.yaml"
    path.write_text("repository: https://example.org/c\ncommit: ABC123\n", encoding="utf-8")
    assert coupling.read_pin(path) == {"repository": "https://example.org/c", "commit": "abc123"}
    path.write_text("repository: https://example.org/c\n", encoding="utf-8")
    with pytest.raises(coupling.PinError):
        coupling.read_pin(path)


def test_run_case_output_is_unbuffered(repo, tmp_path, capsys):
    (repo / "run_case.py").write_text("import os\nprint('unbuffered', os.environ.get('PYTHONUNBUFFERED'))\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "env")
    (tmp_path / "case.yaml").write_text("case_id: x\n", encoding="utf-8")
    assert coupling.run_case(tmp_path / "case.yaml", tmp_path, repo=repo, python=sys.executable, pin=pin_of(repo)) == 0
    assert "unbuffered 1" in capsys.readouterr().out
