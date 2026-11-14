"""Readers of the SMART-DS OpenDSS text files used by the Texas sample.

Only three files are read: ``Loads.dss`` of a feeder, ``Buscoords.dss`` and
``Transformers.dss`` of a substation folder. The files are parsed as text;
no circuit is compiled.

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
