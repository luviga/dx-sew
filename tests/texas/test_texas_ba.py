"""Balancing area by location (decision K56), on small hand-made polygons."""

import math

import pandas as pd
import pytest
import shapefile
from shapely.geometry import box

from dxsew.texas import ba

# Made-up map near the equator, side one degree: two areas of prefix AREA side
# by side, an area OTHER that overlaps AREA-B on its right half, and a second
# record of AREA-A that only repeats part of it.
AREAS = [
    ba.Area("AREA-A", box(0, 0, 1, 1)),
    ba.Area("AREA-B", box(1, 0, 2, 1)),
    ba.Area("OTHER", box(1.5, 0, 3, 1)),
    ba.Area("AREA-A", box(0, 0, 0.5, 0.5)),
]


# One degree of longitude at latitude 0.5, in km.
EAST_KM = ba.KM_PER_DEGREE * math.cos(math.radians(0.5))


def test_point_inside_one_polygon():
    found = ba.containing([0.75, 1.25], [0.75, 0.5], AREAS)
    assert found == [["AREA-A"], ["AREA-B"]]
    assert [ba.label(f) for f in found] == ["AREA-A", "AREA-B"]


def test_two_records_of_one_area_count_once():
    found = ba.containing([0.25], [0.25], AREAS)
    assert found == [["AREA-A"]]
    assert ba.label(found[0]) == "AREA-A"


def test_point_in_no_polygon():
    found = ba.containing([5.0, float("nan")], [5.0, 0.5], AREAS)
    assert found == [[], []]
    assert ba.label(found[0]) == ba.NONE


def test_point_in_two_polygons():
    found = ba.containing([1.75], [0.5], AREAS)
    assert found == [["AREA-B", "OTHER"]]
    assert ba.label(found[0]) == ba.OVERLAP


def test_prefix_setting():
    assert ba.in_prefix("AREA-A", "AREA")
    assert not ba.in_prefix("OTHER", "AREA")
    assert ba.in_prefix("OTHER", "OTH")
    assert not ba.in_prefix(ba.NONE, "n")
    assert not ba.in_prefix(ba.OVERLAP, "o")


def test_assign_columns_and_prefix():
    frame = pd.DataFrame({"centroid_x": [0.5, 1.75, 2.5, 9.0], "centroid_y": [0.5, 0.5, 0.5, 0.5]})
    result = ba.assign(frame, AREAS, "AREA")
    assert result["ba"].tolist() == ["AREA-A", "AREA-B+OTHER", "OTHER", ""]
    assert result["ba_label"].tolist() == ["AREA-A", ba.OVERLAP, "OTHER", ba.NONE]
    assert result["in_ba"].tolist() == [True, False, False, False]
    other = ba.assign(frame, AREAS, "OTHER")
    assert other["in_ba"].tolist() == [False, False, True, False]


def test_distance_in_km():
    # One degree of latitude at the equator is 6371.0088 * pi / 180 km.
    one_degree = ba.EARTH_RADIUS_KM * math.pi / 180
    assert ba.distance_km(5.0, -1.0, box(4, 0, 6, 1)) == pytest.approx(one_degree, rel=1e-9)
    assert ba.distance_km(0.5, 0.5, box(0, 0, 1, 1)) == 0.0
    # East-west degrees shrink with the cosine of the latitude.
    at_60 = ba.distance_km(0.0, 60.0, box(1, 59, 2, 61))
    assert at_60 == pytest.approx(one_degree * 0.5, rel=1e-6)


def test_nearest_area():
    name, km = ba.nearest(5.0, 0.5, AREAS)
    assert name == "OTHER"
    assert km == pytest.approx(2 * EAST_KM, rel=1e-9)
    name, km = ba.nearest(5.0, 0.5, AREAS, keep=lambda area: area.ba.startswith("AREA"))
    assert name == "AREA-B"
    assert km == pytest.approx(3 * EAST_KM, rel=1e-9)


def test_outside_list():
    frame = pd.DataFrame({
        "subregion": ["P1U", "P1U", "P2R"], "substation": ["a", "b", "c"], "county": ["X", "Y", None],
        "feeders": [1, 2, 3], "loads": [10, 20, 30],
        "centroid_x": [0.5, 1.75, 4.0], "centroid_y": [0.5, 0.5, 0.5],
    })
    result = ba.outside_list(ba.assign(frame, AREAS, "AREA"), AREAS)
    assert result["substation"].tolist() == ["b", "c"]
    assert result["ba_label"].tolist() == [ba.OVERLAP, ba.NONE]
    assert result["nearest_ba"].tolist() == ["AREA-B", "OTHER"]
    assert result["nearest_km"].iloc[0] == 0.0
    assert result["nearest_km"].iloc[1] == pytest.approx(EAST_KM, rel=1e-9)


def test_map_files_and_read(tmp_path):
    source = {"ba_shapes": "https://example.org/x/Map_Shapes.shp", "companions": [".shx", ".dbf"], "folder": "m"}
    shp, files = ba.map_files(source, tmp_path)
    assert shp == tmp_path / "m" / "Map_Shapes.shp"
    assert [url for url, _ in files] == [
        "https://example.org/x/Map_Shapes.shp", "https://example.org/x/Map_Shapes.shx",
        "https://example.org/x/Map_Shapes.dbf",
    ]
    shp.parent.mkdir()
    with shapefile.Writer(str(shp)) as writer:
        writer.field("BA", "C")
        writer.field("layer", "C")
        writer.poly([[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]])
        writer.record("AREA-A", "x")
    areas = ba.read_areas(shp)
    assert [area.ba for area in areas] == ["AREA-A"]
    assert ba.containing([0.5], [0.5], areas) == [["AREA-A"]]
