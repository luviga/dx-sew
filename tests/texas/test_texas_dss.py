"""Readers of the OpenDSS text files, on small hand-made inputs."""

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
