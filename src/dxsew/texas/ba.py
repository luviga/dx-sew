"""Balancing area of a substation by location (decision K56).

A substation is in the frame when the centroid of its load buses lies inside
one polygon of the balancing area map whose name starts with the prefix of
the settings (``ERCO`` for ERCOT, whose map polygons are ``ERCO-C``,
``ERCO-E``, ``ERCO-FW``, ``ERCO-N``, ``ERCO-NC``, ``ERCO-S``, ``ERCO-SC`` and
``ERCO-W``). Another balancing area uses the same rule with another prefix.

Source of the map (the URL is a setting in ``configs/texas_sample.yaml``):
PyPSA-USA, ``workflow/repo_data/geospatial/BA_shapes_new/Modified_BE_BA_Shapes``,
https://github.com/PyPSA/pypsa-usa/tree/3d67e2061e02a5b983ed9807a1b2668e7fdf6fd4/workflow/repo_data/geospatial/BA_shapes_new
(MIT license; Tehranchi et al., Environmental Research Letters, volume 21;
software DOI https://doi.org/10.5281/zenodo.10815964). The file holds one
record per balancing area or balancing area region, field ``BA``, NAD83
geographic coordinates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import shapefile
import shapely
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

NONE, OVERLAP = "none", "overlap"
BA_FIELD = "BA"  # field of the map that names the balancing area of a record

# Mean radius of the Earth, R1 = (2a + b) / 3 of the GRS 80 ellipsoid, in km:
# H. Moritz, "Geodetic Reference System 1980", Journal of Geodesy 74 (2000)
# 128-133, https://doi.org/10.1007/s001900050278
EARTH_RADIUS_KM = 6371.0088
KM_PER_DEGREE = EARTH_RADIUS_KM * math.pi / 180


@dataclass(frozen=True)
class Area:
    ba: str
    polygon: BaseGeometry


def map_files(source: dict, folder: Path) -> tuple[Path, list[tuple[str, Path]]]:
    """Path of the map's ``.shp`` under ``folder`` and every ``(url, path)`` of its files.

    ``source`` is ``geography.balancing_area_map`` of the settings: the URL of
    the ``.shp`` (``ba_shapes``), its companion endings and the folder name.
    """
    url = source["ba_shapes"]
    stem = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    base = url.rsplit(".", 1)[0]
    target = Path(folder) / source["folder"]
    files = [(url, target / f"{stem}.shp")]
    files += [(base + ending, target / f"{stem}{ending}") for ending in source["companions"]]
    return files[0][1], files


def fetch_map(source: dict, folder: Path, fetch) -> Path:
    """Download the map's files when absent (``fetch(session, url, path)``); returns the ``.shp``.

    The session asks for uncompressed answers, so the size a HEAD answer
    reports is the size of the file.
    """
    shp, files = map_files(source, folder)
    with requests.Session() as session:
        session.headers["Accept-Encoding"] = "identity"
        for url, path in files:
            fetch(session, url, path)
    return shp


def read_areas(shp_path: Path, field: str = BA_FIELD) -> list[Area]:
    """Every record of the balancing area map, in file order."""
    areas: list[Area] = []
    with shapefile.Reader(str(shp_path)) as reader:
        names = [f[0] for f in reader.fields[1:]]
        at = names.index(field)
        for index, record in enumerate(reader.iterRecords()):
            areas.append(Area(str(record[at]).strip(), shape(reader.shape(index).__geo_interface__)))
    if not areas:
        raise ValueError(f"No record in {shp_path}.")
    return areas


def containing(xs, ys, areas: list[Area]) -> list[list[str]]:
    """Names of the balancing areas whose polygons hold each point, sorted.

    A point on a polygon edge counts as inside it. Two records with the same
    name count as one area. A point with a missing coordinate is in none.

    ASSUMPTION (kept from the county assignment, ``geo.assign_counties``): the
    bus coordinates of the release are longitude and latitude in WGS84 degrees
    and are set against the NAD83 polygons of the map with no datum shift.
    """
    tree = STRtree([area.polygon for area in areas])
    found: list[list[str]] = []
    for x, y in zip(xs, ys):
        if not (math.isfinite(x) and math.isfinite(y)):
            found.append([])
            continue
        hits = tree.query(Point(x, y), predicate="intersects")
        found.append(sorted({areas[int(i)].ba for i in hits}))
    return found


def label(names: list[str]) -> str:
    """The one area holding a point, ``none`` when no area does, ``overlap`` when two or more do."""
    if not names:
        return NONE
    if len(names) > 1:
        return OVERLAP
    return names[0]


def in_prefix(ba_label: str, prefix: str) -> bool:
    """True when a label names one area and that name starts with ``prefix``."""
    return ba_label not in (NONE, OVERLAP) and ba_label.startswith(prefix)


def _local_km(geometry: BaseGeometry, lon0: float, lat0: float) -> BaseGeometry:
    """Geometry in km on a plane tangent at ``(lon0, lat0)`` (equirectangular, local)."""
    scale = np.array([math.cos(math.radians(lat0)) * KM_PER_DEGREE, KM_PER_DEGREE])
    origin = np.array([lon0, lat0])
    return shapely.transform(geometry, lambda xy: (xy - origin) * scale)


def distance_km(lon: float, lat: float, geometry: BaseGeometry) -> float:
    """Distance in km from a point to a geometry (0 when the point is inside it).

    The geometry is set on a plane tangent at the point, with east-west degrees
    shortened by the cosine of the latitude. The error of this plane grows with
    distance and stays far below the distances of interest here (tens of km).
    """
    return float(_local_km(geometry, lon, lat).distance(Point(0.0, 0.0)))


def nearest(lon: float, lat: float, areas: list[Area], keep=lambda area: True) -> tuple[str, float]:
    """Name of the nearest area among those ``keep`` accepts, and its distance in km.

    Candidates are first ranked by distance in degrees; an area whose degree
    distance exceeds the smallest one by more than the factor
    ``1 / cos(latitude)`` cannot be nearer in km, so only the others are
    measured in km.
    """
    chosen = [area for area in areas if keep(area)]
    if not chosen or not (math.isfinite(lon) and math.isfinite(lat)):
        return "", float("nan")
    point = Point(lon, lat)
    degrees = [area.polygon.distance(point) for area in chosen]
    limit = min(degrees) / math.cos(math.radians(lat)) + 1e-9
    best = ("", float("inf"))
    for area, degree in zip(chosen, degrees):
        if degree <= limit:
            km = distance_km(lon, lat, area.polygon)
            if km < best[1]:
                best = (area.ba, km)
    return best


def assign(frame, areas: list[Area], prefix: str):
    """Columns ``ba`` (every area holding the centroid, joined by ``+``) and ``ba_label``."""
    names = containing(frame["centroid_x"], frame["centroid_y"], areas)
    frame = frame.copy()
    frame["ba"] = ["+".join(found) for found in names]
    frame["ba_label"] = [label(found) for found in names]
    frame["in_ba"] = frame["ba_label"].map(lambda value: in_prefix(value, prefix))
    return frame


def outside_list(frame, areas: list[Area]):
    """Substations labelled ``none`` or ``overlap``, with the nearest area and its distance in km.

    For an ``overlap`` substation the nearest area is the first area holding
    it, at distance 0.
    """
    rows = []
    for row in frame[frame["ba_label"].isin([NONE, OVERLAP])].itertuples(index=False):
        if row.ba_label == OVERLAP:
            area, km = row.ba.split("+")[0], 0.0
        else:
            area, km = nearest(row.centroid_x, row.centroid_y, areas)
        rows.append({
            "subregion": row.subregion, "substation": row.substation, "ba_label": row.ba_label,
            "ba": row.ba, "county": row.county, "feeders": row.feeders, "loads": row.loads,
            "centroid_x": row.centroid_x, "centroid_y": row.centroid_y,
            "nearest_ba": area, "nearest_km": km,
        })
    return pd.DataFrame(rows, columns=["subregion", "substation", "ba_label", "ba", "county", "feeders",
                                       "loads", "centroid_x", "centroid_y", "nearest_ba", "nearest_km"])
