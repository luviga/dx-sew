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

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import shapefile
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

# The EIA-861 readers and the veto (decisions K57 and K58) live in dxsew.veto,
# the procedure for any balancing area; they are imported here so every
# existing caller of dxsew.texas.geo works as before.
from dxsew.veto import (  # noqa: F401
    IGNORED_MARK_ONLY,
    MISSING_ROW,
    NO_RTO_MARKED,
    REPORTS_RTO,
    VETOED,
    _sheet,
    county_veto,
    name_key,
    territory_rows,
    utility_rtos,
    veto_reason,
    veto_table,
)

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
