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

Report-only CLI, for one area of ``configs/veto_areas.yaml`` and with no
SMART-DS data::

    uv run python -m dxsew.veto --areas configs/veto_areas.yaml --area CAISO --stamp <stamp>
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import shapefile
import yaml
from shapely import make_valid, unary_union
from shapely.geometry import shape

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


# Report-only run for one area (no SMART-DS data).

# State FIPS codes by postal code, the field STATEFP of the TIGER/Line county
# file. Source: U.S. Census Bureau, "State FIPS codes",
# https://www2.census.gov/geo/docs/reference/state.txt
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
    "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}
# ASSUMPTION, for this report only: a county is "in the map area" when at
# least half of its area lies in polygons whose name starts with ba_prefix.
# The value is not taken from a source; it only sorts counties into classes.
SHARE_THRESHOLD = 0.5
REPORT_COLUMNS = [
    "state", "county_eia", "county", "county_geoid", "share_in_area", "reason_k57", "vetoed_k57",
    "reason", "vetoed", "utility_marks", "class",
]


def read_shapes(shp_path: Path, key: str, keep=lambda record: True) -> list[tuple[dict, object]]:
    """``(record, geometry)`` of the shapefile records that ``keep`` accepts; ``key`` must be a field."""
    found = []
    with shapefile.Reader(str(shp_path)) as reader:
        names = [field[0] for field in reader.fields[1:]]
        if key not in names:
            raise ValueError(f"No field {key!r} in {shp_path}.")
        for index, values in enumerate(reader.iterRecords()):
            record = dict(zip(names, values))
            if keep(record):
                found.append((record, shape(reader.shape(index).__geo_interface__)))
    return found


def area_shares(counties: list[tuple[dict, object]], polygons: list) -> list[float]:
    """Share of each county's area that lies inside the union of ``polygons``.

    ASSUMPTION: shares are computed on longitude and latitude degrees, not on
    an equal-area projection. Within one county the two differ by a
    negligible amount, since the scale of a degree of longitude changes
    little across a county.
    """
    union = unary_union([make_valid(p) for p in polygons]) if polygons else None
    shares = []
    for _, geometry in counties:
        geometry = make_valid(geometry)
        if union is None or geometry.area == 0:
            shares.append(0.0)
        else:
            shares.append(geometry.intersection(union).area / geometry.area)
    return shares


def report_class(share: float, vetoed: bool, reads_as_area: bool) -> str:
    """Class of a county in the report (``SHARE_THRESHOLD``, an ASSUMPTION, splits in from out)."""
    if share >= SHARE_THRESHOLD:
        return "map_in_veto_out" if vetoed else "map_in_kept"
    return "map_out_utilities_in" if reads_as_area else "map_out"


def area_report(
    inputs_by_state: dict[str, Inputs],
    counties_by_state: dict[str, list[tuple[dict, object]]],
    polygons: list,
    area: dict,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """County table, lookup table and notes of one area (report only).

    ``inputs_by_state`` holds the EIA-861 inputs of each state of the area
    (``read_inputs`` with the lookups on), ``counties_by_state`` the Census
    counties of each state (records with ``GEOID`` and ``NAME``), and
    ``polygons`` the map polygons whose name starts with ``ba_prefix``.
    Counties are joined by state and ``name_key``.
    """
    rto, ba_code, ignore = area["rto"], area["ba_code"], area.get("veto_ignore_marks", [])
    tables, lookups, notes = [], [], {"eia_counties_not_in_census": [], "census_counties_not_in_eia": []}
    for state in area["states"]:
        inputs = inputs_by_state[state]
        table = veto_with_lookups(inputs, state, rto, ba_code, ignore, True)
        counties = counties_by_state[state]
        shares = dict(zip((name_key(r["NAME"]) for r, _ in counties), area_shares(counties, polygons)))
        census = {name_key(r["NAME"]): r for r, _ in counties}
        answers = {
            county: resolve_marks(sorted(set(g["Utility Number"])), state, rto, ba_code, inputs.in_state,
                                  inputs.all_states, inputs.sales, True)
            for county, g in inputs.territory.groupby("County")
        }
        table.insert(0, "state", state)
        table["county"] = table["county_key"].map(lambda k: census[k]["NAME"] if k in census else None)
        table["county_geoid"] = table["county_key"].map(lambda k: census[k]["GEOID"] if k in census else None)
        table["share_in_area"] = table["county_key"].map(shares).round(6)
        reads = {c: any(a.marks is not None and rto in a.marks for a in found) for c, found in answers.items()}
        table["class"] = [
            report_class(0.0 if pd.isna(s) else s, bool(v), reads[c])
            for s, v, c in zip(table["share_in_area"], table["vetoed"], table["county_eia"])
        ]
        notes["eia_counties_not_in_census"] += [f"{state} {c}" for c in table.loc[table["county"].isna(), "county_eia"]]
        notes["census_counties_not_in_eia"] += sorted(
            f"{state} {r['NAME']}" for k, r in census.items() if k not in set(table["county_key"])
        )
        tables.append(table)
        found = lookup_rows(inputs, state, rto, ba_code)
        found.insert(0, "state", state)
        lookups.append(found)
    report = pd.concat(tables, ignore_index=True).reindex(columns=REPORT_COLUMNS)
    report = report.sort_values(["state", "county_eia"], kind="mergesort").reset_index(drop=True)
    return report, pd.concat(lookups, ignore_index=True), notes


def _settings_at(settings: dict, dotted: str) -> dict:
    for part in dotted.split("."):
        settings = settings[part]
    return settings


def _read_yaml(path: Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _need(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing; the report reads only files already on disk.")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report-only EIA-861 veto for one balancing area.")
    parser.add_argument("--areas", required=True, help="settings of the areas, configs/veto_areas.yaml")
    parser.add_argument("--area", required=True, help="name of one area of the settings")
    parser.add_argument("--stamp", default=None, help="UTC stamp of the output names; default now")
    args = parser.parse_args(argv)

    areas_path = Path(args.areas).resolve()
    configs = areas_path.parent
    settings = _read_yaml(areas_path)
    by_name = {entry["name"]: entry for entry in settings["areas"]}
    if args.area not in by_name:
        raise KeyError(f"No area {args.area!r} in {areas_path}; areas: {', '.join(by_name)}.")
    area = by_name[args.area]
    sources = settings["sources"]
    shared = _read_yaml(configs / sources["settings_file"])
    eia = dict(_settings_at(shared, sources["eia861"]))
    census = _settings_at(shared, sources["counties"])
    map_source = _settings_at(shared, sources["map"])
    data_root = Path(_read_yaml(configs / "local.yaml")["data_root"])
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    year = int(sources["eia861_year"])

    eia_zip = _need(data_root / "eia" / eia["url"].rsplit("/", 1)[-1])
    county_zip = _need(data_root / "census" / census["url"].rsplit("/", 1)[-1])
    shp = _need(data_root / "geo" / map_source["folder"] / map_source["ba_shapes"].rsplit("/", 1)[-1])

    inputs_by_state, counties_by_state = {}, {}
    for state in area["states"]:
        source = {**eia, "state": state, "rto": area["rto"]}
        inputs_by_state[state] = read_inputs(eia_zip, source, year, True)
        fips = STATE_FIPS[state]
        counties_by_state[state] = sorted(
            read_shapes(county_zip, "STATEFP", lambda r, f=fips: r["STATEFP"] == f), key=lambda item: item[0]["GEOID"]
        )
    prefix = area["ba_prefix"]
    polygons = [g for r, g in read_shapes(shp, "BA") if str(r["BA"]).strip().startswith(prefix)]
    report, lookups, notes = area_report(inputs_by_state, counties_by_state, polygons, area)

    out = data_root / settings["output_folder"]
    out.mkdir(parents=True, exist_ok=True)
    for table, name in ((report, f"veto_area_{area['name']}_{stamp}.csv"), (lookups, f"veto_lookups_{area['name']}_{stamp}.csv")):
        temporary = out / f"{name}.part"
        table.to_csv(temporary, index=False, lineterminator="\n")
        temporary.replace(out / name)
    print(f"area {area['name']}: states {', '.join(area['states'])}, rto {area['rto']}, ba_code {area['ba_code']}, "
          f"ba_prefix {prefix} ({len(polygons)} polygons)")
    for key, value in notes.items():
        print(f"{key}: {value}")
    print(report.groupby(["class"]).size().to_string())
    print(pd.crosstab(report["reason_k57"], report["reason"]).to_string())
    print(f"written to {out} with stamp {stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
