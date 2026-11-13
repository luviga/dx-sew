"""Frame veto from EIA-861 for any balancing area (decisions K57 and K58).

The frame of a balancing area is first drawn by location on a balancing area
map. This module holds the second step: a county is vetoed, and its
substations leave the frame, when the utilities serving it say on EIA-861
that their customers belong to another area. The procedure takes three names
per area as settings: ``rto`` (column of the RTO group of the utility data
file), ``ba_code`` (balancing authority code on the sales rows) and the
polygon prefix of the map (used by the frame and by the report CLI below).

Source: U.S. Energy Information Administration, Form EIA-861 detailed data
files, https://www.eia.gov/electricity/data/eia861/ : the service territory
file (utilities per county), the utility data file (the ISOs and RTOs each
utility operates in, one row per utility and state), the sales to ultimate
customers file and the short form file (balancing authority code of each
utility's rows).

Rule (K57): a county is vetoed when every utility serving it has an answer,
every one marks at least one RTO, none marks ``rto``, and every one marks at
least one RTO that is neither ``rto`` nor an ignored mark (for example
``Other``). The veto only removes, never adds; an unknown answer, an ignored
mark alone, or a county where some utilities mark ``rto`` never removes.

Lookups (K58), inside the same EIA-861 files, for a utility with no answer in
the state of the county:

1. A utility with no utility data row in the state takes the union of the
   marks of its rows in other states.
2. A utility that still has no mark takes the balancing authority codes of
   its own sales rows, in the state first and in any state if it has none
   there. A code equal to ``ba_code`` counts as marking ``rto``; any other
   code counts as another area, written ``BA:<code>``, whether or not that
   area is an RTO.

A utility the lookups do not resolve stays unknown and blocks the veto, as
in K57. With the lookups off the veto is K57 exactly.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from dxsew.sources.eia861 import read_sales

# Reasons of the veto table (decision K57), one per county.
VETOED = "other_rto_only"
REPORTS_RTO, MISSING_ROW, NO_RTO_MARKED, IGNORED_MARK_ONLY = (
    "reports_rto",
    "missing_row",
    "no_rto_marked",
    "ignored_mark_only",
)

# Sources of one utility's answer (decision K58).
ROW, OTHER_STATE_ROW, SALES_BA_CODE, UNKNOWN = "row", "other_state_row", "sales_ba_code", "unknown"
BA_MARK = "BA:"  # prefix of a mark read from a balancing authority code of the sales rows

# Short form file of EIA-861 and its sheet, read beside the sales file for
# the balancing authority code of the utilities that file the short form.
SHORT_FORM_FILE, SHORT_FORM_SHEET = "Short_Form_{year}.xlsx", "861S"


# Readers of the EIA-861 sheets.


def _sheet(zip_path: Path, member: str, sheet: str, header) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        return pd.read_excel(io.BytesIO(archive.read(member)), sheet_name=sheet, header=header)


def name_key(name: str) -> str:
    """County name reduced to its letters, lower case, for joining two sources."""
    return re.sub(r"[^a-z]", "", name.lower())


def territory_rows(zip_path: Path, source: dict) -> pd.DataFrame:
    """Rows of the service territory sheet of EIA-861 for the state of the settings.

    One row per utility and county (columns ``Utility Number``, ``Utility
    Name``, ``State``, ``County`` among others).
    """
    territory = _sheet(zip_path, source["territory_file"], source["territory_sheet"], 0)
    return territory[territory["State"] == source["state"]]


def _utility_rows(zip_path: Path, source: dict) -> list[tuple[int, str, frozenset]]:
    """``(utility number, state, RTO marks)`` of every row of the utility data sheet.

    The sheet has two header rows: a group row whose group
    ``source["rto_group"]`` ("Operating in these RTOs") spans one column per
    ISO or RTO, and a row of column names below it. A row marks an RTO when
    its cell holds ``Y``; ``N`` and a blank cell both mean it does not.
    """
    raw = _sheet(zip_path, source["utility_file"], source["utility_sheet"], None)
    header = source["utility_header_row"]
    group = raw.iloc[header - 1].ffill().astype(str).str.strip()
    names = raw.iloc[header].astype(str).str.strip()
    number_at = names[names == "Utility Number"].index[0]
    state_at = names[names == "State"].index[0]
    rto_at = [i for i in raw.columns if group[i] == source["rto_group"]]
    if not rto_at:
        raise ValueError(f"No column group {source['rto_group']!r} in {source['utility_file']}.")
    rows = []
    for _, row in raw.iloc[header + 1 :].iterrows():
        number = pd.to_numeric(row[number_at], errors="coerce")
        if pd.isna(number):
            continue  # a note row, not a utility
        found = frozenset(names[i] for i in rto_at if str(row[i]).strip().upper() == "Y")
        rows.append((int(number), str(row[state_at]).strip(), found))
    return rows


def utility_rtos(zip_path: Path, source: dict) -> pd.Series:
    """ISOs and RTOs each utility of the state reports operating in, from EIA-861.

    Rows are kept by state (``source["state"]``); a utility with several rows
    in the state operates in every RTO any of its rows reports. Returns
    ``utility number -> frozenset of RTO names``, one entry per utility with
    a row in the state.
    """
    rtos: dict[int, set[str]] = {}
    for number, state, found in _utility_rows(zip_path, source):
        if state == source["state"]:
            rtos.setdefault(number, set()).update(found)
    return pd.Series({number: frozenset(found) for number, found in rtos.items()}, dtype=object)


def utility_marks_all_states(zip_path: Path, source: dict) -> dict[int, dict[str, frozenset]]:
    """RTO marks of every utility in every state of the utility data sheet.

    Returns ``utility number -> state -> frozenset of RTO names``; several
    rows of one utility in one state are joined.
    """
    marks: dict[int, dict[str, frozenset]] = {}
    for number, state, found in _utility_rows(zip_path, source):
        by_state = marks.setdefault(number, {})
        by_state[state] = by_state.get(state, frozenset()) | found
    return marks


def _code(value) -> str | None:
    text = "" if pd.isna(value) else str(value).strip()
    return text if text and text != "." else None


def short_form_rows(zip_path: Path, year: int) -> pd.DataFrame | None:
    """``utility_number``, ``state``, ``ba_code`` of the short form file, or ``None`` when absent.

    ``None`` also when the file has no ``BA Code`` column.
    """
    member = SHORT_FORM_FILE.format(year=year)
    with zipfile.ZipFile(zip_path) as archive:
        if member not in archive.namelist():
            return None
    raw = _sheet(zip_path, member, SHORT_FORM_SHEET, 0)
    if "BA Code" not in raw:
        return None
    rows = raw.rename(columns={"Utility Number": "utility_number", "State": "state", "BA Code": "ba_code"})
    rows = rows[pd.to_numeric(rows["Data Year"], errors="coerce") == year]
    return rows[["utility_number", "state", "ba_code"]].reset_index(drop=True)


def sales_ba_codes(zip_path: Path, year: int) -> dict[int, dict[str, frozenset]]:
    """Balancing authority codes of every utility's sales rows, by state.

    Reads ``Sales_Ult_Cust_<year>.xlsx`` (``dxsew.sources.eia861.read_sales``)
    with every service type kept, and the short form file of the same year
    when it is in the zip with a ``BA Code`` column. Empty codes are dropped.
    Returns ``utility number -> state -> frozenset of codes``.
    """
    parts = [read_sales(zip_path, year)[["utility_number", "state", "ba_code"]]]
    short = short_form_rows(zip_path, year)
    if short is not None:
        parts.append(short)
    codes: dict[int, dict[str, set]] = {}
    for number, state, code in pd.concat(parts, ignore_index=True).itertuples(index=False):
        code = _code(code)
        if code is None or pd.isna(number):
            continue
        codes.setdefault(int(number), {}).setdefault(str(state).strip(), set()).add(code)
    return {n: {s: frozenset(c) for s, c in by_state.items()} for n, by_state in codes.items()}


# The rule and the lookups.


def veto_reason(marks: list[frozenset | None], rto: str, ignore) -> str:
    """Veto reason of one county from the RTO marks of the utilities serving it.

    ``marks`` holds one entry per utility: its RTO marks, or ``None`` when it
    has no utility data row in the state. The county is vetoed
    (``other_rto_only``) when every utility has a row, every one marks at
    least one RTO, none marks ``rto``, and every one marks at least one RTO
    that is neither ``rto`` nor in ``ignore``. Otherwise the first reason that
    applies: ``reports_rto``, ``missing_row``, ``no_rto_marked``,
    ``ignored_mark_only``. A mark ``BA:<code>`` from the lookups is a mark of
    another area like any RTO name other than ``rto``.
    """
    ignore = set(ignore)
    known = [found for found in marks if found is not None]
    if any(rto in found for found in known):
        return REPORTS_RTO
    if len(known) < len(marks) or not marks:
        return MISSING_ROW
    if any(not found for found in known):
        return NO_RTO_MARKED
    if any(not (found - ignore - {rto}) for found in known):
        return IGNORED_MARK_ONLY
    return VETOED


def veto_table(territory: pd.DataFrame, rtos: pd.Series, rto: str, ignore) -> pd.DataFrame:
    """Veto of every county of the territory rows (decision K57).

    ``territory`` holds the service territory rows of the state
    (``territory_rows``) and ``rtos`` the RTO marks of each utility
    (``utility_rtos``). Returns one row per county: ``county_key``,
    ``county_eia``, ``vetoed``, ``reason`` (``veto_reason``), ``utilities``
    (count), ``utilities_with_row`` and ``utility_marks``, the utilities as
    ``number name [marks]`` joined by ``; `` with ``no utility data row`` or
    ``no RTO marked`` where that applies.
    """
    names = territory.drop_duplicates("Utility Number").set_index("Utility Number")["Utility Name"]
    rows = []
    for county, group in territory.groupby("County"):
        utilities = sorted(set(group["Utility Number"]))
        marks = [rtos[u] if u in rtos.index else None for u in utilities]
        reason = veto_reason(marks, rto, ignore)
        described = []
        for number, found in zip(utilities, marks):
            if found is None:
                text = "no utility data row"
            elif not found:
                text = "no RTO marked"
            else:
                text = ", ".join(sorted(found))
            described.append(f"{number} {names[number]} [{text}]")
        rows.append(
            {
                "county_key": name_key(county),
                "county_eia": county,
                "vetoed": reason == VETOED,
                "reason": reason,
                "utilities": len(utilities),
                "utilities_with_row": sum(found is not None for found in marks),
                "utility_marks": "; ".join(described),
            }
        )
    columns = ["county_key", "county_eia", "vetoed", "reason", "utilities", "utilities_with_row", "utility_marks"]
    return pd.DataFrame(rows, columns=columns)


@dataclass(frozen=True)
class Answer:
    """One utility's answer for the veto: its marks, where they came from, and what was read."""

    number: int
    marks: frozenset | None  # None: no row anywhere and nothing found
    source: str  # row, other_state_row:<states>, sales_ba_code:<codes> or unknown
    lookup: str  # "" (none needed), lookup_1, lookup_2 or unknown
    in_state: frozenset | None  # marks of its row in the state, None without a row
    other_states: dict  # state -> marks of its rows in other states
    sales_codes: frozenset  # codes of its sales rows that lookup 2 read
    sales_states: str  # "state" (codes in the state), "any" (codes elsewhere only) or ""


def resolve_marks(utilities, state, rto, ba_code, in_state, all_states, sales, lookups) -> list[Answer]:
    """One answer per utility of a county, with its source (decision K58).

    ``in_state`` maps utility number to the marks of its rows in ``state``
    (``utility_rtos``); ``all_states`` maps utility number to state to marks
    (``utility_marks_all_states``); ``sales`` maps utility number to state to
    balancing authority codes (``sales_ba_codes``). With ``lookups`` off the
    answer is K57 exactly: the marks of the row in ``state``, or ``None``.
    With ``lookups`` on: a row in ``state`` that marks at least one RTO keeps
    its marks; lookup 1 gives a utility with no row in ``state`` the union of
    the marks of its rows in other states; lookup 2 gives a utility that
    still has no mark ``rto`` when a code of its sales rows (in ``state``
    first, in any state when it has none there) equals ``ba_code``, and
    ``BA:<code>`` for each code otherwise; else the answer stays unknown:
    ``None`` with no row anywhere, the empty set with a row and no mark.
    """
    in_state = in_state if isinstance(in_state, dict) else dict(in_state.items())
    answers = []
    for number in utilities:
        own = in_state.get(number)
        others = {s: m for s, m in sorted(all_states.get(number, {}).items()) if s != state}
        if not lookups:
            answers.append(Answer(number, own, ROW if own is not None else UNKNOWN, "", own, others, frozenset(), ""))
            continue
        if own:
            answers.append(Answer(number, own, ROW, "", own, others, frozenset(), ""))
            continue
        if own is None and others:
            union = frozenset().union(*others.values())
            if union:
                marked = ",".join(s for s, m in others.items() if m)
                answers.append(Answer(number, union, f"{OTHER_STATE_ROW}:{marked}", "lookup_1", own, others, frozenset(), ""))
                continue
        by_state = sales.get(number, {})
        codes, where = by_state.get(state, frozenset()), "state"
        if not codes:
            codes = frozenset().union(*by_state.values()) if by_state else frozenset()
            where = "any" if codes else ""
        if codes:
            marks = frozenset({rto}) if ba_code in codes else frozenset(BA_MARK + c for c in codes)
            source = f"{SALES_BA_CODE}:{','.join(sorted(codes))}"
            answers.append(Answer(number, marks, source, "lookup_2", own, others, codes, where))
            continue
        has_row = own is not None or bool(others)
        answers.append(Answer(number, frozenset() if has_row else None, UNKNOWN, UNKNOWN, own, others, frozenset(), ""))
    return answers


def describe(answer: Answer, ba_code: str) -> str:
    """Text of one answer in ``utility_marks``: the marks and, for a looked-up answer, its source."""
    after = ", nothing found by the lookups" if answer.lookup == UNKNOWN else ""
    if answer.marks is None:
        return "no utility data row" + after
    if answer.lookup == UNKNOWN:
        return "no RTO marked" + after
    if not answer.marks:
        return "no RTO marked"
    if answer.lookup == "lookup_1":
        states = answer.source.split(":", 1)[1].replace(",", ", ")
        return f"{', '.join(sorted(answer.marks))} via {states} row"
    if answer.lookup == "lookup_2":
        where = "" if answer.sales_states == "state" else ", other states"
        if any(m.startswith(BA_MARK) for m in answer.marks):
            codes = ", ".join("BA " + m[len(BA_MARK):] for m in sorted(answer.marks))
            return f"{codes} via sales rows{where}"
        return f"{', '.join(sorted(answer.marks))} via sales rows{where}, BA {ba_code}"
    return ", ".join(sorted(answer.marks))


@dataclass(frozen=True)
class Inputs:
    """The EIA-861 tables the veto of one state reads."""

    territory: pd.DataFrame
    in_state: pd.Series
    all_states: dict
    sales: dict


def read_inputs(zip_path: Path, source: dict, sales_year: int | None, lookups: bool) -> Inputs:
    """Territory rows, marks in the state and, with ``lookups`` on, marks in every state and sales codes."""
    territory = territory_rows(zip_path, source)
    in_state = utility_rtos(zip_path, source)
    if not lookups:
        return Inputs(territory, in_state, {}, {})
    if sales_year is None:
        raise ValueError("The veto lookups need the year of the EIA-861 sales file.")
    return Inputs(territory, in_state, utility_marks_all_states(zip_path, source), sales_ba_codes(zip_path, sales_year))


def lookup_settings(source: dict) -> tuple[bool, str | None]:
    """``veto_lookups`` (default false) and ``ba_code`` (required when the lookups are on)."""
    lookups = bool(source.get("veto_lookups", False))
    ba_code = source.get("ba_code")
    if lookups and not ba_code:
        raise KeyError(
            "veto_lookups is on but ba_code is missing: give the balancing authority code of the "
            "area on the EIA-861 sales rows (for example ERCO, CISO, NYIS)."
        )
    return lookups, ba_code


def veto_with_lookups(inputs: Inputs, state: str, rto: str, ba_code: str | None, ignore, lookups: bool) -> pd.DataFrame:
    """Veto of every county of ``inputs.territory`` with and without the lookups.

    Columns: the K57 columns of ``veto_table`` (``reason``, ``vetoed`` and
    ``utility_marks`` follow ``lookups``; ``utilities_with_row`` counts rows
    in the state), then ``reason_k57`` and ``vetoed_k57`` (lookups off).
    """
    territory = inputs.territory
    names = territory.drop_duplicates("Utility Number").set_index("Utility Number")["Utility Name"]
    rows = []
    for county, group in territory.groupby("County"):
        utilities = sorted(set(group["Utility Number"]))
        k57 = resolve_marks(utilities, state, rto, ba_code, inputs.in_state, inputs.all_states, inputs.sales, False)
        used = resolve_marks(utilities, state, rto, ba_code, inputs.in_state, inputs.all_states, inputs.sales, lookups)
        reason_k57 = veto_reason([a.marks for a in k57], rto, ignore)
        reason = veto_reason([a.marks for a in used], rto, ignore)
        described = [f"{a.number} {names[a.number]} [{describe(a, ba_code)}]" for a in used]
        rows.append(
            {
                "county_key": name_key(county),
                "county_eia": county,
                "vetoed": reason == VETOED,
                "reason": reason,
                "utilities": len(utilities),
                "utilities_with_row": sum(a.in_state is not None for a in used),
                "utility_marks": "; ".join(described),
                "reason_k57": reason_k57,
                "vetoed_k57": reason_k57 == VETOED,
            }
        )
    columns = [
        "county_key", "county_eia", "vetoed", "reason", "utilities", "utilities_with_row",
        "utility_marks", "reason_k57", "vetoed_k57",
    ]
    return pd.DataFrame(rows, columns=columns)


def county_veto(zip_path: Path, source: dict, sales_year: int | None = None) -> pd.DataFrame:
    """EIA-861 veto of every county of the state's service territory sheet (decisions K57, K58).

    Settings in ``source``: ``state``, ``rto``, ``veto_ignore_marks``,
    ``veto_lookups`` (default false), ``ba_code`` (required when the lookups
    are on) and the file and sheet names. ``sales_year`` is the year of the
    sales file the lookups read. See ``veto_with_lookups`` for the columns.
    """
    lookups, ba_code = lookup_settings(source)
    inputs = read_inputs(zip_path, source, sales_year, lookups)
    return veto_with_lookups(inputs, source["state"], source["rto"], ba_code, source.get("veto_ignore_marks", []), lookups)


LOOKUP_COLUMNS = [
    "utility_number", "utility_name", "lookup", "row_in_state", "other_state_rows",
    "sales_codes", "sales_codes_from", "answer", "source", "counties", "county_list",
]


def _marks_text(marks) -> str:
    return ", ".join(sorted(marks)) if marks else "none"


def lookup_rows(inputs: Inputs, state: str, rto: str, ba_code: str | None) -> pd.DataFrame:
    """One row per utility of the state that a lookup touched (decision K58).

    A utility is touched when it has no row in the state, or a row that
    marks no RTO. Columns: number, name, ``lookup`` (``lookup_1``,
    ``lookup_2`` or ``unknown``), ``row_in_state`` (its marks, ``none`` or
    ``no row``), ``other_state_rows`` (``state: marks`` per state),
    ``sales_codes`` and ``sales_codes_from`` (``state`` or ``any``) read by
    lookup 2, the ``answer`` and its ``source``, and the counties it serves.
    """
    territory = inputs.territory
    names = territory.drop_duplicates("Utility Number").set_index("Utility Number")["Utility Name"]
    counties = territory.groupby("Utility Number")["County"].agg(lambda c: sorted(set(c)))
    answers = resolve_marks(sorted(names.index), state, rto, ba_code, inputs.in_state, inputs.all_states, inputs.sales, True)
    rows = []
    for answer in answers:
        if answer.lookup == "":
            continue
        served = counties[answer.number]
        rows.append(
            {
                "utility_number": answer.number,
                "utility_name": names[answer.number],
                "lookup": answer.lookup,
                "row_in_state": "no row" if answer.in_state is None else _marks_text(answer.in_state),
                "other_state_rows": "; ".join(f"{s}: {_marks_text(m)}" for s, m in answer.other_states.items()),
                "sales_codes": ", ".join(sorted(answer.sales_codes)),
                "sales_codes_from": answer.sales_states,
                "answer": "unknown" if answer.lookup == UNKNOWN else describe(answer, ba_code),
                "source": answer.source,
                "counties": len(served),
                "county_list": "; ".join(served),
            }
        )
    return pd.DataFrame(rows, columns=LOOKUP_COLUMNS)


def lookup_table(zip_path: Path, source: dict, sales_year: int) -> pd.DataFrame:
    """The list of every looked-up utility of the state of ``source`` (see ``lookup_rows``).

    Written before anything is applied; empty (with its columns) when
    ``veto_lookups`` is off.
    """
    lookups, ba_code = lookup_settings(source)
    if not lookups:
        return pd.DataFrame(columns=LOOKUP_COLUMNS)
    inputs = read_inputs(zip_path, source, sales_year, True)
    return lookup_rows(inputs, source["state"], source["rto"], ba_code)
