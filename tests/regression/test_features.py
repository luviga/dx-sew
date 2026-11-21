"""Feature table of a feeder, on small hand-made text files."""

import math

import pandas as pd
import pytest

from dxsew.frame import features
from dxsew.sources import smartds_dss as dss


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
        "regulator_max_low_kv": 1.0,
        "primary_kv_decimals": 3,
        "name_kv": {"1247": 12.47, "25": 25.0, "4": 4.0},
        "missing_transformers_file": "no_transformers",
        "primary_kv_fallback": "name_kv",
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
    # no Lines.dss: line columns empty; no Transformers.dss: no transformer
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2)))
    for column in features.LINE_COLUMNS:
        assert math.isnan(row[column])
    assert row["service_transformers"] == 0
    assert row["regulators"] == 0
    assert row["service_kva"] == 0.0
    assert row["kva_per_kw"] == 0.0
    assert row["primary_kv"] == row["name_kv"] == 12.47
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


SERVICE_1PH = (
    "New Transformer.tr(r:{b}-{b}lv) phases=1 windings=3 wdg=1 bus={b}.{node} Kv={kv} kva=25.0 "
    "wdg=2 bus={b}lv.1.0 Kv=0.12 kva=25.0 wdg=3 bus={b}lv.0.2 Kv=0.12 kva=25.0\n"
)
REGULATOR = (
    "New Transformer.trans_reg_{n}(r:{a}-{b}) phases=3 windings=2 buses=({a}.1.2.3,{b}.1.2.3) "
    "kvs=(12.47, 12.47) kvas=(10000.0, 10000.0)\n"
)


def test_regulators_are_split_from_the_service_transformers(tmp_path):
    transformers = (
        SERVICE_1PH.format(b="x", node="2", kv=7.2)
        + "New Transformer.t2 phases=3 windings=2 buses=(y.1.2.3, ylv.1.2.3) kvs=(12.47, 0.48) kvas=(75, 75)\n"
        + REGULATOR.format(n="a", a="m", b="n")
        + REGULATOR.format(n="b", a="p", b="q")
    )
    row = features.read_feeder(
        task(tmp_path, res("r", 1, 1, 50.0) + com("c", 3, 2, 50.0), transformers=transformers)
    )
    assert row["service_transformers"] == 2
    assert row["regulators"] == 2
    assert row["service_kva"] == 100.0
    assert row["kva_per_kw"] == 1.0
    assert features.is_service(dss.parse_transformers(transformers)[1], 1.0)
    assert not features.is_service(dss.parse_transformers(transformers)[2], 1.0)


def test_a_regulator_is_an_edge_of_0_km(tmp_path):
    lines = (
        "New Line.l1 Units=km Length=1 bus1=a.1.2.3 bus2=b.1.2.3\n"
        "New Line.l2 Units=km Length=2 bus1=bx.1.2.3 bus2=c.1.2.3\n"
    )
    loads = res("r", 1, 1) + com("c", 3, 2)
    alone = features.read_feeder(task(tmp_path, loads, lines=lines, transformers=SERVICE_1PH.format(b="c", node="1", kv=7.2)))
    assert (alone["extent_km"], alone["graph_parts"]) == (2.0, 2)
    bridged = features.read_feeder(
        task(tmp_path, loads, lines=lines, transformers=REGULATOR.format(n="1", a="b", b="bx"))
    )
    assert bridged["regulators"] == 1
    assert (bridged["extent_km"], bridged["graph_parts"]) == (3.0, 1)
    assert bridged["lines"] == 2 and bridged["line_km"] == 3.0
    assert features.extent([("a", "b", 1.0), ("b", "bx", 0.0), ("bx", "c", 2.0)]) == (3.0, 1)


def test_primary_kv_one_phase_node_is_line_to_neutral(tmp_path):
    for node in ("2", "2.0", "0.3"):
        row = features.read_feeder(
            task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=SERVICE_1PH.format(b="x", node=node, kv=7.2))
        )
        assert row["primary_kv"] == round(7.2 * math.sqrt(3), 3) == 12.471
    row = features.read_feeder(
        task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=SERVICE_1PH.format(b="x", node="1", kv=14.434))
    )
    assert row["primary_kv"] == 25.0


def test_primary_kv_two_phase_nodes_is_line_to_line(tmp_path):
    transformers = SERVICE_1PH.format(b="x", node="1.2", kv=12.47)
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=transformers))
    assert row["primary_kv"] == 12.47


def test_primary_kv_three_phase_is_line_to_line_and_regulators_do_not_count(tmp_path):
    transformers = (
        "New Transformer.t3 phases=3 windings=2 buses=(y.1.2.3, ylv.1.2.3) kvs=(4.0, 0.48) kvas=(75, 75)\n"
        + SERVICE_1PH.format(b="x", node="1", kv=2.309)
        + "New Transformer.trans_reg_z phases=3 windings=2 buses=(m.1.2.3, n.1.2.3) kvs=(12.47, 12.47) kvas=(9, 9)\n"
    )
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=transformers))
    # 2.309 times the square root of 3 is 3.999; the three-phase 4.0 is larger; the 12.47 regulator is left out
    assert row["primary_kv"] == 4.0
    assert row["regulators"] == 1


def test_primary_kv_is_empty_without_a_service_transformer(tmp_path):
    # a regulator only: no service transformer gives a voltage, so primary_kv is name_kv
    transformers = REGULATOR.format(n="1", a="a", b="b")
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=transformers))
    assert row["service_transformers"] == 0 and row["regulators"] == 1
    assert row["primary_kv"] == row["name_kv"] == 12.47


def test_primary_kv_stays_empty_without_a_service_transformer_or_a_name_kv(tmp_path):
    for transformers in (None, REGULATOR.format(n="1", a="a", b="b")):
        given = task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=transformers)
        given["substation"] = "p9uhs1_69"
        row = features.read_feeder(given)
        assert math.isnan(row["name_kv"])
        assert math.isnan(row["primary_kv"])


def test_primary_kv_two_decimals_give_one_spelling_per_system_voltage(tmp_path):
    for kv, expected in ((7.2, 12.47), (2.309, 4.0), (14.434, 25.0)):
        given = task(tmp_path, res("r", 1, 1) + com("c", 3, 2), transformers=SERVICE_1PH.format(b="x", node="1", kv=kv))
        given["primary_kv_decimals"] = 2
        assert features.read_feeder(given)["primary_kv"] == expected


def test_unknown_values_of_the_new_settings_raise(tmp_path):
    for key in ("missing_transformers_file", "primary_kv_fallback"):
        given = task(tmp_path, res("r", 1, 1) + com("c", 3, 2))
        given[key] = "something_else"
        with pytest.raises(ValueError, match=key):
            features.read_feeder(given)


def test_name_kv_reads_the_last_part_of_the_substation_name():
    table = {"1247": 12.47, "25": 25.0, "4": 4.0}
    assert features.name_kv("p9uhs1_1247", table) == 12.47
    assert features.name_kv("p1rhs2_25", table) == 25.0
    assert features.name_kv("p3uhs0_4", table) == 4.0
    assert math.isnan(features.name_kv("p3uhs0_69", table))
    assert math.isnan(features.name_kv("p3uhs0", table))


def test_name_kv_is_a_column_of_the_row(tmp_path):
    row = features.read_feeder(task(tmp_path, res("r", 1, 1) + com("c", 3, 2)))
    assert row["name_kv"] == 12.47
