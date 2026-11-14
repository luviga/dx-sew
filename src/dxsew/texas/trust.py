"""Trust checks of the balancing area map (decision K56).

Run from the repository::

    uv run python -m dxsew.texas.trust --stamp <stamp> [--puct-counties <csv>]

The frame rule places a substation by the polygon of the balancing area map
that holds the centroid of its load buses (``ba``). The map's shapes are rough:
EIA, which publishes balancing area shapes from the same HIFLD control areas,
says they overlap, have gaps and are not strict boundaries
(https://services7.arcgis.com/FGr1D95XCGALKXqM/ArcGIS/rest/services/Balancing_Authorities/FeatureServer).
Four checks set the map against other sources. None changes the frame; each
writes a CSV with the stamp to ``<data_root>/texas/``.

1. Zone agreement: the zone ending of the load shape names of the release
   (``-Coast``, ``-North-Central``, ...) against the ERCOT polygon zone.
2. County list of the Public Utility Commission of Texas ("Counties Served by
   ERCOT", https://www.puc.texas.gov/industry/maps/supplychain/Default.aspx),
   run when that list is given.
3. EIA-861 2018: the ISOs and RTOs each utility reports operating in, against
   the share of substations of its counties inside the polygons.
4. Edges: frame substations near the edge of another balancing area.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from . import ba, dss, frame as frame_module, geo, names
from .config import Paths, find_configs, load_local, load_sample_settings
from .sample import fetch_source, get_listing, write_csv

NO_ENDING = "(no ending)"
ZONE_CODES_NAME = "texas_zone_codes.csv"

# ASSUMPTION: 10 km marks a substation as near a balancing area edge, about the size of a large distribution substation's service area; the count shows how much of the frame depends on where the map draws its edges.
EDGE_KM = 10.0

# ------------------------------------------------------------ 1 zone agreement


def initials(zone: str) -> str:
    """Upper-case initials of the hyphen-separated words of a zone ending: ``North-Central`` gives ``NC``."""
    return "".join(word[0].upper() for word in zone.split("-") if word)


def pair_zones(areas: list[str], endings: list[str], prefix: str) -> dict[str, str]:
    """Zone ending of each polygon whose name starts with ``prefix``.

    The polygon ``<prefix>-<code>`` pairs with the zone ending whose initials
    are ``<code>``: ``ERCO-NC`` with ``North-Central``, ``ERCO-FW`` with
    ``Far-West``. A polygon with no ending, or with several, stops the check.
    """
    by_initials: dict[str, list[str]] = {}
    for ending in endings:
        if ending:
            by_initials.setdefault(initials(ending), []).append(ending)
    pairs = {}
    for area in sorted({a for a in areas if ba.in_prefix(a, prefix)}):
        code = area[len(prefix):].lstrip("-")
        found = by_initials.get(code, [])
        if len(found) != 1:
            raise ValueError(f"polygon {area} pairs with {len(found)} zone endings: {found}")
        pairs[area] = found[0]
    return pairs


def zone_counts(yearly: list[str | None]) -> Counter:
    """Count of loads by zone ending of their ``yearly=`` name; a name of no form counts as ``None``."""
    counts: Counter = Counter()
    for name in yearly:
        ending = names.zone_ending(name) if name else None
        counts[NO_ENDING if ending == "" else ending] += 1
    return counts


def most_common(counts: Counter) -> str | None:
    """The zone ending with the most loads; a tie goes to the first in sorted order."""
    usable = {k: v for k, v in counts.items() if k is not None}
    if not usable:
        return None
    top = max(usable.values())
    return sorted(k for k, v in usable.items() if v == top)[0]


def read_zone_counts(task: dict) -> dict:
    """Zone ending counts of the loads of one substation, from its ``Loads.dss`` files."""
    yearly: list[str | None] = []
    for _, path in sorted(task["loads"].items()):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        yearly.extend(load.yearly for load in dss.parse_loads(text))
    return {"subregion": task["subregion"], "substation": task["substation"], "counts": zone_counts(yearly)}


def zone_agreement(frame: pd.DataFrame, counts: dict[tuple[str, str], Counter],
                   pairs: dict[str, str]) -> pd.DataFrame:
    """One row per frame substation: polygon zone, most common zone ending, agreement.

    ``loads_in_polygon_zone`` counts the loads whose own ending is the
    polygon zone.
    """
    rows = []
    for unit in frame[frame["in_frame"]].itertuples(index=False):
        found = counts.get((unit.subregion, unit.substation), Counter())
        polygon_zone = pairs.get(unit.ba_label)
        top = most_common(found)
        rows.append({
            "subregion": unit.subregion, "substation": unit.substation, "county": unit.county,
            "ba_label": unit.ba_label, "polygon_zone": polygon_zone, "most_common_ending": top,
            "loads": int(unit.loads), "loads_with_top_ending": int(found.get(top, 0)) if top else 0,
            "loads_in_polygon_zone": int(found.get(polygon_zone, 0)),
            "loads_of_no_form": int(found.get(None, 0)),
            "endings": " ".join(f"{k}:{v}" for k, v in sorted(found.items(), key=lambda kv: str(kv[0]))),
            "agree": top is not None and top == polygon_zone,
        })
    return pd.DataFrame(rows)


def zone_table(agreement: pd.DataFrame, endings: list[str], pairs: dict[str, str]) -> pd.DataFrame:
    """Substations by most common zone ending (rows, with no ending) and polygon zone (columns)."""
    rows = [NO_ENDING] + sorted(e for e in endings if e)
    columns = [pairs[a] for a in sorted(pairs, key=lambda a: pairs[a])]
    table = pd.crosstab(agreement["most_common_ending"], agreement["polygon_zone"])
    table = table.reindex(index=rows, columns=columns, fill_value=0)
    table.index.name, table.columns.name = "most_common_ending", None
    return table.reset_index()


def zone_summary(agreement: pd.DataFrame) -> dict:
    loads = agreement["loads"].sum()
    return {
        "substations": len(agreement),
        "substations_agree": int(agreement["agree"].sum()),
        "share_substations_agree": float(agreement["agree"].mean()) if len(agreement) else float("nan"),
        "loads": int(loads),
        "loads_of_agreeing_substations": int(agreement.loc[agreement["agree"], "loads"].sum()),
        "share_loads_agree": float(agreement.loc[agreement["agree"], "loads"].sum() / loads) if loads else float("nan"),
        "share_loads_own_ending_in_polygon_zone": float(agreement["loads_in_polygon_zone"].sum() / loads) if loads else float("nan"),
    }


# ------------------------------------------------------------ county shares


def county_shares(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Substations of each county and how many lie in polygons of ``prefix``."""
    rows = frame[frame["county"].notna()].assign(
        inside=frame["ba_label"].map(lambda value: ba.in_prefix(value, prefix))
    )
    table = rows.groupby(["county_geoid", "county"]).agg(
        substations=("substation", "size"), inside=("inside", "sum"), loads=("loads", "sum"),
        county_status_eia861=("county_status_eia861", "first"),
    ).reset_index()
    table["inside"] = table["inside"].astype(int)
    table["share_inside"] = table["inside"] / table["substations"]
    table["county_key"] = table["county"].map(geo.name_key)
    return table


# ------------------------------------------------------------ 2 PUCT county list

SERVED, PARTLY, NOT_SERVED = "served", "partly", "not served"


def puct_compare(shares: pd.DataFrame, puct: pd.DataFrame) -> pd.DataFrame:
    """Counties where the PUCT status and the polygons disagree.

    ``puct`` has columns ``county`` and ``status`` (``served``, ``partly`` or
    ``not served``). Listed: a county the PUCT marks served where fewer than
    half its substations lie in the polygons, and a county the PUCT marks not
    served where any substation does. Every county is returned with the column
    ``listed``.
    """
    status = dict(zip(puct["county"].map(geo.name_key), puct["status"]))
    table = shares.assign(puct_status=shares["county_key"].map(status))
    table["listed"] = ((table["puct_status"] == SERVED) & (table["share_inside"] < 0.5)) | (
        (table["puct_status"] == NOT_SERVED) & (table["inside"] > 0)
    )
    return table


# ------------------------------------------------------------ 3 EIA-861


def eia861_check(shares: pd.DataFrame, territory: pd.DataFrame, rtos: pd.Series, rto: str) -> pd.DataFrame:
    """Counties of the utilities that report only ``rto`` and of those that report only other RTOs.

    One row per group and county with the group's utilities in it, its
    substations and how many lie in the polygons. Group ``only_<rto>``: a
    utility whose utility data row marks ``rto`` and no other; group
    ``only_other``: a utility that marks at least one RTO and not ``rto``.
    """
    groups = {
        f"only_{rto}": {u for u, found in rtos.items() if found == frozenset({rto})},
        "only_other": {u for u, found in rtos.items() if found and rto not in found},
    }
    by_key = shares.set_index("county_key")
    rows = []
    for group, utilities in groups.items():
        mine = territory[territory["Utility Number"].isin(utilities)]
        for county, part in mine.groupby("County"):
            key = geo.name_key(county)
            found = by_key.loc[key] if key in by_key.index else None
            rows.append({
                "group": group, "county": county,
                "utilities": " | ".join(sorted(set(part["Utility Name"].astype(str)))),
                "substations": int(found["substations"]) if found is not None else 0,
                "inside": int(found["inside"]) if found is not None else 0,
            })
    table = pd.DataFrame(rows, columns=["group", "county", "utilities", "substations", "inside"])
    table["pulls_away"] = (
        ((table["group"] == f"only_{rto}") & (table["inside"] < table["substations"]))
        | ((table["group"] == "only_other") & (table["inside"] > 0))
    )
    return table


def eia861_summary(table: pd.DataFrame) -> pd.DataFrame:
    """Per group: counties, substations in them, and the share inside the polygons (each county once)."""
    return (
        table.groupby("group")
        .agg(counties=("county", "size"), substations=("substations", "sum"), inside=("inside", "sum"),
             counties_pulling_away=("pulls_away", "sum"))
        .assign(share_inside=lambda t: t["inside"] / t["substations"])
        .reset_index()
    )


# ------------------------------------------------------------ 4 edges


def edges(frame: pd.DataFrame, areas: list[ba.Area], prefix: str, limit_km: float = EDGE_KM) -> pd.DataFrame:
    """Distance from each frame substation to the nearest polygon of another balancing area.

    A frame substation lies inside a polygon of ``prefix``; its distance to
    the nearest polygon whose name does not start with ``prefix`` is its
    distance to the edge between its balancing area and another one. A
    coastline or a national border with no polygon beyond it is no such edge.
    """
    rows = []
    for unit in frame[frame["in_frame"]].itertuples(index=False):
        other, km = ba.nearest(unit.centroid_x, unit.centroid_y, areas,
                               keep=lambda area: not area.ba.startswith(prefix))
        rows.append({
            "subregion": unit.subregion, "substation": unit.substation, "county": unit.county,
            "ba_label": unit.ba_label, "feeders": int(unit.feeders), "loads": int(unit.loads),
            "nearest_other_ba": other, "km_to_other_ba": km, "near_edge": km <= limit_km,
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of the frame to check")
    parser.add_argument("--puct-counties", type=Path, default=None,
                        help="CSV with columns county,status (served, partly, not served) from the PUCT layer")
    parser.add_argument("--processes", type=int, default=8, help="reader processes")
    args = parser.parse_args(argv)

    settings = load_sample_settings()
    paths = Paths(load_local(), settings)
    out = paths.texas
    frame = pd.read_parquet(out / f"frame_{args.stamp}.parquet")
    source = settings["geography"]["balancing_area_map"]
    prefix = source["ba_prefix"]
    areas = ba.read_areas(ba.map_files(source, paths.geo)[0])

    # 1 zone agreement
    codes = names.read_zone_codes(find_configs() / ZONE_CODES_NAME)
    endings = sorted(codes)
    pairs = pair_zones([a.ba for a in areas], endings, prefix)
    print("polygon zones:", pairs)
    objects = frame_module.opendss_objects(get_listing(settings, paths, refresh=False), settings)
    members = set(zip(frame.loc[frame["in_frame"], "subregion"], frame.loc[frame["in_frame"], "substation"]))
    tasks = [t for t in frame_module.substation_tasks(objects, paths) if (t["subregion"], t["substation"]) in members]
    with ProcessPoolExecutor(max_workers=max(1, args.processes)) as pool:
        found = list(pool.map(read_zone_counts, tasks, chunksize=16))
    counts = {(f["subregion"], f["substation"]): f["counts"] for f in found}
    agreement = zone_agreement(frame, counts, pairs)
    write_csv(agreement, out / f"trust_zone_agreement_{args.stamp}.csv")
    table = zone_table(agreement, endings, pairs)
    write_csv(table, out / f"trust_zone_table_{args.stamp}.csv")
    print(pd.Series(zone_summary(agreement)).to_string())
    print(table.to_string(index=False))
    worst = agreement[~agreement["agree"]].sort_values(["loads", "subregion", "substation"],
                                                       ascending=[False, True, True], kind="mergesort")
    print(worst.head(20).drop(columns="endings").to_string(index=False))

    # 2 PUCT county list
    shares = county_shares(frame, prefix)
    write_csv(shares, out / f"trust_county_shares_{args.stamp}.csv")
    if args.puct_counties:
        puct = puct_compare(shares, pd.read_csv(args.puct_counties))
        write_csv(puct, out / f"trust_puct_counties_{args.stamp}.csv")
        print(puct[puct["listed"]].to_string(index=False))
    else:
        print("PUCT county list not given; check 2 not run")

    # 3 EIA-861
    balancing = settings["geography"]["balancing_area"]
    eia_zip = fetch_source(balancing["url"], paths.eia)
    territory = geo._sheet(eia_zip, balancing["territory_file"], balancing["territory_sheet"], 0)
    territory = territory[territory["State"] == balancing["state"]]
    rtos = geo.utility_rtos(eia_zip, balancing)
    eia = eia861_check(shares, territory, rtos, balancing["rto"])
    write_csv(eia, out / f"trust_eia861_{args.stamp}.csv")
    print(eia861_summary(eia).to_string(index=False))
    print(eia[eia["pulls_away"]].to_string(index=False))

    # 4 edges
    near = edges(frame, areas, prefix)
    write_csv(near, out / f"trust_edges_{args.stamp}.csv")
    hit = near[near["near_edge"]]
    print(f"frame substations within {EDGE_KM:g} km of another balancing area: {len(hit)} of {len(near)}, "
          f"{int(hit['loads'].sum())} of {int(near['loads'].sum())} loads")
    print(hit.sort_values("km_to_other_ba").to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
