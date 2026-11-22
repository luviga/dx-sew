"""Feeder results of the trial's power flows, one row per pass, case, feeder, season and mix (decision K73).

Run from the repository::

    uv run python -m dxsew.runs.results --stamp <stamp> --trial-cases <trial_cases csv> \\
        --batch <pass>=<batch csv> [--batch ...] [--setup <id>]

For each case-season workspace of DeltaE/dss-eulp-coupling (``<runs_root>/<case>/<season>/workspace``):

- the circuit folders of phase 7 are mapped to feeders and mixes by the columns
  ``substation``, ``feeder`` (the circuit, ``circuit_<id>``) and ``scenario`` (the
  mix) of the circuit summary (setup key ``curves.circuit_summary_file``), and
  each circuit to its feeder folder by the workspace's feeder registry
  (``curves.feeder_registry_file``, ``reverse_circuit_map``);
- the day and building-curve ``Peak_kW`` come from the season's row of
  ``curves.circuit_days_file``;
- from the monitor m2 totals (``curves.aggregate_file``, rows ``row_type`` data):
  the highest ``P_3ph (kW)``, the highest ``S_3ph (kVA)`` and P over S at the
  step of highest S; active power is never read from monitor m1 (problem log P6);
- ``feeder_head_kva`` and the highest S over it; the load voltages
  (``curves.voltage_summary_file``); the counts of loads, EVs, PV and storage.

A case-season whose batch recorded a failed call gets one row with its failing
phase. Writes ``<data_root>/<output_folder>/trial_feeders_<stamp>.csv``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from dxsew import config
from dxsew.config import Paths, load_local, load_sample_settings
from dxsew.curves import year
from dxsew.curves.mix import read_exact
from dxsew.frame.sample import write_csv
from dxsew.runs import coupling as coupling_module
from dxsew.runs.batch import FAILED, coupling_order

P_COLUMN, S_COLUMN = "P_3ph (kW)", "S_3ph (kVA)"
DATA_ROW = "data"
VOLTAGE_COLUMNS = ("v_pu_min", "v_pu_max", "n_loads_below", "n_loads_above", "n_load_steps_outside")
COUNT_COLUMNS = ("n_loads", "n_evs", "n_pv", "n_storage")


def circuit_feeders(registry: dict) -> dict[str, str]:
    """``circuit_<id> -> feeder folder`` of a workspace's feeder registry."""
    return dict(registry.get("reverse_circuit_map") or {entry["circuit"]: entry["feeder_name"]
                                                         for entry in registry["feeders"]})


def circuit_rows(summary: pd.DataFrame, aggregate: pd.DataFrame, voltages: pd.DataFrame, days: pd.DataFrame,
                 feeders: dict[str, str], season: str) -> list[dict]:
    """One row per circuit folder of a workspace."""
    data = aggregate[aggregate["row_type"] == DATA_ROW] if "row_type" in aggregate else aggregate
    rows = []
    for item in summary.to_dict("records"):
        folder = item["circuit_folder"]
        feeder = feeders.get(item["feeder"], "")
        mix = item["scenario"]
        steps = data[data["circuit_folder"] == folder]
        row = {"feeder": feeder, "circuit": item["feeder"], "circuit_folder": folder, "mix": mix, "season": season}
        day = days[(days["Feeder"] == feeder) & (days["Season"] == season) & (days["Mix"] == mix)]
        if len(day) == 1:
            row.update(month=int(day["Month"].iloc[0]), day=int(day["Day"].iloc[0]),
                       building_peak_kw=float(day["Peak_kW"].iloc[0]))
        if len(steps):
            p = steps[P_COLUMN].astype(float).to_numpy()
            s = steps[S_COLUMN].astype(float).to_numpy()
            at = int(np.argmax(s))
            row.update(steps=len(steps), max_p_kw=float(p.max()), max_s_kva=float(s.max()),
                       p_over_s_at_max_s=float(p[at] / s[at]) if s[at] else np.nan,
                       max_s_step=int(steps["timestep"].iloc[at]) if "timestep" in steps else at)
        head = float(item["feeder_head_kva"])
        row["feeder_head_kva"] = head
        row["max_s_over_head_kva"] = row.get("max_s_kva", np.nan) / head if head else np.nan
        volt = voltages[voltages["circuit_folder"] == folder]
        for column in VOLTAGE_COLUMNS:
            row[column] = volt[column].iloc[0] if len(volt) else np.nan
        for column in COUNT_COLUMNS:
            row[column] = item.get(column)
        rows.append(row)
    return rows


def workspace_rows(workspace: Path, rule: dict, season: str) -> list[dict]:
    """Rows of one case-season workspace (empty when its phase 8 files are missing)."""
    results = Path(workspace) / rule["results_folder"]
    with (Path(workspace) / rule["feeder_registry_file"]).open(encoding="utf-8") as handle:
        feeders = circuit_feeders(json.load(handle))
    summary = pd.read_csv(results / rule["circuit_summary_file"])
    aggregate = pd.read_csv(results / rule["aggregate_file"])
    voltages = pd.read_csv(results / rule["voltage_summary_file"])
    days = read_exact(Path(workspace) / rule["mix_days_folder"] / rule["circuit_days_file"])
    return circuit_rows(summary, aggregate, voltages, days, feeders, season)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--trial-cases", type=Path, required=True, help="trial_cases_<stamp>.csv of dxsew.runs.trial")
    parser.add_argument("--batch", action="append", default=[], help="<pass>=<batch csv of dxsew.runs.batch>")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    settings = load_sample_settings()
    rule = settings["curves"]
    paths = Paths(load_local(), settings)
    seasons = coupling_order(Path(coupling_module.read_local()["coupling_repo"]))[1]
    batches = {}
    for item in args.batch:
        name, _, path = item.partition("=")
        batches[name] = pd.read_csv(path)
    rows = []
    for case in pd.read_csv(args.trial_cases).to_dict("records"):
        for season in seasons:
            base = {"pass": case["pass"], "peak_day_rule": case["peak_day_rule"], "case_id": case["case_id"],
                    "kind": case["kind"]}
            batch = batches.get(case["pass"], pd.DataFrame(columns=["case", "season", "status", "phase"]))
            failed = batch[(batch["case"] == case["case_id"]) & (batch["season"] == season)
                           & (batch["status"] == FAILED)]
            if len(failed):
                rows.append({**base, "season": season, "failed_phase": failed["phase"].iloc[0]})
                continue
            workspace = year.workspace_of(Path(case["runs_root"]), case["case_id"], season)
            try:
                found = workspace_rows(workspace, rule, season)
            except (OSError, KeyError, ValueError) as error:
                rows.append({**base, "season": season, "failed_phase": "", "note": f"no results: {error}"})
                continue
            rows += [{**base, **row, "failed_phase": ""} for row in found]
    table = pd.DataFrame(rows)
    write_csv(table, paths.output / f"trial_feeders_{args.stamp}.csv")
    print(f"rows: {len(table)}; case-seasons with a failed phase: {int((table['failed_phase'].fillna('') != '').sum())}")
    print(f"written: {paths.output / f'trial_feeders_{args.stamp}.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
