"""County, balancing-area status, climate zone and stratum, on small hand-made inputs."""

import zipfile

import pandas as pd
import pytest
import shapefile
from shapely.geometry import box

from dxsew.texas import geo
from dxsew.texas.sample import geography_columns

# Four square counties of a made-up state 99, side one degree.
COUNTIES = [
    geo.County("99001", "Alpha", box(0, 0, 1, 1)),
    geo.County("99003", "Beta", box(1, 0, 2, 1)),
    geo.County("99005", "De Witt", box(2, 0, 3, 1)),
    geo.County("99007", "Delta", box(3, 0, 4, 1)),
]

SOURCE = {
    "state": "ZZ",
    "member_code": "AREA",
    "territory_file": "territory.xlsx",
    "territory_sheet": "Counties_States",
    "sales_file": "sales.xlsx",
    "sales_sheet": "States",
    "sales_header_row": 2,
}


@pytest.fixture
def eia_zip(tmp_path):
    territory = pd.DataFrame(
        [
            (2018, 1, "Inside Coop", "ZZ", "Alpha"),
            (2018, 2, "No Sales Row City", "ZZ", "Alpha"),
            (2018, 1, "Inside Coop", "ZZ", "Beta"),
            (2018, 3, "Outside Power", "ZZ", "Beta"),
            (2018, 3, "Outside Power", "ZZ", "DeWitt"),
            (2018, 2, "No Sales Row City", "ZZ", "Delta"),
            (2018, 1, "Inside Coop", "YY", "Alpha"),
        ],
        columns=["Data Year", "Utility Number", "Utility Name", "State", "County"],
    )
    sales_rows = [
        ["Utility Characteristics", None, None, None, None],
        [None, None, None, None, None],
        ["Data Year", "Utility Number", "Utility Name", "State", "BA Code"],
        [2018, 1, "Inside Coop", "ZZ", "AREA"],
        [2018, 3, "Outside Power", "ZZ", "ELSE"],
        [2018, 2, "No Sales Row City", "YY", "AREA"],
    ]
    territory.to_excel(tmp_path / "territory.xlsx", sheet_name="Counties_States", index=False)
    pd.DataFrame(sales_rows).to_excel(
        tmp_path / "sales.xlsx", sheet_name="States", index=False, header=False
    )
    path = tmp_path / "eia.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(tmp_path / "territory.xlsx", "territory.xlsx")
        archive.write(tmp_path / "sales.xlsx", "sales.xlsx")
    return path


def test_read_counties_keeps_one_state(tmp_path):
    stem = tmp_path / "counties"
    with shapefile.Writer(str(stem)) as writer:
        writer.field("STATEFP", "C", 2)
        writer.field("GEOID", "C", 5)
        writer.field("NAME", "C", 40)
        for state, geoid, name, west in (("99", "99003", "Beta", 1), ("99", "99001", "Alpha", 0), ("98", "98001", "Other", 5)):
            writer.poly([[(west, 0), (west, 1), (west + 1, 1), (west + 1, 0), (west, 0)]])
            writer.record(state, geoid, name)
    archive_path = tmp_path / "counties.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for suffix in ("shp", "shx", "dbf"):
            archive.write(f"{stem}.{suffix}", f"counties.{suffix}")
    counties = geo.read_counties(archive_path, "99")
    assert [(county.geoid, county.name) for county in counties] == [("99001", "Alpha"), ("99003", "Beta")]
    assert geo.bounds_of(counties) == (0.0, 0.0, 2.0, 1.0)


def test_assign_counties_point_in_polygon():
    found = geo.assign_counties([0.5, 1.5, 3.9, 9.0, float("nan")], [0.5, 0.2, 0.9, 0.5, 0.5], COUNTIES)
    assert [county.name if county else None for county in found] == ["Alpha", "Beta", "Delta", None, None]


def test_coordinates_inside_the_state_box():
    state_box = geo.bounds_of(COUNTIES)
    assert geo.coordinates_inside([0.5, 3.5], [0.5, 0.5], state_box)
    assert not geo.coordinates_inside([0.5, 3000000.0], [0.5, 0.5], state_box)


def test_gisjoin_and_name_key():
    assert geo.gisjoin("48303") == "G4803030"
    assert geo.name_key("De Witt") == geo.name_key("DeWitt") == "dewitt"


def test_membership_status_per_county(eia_zip):
    table, counts = geo.membership(eia_zip, SOURCE)
    status = dict(zip(table["county_eia"], table["status"]))
    assert status == {
        "Alpha": geo.MEMBER,  # the utility without a sales row is left out
        "Beta": geo.PARTIAL,
        "DeWitt": geo.OUTSIDE,
        "Delta": geo.NO_DATA,
    }
    assert counts == {"utilities_in_territory_sheet": 3, "utilities_without_sales_row": 1}


def test_climate_zones_and_nearest_county_fill(tmp_path):
    csv = tmp_path / "metadata.csv"
    pd.DataFrame(
        {
            "in.county": ["G9900010", "G9900010", "G9900070", "G9900070", "G9900070"],
            "in.building_america_climate_zone": ["Hot-Humid", "Hot-Humid", "Hot-Dry", "Hot-Dry", "Mixed-Dry"],
            "other": [1, 2, 3, 4, 5],
        }
    ).to_csv(csv, index=False)
    source = {"county_column": "in.county", "zone_column": "in.building_america_climate_zone"}
    zones, several = geo.climate_zones(csv, source)
    assert zones == {"G9900010": "Hot-Humid", "G9900070": "Hot-Dry"}
    assert several == 1
    zone_of, filled = geo.county_zones(COUNTIES, zones)
    assert zone_of == {"99001": "Hot-Humid", "99003": "Hot-Humid", "99005": "Hot-Dry", "99007": "Hot-Dry"}
    assert filled == ["99003", "99005"]


def test_county_and_stratum_of_a_few_substations(eia_zip):
    frame = pd.DataFrame(
        {
            "subregion": ["P1U", "P1U", "P2R", "P2R", "P3U"],
            "kind": ["U", "U", "R", "R", "U"],
            "substation": ["a", "b", "c", "d", "e"],
            "centroid_x": [0.5, 1.5, 0.25, 2.5, 8.0],
            "centroid_y": [0.5, 0.5, 0.75, 0.5, 0.5],
        }
    )
    status, _ = geo.membership(eia_zip, SOURCE)
    zone_of = {"99001": "Hot-Humid", "99003": "Hot-Humid", "99005": "Hot-Dry", "99007": "Hot-Dry"}
    result = geography_columns(frame, COUNTIES, status, zone_of, ["99003"])
    assert result["county"].tolist()[:4] == ["Alpha", "Beta", "Alpha", "De Witt"]
    assert pd.isna(result["county"].iloc[4])
    assert result["ba_status"].tolist()[:4] == [geo.MEMBER, geo.PARTIAL, geo.MEMBER, geo.OUTSIDE]
    assert result["in_frame"].tolist() == [True, False, True, False, False]
    assert result["stratum"].tolist()[0] == "U|Hot-Humid"
    assert result["stratum"].tolist()[2] == "R|Hot-Humid"
    assert result["stratum"].isna().tolist() == [False, True, False, True, True]
    assert result["zone_from_nearest_county"].tolist() == [False, True, False, False, False]
