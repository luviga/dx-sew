"""Readers of the SMART-DS OpenDSS text files used by the Texas sample.

The files read are ``Loads.dss``, ``Lines.dss`` and ``Transformers.dss`` of a
feeder, and ``Buscoords.dss`` and ``Transformers.dss`` of a substation
folder. The files are parsed as text; no circuit is compiled.

Format of the release: https://data.openei.org/submissions/2981 (SMART-DS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROPERTY = re.compile(r"(\S+?)\s*=\s*(\S+)")
PHASE_COPY = re.compile(r"_[12]$")


@dataclass(frozen=True)
class Load:
    name: str
    bus: str
    kw: float
    phases: int | None
    yearly: str | None


def properties(line: str) -> dict[str, str]:
    """Property values of one OpenDSS line, with lower-case property names."""
    return {name.lower(): value for name, value in PROPERTY.findall(line)}


def parse_loads(text: str) -> list[Load]:
    """One ``Load`` per ``New Load.`` line of a ``Loads.dss`` file.

    The bus is ``bus1`` without its phase nodes (``p1ulv5.1.2`` gives
    ``p1ulv5``). ``yearly`` is the name of the load shape of the load.
    """
    loads: list[Load] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].lower() != "new" or not parts[1].lower().startswith("load."):
            continue
        values = properties(line)
        phases = values.get("phases")
        loads.append(
            Load(
                name=parts[1].split(".", 1)[1],
                bus=values.get("bus1", "").split(".", 1)[0].lower(),
                kw=float(values.get("kw", "nan")),
                phases=int(phases) if phases is not None else None,
                yearly=values.get("yearly"),
            )
        )
    return loads


def customer_count(loads: list[Load]) -> int:
    """Number of customers behind the load objects.

    SMART-DS writes a split-phase customer as two load objects whose names end
    in ``_1`` and ``_2``; a customer is a load name without that ending.
    """
    return len({PHASE_COPY.sub("", load.name) for load in loads})


def parquet_name(yearly: str) -> str | None:
    """Load-data parquet file that a load shape name refers to.

    ``res_kw_33706-Coast_pu`` gives ``res_33706-Coast.parquet``: the shape name
    without its ``kw`` part and its ``_pu`` ending. This is the rule of
    ``0_download_smartds/generate_loads_pivot.py`` of DeltaE/dss-eulp-coupling
    (``Parquet_Name``). A name of another form gives ``None``.
    """
    parts = yearly.split("_", 2)
    if len(parts) != 3 or parts[1].lower() != "kw" or not parts[2].lower().endswith("_pu"):
        return None
    return f"{parts[0]}_{parts[2][:-3]}.parquet"


def load_references(loads: list[Load]) -> tuple[set[str], int]:
    """Parquet names the loads refer to, and the count of loads with no usable reference."""
    names: set[str] = set()
    without = 0
    for load in loads:
        name = parquet_name(load.yearly) if load.yearly else None
        if name is None:
            without += 1
        else:
            names.add(name)
    return names, without


def parse_buscoords(text: str) -> dict[str, tuple[float, float]]:
    """``bus -> (x, y)`` of a ``Buscoords.dss`` file (blank or comma separated)."""
    coords: dict[str, tuple[float, float]] = {}
    for line in text.splitlines():
        parts = line.replace(",", " ").split()
        if len(parts) < 3:
            continue
        try:
            coords[parts[0].lower()] = (float(parts[1]), float(parts[2]))
        except ValueError:
            continue
    return coords


def parse_transformer_kva(text: str) -> list[float]:
    """Rating in kVA of every transformer of a ``Transformers.dss`` file.

    The rating of a transformer is the ``kva`` of its first winding (or the
    first value of ``kvas``).
    """
    ratings: list[float] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].lower() != "new" or not parts[1].lower().startswith("transformer."):
            continue
        found = re.search(r"\bkvas?\s*=\s*[\[(\"']?\s*([0-9.eE+-]+)", line, flags=re.IGNORECASE)
        if found:
            ratings.append(float(found.group(1)))
    return ratings


# Length units a Line accepts (OpenDSS, https://opendss.epri.com/Units.html:
# "none | mi | kft | km | m | Ft | in | cm"), as kilometres per unit. Factors
# from NIST SP 811 Appendix B.9 (foot 0.3048 m, inch 0.0254 m, mile
# 1609.344 m, all exact):
# https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b9
KM_PER_UNIT = {
    "mi": 1.609344,
    "kft": 0.3048,
    "km": 1.0,
    "m": 0.001,
    "ft": 0.0003048,
    "in": 0.0000254,
    "cm": 0.00001,
}
KV_VALUE = re.compile(r"\bkv\s*=\s*([0-9.eE+-]+)", flags=re.IGNORECASE)
KVS_VALUES = re.compile(r"\bkvs\s*=\s*[\[(\"']([^\])\"']*)", flags=re.IGNORECASE)
NUMBER = re.compile(r"[0-9.eE+-]+")
# One property of a transformer line, in order: a name, "=", and a value that
# is a bracketed or quoted array (OpenDSS accepts [], (), "" and '') or one
# blank-free word.
TRANSFORMER_PROPERTY = re.compile(
    r"([A-Za-z%][\w%]*)\s*=\s*(\[[^\]]*\]|\([^)]*\)|\"[^\"]*\"|'[^']*'|[^\s\[(\"']\S*)"
)


@dataclass(frozen=True)
class Line:
    name: str
    bus1: str
    bus2: str
    length: float
    units: str | None


@dataclass(frozen=True)
class Winding:
    bus: str
    nodes: tuple[int, ...]
    kv: float | None


@dataclass(frozen=True)
class Transformer:
    name: str
    kva: float | None
    kvs: tuple[float, ...]
    phases: int | None = None
    windings: tuple[Winding, ...] = ()


def _bus(value: str) -> str:
    return value.split(".", 1)[0].lower()


def _nodes(value: str) -> tuple[int, ...]:
    """Node numbers of a bus name: ``p1lv.1.0`` gives (1, 0); none gives ()."""
    found = []
    for part in value.split(".")[1:]:
        try:
            found.append(int(part))
        except ValueError:
            continue
    return tuple(found)


def _array(value: str) -> list[str]:
    """Items of an OpenDSS array value, brackets or quotes removed, split on commas or blanks."""
    inner = value.strip()
    if inner[:1] in "[(\"'" and inner[-1:] in "])\"'":
        inner = inner[1:-1]
    return [item for item in re.split(r"[\s,]+", inner) if item]


def _windings(row: str) -> tuple[int | None, tuple[Winding, ...]]:
    """Phases and windings of one ``New Transformer.`` line.

    Both OpenDSS forms are read (https://opendss.epri.com/Properties16.html):
    ``buses=[...]`` with ``kvs=(...)``, one item per winding, and blocks of
    ``wdg=<n>`` followed by ``bus=`` and ``kv=`` of winding n. ``bus`` and
    ``kv`` before any ``wdg`` belong to winding 1, the active winding of a new
    transformer. The winding count is the largest of ``windings`` (2 when not
    given, the OpenDSS default), the highest ``wdg`` and the length of
    ``buses`` and ``kvs``. A winding the line gives no bus has bus "" and no
    nodes; one it gives no voltage has ``kv`` None.
    """
    phases = None
    declared = 2
    active = 1
    buses: dict[int, str] = {}
    kvs: dict[int, float] = {}
    for name, value in TRANSFORMER_PROPERTY.findall(row):
        name = name.lower()
        try:
            if name == "phases":
                phases = int(float(value))
            elif name == "windings":
                declared = int(float(value))
            elif name == "wdg":
                active = int(float(value))
            elif name == "bus":
                buses[active] = value
            elif name == "kv":
                kvs[active] = float(value)
            elif name == "buses":
                buses.update({index: item for index, item in enumerate(_array(value), start=1)})
            elif name == "kvs":
                kvs.update({index: float(item) for index, item in enumerate(_array(value), start=1)})
        except ValueError:
            continue
    count = max([declared, *buses, *kvs])
    windings = tuple(
        Winding(bus=_bus(buses.get(index, "")), nodes=_nodes(buses.get(index, "")), kv=kvs.get(index))
        for index in range(1, count + 1)
    )
    return phases, windings


def parse_lines(text: str) -> list[Line]:
    """One ``Line`` per ``New Line.`` line of a ``Lines.dss`` file.

    Buses lose their phase nodes and are lower case. A line without
    ``length`` has length 0. ``units`` is the lower-case ``units`` value, or
    None when the line gives none.
    """
    lines: list[Line] = []
    for row in text.splitlines():
        parts = row.split()
        if len(parts) < 2 or parts[0].lower() != "new" or not parts[1].lower().startswith("line."):
            continue
        values = properties(row)
        length = values.get("length")
        units = values.get("units")
        lines.append(
            Line(
                name=parts[1].split(".", 1)[1],
                bus1=_bus(values.get("bus1", "")),
                bus2=_bus(values.get("bus2", "")),
                length=float(length) if length is not None else 0.0,
                units=units.lower() if units is not None else None,
            )
        )
    return lines


def line_length_km(line: Line, default_unit: str) -> tuple[float, bool]:
    """Length of a line in km, and whether the line names a known unit.

    A line with no ``units``, ``units=none`` or a name outside ``KM_PER_UNIT``
    is read in ``default_unit``.
    """
    if line.units in KM_PER_UNIT:
        return line.length * KM_PER_UNIT[line.units], True
    return line.length * KM_PER_UNIT[default_unit], False


def parse_transformers(text: str) -> list[Transformer]:
    """One ``Transformer`` per ``New Transformer.`` line of a ``Transformers.dss`` file.

    ``kva`` is the rating as in ``parse_transformer_kva`` (None when the line
    gives none); ``kvs`` holds every ``kv`` value and every value of ``kvs``.
    ``phases`` is the ``phases`` value (None when not given) and ``windings``
    holds each winding's bus (without nodes), node list and ``kv``
    (``_windings``).
    """
    found: list[Transformer] = []
    for row in text.splitlines():
        parts = row.split()
        if len(parts) < 2 or parts[0].lower() != "new" or not parts[1].lower().startswith("transformer."):
            continue
        ratings = parse_transformer_kva(row)
        kvs = [float(value) for value in KV_VALUE.findall(row)]
        for group in KVS_VALUES.findall(row):
            kvs.extend(float(value) for value in NUMBER.findall(group))
        phases, windings = _windings(row)
        found.append(
            Transformer(
                name=parts[1].split(".", 1)[1],
                kva=ratings[0] if ratings else None,
                kvs=tuple(kvs),
                phases=phases,
                windings=windings,
            )
        )
    return found


def load_sector(yearly: str | None) -> str | None:
    """Sector of a load: its load shape name before ``_kw`` (``res`` or ``com``).

    The same split as ``parquet_name``; a name of another form gives None.
    """
    if not yearly or parquet_name(yearly) is None:
        return None
    return yearly.split("_", 1)[0].lower()
