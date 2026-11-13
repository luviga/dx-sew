"""County, RTO membership and climate zone of a substation.

Sources (the URLs are settings in ``configs/texas_sample.yaml``):

- County boundaries: U.S. Census Bureau, TIGER/Line 2018 counties,
  https://www2.census.gov/geo/tiger/TIGER2018/COUNTY/tl_2018_us_county.zip
- Utilities per county and the ISOs and RTOs each utility operates in: U.S.
  Energy Information Administration, Form EIA-861 detailed data files, 2018
  (service territory and utility data files),
  https://www.eia.gov/electricity/data/eia861/ ; the same utility data are
  published by PUDL as ``core_eia861__yearly_utility_data_rto``,
  https://data.catalyst.coop/preview/pudl/core_eia861__yearly_utility_data_rto
- Climate zone per county: ResStock 2024 release 2 metadata (columns
  ``in.county`` and ``in.building_america_climate_zone``),
  https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/resstock_tmy3_release_2/
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import shapefile
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

PARTIAL, OUTSIDE, NO_DATA = "partial", "outside", "no_utility_row"
# The member status of a county is the RTO name of the settings (``rto``).


@dataclass(frozen=True)
class County:
    geoid: str  # state and county FIPS codes, five digits
    name: str
    polygon: BaseGeometry


def read_counties(zip_path: Path, state_fips: str) -> list[County]:
    """Counties of one state from the TIGER/Line county shapefile (zipped)."""
    counties: list[County] = []
    with shapefile.Reader(str(zip_path)) as reader:
        names = [field[0] for field in reader.fields[1:]]
        state_at, geoid_at, name_at = (names.index(n) for n in ("STATEFP", "GEOID", "NAME"))
        for index, record in enumerate(reader.iterRecords()):
            if record[state_at] == state_fips:
                polygon = shape(reader.shape(index).__geo_interface__)
                counties.append(County(record[geoid_at], record[name_at], polygon))
    if not counties:
        raise ValueError(f"No county with state code {state_fips} in {zip_path}.")
    return sorted(counties, key=lambda county: county.geoid)


def bounds_of(counties: list[County]) -> tuple[float, float, float, float]:
    """``(west, south, east, north)`` of the counties, in degrees."""
    boxes = [county.polygon.bounds for county in counties]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def coordinates_inside(xs, ys, box: tuple[float, float, float, float]) -> bool:
    """True when every point reads as a longitude and latitude inside the box."""
    west, south, east, north = box
    return all(
        math.isfinite(x) and math.isfinite(y) and west <= x <= east and south <= y <= north
        for x, y in zip(xs, ys)
    )


def assign_counties(xs, ys, counties: list[County]) -> list[County | None]:
    """County whose polygon holds each point; ``None`` when no county does.

    ASSUMPTION: the bus coordinates of the release are longitude and latitude
    in WGS84 degrees and are set against the NAD83 county boundaries with no
    datum shift. The release states no coordinate system. Positions in the
    two datums differ by up to about two metres in the United States (NOAA
    National Geodetic Survey, "Improving the National Spatial Reference
    System", https://www.ngs.noaa.gov/2010Summit/Improving_the_NSRS.pdf),
    far below the size of a county.
    """
    tree = STRtree([county.polygon for county in counties])
    found: list[County | None] = []
    for x, y in zip(xs, ys):
        if not (math.isfinite(x) and math.isfinite(y)):
            found.append(None)
            continue
        point = Point(x, y)
        hits = [int(i) for i in tree.query(point, predicate="intersects")]
        found.append(counties[min(hits)] if hits else None)
    return found


def gisjoin(geoid: str) -> str:
    """NHGIS GISJOIN code of a county from its five-digit FIPS code.

    ``48303`` gives ``G4803030``: the letter G, the state code and a zero,
    the county code and a zero (IPUMS NHGIS, https://www.nhgis.org/geographic-crosswalks).
    """
    return f"G{geoid[:2]}0{geoid[2:]}0"


def name_key(name: str) -> str:
    """County name reduced to its letters, lower case, for joining two sources."""
    return re.sub(r"[^a-z]", "", name.lower())


def _sheet(zip_path: Path, member: str, sheet: str, header) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        return pd.read_excel(io.BytesIO(archive.read(member)), sheet_name=sheet, header=header)


def utility_rtos(zip_path: Path, source: dict) -> pd.Series:
    """ISOs and RTOs each utility of the state reports operating in, from EIA-861.

    The utility data sheet has two header rows: a group row whose group
    ``source["rto_group"]`` ("Operating in these RTOs") spans one column per
    ISO or RTO, and a row of column names below it. A utility operates in an
    RTO when its cell holds ``Y``; ``N`` and a blank cell both mean it does not
    report operating there. Rows are kept by state; a utility with several rows
    in the state operates in every RTO any of its rows reports.

    Returns ``utility number -> frozenset of RTO names``, one entry per utility
    with a row in the state.
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
    body = raw.iloc[header + 1 :]
    body = body[body[state_at] == source["state"]]
    rtos: dict[int, set[str]] = {}
    for _, row in body.iterrows():
        found = {names[i] for i in rto_at if str(row[i]).strip().upper() == "Y"}
        rtos.setdefault(int(row[number_at]), set()).update(found)
    return pd.Series({number: frozenset(found) for number, found in rtos.items()}, dtype=object)


def territory_rows(zip_path: Path, source: dict) -> pd.DataFrame:
    """Rows of the service territory sheet of EIA-861 for the state of the settings.

    One row per utility and county (columns ``Utility Number``, ``Utility
    Name``, ``State``, ``County`` among others).
    """
    territory = _sheet(zip_path, source["territory_file"], source["territory_sheet"], 0)
    return territory[territory["State"] == source["state"]]


def membership(zip_path: Path, source: dict) -> tuple[pd.DataFrame, dict]:
    """RTO status of every county of the state, from EIA-861 (decision K52).

    The service territory sheet lists the utilities serving each county; the
    utility data sheet gives the ISOs and RTOs each utility reports operating
    in (``utility_rtos``). With ``rto = source["rto"]``, a county is ``rto``
    (for example ``ERCOT``) when every utility serving it operates in ``rto``,
    ``partial`` when some do, and ``outside`` when none do. A utility with no
    utility data row in the state is left out of its counties and counted; a
    county left with no utility is ``no_utility_row``.

    Returns one row per county (``county_key``, ``county_eia``, ``status``,
    ``utilities``, ``utilities_with_row``, ``utilities_in_rto``) and counts of
    utilities, with the left-out utilities listed.
    """
    rto = source["rto"]
    territory = territory_rows(zip_path, source)
    rtos = utility_rtos(zip_path, source)

    rows = []
    for county, group in territory.groupby("County"):
        utilities = sorted(set(group["Utility Number"]))
        known = [rtos[u] for u in utilities if u in rtos.index]
        inside = [rto in found for found in known]
        if not known:
            status = NO_DATA
        elif all(inside):
            status = rto
        elif any(inside):
            status = PARTIAL
        else:
            status = OUTSIDE
        rows.append(
            {
                "county_key": name_key(county),
                "county_eia": county,
                "status": status,
                "utilities": len(utilities),
                "utilities_with_row": len(known),
                "utilities_in_rto": sum(inside),
            }
        )
    table = pd.DataFrame(rows)
    serving = territory.drop_duplicates("Utility Number").set_index("Utility Number")["Utility Name"]
    left_out = serving[~serving.index.isin(rtos.index)]
    with_row = rtos[rtos.index.isin(serving.index)]
    counts = {
        "utilities_in_territory_sheet": len(serving),
        "utilities_without_utility_row": len(left_out),
        "utilities_reporting_no_rto": int((with_row.map(len) == 0).sum()),
        "utilities_in_rto": int(with_row.map(lambda found: rto in found).sum()),
        "left_out_utilities": [f"{number} {name}" for number, name in left_out.sort_index().items()],
    }
    return table, counts


# Reasons of the veto table (decision K57), one per county.
VETOED = "other_rto_only"
REPORTS_RTO, MISSING_ROW, NO_RTO_MARKED, IGNORED_MARK_ONLY = (
    "reports_rto",
    "missing_row",
    "no_rto_marked",
    "ignored_mark_only",
)


def veto_reason(marks: list[frozenset | None], rto: str, ignore) -> str:
    """Veto reason of one county from the RTO marks of the utilities serving it.

    ``marks`` holds one entry per utility: its RTO marks, or ``None`` when it
    has no utility data row in the state. The county is vetoed
    (``other_rto_only``) when every utility has a row, every one marks at
    least one RTO, none marks ``rto``, and every one marks at least one RTO
    that is neither ``rto`` nor in ``ignore``. Otherwise the first reason that
    applies: ``reports_rto``, ``missing_row``, ``no_rto_marked``,
    ``ignored_mark_only``.
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


def county_veto(zip_path: Path, source: dict) -> pd.DataFrame:
    """EIA-861 veto of every county of the state's service territory sheet (decision K57).

    A county is vetoed when the utilities serving it report, on the utility
    data file, that their customers belong to another RTO: every utility has
    a utility data row in the state, every one marks at least one RTO, none
    marks ``source["rto"]``, and every one marks at least one RTO outside
    ``source["veto_ignore_marks"]`` (for example ``Other``). The veto only
    removes substations from the frame; a blank answer, a missing row, a
    mark in ``veto_ignore_marks`` alone, or a county where some utilities mark
    ``rto`` never removes. See ``veto_table`` for the columns.
    """
    return veto_table(
        territory_rows(zip_path, source),
        utility_rtos(zip_path, source),
        source["rto"],
        source.get("veto_ignore_marks", []),
    )


def climate_zones(metadata_csv: Path, source: dict) -> tuple[dict[str, str], int]:
    """Climate zone per county code of the ResStock metadata.

    Returns ``county code -> zone`` and the number of counties whose rows name
    more than one zone (the most frequent one is kept for those).
    """
    county, zone = source["county_column"], source["zone_column"]
    rows = pd.read_csv(metadata_csv, usecols=[county, zone], dtype=str).dropna()
    counts = rows.groupby([county, zone]).size().rename("n").reset_index()
    counts = counts.sort_values([county, "n", zone], ascending=[True, False, True])
    several = int((counts.groupby(county).size() > 1).sum())
    first = counts.drop_duplicates(county)
    return dict(zip(first[county], first[zone])), several


def central_angle(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle angle between two points, in radians (haversine formula)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    half_lat = (p2 - p1) / 2
    half_lon = math.radians(lon2 - lon1) / 2
    a = math.sin(half_lat) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(half_lon) ** 2
    return 2 * math.asin(min(1.0, math.sqrt(a)))


def county_zones(counties: list[County], zones: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Zone of every county; a county with no row takes the zone of the nearest county.

    Nearest is by the great-circle angle between polygon centroids, among the
    counties that have a zone. Returns ``geoid -> zone`` and the geoids filled
    this way.
    """
    own = {c.geoid: zones[gisjoin(c.geoid)] for c in counties if gisjoin(c.geoid) in zones}
    if not own:
        raise ValueError("No county of the state has a climate zone in the metadata.")
    centres = {c.geoid: (c.polygon.centroid.x, c.polygon.centroid.y) for c in counties}
    filled: list[str] = []
    result = dict(own)
    for county in counties:
        if county.geoid in own:
            continue
        here = centres[county.geoid]
        nearest = min(sorted(own), key=lambda geoid: central_angle(*here, *centres[geoid]))
        result[county.geoid] = own[nearest]
        filled.append(county.geoid)
    return result, filled
