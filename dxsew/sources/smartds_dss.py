"""Readers of the SMART-DS OpenDSS text files used by the Texas sample.

The files read are ``Loads.dss``, ``Lines.dss`` and ``Transformers.dss`` of a
feeder, and ``Buscoords.dss`` and ``Transformers.dss`` of a substation
folder. The files are parsed as text; no circuit is compiled.

Format of the release: https://data.openei.org/submissions/2981 (SMART-DS).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dxsew import config
from dxsew.config import load_units

PROPERTY = re.compile(r"(\S+?)\s*=\s*(\S+)")
PHASE_COPY = re.compile(r"_[12]$")
SHAPE_ENDING = "_pu"  # ending of a load shape name of the release


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
        if len(parts) <= 1 or parts[0].lower() != "new" or not parts[1].lower().startswith("load."):
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
    kind, first, rest = yearly.partition("_")
    qty, second, tail = rest.partition("_")
    if not first or not second or qty.lower() != "kw" or not tail.lower().endswith(SHAPE_ENDING):
        return None
    return f"{kind}_{tail[: -len(SHAPE_ENDING)]}.parquet"


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
        try:
            bus, x, y, *_ = line.replace(",", " ").split()
            coords[bus.lower()] = (float(x), float(y))
        except ValueError:  # fewer than three items, or a value that is not a number
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
        if len(parts) <= 1 or parts[0].lower() != "new" or not parts[1].lower().startswith("transformer."):
            continue
        found = re.search(r"\bkvas?\s*=\s*[\[(\"']?\s*([0-9.eE+-]+)", line, flags=re.IGNORECASE)
        if found:
            ratings.append(float(found.group(1)))
    return ratings


# Length units a Line accepts, as kilometres per unit: config/units.yaml,
# length_km_per_unit (OpenDSS units, factors from NIST SP 811).
KM_PER_UNIT = dict(load_units()["length_km_per_unit"])
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


def _windings(row: str, default_windings: int | None = None) -> tuple[int | None, tuple[Winding, ...]]:
    """Phases and windings of one ``New Transformer.`` line.

    Both OpenDSS forms are read (https://opendss.epri.com/Properties16.html):
    ``buses=[...]`` with ``kvs=(...)``, one item per winding, and blocks of
    ``wdg=<n>`` followed by ``bus=`` and ``kv=`` of winding n. ``bus`` and
    ``kv`` before any ``wdg`` belong to winding 1, the active winding of a new
    transformer. The winding count is the largest of ``windings`` (the setup's
    ``release.opendss_default_windings`` when not given), the highest ``wdg`` and the length of
    ``buses`` and ``kvs``. A winding the line gives no bus has bus "" and no
    nodes; one it gives no voltage has ``kv`` None.
    """
    phases = None
    declared = default_windings or config.setup()["release"]["opendss_default_windings"]
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
        if len(parts) <= 1 or parts[0].lower() != "new" or not parts[1].lower().startswith("line."):
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


def parse_transformers(text: str, default_windings: int | None = None) -> list[Transformer]:
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
        if len(parts) <= 1 or parts[0].lower() != "new" or not parts[1].lower().startswith("transformer."):
            continue
        ratings = parse_transformer_kva(row)
        kvs = [float(value) for value in KV_VALUE.findall(row)]
        for group in KVS_VALUES.findall(row):
            kvs.extend(float(value) for value in NUMBER.findall(group))
        phases, windings = _windings(row, default_windings)
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
