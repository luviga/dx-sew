"""Readers of the OpenDSS text files, on small hand-made inputs."""

import math

from dxsew.texas import dss

LOADS = """\
New Load.load_a_1 conn=wye bus1=busa.1 kV=0.12 Vminpu=0.8 Vmaxpu=1.2 model=1 kW=1.5 kvar=0.3 Phases=1 yearly=res_kw_10-Coast_pu

New Load.load_a_2 conn=wye bus1=busa.2 kV=0.12 Vminpu=0.8 Vmaxpu=1.2 model=1 kW=1.5 kvar=0.3 Phases=1 yearly=res_kw_10-Coast_pu
New Load.load_b conn=wye bus1=BusB.1.2.3 kV=0.48 model=1 kW=20 kvar=4 Phases=3 yearly=com_kw_7-South-Central_pu
New Load.load_c conn=wye bus1=busc.1.2.3 kV=0.48 model=1 kW=2.5 kvar=1 Phases=3 yearly=res_kw_12_pu
New Load.load_d conn=wye bus1=busd.1.2.3 kV=0.48 model=1 kW=1 kvar=0 Phases=3
! New Load.load_off bus1=x kW=99
"""


def test_parse_loads_reads_every_load_line():
    loads = dss.parse_loads(LOADS)
    assert [load.name for load in loads] == ["load_a_1", "load_a_2", "load_b", "load_c", "load_d"]
    assert [load.bus for load in loads] == ["busa", "busa", "busb", "busc", "busd"]
    assert sum(load.kw for load in loads) == 26.5
    assert loads[2].phases == 3
    assert loads[4].yearly is None


def test_customer_count_joins_the_two_objects_of_a_split_phase_customer():
    assert dss.customer_count(dss.parse_loads(LOADS)) == 4


def test_parquet_name_follows_the_load_shape_name():
    assert dss.parquet_name("res_kw_33706-Coast_pu") == "res_33706-Coast.parquet"
    assert dss.parquet_name("com_kw_7-South-Central_pu") == "com_7-South-Central.parquet"
    assert dss.parquet_name("res_kw_12_pu") == "res_12.parquet"
    assert dss.parquet_name("flat") is None
    assert dss.parquet_name("res_kvar_12_pu") is None


def test_load_references_are_unique_and_count_loads_without_one():
    names, without = dss.load_references(dss.parse_loads(LOADS))
    assert names == {"res_10-Coast.parquet", "com_7-South-Central.parquet", "res_12.parquet"}
    assert without == 1


def test_parse_buscoords_reads_blank_and_comma_separated_lines():
    coords = dss.parse_buscoords("BusA -97.5 30.25\nbusb, -97.25, 30.5\n\nbad line\n")
    assert coords == {"busa": (-97.5, 30.25), "busb": (-97.25, 30.5)}


def test_parse_transformer_kva_takes_the_first_winding():
    text = (
        "New Transformer.sub_1 phases=3 windings=2 wdg=1 conn=delta Kv=69.0 kva=16000.0 "
        "%R=0.37 bus=hv wdg=2 conn=wye Kv=12.47 kva=16000.0 %R=0.37 bus=lv XHL=6.7\n"
        "\n"
        "New Transformer.sub_2 phases=3 windings=2 normhkva=44000 wdg=1 Kv=69.0 kva=40000 bus=hv "
        "wdg=2 Kv=12.47 kva=40000 bus=lv2\n"
        "New Transformer.sub_3 phases=3 windings=2 buses=(hv, lv3) kvs=(69, 12.47) kvas=(10000, 10000)\n"
    )
    assert dss.parse_transformer_kva(text) == [16000.0, 40000.0, 10000.0]


LINES = """\
New Line.l(r:a-b) Units=km Length=1.5 bus1=A.1.2.3 bus2=b.1.2.3 phases=3 Linecode=x
New Line.l2 units=kft length=2 bus1=b.1 bus2=c.1 phases=1
New Line.l3 Length=0.25 bus1=c.1 bus2=d.1
New Line.l4 Units=m bus1=d bus2=e
! New Line.off Units=km Length=99 bus1=x bus2=y
"""


def test_parse_lines_reads_buses_length_and_units():
    lines = dss.parse_lines(LINES)
    assert [line.name for line in lines] == ["l(r:a-b)", "l2", "l3", "l4"]
    assert [(line.bus1, line.bus2) for line in lines] == [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")]
    assert [line.length for line in lines] == [1.5, 2.0, 0.25, 0.0]
    assert [line.units for line in lines] == ["km", "kft", None, "m"]


def test_line_length_km_converts_every_opendss_unit():
    def km(units, length=1.0):
        return dss.line_length_km(dss.Line("x", "a", "b", length, units), "km")

    assert km("km", 2.0) == (2.0, True)
    assert km("m", 500.0) == (0.5, True)
    assert km("kft", 1.0) == (0.3048, True)
    assert math.isclose(km("ft", 1000.0)[0], 0.3048)
    assert km("mi") == (1.609344, True)
    assert math.isclose(km("in", 1e5)[0], 2.54)
    assert km("cm", 1e5) == (1.0, True)
    assert km(None, 3.0) == (3.0, False)
    assert km("none", 3.0) == (3.0, False)
    assert dss.line_length_km(dss.Line("x", "a", "b", 1.0, None), "m") == (0.001, False)


def test_parse_transformers_reads_every_winding_voltage():
    text = (
        "New Transformer.t1 phases=1 windings=3 normhkva=11.0 wdg=1 bus=a.2 Kv=7.2 kva=10.0 "
        "wdg=2 bus=alv.1.0 Kv=0.12 kva=10.0 wdg=3 bus=alv.0.2 Kv=0.12 kva=10.0\n"
        "New Transformer.t2 phases=3 windings=2 buses=(b, blv) kvs=(12.47, 0.48) kvas=(75, 75)\n"
        "New Transformer.t3 phases=1 windings=2 bus=c.1 bus=clv.1\n"
    )
    transformers = dss.parse_transformers(text)
    assert [t.name for t in transformers] == ["t1", "t2", "t3"]
    assert transformers[0].kvs == (7.2, 0.12, 0.12)
    assert transformers[1].kvs == (12.47, 0.48)
    assert [t.kva for t in transformers] == [10.0, 75.0, None]


def test_load_sector_is_the_shape_name_before_kw():
    assert dss.load_sector("res_kw_33706-Coast_pu") == "res"
    assert dss.load_sector("COM_kw_7_pu") == "com"
    assert dss.load_sector("flat") is None
    assert dss.load_sector(None) is None


def test_parse_transformers_reads_the_wdg_blocks():
    text = (
        "New Transformer.tr(r:p1-p1lv) phases=1 windings=3 %loadloss=0.8 normhkva=27.5 wdg=1 conn=wye "
        "bus=P1.2 Kv=7.2 kva=25.0 %r=0.27 wdg=2 conn=wye bus=p1lv.1.0 Kv=0.12 kva=25.0 "
        "wdg=3 conn=wye bus=p1lv.0.2 Kv=0.12 kva=25.0 XHL=2.4\n"
    )
    (transformer,) = dss.parse_transformers(text)
    assert transformer.name == "tr(r:p1-p1lv)"
    assert transformer.phases == 1
    assert [w.bus for w in transformer.windings] == ["p1", "p1lv", "p1lv"]
    assert [w.nodes for w in transformer.windings] == [(2,), (1, 0), (0, 2)]
    assert [w.kv for w in transformer.windings] == [7.2, 0.12, 0.12]


def test_parse_transformers_reads_the_buses_and_kvs_arrays():
    text = (
        "New Transformer.trans_reg_x(r:a-b) phases=3 windings=2 buses=(a-bx.1.2.3,a.1.2.3) "
        "kvs=(25.0, 25.0) kvas=(20000.0, 20000.0)\n"
        "New Transformer.t2 phases=1 windings=2 Buses=[c.3 clv.1.2] kVs=[14.434 0.24] kvas=[50 50]\n"
        "New Transformer.t3 windings=2 bus=d kv=4.16\n"
    )
    reg, t2, t3 = dss.parse_transformers(text)
    assert reg.phases == 3
    assert [(w.bus, w.nodes, w.kv) for w in reg.windings] == [("a-bx", (1, 2, 3), 25.0), ("a", (1, 2, 3), 25.0)]
    assert reg.kva == 20000.0
    assert [(w.bus, w.nodes, w.kv) for w in t2.windings] == [("c", (3,), 14.434), ("clv", (1, 2), 0.24)]
    # no phases given; bus and kv before any wdg belong to winding 1; two windings by default
    assert t3.phases is None
    assert [(w.bus, w.nodes, w.kv) for w in t3.windings] == [("d", (), 4.16), ("", (), None)]
