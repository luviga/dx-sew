"""Systematic selection, the size table and the frame readers, on small hand-made inputs."""

import pandas as pd

from dxsew.texas import frame as frame_module
from dxsew.texas import sample
from dxsew.texas.config import Paths

SETTINGS = {
    "release": {
        "bucket_url": "https://example.invalid",
        "prefix": "REL/2018/State/",
        "opendss_tree": "scenarios/base/opendss",
        "not_substations": ["subtransmission"],
        "load_data_folder": "load_data",
        "listing_file": "listing.parquet",
    },
    "staging": {"folder": "STAGED", "parquet_folder": "parquet_data", "opendss_relpath": "base/opendss"},
}


def make_frame() -> pd.DataFrame:
    """Two strata: ``U|Hot`` with ten substations and ``R|Hot`` with two."""
    rows = []
    for i in range(10):
        rows.append(("P1U", "U", f"u{i}", 100 * (10 - i), 2, "U|Hot"))
    rows.append(("P2R", "R", "r0", 50, 1, "R|Hot"))
    rows.append(("P2R", "R", "r1", 70, 3, "R|Hot"))
    rows.append(("P3U", "U", "out", 999, 4, None))
    frame = pd.DataFrame(rows, columns=["subregion", "kind", "substation", "loads", "feeders", "stratum"])
    frame["in_frame"] = frame["stratum"].notna()
    frame["customers"] = frame["loads"] // 2
    frame["opendss_bytes"] = 1_000_000
    return frame


def test_systematic_positions_spread_over_the_ordered_stratum():
    assert sample.systematic_positions(10, 2, 0.0) == [0, 5]
    assert sample.systematic_positions(10, 2, 0.99) == [4, 9]
    assert sample.systematic_positions(7, 3, 0.5) == [1, 3, 5]
    for size in (3, 10, 101):
        for target in (1, 2, 3, 5):
            for start in (0.0, 0.3, 0.999999):
                positions = sample.systematic_positions(size, target, start)
                assert len(set(positions)) == min(size, target)
                assert all(0 <= position < size for position in positions)


def test_a_small_stratum_is_taken_whole():
    assert sample.systematic_positions(2, 5, 0.7) == [0, 1]
    drawn = sample.draw_sample(make_frame(), 3, 555)
    assert sorted(drawn.loc[drawn["stratum"] == "R|Hot", "substation"]) == ["r0", "r1"]
    assert (drawn.loc[drawn["stratum"] == "U|Hot", "stratum_size"] == 10).all()
    assert len(drawn) == 5


def test_same_input_and_seed_give_the_same_sample():
    frame = make_frame()
    first = sample.draw_sample(frame, 2, 555)
    second = sample.draw_sample(frame.sample(frac=1.0, random_state=1).reset_index(drop=True), 2, 555)
    pd.testing.assert_frame_equal(first, second)
    assert "out" not in set(first["substation"])


def test_the_sample_spreads_over_load_counts():
    drawn = sample.draw_sample(make_frame(), 2, 555)
    urban = drawn[drawn["stratum"] == "U|Hot"].sort_values("position_in_stratum")
    assert list(urban["position_in_stratum"].diff().dropna()) == [5]
    ordered = make_frame().query("stratum == 'U|Hot'").sort_values("loads")["substation"].tolist()
    assert urban["substation"].tolist() == [ordered[i] for i in urban["position_in_stratum"]]


def test_size_row_counts_each_parquet_once():
    drawn = sample.draw_sample(make_frame(), 2, 555)
    picked = drawn["substation"].tolist()
    references = pd.DataFrame(
        {
            "subregion": [drawn["subregion"].iloc[0]] * 2 + [drawn["subregion"].iloc[1]] * 2 + ["P3U"],
            "substation": [picked[0], picked[0], picked[1], picked[1], "out"],
            "parquet": ["a.parquet", "b.parquet", "b.parquet", "c.parquet", "z.parquet"],
            "bytes": pd.array([1_000_000_000, 500_000_000, 500_000_000, 250_000_000, 9], dtype="Int64"),
        }
    )
    row = sample.size_row(drawn, references, 2, [10.0, 2.5])
    assert row["substations"] == 4 and row["strata"] == 2
    assert row["feeders"] == int(drawn["feeders"].sum())
    assert row["parquets"] == 3
    assert row["parquet_gb"] == 1.75
    assert row["opendss_gb"] == 0.004
    assert row["hours_at_10_min_per_feeder"] == row["feeders"] * 10 / 60
    assert row["hours_at_2.5_min_per_feeder"] == row["feeders"] * 2.5 / 60


def listing_and_files(tmp_path):
    """A release of one sub-region with two substations, written under ``tmp_path``."""
    tree = "REL/2018/State/P9U/scenarios/base/opendss/"
    files = {
        tree + "Master.dss": "Clear\n",
        tree + "subtransmission/Buscoords.dss": "st_mat -97 30\n",
        tree + "s1/Buscoords.dss": "b1 -97.0 30.0\nb2 -97.5 30.5\nb3 -98.0 31.0\n",
        tree + "s1/Transformers.dss": "New Transformer.t1 wdg=1 Kv=69 kva=16000 bus=h wdg=2 Kv=12.47 kva=16000 bus=l\n",
        tree + "s1/LoadShapes.dss": "\n",
        tree + "s1/s1--f1/Loads.dss": (
            "New Load.load_x_1 bus1=b1.1 kW=1 Phases=1 yearly=res_kw_1-Coast_pu\n"
            "New Load.load_x_2 bus1=b1.2 kW=1 Phases=1 yearly=res_kw_1-Coast_pu\n"
        ),
        tree + "s1/s1--f1/LoadShapes.dss": "\n",
        tree + "s1/s1--f2/Loads.dss": "New Load.load_y bus1=b2.1.2.3 kW=8 Phases=3 yearly=com_kw_2-Coast_pu\n",
        tree + "s1/s1--f2/LoadShapes.dss": "\n",
        tree + "s1/s1--f3/Lines.dss": "\n",
        tree + "s2/Buscoords.dss": "c1 -96.0 29.0\n",
        tree + "s2/Transformers.dss": (
            "New Transformer.t1 wdg=1 Kv=69 kva=10000 bus=h wdg=2 Kv=12.47 kva=10000 bus=l\n"
            "New Transformer.t2 wdg=1 Kv=69 kva=6000 bus=h wdg=2 Kv=12.47 kva=6000 bus=m\n"
        ),
        tree + "s2/s2--f1/Loads.dss": "New Load.load_z bus1=c1.1.2.3 kW=5 Phases=3 yearly=res_kw_1-Coast_pu\n",
        tree + "s2/s2--f1/LoadShapes.dss": "\n",
        "REL/2018/State/load_data/res_1-Coast.parquet": "x" * 40,
        "REL/2018/State/profiles/res_kw_1-Coast_pu.csv": "1\n",
    }
    local = {"data_root": str(tmp_path / "data"), "smart_ds_base": str(tmp_path / "base"), "eulp_metadata_root": str(tmp_path / "meta")}
    paths = Paths(local, SETTINGS)
    for key, text in files.items():
        target = paths.release_file(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    listing = pd.DataFrame({"key": list(files), "size": [len(text) for text in files.values()]})
    return listing, paths


def test_frame_from_a_small_release(tmp_path):
    listing, paths = listing_and_files(tmp_path)
    frame, references = frame_module.build_frame(listing, SETTINGS, paths, workers=1, processes=1)
    assert frame["substation"].tolist() == ["s1", "s2"]
    first, second = frame.iloc[0], frame.iloc[1]
    assert (first["subregion"], first["kind"], first["feeders"]) == ("P9U", "U", 2)
    assert (first["loads"], first["customers"], first["load_kw"]) == (3, 2, 10.0)
    assert (first["centroid_x"], first["centroid_y"]) == (-97.25, 30.25)
    assert (first["transformers"], first["transformer_kva"]) == (1, 16000.0)
    assert (second["transformers"], second["transformer_kva"]) == (2, 16000.0)
    assert first["parquets_missing"] == 1 and second["parquets_missing"] == 0
    assert first["opendss_files"] == 8
    assert sorted(references.loc[references["substation"] == "s1", "parquet"]) == [
        "com_2-Coast.parquet",
        "res_1-Coast.parquet",
    ]
    assert references.loc[references["parquet"] == "res_1-Coast.parquet", "bytes"].tolist() == [40, 40]


def test_listing_counts_by_top_folder_and_subregion(tmp_path):
    listing, _ = listing_and_files(tmp_path)
    by_top, by_subregion = sample.listing_counts(listing, SETTINGS)
    assert by_top.set_index("top_folder")["objects"].to_dict() == {"P9U": 14, "load_data": 1, "profiles": 1}
    assert by_subregion[["subregion", "kind", "substations"]].values.tolist() == [["P9U", "U", 2]]
