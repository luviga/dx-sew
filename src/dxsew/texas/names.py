"""Digit-only load shape names for the staged SMART-DS Texas substations (decision K55).

The Full Texas release names a building's load shape ``<type>_<qty>_<digits>-<zone>_pu``
(for example ``res_kw_9312-North_pu``) and its load-data parquet
``<type>_<digits>-<zone>.parquet``. DeltaE/dss-eulp-coupling takes the third
underscore field of the name as the building number and, in phase 7c, compares it
with the integer digits of the name, so a zone ending stops the run. While staging,
every load shape name becomes digits only: the released digits followed by the
two-digit code of its zone ending, from the tracked table
``configs/texas_zone_codes.csv``. ``res_kw_9312-North_pu`` becomes ``res_kw_931204_pu``
when ``North`` has code ``04``, and its parquet becomes ``res_931204.parquet``.

This module holds the name patterns, the zone code table, the name map, the
replacement of names as whole tokens in the ``.dss`` files, and the four checks
that the staging runs on every substation.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

ZONE = r"[A-Za-z]+(?:-[A-Za-z]+)*"
# Load shape name of the release, anchored at both ends.
LOADSHAPE = re.compile(rf"^(?P<type>res|com)_(?P<qty>kw|kvar)_(?P<digits>\d+)(?:-(?P<zone>{ZONE}))?_pu$")
# Load-data parquet name of the release, anchored at both ends.
PARQUET = re.compile(rf"^(?P<type>res|com)_(?P<digits>\d+)(?:-(?P<zone>{ZONE}))?\.parquet$")
# A token of a .dss file: a run of letters, digits, underscores and hyphens.
TOKEN_CHARS = rb"A-Za-z0-9_\-"
# A token that looks like a load shape name: it starts with res_ or com_, any case.
SHAPE_LIKE = re.compile(rb"(?<![" + TOKEN_CHARS + rb"])(?i:res|com)_[" + TOKEN_CHARS + rb"]*")
NO_ENDING_CODE = "00"
CODE_WIDTH = 2
ZONE_CODE_COLUMNS = ("zone_ending", "code")
MAP_COLUMNS = (
    "released_name",
    "new_name",
    "kind",
    "type",
    "qty",
    "number",
    "zone_ending",
    "code",
    "source",
    "sha256",
)


class NameFormError(ValueError):
    """A name that fits no listed form, or a zone ending with no code."""


@dataclass(frozen=True)
class ShapeName:
    released: str
    type: str
    qty: str
    digits: str
    zone: str  # empty when the name has no zone ending


def parse_loadshape(name: str) -> ShapeName:
    found = LOADSHAPE.match(name)
    if not found:
        raise NameFormError(f"{name!r} fits no load shape form <type>_<qty>_<digits>[-<zone>]_pu")
    return ShapeName(name, found["type"], found["qty"], found["digits"], found["zone"] or "")


def zone_ending(name: str) -> str | None:
    """Zone ending of a load shape or parquet name of the release, '' for none, None for no form."""
    found = LOADSHAPE.match(name) or PARQUET.match(name)
    return (found["zone"] or "") if found else None


# ---------------------------------------------------------------- zone codes


def endings_in_release(keys) -> list[str]:
    """Distinct zone endings of the load-data parquets and profiles in a listing, sorted.

    Every key whose file name fits the parquet form or the profile form
    (``<load shape name>.csv``) counts; names without an ending are left out.
    """
    endings: set[str] = set()
    for key in keys:
        base = str(key).rsplit("/", 1)[-1]
        stem = base[:-4] if base.endswith(".csv") else None
        found = PARQUET.match(base) or (LOADSHAPE.match(stem) if stem else None)
        if found and found["zone"]:
            endings.add(found["zone"])
    return sorted(endings)


def extend_zone_codes(table: dict[str, str], endings) -> dict[str, str]:
    """Zone code table with every ending of ``endings``.

    Codes already in ``table`` keep their meaning. A new ending takes the next
    free code, new endings in sorted order. The empty ending has code ``00``.
    Stops when more than 99 endings would need a code.
    """
    table = dict(table)
    table.setdefault("", NO_ENDING_CODE)
    if table[""] != NO_ENDING_CODE:
        raise NameFormError("the empty zone ending must keep code 00")
    used = {int(code) for code in table.values()}
    for ending in sorted(set(endings) - set(table)):
        if "_" in ending:
            raise NameFormError(f"zone ending {ending!r} holds an underscore")
        free = next((n for n in range(1, 10**CODE_WIDTH) if n not in used), None)
        if free is None:
            raise NameFormError(f"more than {10**CODE_WIDTH - 1} zone endings; no free two-digit code")
        table[ending] = f"{free:0{CODE_WIDTH}d}"
        used.add(free)
    return table


def read_zone_codes(path: Path) -> dict[str, str]:
    """``zone ending -> code`` of the tracked table; the empty ending is code 00."""
    with Path(path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    table = {row["zone_ending"]: row["code"] for row in rows}
    if len(table) != len(rows) or len(set(table.values())) != len(rows):
        raise NameFormError(f"{path}: a zone ending or a code appears twice")
    for ending, code in table.items():
        if not re.fullmatch(r"\d{%d}" % CODE_WIDTH, code):
            raise NameFormError(f"{path}: code {code!r} of {ending!r} is not two digits")
    if table.get("") != NO_ENDING_CODE:
        raise NameFormError(f"{path}: the empty zone ending must have code 00")
    return table


def write_zone_codes(table: dict[str, str], path: Path) -> None:
    """Write the table, rows ordered by code."""
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(ZONE_CODE_COLUMNS)
        for ending, code in sorted(table.items(), key=lambda item: item[1]):
            writer.writerow((ending, code))


# ---------------------------------------------------------------- new names


def new_number(digits: str, zone: str, codes: dict[str, str]) -> str:
    if zone not in codes:
        raise NameFormError(f"zone ending {zone!r} has no code in the zone code table")
    return f"{digits}{codes[zone]}"


def new_loadshape(shape: ShapeName, codes: dict[str, str]) -> str:
    return f"{shape.type}_{shape.qty}_{new_number(shape.digits, shape.zone, codes)}_pu"


def parquet_of(shape: ShapeName) -> str:
    """Released parquet of a load shape: ``res_kw_9312-North_pu`` gives ``res_9312-North.parquet``."""
    ending = f"-{shape.zone}" if shape.zone else ""
    return f"{shape.type}_{shape.digits}{ending}.parquet"


def new_parquet(shape: ShapeName, codes: dict[str, str]) -> str:
    """Parquet name the coupling builds from the new load shape name, ``<type>_<number>.parquet``."""
    return f"{shape.type}_{new_number(shape.digits, shape.zone, codes)}.parquet"


def shape_tokens(data: bytes) -> list[tuple[int, str]]:
    """``(line number, token)`` of every token of a file that looks like a load shape name."""
    found = []
    for match in SHAPE_LIKE.finditer(data):
        line = data.count(b"\n", 0, match.start()) + 1
        found.append((line, match.group().decode("ascii", errors="replace")))
    return found


def scan_folder(folder: Path) -> tuple[dict[str, ShapeName], set[str], list[str]]:
    """Load shape names of every ``.dss`` file below ``folder``.

    Returns the parsed names, the released parquets the ``Loads.dss`` files
    refer to (through ``yearly=``), and one problem line per token that looks
    like a load shape name but fits no form (``file:line: token``).
    """
    names: dict[str, ShapeName] = {}
    parquets: set[str] = set()
    problems: list[str] = []
    for path in dss_files(folder):
        data = path.read_bytes()
        loads_file = path.name.lower() == "loads.dss"
        for line, token in shape_tokens(data):
            try:
                shape = names.get(token) or parse_loadshape(token)
            except NameFormError:
                problems.append(f"{path.relative_to(folder).as_posix()}:{line}: {token}")
                continue
            names[token] = shape
        if loads_file:
            for value in re.findall(rb"(?i)\byearly\s*=\s*([" + TOKEN_CHARS + rb"]+)", data):
                token = value.decode("ascii", errors="replace")
                if token in names:
                    parquets.add(parquet_of(names[token]))
    return names, parquets, problems


def dss_files(folder: Path) -> list[Path]:
    return sorted(path for path in Path(folder).rglob("*") if path.is_file() and path.suffix.lower() == ".dss")


# ---------------------------------------------------------------- name map


def map_rows(shapes, codes: dict[str, str], profile_key, parquet_key, parquet_sha: dict[str, str]) -> list[dict]:
    """Rows of the name map for the load shapes given and the parquets of their ``kw`` shapes.

    ``profile_key(name)`` and ``parquet_key(name)`` give the OEDI key of the
    released profile CSV and parquet; ``parquet_sha`` the SHA-256 of each
    released parquet that is staged.
    """
    rows: dict[str, dict] = {}
    for shape in sorted(shapes, key=lambda s: s.released):
        code = codes[shape.zone] if shape.zone in codes else None
        if code is None:
            raise NameFormError(f"zone ending {shape.zone!r} of {shape.released} has no code")
        rows[shape.released] = {
            "released_name": shape.released,
            "new_name": new_loadshape(shape, codes),
            "kind": "loadshape",
            "type": shape.type,
            "qty": shape.qty,
            "number": shape.digits,
            "zone_ending": shape.zone,
            "code": code,
            "source": profile_key(shape.released),
            "sha256": "",
        }
        released_parquet = parquet_of(shape)
        if shape.qty == "kw" and released_parquet in parquet_sha:
            rows[released_parquet] = {
                "released_name": released_parquet,
                "new_name": new_parquet(shape, codes),
                "kind": "parquet",
                "type": shape.type,
                "qty": "",
                "number": shape.digits,
                "zone_ending": shape.zone,
                "code": code,
                "source": parquet_key(released_parquet),
                "sha256": parquet_sha[released_parquet],
            }
    return sorted(rows.values(), key=lambda row: (row["kind"], row["released_name"]))


def write_map(rows: list[dict], path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAP_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_map(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def loadshape_maps(rows: list[dict]) -> tuple[dict[bytes, bytes], dict[bytes, bytes]]:
    """Forward (released to new) and backward (new to released) maps of the load shape rows."""
    forward = {r["released_name"].encode(): r["new_name"].encode() for r in rows if r["kind"] == "loadshape"}
    backward = {new: old for old, new in forward.items()}
    return forward, backward


def replace_tokens(data: bytes, mapping: dict[bytes, bytes]) -> bytes:
    """Replace every whole token found in ``mapping``; nothing else changes."""
    return SHAPE_LIKE.sub(lambda match: mapping.get(match.group(), match.group()), data)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------- checks


def first_difference(a: bytes, b: bytes) -> int:
    """Line number (from 1) of the first line where two byte strings differ."""
    lines_a, lines_b = a.split(b"\n"), b.split(b"\n")
    for number, (x, y) in enumerate(zip(lines_a, lines_b), start=1):
        if x != y:
            return number
    return min(len(lines_a), len(lines_b)) + 1


def _files(folder: Path) -> dict[str, Path]:
    return {p.relative_to(folder).as_posix(): p for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def load_totals(data: bytes) -> tuple[int, float]:
    """Count of ``New Load`` lines and the sum of their ``kW``."""
    count, total = 0, 0.0
    for line in data.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "new" and parts[1].lower().startswith("load."):
            count += 1
            found = re.search(r"(?i)\bkw\s*=\s*([0-9.eE+-]+)", line)
            total += float(found.group(1)) if found else 0.0
    return count, total


def check_same_loads(released: Path, staged: Path) -> list[str]:
    """Check 1: per feeder, the count of ``New Load`` lines and the sum of their kW are equal."""
    failures = []
    staged_files = _files(staged)
    for rel, path in _files(released).items():
        if path.name.lower() != "loads.dss":
            continue
        if rel not in staged_files:
            failures.append(f"{rel}: missing in the staged folder")
            continue
        before = load_totals(path.read_bytes())
        after = load_totals(staged_files[rel].read_bytes())
        if before != after:
            failures.append(f"{rel}: released {before[0]} loads, {before[1]!r} kW; staged {after[0]} loads, {after[1]!r} kW")
    return failures


def check_clean(released: Path, staged: Path, rows: list[dict], parquet_folder: Path) -> list[str]:
    """Check 2: the forward map turns each released file into the staged file, byte for byte,
    and every staged parquet has the SHA-256 of its released file.

    Files other than ``.dss`` must be equal as they are.
    """
    forward, _ = loadshape_maps(rows)
    failures = []
    released_files, staged_files = _files(released), _files(staged)
    for rel in sorted(set(released_files) ^ set(staged_files)):
        failures.append(f"{rel}: in only one of the released and staged folders")
    for rel in sorted(set(released_files) & set(staged_files)):
        before = released_files[rel].read_bytes()
        expected = replace_tokens(before, forward) if rel.lower().endswith(".dss") else before
        got = staged_files[rel].read_bytes()
        if expected != got:
            failures.append(f"{rel}:{first_difference(expected, got)}: forward map does not give the staged file")
    for row in rows:
        if row["kind"] != "parquet":
            continue
        path = Path(parquet_folder) / row["new_name"]
        if not path.is_file():
            failures.append(f"{row['new_name']}: staged parquet missing")
        elif sha256_of(path) != row["sha256"]:
            failures.append(f"{row['new_name']}: SHA-256 differs from released {row['released_name']}")
    return failures


def check_nothing_lost(released: Path, staged: Path, rows: list[dict]) -> list[str]:
    """Check 3: the backward map turns each staged ``.dss`` file into the released file, byte for byte."""
    _, backward = loadshape_maps(rows)
    failures = []
    released_files = _files(released)
    for rel, path in _files(staged).items():
        if not rel.lower().endswith(".dss"):
            continue
        if rel not in released_files:
            failures.append(f"{rel}: not in the released folder")
            continue
        back = replace_tokens(path.read_bytes(), backward)
        original = released_files[rel].read_bytes()
        if back != original:
            failures.append(f"{rel}:{first_difference(back, original)}: backward map does not give the released file")
    return failures


def check_map(staged: Path, rows: list[dict], referenced_parquets: set[str]) -> list[str]:
    """Check 4: the map is one to one; every load shape and parquet name of the staged
    tree is in it; no name with a zone ending remains in a staged ``.dss`` file.

    ``referenced_parquets`` are the new parquet names the staged loads refer to.
    """
    failures = []
    for kind in ("loadshape", "parquet"):
        mine = [r for r in rows if r["kind"] == kind]
        for column in ("released_name", "new_name"):
            values = [r[column] for r in mine]
            if len(values) != len(set(values)):
                failures.append(f"name map: a {kind} {column} appears twice")
    new_shapes = {r["new_name"] for r in rows if r["kind"] == "loadshape"}
    new_parquets = {r["new_name"] for r in rows if r["kind"] == "parquet"}
    for path in dss_files(staged):
        rel = path.relative_to(staged).as_posix()
        for line, token in shape_tokens(path.read_bytes()):
            if zone_ending(token):
                failures.append(f"{rel}:{line}: {token} still has a zone ending")
            elif token not in new_shapes:
                failures.append(f"{rel}:{line}: {token} is not in the name map")
    for name in sorted(referenced_parquets - new_parquets):
        failures.append(f"{name}: referenced parquet not in the name map")
    return failures


def staged_parquets(staged: Path) -> set[str]:
    """Parquet names the coupling builds from the staged ``Loads.dss`` files, ``<type>_<number>.parquet``."""
    names = set()
    for path in dss_files(staged):
        if path.name.lower() != "loads.dss":
            continue
        for value in re.findall(rb"(?i)\byearly\s*=\s*([" + TOKEN_CHARS + rb"]+)", path.read_bytes()):
            parts = value.decode("ascii", errors="replace").split("_")
            if len(parts) >= 3:
                names.add(f"{parts[0]}_{parts[2]}.parquet")
    return names


def main(argv: list[str] | None = None) -> int:
    """Write or extend ``configs/texas_zone_codes.csv`` from the saved listing of the release.

    Run from the repository::

        uv run python -m dxsew.texas.names

    Codes already in the table keep their meaning; a new ending takes the next free code.
    """
    import argparse

    from .config import Paths, find_configs, load_local, load_sample_settings
    from .sample import get_listing

    parser = argparse.ArgumentParser(description="Write or extend the zone code table from the listing.")
    parser.parse_args(argv)
    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    listing = get_listing(settings, paths, refresh=False)
    path = find_configs() / "texas_zone_codes.csv"
    table = read_zone_codes(path) if path.is_file() else {}
    endings = endings_in_release(listing["key"])
    print(f"zone endings in the release: {len(endings)}: {', '.join(endings)}")
    print(f"endings holding an underscore: {[e for e in endings if '_' in e]}")
    extended = extend_zone_codes(table, endings)
    write_zone_codes(extended, path)
    for ending, code in sorted(extended.items(), key=lambda item: item[1]):
        print(f"{code} {ending or '(no ending)'}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
