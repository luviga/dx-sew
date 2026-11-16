"""Feature table of a feeder, on small hand-made text files."""

import math

import pandas as pd

from dxsew.texas import features


def res(name, phases, shape, kw=1.0, bus="b1"):
    return f"New Load.{name} bus1={bus}.1 kW={kw} Phases={phases} yearly=res_kw_{shape}_pu\n"


def com(name, phases, shape, kw=1.0, bus="b2"):
    return f"New Load.{name} bus1={bus}.1.2.3 kW={kw} Phases={phases} yearly=com_kw_{shape}_pu\n"


def write(folder, name, text):
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def task(folder, loads, lines=None, transformers=None):
    return {
        "subregion": "P9U",
        "substation": "p9uhs1_1247",
        "feeder": "p9uhs1_1247--p9udt1",
        "stratum": "U|Hot-Humid",
        "county": "Somewhere",
        "loads": write(folder, "Loads.dss", loads),
        "lines": write(folder, "Lines.dss", lines) if lines is not None else None,
        "transformers": write(folder, "Transformers.dss", transformers) if transformers is not None else None,
        "default_unit": "km",
    }


def test_two_sweep_extent_on_a_small_tree():
    # a-b 1, b-c 2, b-d 4, d-e 0.5: the longest path is c-b-d-e = 6.5.
    tree = [("a", "b", 1.0), ("b", "c", 2.0), ("b", "d", 4.0), ("d", "e", 0.5)]
    assert features.extent(tree) == (6.5, 1)


def test_extent_counts_parts_and_takes_the_longest():
    lines = [("a", "b", 1.0), ("x", "y", 3.0), ("y", "z", 1.0), ("q", "q", 9.0)]
    assert features.extent(lines) == (4.0, 3)


def test_extent_keeps_the_shortest_parallel_line():
    assert features.extent([("a", "b", 5.0), ("b", "a", 2.0)]) == (2.0, 1)


def test_line_km_converts_units_and_counts_lines_without_units(tmp_path):
    lines = (
        "New Line.l1 Units=km Length=1 bus1=a.1 bus2=b.1\n"
        "New Line.l2 Units=m Length=500 bus1=b.1 bus2=c.1\n"
        "New Line.l3 Units=kft Length=1 bus1=c.1 bus2=d.1\n"
        "New Line.l4 Length=2 bus1=d.1 bus2=e.1\n"
        "New Line.l5 Units=km bus1=e.1 bus2=f.1\n"
    )
    row = features.read_feeder(task(tmp_path, res("r_1", 1, 1) + com("c", 3, 2), lines=lines))
    assert math.isclose(row["line_km"], 1 + 0.5 + 0.3048 + 2)
    assert row["lines"] == 5
    assert row["lines_without_units"] == 1
    assert math.isclose(row["extent_km"], 1 + 0.5 + 0.3048 + 2)
    assert row["graph_parts"] == 1


def test_customers_merge_the_two_copies_of_a_split_phase_customer(tmp_path):
    loads = res("load_x_1", 1, 1, 2.0) + res("load_x_2", 1, 1, 2.0) + res("load_y_1", 1, 1) + com("c", 3, 2, 6.0)
    row = features.read_feeder(task(tmp_path, loads))
    assert row["loads"] == 4
    assert row["customers"] == 3
    assert row["res_customers"] == 2
    assert row["com_customers"] == 1
    assert row["load_kw"] == 11.0
    assert math.isclose(row["com_kw_share"], 6.0 / 11.0)
    assert math.isclose(row["kw_per_customer"], 11.0 / 3)


def test_transformer_features(tmp_path):
    transformers = (
        "New Transformer.t1 phases=1 windings=3 wdg=1 Kv=7.2 kva=25 wdg=2 Kv=0.12 kva=25 wdg=3 Kv=0.12 kva=25\n"
        "New Transformer.t2 phases=3 windings=2 kvs=(12.47, 0.48) kvas=(75, 75)\n"
    )
    row = features.read_feeder(
        task(tmp_path, res("r", 1, 1, 50.0) + com("c", 3, 2, 50.0), transformers=transformers)
    )
    assert row["service_transformers"] == 2
    assert row["service_kva"] == 100.0
    assert row["primary_kv"] == 12.47
    assert row["kva_per_kw"] == 1.0


def test_missing_network_files_leave_their_features_empty(tmp_path):
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2)))
    for column in features.LINE_COLUMNS + features.TRANSFORMER_COLUMNS:
        assert math.isnan(row[column])
    assert row["runnable_alone"]


def test_flag_no_res(tmp_path):
    row = features.read_feeder(task(tmp_path, com("c", 3, 2)))
    assert row["no_res"] and not row["no_com"]
    assert not row["runnable_alone"]


def test_flag_no_com(tmp_path):
    row = features.read_feeder(task(tmp_path, res("r_1", 1, 1) + res("r_2", 1, 1)))
    assert row["no_com"] and not row["no_res"]
    assert not row["runnable_alone"]


def test_flag_shared_shape_on_phases_1_and_3(tmp_path):
    loads = (
        res("r_1", 1, 5) + res("r_2", 1, 5) + res("big", 3, 5)  # shape 5 on 1 and 3 phases
        + com("c1", 3, 7) + com("c2", 3, 7)  # shape 7 on 3 phases only
        + com("c3", 1, 8) + com("c4", 2, 8) + com("c5", 3, 8)  # shape 8 on 1, 2 and 3 phases
    )
    row = features.read_feeder(task(tmp_path, loads))
    assert row["shared_shapes"] == 2
    assert row["shared_pairs_total"] == 2 + 1 + 3
    assert not row["no_res"] and not row["no_com"]
    assert not row["runnable_alone"]


def test_runnable_alone_when_no_flag(tmp_path):
    row = features.read_feeder(task(tmp_path, res("r_1", 1, 1) + res("r_2", 1, 1) + com("c", 3, 2)))
    assert row["shared_shapes"] == 0
    assert row["shared_pairs_total"] == 2
    assert row["runnable_alone"]


def test_feature_table_keeps_the_column_order(tmp_path):
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2)))
    table, other = features.feature_table([row])
    assert list(table.columns) == features.COLUMNS
    assert other == 0


def test_substation_check_flags_a_sum_that_differs():
    table = pd.DataFrame(
        {"subregion": ["P9U"] * 2, "substation": ["s"] * 2, "loads": [2, 3], "customers": [1, 2], "load_kw": [1.0, 2.0]}
    )
    frame = pd.DataFrame(
        {"subregion": ["P9U"], "substation": ["s"], "in_frame": [True], "loads": [5], "customers": [3], "load_kw": [3.0]}
    )
    assert features.substation_check(table, frame).empty
    frame["customers"] = 4
    assert len(features.substation_check(table, frame)) == 1
