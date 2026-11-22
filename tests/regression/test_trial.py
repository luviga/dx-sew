"""Trial settings, substation ratings, the baseline assembly, mix curves and feeder results (decision K73)."""

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dxsew import config
from dxsew.assembly import substations
from dxsew.curves import mix as mix_module
from dxsew.curves import year
from dxsew.runs import results, trial
from dxsew.sources import smartds_dss as dss


@pytest.fixture(autouse=True)
def default_setup():
    config.use_setup(None)


# Trial settings.

def typical_rows():
    return pd.DataFrame([
        {"stratum": "S1", "group": 1, "customers": 50, "placeholder_group": np.nan, "run_feeder": "a",
         "run_feeder_loads": 10, "run_feeder_customers": 5},
        {"stratum": "S1", "group": 2, "customers": 90, "placeholder_group": 1.0, "run_feeder": "a",
         "run_feeder_loads": 10, "run_feeder_customers": 5},
        {"stratum": "S1", "group": 3, "customers": 70, "placeholder_group": np.nan, "run_feeder": "b",
         "run_feeder_loads": 20, "run_feeder_customers": 7},
        {"stratum": "S2", "group": 1, "customers": 30, "placeholder_group": np.nan, "run_feeder": "c",
         "run_feeder_loads": 30, "run_feeder_customers": 3},
        {"stratum": "S2", "group": 2, "customers": 30, "placeholder_group": np.nan, "run_feeder": "d",
         "run_feeder_loads": 40, "run_feeder_customers": 4},
    ])


def test_trial_feeders_skip_placeholder_groups_and_break_ties_by_group():
    chosen = trial.trial_feeders(typical_rows())
    assert chosen["run_feeder"].tolist() == ["b", "c"]
    assert chosen["group"].tolist() == [3, 1]


def test_timing_set_takes_the_loads_nearest_the_median_with_name_order_on_ties():
    loads = {"a": 168, "b": 712, "c": 749, "d": 958, "e": 1588, "f": 2463, "g": 3295}
    assert trial.timing_set(loads, 2) == ["c", "d"]
    assert trial.timing_set({"x": 1, "y": 3, "z": 5, "w": 7}, 2) == ["y", "z"]


def test_pass_pipeline_merges_the_override_with_the_pass_rule():
    setup = config.setup()
    for name, spec in setup["trial"]["passes"].items():
        pipeline = trial.pass_pipeline(setup, name)
        assert pipeline["peak_day_rule"] == spec["peak_day_rule"]
        for key, value in setup["runs"]["pipeline_override"].items():
            assert pipeline[key] == value
    assert "peak_day_rule" not in setup["runs"]["pipeline_override"]


def test_given_rows_copy_the_run_feeders_and_give_the_check_feeders_the_same_dates():
    given = pd.DataFrame({"Feeder": ["a", "a", "b", "b", "z", "z"], "Season": ["winter", "summer"] * 3,
                          "Month": [1, 7] * 3, "Day": [17, 19] * 3})
    rows = trial.given_rows(given, ["a", "b"], ["s--1", "s--2"])
    assert len(rows) == 8 and set(rows["Feeder"]) == {"a", "b", "s--1", "s--2"}
    assert set(map(tuple, rows[rows["Season"] == "winter"][["Month", "Day"]].values)) == {(1, 17)}
    with pytest.raises(ValueError):
        trial.given_rows(given, ["a", "q"], [])
    two_dates = given.assign(Day=[17, 19, 18, 19, 17, 19])
    with pytest.raises(ValueError):
        trial.given_rows(two_dates, ["a", "b"], [])


def test_check_set_unit_prefers_the_runs_staging():
    setup = config.setup()
    sample = pd.DataFrame([{"subregion": "P1U", "substation": "s1", "county": "C", "zone": "Z", "feeders": 2,
                            "loads": 10}])
    staged = pd.DataFrame([{"subregion": "P1U", "substation": "s1", "staged": True}])
    empty = staged.iloc[:0]
    unit, used, which = trial.check_set_unit("P1U", "s1", setup, empty, sample, staged)
    assert which == "staging.folder" and used["staging"]["folder"] == setup["staging"]["folder"]
    unit, used, which = trial.check_set_unit("P1U", "s1", setup, staged, sample, empty)
    assert which == "runs.staging_folder" and used["staging"]["folder"] == setup["runs"]["staging_folder"]
    with pytest.raises(ValueError):
        trial.check_set_unit("P1U", "s1", setup, empty, sample, empty)


# Ratings.

TRANSFORMERS = """New Transformer.t1 phases=3 windings=2 wdg=1 conn=delta Kv=69.0 kva=16000.0 bus=n1 wdg=2 conn=wye Kv=12.47 kva=16000.0 bus=n2
New Transformer.t2 phases=3 windings=2 wdg=1 Kv=69.0 kva=10000.0 bus=n1 wdg=2 Kv=12.47 kva=10000.0 bus=n2
New Transformer.t3 phases=3 windings=2 wdg=1 Kv=69.0 kva=8000.0 bus=n1 wdg=2 Kv=12.47 kva=8000.0 bus=n3
New Transformer.reg phases=3 windings=2 buses=(n2.1.2.3,n4.1.2.3) kvs=(12.47, 12.47) kvas=(10000.0, 10000.0)
"""


def test_classes_parallel_sets_and_rating():
    parsed = dss.parse_transformers(TRANSFORMERS, default_windings=2)
    rows = pd.DataFrame(substations.transformer_rows("P1U", "s1", parsed))
    assert rows["class"].tolist() == ["unit", "unit", "unit", "regulator"]
    assert rows.loc[0, ["high_bus", "low_bus"]].tolist() == ["n1", "n2"]
    rating = substations.substation_rating(rows)
    assert rating["units"] == 3 and rating["regulators"] == 1
    assert rating["parallel_sets"] == 1 and rating["units_in_parallel_sets"] == 2
    assert rating["low_buses"] == 2 and rating["split_buses"] is True
    assert rating["rating_kva"] == 34000.0 and rating["largest_unit_kva"] == 16000.0
    assert substations.rating_class({**rating}) == "split_buses"


def test_one_unit_rating_equals_the_frame_reading_and_no_unit_is_empty():
    one = TRANSFORMERS.splitlines()[0]
    rows = pd.DataFrame(substations.transformer_rows("P1U", "s1", dss.parse_transformers(one, default_windings=2)))
    rating = substations.substation_rating(rows)
    assert rating["rating_kva"] == sum(dss.parse_transformer_kva(one))
    assert substations.rating_class(rating) == "one_unit"
    none = substations.substation_rating(rows.iloc[:0])
    assert none["units"] == 0 and np.isnan(none["rating_kva"])
    assert substations.rating_class(none) == "no_unit"


def test_ratings_table_over_files(tmp_path):
    path = tmp_path / "Transformers.dss"
    path.write_text(TRANSFORMERS, encoding="utf-8")
    frame = pd.DataFrame([{"subregion": "P1U", "substation": "s1", "in_frame": True, "transformers": 4,
                           "transformer_kva": 44000.0},
                          {"subregion": "P1U", "substation": "s2", "in_frame": True, "transformers": 0,
                           "transformer_kva": np.nan},
                          {"subregion": "P1U", "substation": "s3", "in_frame": False, "transformers": 1,
                           "transformer_kva": 1.0}])
    table, rated = substations.ratings(frame, {("P1U", "s1"): path})
    assert len(table) == 4 and len(rated) == 2
    assert rated.loc[0, "rating_kva"] == 34000.0 and not rated.loc[0, "ratings_equal"]
    assert rated.loc[1, "rating_class"] == "no_unit" and not rated.loc[1, "transformers_file"]


# Assembly.

def test_substation_curves_scale_each_feeder_by_customers_over_run_feeder_customers():
    factors = pd.DataFrame([
        {"subregion": "R", "substation": "s1", "feeder": "f1", "stratum": "S", "county": "C", "customers": 10,
         "run_feeder": "a", "run_feeder_customers": 5},
        {"subregion": "R", "substation": "s1", "feeder": "f2", "stratum": "S", "county": "C", "customers": 4,
         "run_feeder": "b", "run_feeder_customers": 2},
        {"subregion": "R", "substation": "s2", "feeder": "f3", "stratum": "S", "county": "C", "customers": 5,
         "run_feeder": "a", "run_feeder_customers": 5},
    ])
    factors["factor"] = factors["customers"] / factors["run_feeder_customers"]
    curves = {"a": np.array([1.0, 3.0, 2.0]), "b": np.array([2.0, 0.0, 1.0])}
    subs, values, order = substations.substation_curves(factors, curves)
    assert order.tolist() == ["a", "b"]
    np.testing.assert_allclose(values[0], 2 * curves["a"] + 2 * curves["b"])
    np.testing.assert_allclose(values[1], curves["a"])
    assert subs["sum_of_feeder_peaks_kw"].tolist() == [2 * 3 + 2 * 2, 3]
    assert subs["customers"].tolist() == [14, 5]


def test_feeder_factors_take_the_k_of_the_typical_table():
    features = pd.DataFrame([{"subregion": "R", "substation": "s1", "feeder": "f1", "stratum": "S", "county": "C",
                              "customers": 10}])
    groups = pd.DataFrame([{"stratum": "S", "k": 1, "feeder": "f1", "group": 1},
                           {"stratum": "S", "k": 2, "feeder": "f1", "group": 2}])
    typical = pd.DataFrame([{"stratum": "S", "k": 2, "group": 1, "run_feeder": "x", "run_feeder_customers": 1},
                            {"stratum": "S", "k": 2, "group": 2, "run_feeder": "y", "run_feeder_customers": 4}])
    table = substations.feeder_factors(features, groups, typical)
    assert table.loc[0, "run_feeder"] == "y" and table.loc[0, "factor"] == 2.5


def test_bands_and_counts():
    shares = pd.Series([0.0, 0.029, 0.03, 0.6, np.nan])
    bands = substations.band_of(shares, [0.03, 0.5]).astype(str).tolist()
    assert bands[:4] == ["[0, 0.03)", "[0, 0.03)", "[0.03, 0.5)", "[0.5, inf)"]
    table = pd.DataFrame({"stratum": ["S", "S", "T"], "rating_class": ["one_unit", "split_buses", "one_unit"],
                          "customers": [10, 20, 30], "load": [1.2, 0.5, 1.01]})
    counts = substations.count_over(table, {"peak": "load"})
    total = counts[(counts["stratum"] == "all") & (counts["rating_class"] == "all")].iloc[0]
    assert total["over_1_peak_substations"] == 2 and total["over_1_peak_customers"] == 40


# Mix curves.

def building(path, energy, stamps, extra=None):
    columns = {"timestamp": pa.array(stamps), "out.electricity.total.energy_consumption": pa.array(energy)}
    columns.update(extra or {})
    pq.write_table(pa.table(columns), path)


def test_mix_curve_follows_the_weights_and_the_list_order(tmp_path):
    stamps = pd.date_range("2018-01-01 00:15", periods=4, freq="15min").to_numpy()
    (tmp_path / "residential").mkdir()
    (tmp_path / "residential_up").mkdir()
    building(tmp_path / "residential" / "1.parquet", np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32), stamps,
             {"out.electricity.heating.energy_consumption": pa.array(np.array([1, 0, 0, 0], dtype=np.float32))})
    building(tmp_path / "residential" / "2.parquet", np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32), stamps)
    building(tmp_path / "residential_up" / "1.parquet", np.array([2.0, 2.0, 2.0, 2.0], dtype=np.float32), stamps)
    lists = {"baseline": pd.DataFrame({"Feeder": ["f", "f"], "Parquet_Folder": ["residential", "residential"],
                                       "Parquet_File": ["1.parquet", "2.parquet"], "REAL_LOAD_COUNT": [3, 1]}),
             "dm": pd.DataFrame({"Feeder": ["f"], "Parquet_Folder": ["residential_up"], "Parquet_File": ["1.parquet"],
                                 "REAL_LOAD_COUNT": [3]})}
    weights = pd.DataFrame({"Feeder": ["f", "f", "f"], "Mix": ["m", "m", "m"], "Group": ["baseline", "baseline", "dm"],
                            "Parquet_File": ["1.parquet", "2.parquet", "1.parquet"], "Weight": [2.0, 1.0, 1.0]})
    curve = mix_module.mix_curve("f", "m", weights, lists, tmp_path)
    expected = (np.array([1, 2, 3, 4], dtype=np.float32) * 4) * 2.0
    expected = expected + (np.array([0.5] * 4, dtype=np.float32) * 4) * 1.0
    expected = expected + (np.array([2.0] * 4, dtype=np.float32) * 4) * 1.0
    np.testing.assert_array_equal(curve["total"], expected)
    assert curve["total"].dtype == np.float32 and curve["rows_used"] == 3
    np.testing.assert_array_equal(curve["end_uses"][("res", "heating")], np.array([8, 0, 0, 0], dtype=np.float32))
    weights.loc[2, "Weight"] = 0.0
    assert mix_module.mix_curve("f", "m", weights, lists, tmp_path)["rows_used"] == 2
    lists["dm"].loc[0, "Parquet_Folder"] = "missing"
    weights.loc[2, "Weight"] = 1.0
    with pytest.raises(mix_module.MixError):
        mix_module.mix_curve("f", "m", weights, lists, tmp_path)


def test_peak_check_on_own_and_given_days():
    rule = config.setup()["curves"]
    days_in_year = 365
    curve = np.ones(days_in_year * year.POINTS_PER_DAY)
    times = pd.date_range(rule["coupling_start"], periods=len(curve), freq=year.STEP)
    curve[np.flatnonzero((times.month == 7) & (times.day == 3))[10]] = 9.0
    curve[np.flatnonzero((times.month == 1) & (times.day == 17))[40]] = 5.0
    days = pd.DataFrame({"Feeder": ["f", "f"], "Season": ["summer", "winter"], "Mix": ["m", "m"],
                         "Month": [7, 1], "Day": [3, 17], "Peak_kW": [9.0, 5.0]})
    assert mix_module.peak_check("f", "m", "summer", curve, days, rule)["passed"]
    assert mix_module.peak_check("f", "m", "winter", curve, days, rule)["passed"]
    days.loc[0, "Day"] = 4
    assert not mix_module.peak_check("f", "m", "summer", curve, days, rule)["passed"]
    given = days.assign(Month=[7, 1], Day=[19, 17], Peak_kW=[1.0, 5.0])
    assert mix_module.peak_check("f", "m", "summer", curve, given, rule, given=(7, 19))["passed"]
    assert mix_module.peak_check("f", "m", "winter", curve, given, rule, given=(1, 17))["passed"]
    assert not mix_module.peak_check("f", "m", "summer", curve, given, rule, given=(7, 3))["passed"]


def test_read_exact_reads_floats_back_as_written(tmp_path):
    value = 0.1 + 0.2
    path = tmp_path / "w.csv"
    pd.DataFrame({"Weight": [value]}).to_csv(path, index=False)
    assert mix_module.read_exact(path)["Weight"].iloc[0] == value


# Feeder results.

def test_circuit_rows_map_circuits_to_feeders_and_mixes():
    summary = pd.DataFrame([{"circuit_folder": "s_circuit_1_baseline", "substation": "s", "feeder": "circuit_1",
                             "scenario": "baseline", "n_loads": 5, "n_evs": 1, "n_storage": 0, "n_pv": 1,
                             "feeder_head_kva": 100.0}])
    aggregate = pd.DataFrame({"circuit_folder": ["s_circuit_1_baseline"] * 3, "row_type": ["data", "data", "total"],
                              "P_3ph (kW)": [30.0, 40.0, 999.0], "S_3ph (kVA)": [50.0, 45.0, 999.0],
                              "timestep": [1, 2, 0]})
    voltages = pd.DataFrame([{"circuit_folder": "s_circuit_1_baseline", "v_pu_min": 0.97, "v_pu_max": 1.02,
                              "n_loads_below": 0, "n_loads_above": 0, "n_load_steps_outside": 0}])
    days = pd.DataFrame([{"Feeder": "s--f1", "Season": "summer", "Mix": "baseline", "Month": 7, "Day": 19,
                          "Peak_kW": 41.0}])
    rows = results.circuit_rows(summary, aggregate, voltages, days, {"circuit_1": "s--f1"}, "summer")
    row = rows[0]
    assert row["feeder"] == "s--f1" and row["mix"] == "baseline" and row["day"] == 19
    assert row["max_p_kw"] == 40.0 and row["max_s_kva"] == 50.0 and row["p_over_s_at_max_s"] == 30.0 / 50.0
    assert row["max_s_over_head_kva"] == 0.5 and row["steps"] == 2 and row["n_pv"] == 1
