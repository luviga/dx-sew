"""No data in code: every value of the package lives in config/ or inputs/ (decision K72).

The test walks every ``.py`` file under ``dxsew/`` with ``ast`` and fails, naming
file and line, on:

- an int or float literal other than 0, 1 and -1, except the right operand of ``**``;
- a string literal or f-string part outside a docstring that holds ``ASSUMPTION`` in
  any case, a year 1900 to 2099 as a whole word, a token ending in ``.xlsx``,
  ``.xls``, ``.zip``, ``.shp``, ``.dbf`` or ``.tsv``, or as a whole word any term of
  ``scan.forbidden_terms`` in ``config/scan.yaml``;
- a docstring that holds ``ASSUMPTION``.

A whole word is bounded by characters other than letters and digits, so ``_2018``
and ``ercot_peak`` count. There is no exemption per line. Tests are not scanned.

Run on another tree, for the counts per rule: ``python tests/regression/test_no_data_in_code.py <folder>``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "dxsew"
WORD = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"
YEAR = re.compile(WORD.format(r"(?:19|20)\d\d"))
DATA_FILE = re.compile(r"\.(?:xlsx|xls|zip|shp|dbf|tsv)(?![A-Za-z0-9])", re.IGNORECASE)
MARKER = "assumption"
ALLOWED_NUMBERS = (0, 1, -1)


def forbidden_terms(root: Path = ROOT) -> list[str]:
    with (root / "config" / "scan.yaml").open(encoding="utf-8") as handle:
        return list(yaml.safe_load(handle)["scan"]["forbidden_terms"])


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _exponents(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        right = None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            right = node.right
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Pow):
            right = node.value
        if isinstance(right, ast.UnaryOp):
            right = right.operand
        if right is not None:
            found.add(id(right))
    return found


def scan_source(text: str, terms: list[str]) -> list[tuple[int, str, str]]:
    """``(line, rule, text)`` of every finding in one source file."""
    tree = ast.parse(text)
    docstrings, exponents = _docstrings(tree), _exponents(tree)
    patterns = [(term, re.compile(WORD.format(re.escape(term)), re.IGNORECASE)) for term in terms]
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool) or node.value is None:
            continue
        value = node.value
        if isinstance(value, (int, float, complex)):
            if value not in ALLOWED_NUMBERS and id(node) not in exponents:
                findings.append((node.lineno, "number", repr(value)))
            continue
        if not isinstance(value, str):
            continue
        if id(node) in docstrings:
            if MARKER in value.lower():
                findings.append((node.lineno, "docstring_assumption", value.strip().splitlines()[0]))
            continue
        if MARKER in value.lower():
            findings.append((node.lineno, "assumption", value))
        if YEAR.search(value):
            findings.append((node.lineno, "year", value))
        if DATA_FILE.search(value):
            findings.append((node.lineno, "data_file", value))
        findings += [(node.lineno, "term", f"{term}: {value}") for term, pattern in patterns if pattern.search(value)]
    return sorted(findings)


def scan_tree(folder: Path, terms: list[str]) -> list[str]:
    """``<file>:<line>: <rule>: <text>`` of every finding under ``folder``."""
    lines = []
    for path in sorted(Path(folder).rglob("*.py")):
        for line, rule, text in scan_source(path.read_text(encoding="utf-8"), terms):
            lines.append(f"{path.relative_to(folder).as_posix()}:{line}: {rule}: {text[:100]!r}")
    return lines


def test_no_data_in_any_module():
    findings = scan_tree(PACKAGE, forbidden_terms())
    assert not findings, f"{len(findings)} values in code:\n" + "\n".join(findings)


def test_no_assumption_in_comments():
    lines = [f"{path.relative_to(ROOT).as_posix()}:{number}" for path in sorted(PACKAGE.rglob("*.py"))
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
             if "#" in line and MARKER in line.split("#", 1)[1].lower()]
    assert not lines, "ASSUMPTION in comments: " + ", ".join(lines)


def test_scan_terms_hold_every_area_name():
    with (ROOT / "config" / "areas.yaml").open(encoding="utf-8") as handle:
        areas = yaml.safe_load(handle)["areas"]
    names = {area[key] for area in areas for key in ("name", "rto", "ba_code", "ba_prefix")}
    names |= {state for area in areas for state in area["states"]}
    assert names <= set(forbidden_terms())


def test_scan_finds_each_rule():
    source = '''"""Doc with ASSUMPTION."""
X = 2 + 3 ** 2 + 2.5
Y = "ASSUMPTION: no"
Z = f"file_2018 {X}.xlsx"
W = "ercot_peak"
V = -1 + 0 + 1 + 4 ** -2
'''
    rules = Counter(rule for _, rule, _ in scan_source(source, ["ERCOT"]))
    assert rules == {"docstring_assumption": 1, "number": 4, "assumption": 1, "year": 1, "data_file": 1, "term": 1}


if __name__ == "__main__":
    found = scan_tree(Path(sys.argv[1]), forbidden_terms())
    print("\n".join(found))
    counts = Counter(line.split(": ", 2)[1] for line in found)
    print(dict(sorted(counts.items())), "total", len(found))
