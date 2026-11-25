"""Error curve of the weighted representatives on a random sample of feeders, its band and the stop rule (decision K80).

Run from the repository::

    uv run python -m dxsew.frame.error_curve --stamp <stamp> --features-stamp <stamp> --representatives-stamp <stamp> --curves <feeder_curves parquet> --sample <csv> [--cap <n>] [--on-curves] [--setup <id>]

Reads ``<data_root>/<output_folder>/features_<features stamp>.parquet``
(``dxsew.frame.features``), ``representatives_<representatives stamp>.parquet``
and ``representatives_groups_<representatives stamp>.parquet``
(``dxsew.frame.representatives``), a long table of year curves ``feeder``,
``timestamp``, ``kw`` (``feeder_curves.parquet`` of ``dxsew.curves.year``),
a CSV with a ``feeder`` column naming the sample feeders (when it also has a
``role`` column, as ``match_list_<stamp>.csv`` of ``dxsew.frame.sample_order``
does, only the rows of role ``sample`` or ``both`` count), and the settings
blocks ``representatives`` and ``error_curve`` of the setup. Method note:
docs/error_curve.md.

1. Groups: those of rule ``error_curve.order_rule`` at every step n. With
   ``--on-curves`` the groups come instead from the method on the feeders
   with curves alone (``prepare`` with ``keep`` set to them, ``sequence``
   with the rule and ``representatives.representative_choice``), as the
   head start of ``dxsew.frame.representatives`` does; the representatives
   stamp is then not read.
2. For each stratum s and step n, over the sample feeders S_s of s that are
   in grouping and have a curve: ``true`` is the sum of their own curves;
   ``estimate`` is the sum over f in S_s of the curve of f's group
   representative at step n times f's ``load_kw`` over that
   representative's ``load_kw``. Both are scored with
   ``representatives.curve_errors``.
3. Area figure of ``cv_rmse``, ``error_of_peak`` and ``error_energy``: the
   strata's values averaged with weights equal to each stratum's share of
   the ``load_kw`` of the group rows (over the strata that have sample
   feeders). No curve is scaled to build it.
4. Bootstrap: ``error_curve.bootstrap_draws`` resamples, each drawing the
   sample feeders of each stratum with replacement, as many as the stratum
   has; the draws are made up front from ``numpy.random.default_rng(error_curve.sample_seed)``,
   strata in name order. The area ``cv_rmse`` and ``error_of_peak`` of each
   resample at each n give the band at the fractions ``error_curve.band``.
5. Stop rule: the smallest n, from the first step to the cap
   (``error_curve.power_flow_cap``, or ``--cap``), such that for every
   larger m up to the cap ``cv_rmse(n) - cv_rmse(m)`` and
   ``abs(error_of_peak(n)) - abs(error_of_peak(m))`` are both at most the
   gain, for ``error_curve.stop_gain`` and each of ``error_curve.report_gains``.
6. Missing curves: a sample feeder without a curve is left out and counted;
   the curve runs from the first step to the last step at which every
   representative a sample feeder needs has a curve, and to at most
   ``representatives.max_representatives``.

Writes, under ``<data_root>/<output_folder>/``: ``error_curve_<stamp>.csv``
(``n``, ``scope``, ``sample_feeders``, the four errors, and for the scope
``area`` the band ``cv_rmse_low``, ``cv_rmse_high``, ``peak_low``,
``peak_high``), ``error_stop_<stamp>.csv`` (``gain``, ``n``, ``cv_rmse``,
``error_of_peak``, ``cap``, ``last_n_with_curves``) and
``error_curve_<texts key>_<stamp>.csv``: ``block``, ``key`` and ``sentence``
of every key of the blocks ``representatives`` and ``error_curve`` that ends
in the note suffix (``names.texts_key`` and ``names.note_suffix`` of
``config/runtime.yaml``).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from dxsew import config
from dxsew.config import Paths, load_local, load_runtime, load_sample_settings
from dxsew.frame.representatives import (
    assumption_table,
    curve_errors,
    curve_matrix,
    group_table,
    label_table,
    prepare,
    sequence,
)
from dxsew.frame.sample import write_csv
from dxsew.frame.sample_order import check_block

ASSUMPTION_BLOCKS = ("representatives", "error_curve")
AREA_SCOPE = "area"
ERROR_COLUMNS = ["cv_rmse", "error_at_true_peak", "error_of_peak", "error_energy"]
AREA_FIGURES = ("cv_rmse", "error_of_peak", "error_energy")
BAND_COLUMNS = ["cv_rmse_low", "cv_rmse_high", "peak_low", "peak_high"]
CURVE_COLUMNS = ["n", "scope", "sample_feeders"] + ERROR_COLUMNS + BAND_COLUMNS
STOP_COLUMNS = ["gain", "n", "cv_rmse", "error_of_peak", "cap", "last_n_with_curves"]
SAMPLE_ROLES = ("sample", "both")


def read_sample(path: Path) -> list[str]:
    """Sample feeders of a CSV with a ``feeder`` column, each once, in file order (module note)."""
    table = pd.read_csv(path, dtype={"feeder": str})
    if "role" in table.columns:
        table = table[table["role"].isin(SAMPLE_ROLES)]
    return list(dict.fromkeys(table["feeder"]))


def groups_on_curves(
    features: pd.DataFrame, names: list[str], groups_rule: dict, representatives_block: dict, rule: str,
    average_rule: dict | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group rows and feeder labels of the method on the feeders with curves alone (module note, step 1)."""
    strata, in_grouping, _ = prepare(features, groups_rule, representatives_block["eligible"], keep=set(names))
    result = sequence(strata, rule, int(representatives_block["max_representatives"]),
                      representatives_block["representative_choice"], average_rule)
    return group_table(result["rows"]), label_table(rule, result)


def assignments(labels: pd.DataFrame, table: pd.DataFrame, rule: str, sample: list[str]) -> pd.DataFrame:
    """Per step and sample feeder in grouping: ``n``, ``feeder``, ``stratum``, ``representative``, ``representative_load_kw``."""
    part = labels[(labels["rule"].astype(str) == rule) & labels["feeder"].isin(sample)]
    part = pd.DataFrame(
        {
            "n": part["n"].to_numpy(dtype=np.int64),
            "feeder": part["feeder"].astype(str).to_numpy(),
            "stratum": part["stratum"].astype(str).to_numpy(),
            "group": part["group"].to_numpy(dtype=np.int64),
        }
    )
    rows = table[table["rule"].astype(str) == rule]
    rows = pd.DataFrame(
        {
            "n": rows["n"].to_numpy(dtype=np.int64),
            "stratum": rows["stratum"].astype(str).to_numpy(),
            "group": rows["group"].to_numpy(dtype=np.int64),
            "representative": rows["representative"].to_numpy(dtype=object),
            "representative_load_kw": rows["representative_load_kw"].to_numpy(dtype="float64"),
        }
    )
    merged = part.merge(rows, on=["n", "stratum", "group"], how="left", validate="many_to_one")
    return merged.sort_values(["n", "stratum", "feeder"], kind="stable").reset_index(drop=True)[
        ["n", "feeder", "stratum", "representative", "representative_load_kw"]
    ]


def stratum_shares(table: pd.DataFrame, rule: str) -> pd.Series:
    """Each stratum's share of the ``load_kw`` of the group rows, at the first step of ``rule``."""
    rows = table[table["rule"].astype(str) == rule]
    first = rows[rows["n"] == rows["n"].min()]
    load = first.groupby(first["stratum"].astype(str))["load_kw"].sum().astype("float64")
    return load / load.sum()


def curve_end(assigned: pd.DataFrame, have: set[str], max_n: int) -> dict:
    """Last step of the curve: the step before a needed representative has no curve, at most ``max_n``."""
    steps = sorted(n for n in assigned["n"].unique() if n <= max_n)
    used = assigned[assigned["feeder"].isin(have)]
    found = {"last_n": steps[-1] if steps else None, "ended_by": None, "needed_at": None}
    for position, n in enumerate(steps):
        needed = used.loc[used["n"] == n, "representative"]
        missing = sorted({str(name) for name in needed if not isinstance(name, str) or name not in have})
        if missing:
            found.update(last_n=steps[position - 1] if position else None, ended_by=missing[0], needed_at=int(n))
            break
    return found


def multiplicities(rng: np.random.Generator, count: int, draws: int) -> np.ndarray:
    """``draws`` rows of how often each of ``count`` feeders is drawn, drawing ``count`` with replacement."""
    picks = rng.integers(0, count, size=(draws, count))
    found = np.zeros((draws, count), dtype="float64")
    np.add.at(found, (np.repeat(np.arange(draws), count), picks.ravel()), 1.0)
    return found


def stratum_steps(part: pd.DataFrame, members: list[str], position: dict[str, int], load: pd.Series,
                  steps: list[int]) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per step: curve rows of each member's representative and each member's ``load_kw`` over the representative's."""
    found = []
    own = load.reindex(members).to_numpy(dtype="float64")
    for n in steps:
        at = part[part["n"] == n].set_index("feeder").loc[members]
        rows = np.array([position[name] for name in at["representative"]], dtype=np.int64)
        found.append((rows, own / at["representative_load_kw"].to_numpy(dtype="float64")))
    return found


def point_errors(true: np.ndarray, matrix: np.ndarray, plan: list[tuple[np.ndarray, np.ndarray]]) -> list[dict]:
    """``curve_errors`` of the estimate at every step against the summed sample curve (module note, step 2)."""
    found, previous = [], None
    for rows, factors in plan:
        if previous is None or not (np.array_equal(rows, previous[0]) and np.array_equal(factors, previous[1])):
            estimate = factors @ matrix[rows]
            scores = curve_errors(estimate, true)
        previous = (rows, factors)
        found.append(scores)
    return found


def bootstrap_stratum(
    weights: np.ndarray, true_rows: np.ndarray, matrix: np.ndarray, plan: list[tuple[np.ndarray, np.ndarray]],
    block: int,
) -> tuple[np.ndarray, np.ndarray]:
    """``cv_rmse`` and ``error_of_peak`` of every resample (rows) at every step (columns) of one stratum.

    The estimate of a resample is the sum over the drawn feeders of their
    factor times their representative's curve; from one step to the next
    only the feeders whose representative changed are taken out and put back.
    ``block`` resamples are held in memory at a time; it changes no figure.
    """
    draws = len(weights)
    cv = np.empty((draws, len(plan)), dtype="float64")
    peak = np.empty((draws, len(plan)), dtype="float64")
    for start in range(0, draws, block):
        mine = weights[start:start + block]
        true = mine @ matrix[true_rows]
        mean, top = true.mean(axis=1), true.max(axis=1)
        estimate, previous = None, None
        for step, (rows, factors) in enumerate(plan):
            if estimate is None:
                estimate = (mine * factors) @ matrix[rows]
            else:
                moved = (rows != previous[0]) | (factors != previous[1])
                if not moved.any():
                    cv[start:start + block, step] = cv[start:start + block, step - 1]
                    peak[start:start + block, step] = peak[start:start + block, step - 1]
                    continue
                involved = np.unique(np.concatenate([rows[moved], previous[0][moved]]))
                change = np.zeros((len(mine), len(involved)), dtype="float64")
                for f in np.flatnonzero(moved):
                    change[:, np.searchsorted(involved, rows[f])] += mine[:, f] * factors[f]
                    change[:, np.searchsorted(involved, previous[0][f])] -= mine[:, f] * previous[1][f]
                estimate += change @ matrix[involved]
            previous = (rows, factors)
            cv[start:start + block, step] = np.sqrt(((estimate - true) ** 2).mean(axis=1)) / mean
            peak[start:start + block, step] = estimate.max(axis=1) / top - 1.0
    return cv, peak


def build_curve(
    assigned: pd.DataFrame, load: pd.Series, names: list[str], matrix: np.ndarray, shares: pd.Series, block: dict,
    max_n: int, draws_in_memory: int,
) -> dict:
    """Error curve per stratum and for the area with the band of the area (module note, steps 2 to 4 and 6).

    ``assigned`` is the table of ``assignments``; ``load`` the ``load_kw``
    of every feeder by name; ``names`` and ``matrix`` the curves of
    ``curve_matrix``; ``shares`` of ``stratum_shares``.
    """
    position = {name: row for row, name in enumerate(names)}
    have = set(names)
    sample = assigned.drop_duplicates("feeder")[["feeder", "stratum"]]
    without = sample[~sample["feeder"].isin(have)]
    end = curve_end(assigned, have, max_n)
    steps = sorted(n for n in assigned["n"].unique() if end["last_n"] is not None and n <= end["last_n"])
    strata = sorted(sample.loc[sample["feeder"].isin(have), "stratum"].unique())
    rng = np.random.default_rng(int(block["sample_seed"]))
    draws = int(block["bootstrap_draws"])
    rows, members_of = [], {}
    area_cv = np.zeros((draws, len(steps)), dtype="float64")
    area_peak = np.zeros((draws, len(steps)), dtype="float64")
    total_share = float(shares.reindex(strata).sum())
    point = {}
    for name in strata:
        members = sorted(sample.loc[(sample["stratum"] == name) & sample["feeder"].isin(have), "feeder"])
        members_of[name] = members
        weights = multiplicities(rng, len(members), draws)
        if not steps:
            continue
        part = assigned[(assigned["stratum"] == name) & assigned["feeder"].isin(members)]
        plan = stratum_steps(part, members, position, load, steps)
        true_rows = np.array([position[feeder] for feeder in members], dtype=np.int64)
        point[name] = point_errors(matrix[true_rows].sum(axis=0), matrix, plan)
        cv, peak = bootstrap_stratum(weights, true_rows, matrix, plan, draws_in_memory)
        share = float(shares[name]) / total_share
        area_cv += share * cv
        area_peak += share * peak
    low, high = (float(value) for value in block["band"])
    for step, n in enumerate(steps):
        area = {figure: 0.0 for figure in AREA_FIGURES}
        for name in strata:
            scores = point[name][step]
            rows.append({"n": n, "scope": name, "sample_feeders": len(members_of[name]), **scores})
            for figure in AREA_FIGURES:
                area[figure] += float(shares[name]) / total_share * scores[figure]
        rows.append(
            {
                "n": n,
                "scope": AREA_SCOPE,
                "sample_feeders": sum(len(members) for members in members_of.values()),
                "cv_rmse": area["cv_rmse"],
                "error_at_true_peak": float("nan"),
                "error_of_peak": area["error_of_peak"],
                "error_energy": area["error_energy"],
                "cv_rmse_low": float(np.quantile(area_cv[:, step], low)),
                "cv_rmse_high": float(np.quantile(area_cv[:, step], high)),
                "peak_low": float(np.quantile(area_peak[:, step], low)),
                "peak_high": float(np.quantile(area_peak[:, step], high)),
            }
        )
    curve = pd.DataFrame(rows, columns=CURVE_COLUMNS)
    order = {name: rank for rank, name in enumerate([AREA_SCOPE] + strata)}
    curve = curve.sort_values(["n", "scope"], key=lambda column: column.map(order) if column.name == "scope" else column,
                              kind="stable").reset_index(drop=True)
    counts = {
        "sample_feeders": len(sample),
        "without_curve": len(without),
        "without_curve_by_stratum": without.groupby("stratum").size().to_dict(),
        "strata": len(strata),
    }
    return {"curve": curve, "counts": counts, "end": end, "bootstrap": {"cv_rmse": area_cv, "error_of_peak": area_peak}}


def stop_point(area: pd.DataFrame, gain: float, cap: int) -> dict:
    """Smallest n up to ``cap`` after which no larger n up to ``cap`` gains more than ``gain`` (module note, step 5)."""
    part = area[area["n"] <= cap].sort_values("n")
    if not len(part):
        return {"n": None, "cv_rmse": float("nan"), "error_of_peak": float("nan")}
    cv = part["cv_rmse"].to_numpy(dtype="float64")
    peak = np.abs(part["error_of_peak"].to_numpy(dtype="float64"))
    later_cv = np.append(np.minimum.accumulate(cv[::-1])[::-1][1:], np.inf)
    later_peak = np.append(np.minimum.accumulate(peak[::-1])[::-1][1:], np.inf)
    ok = (cv - later_cv <= gain) & (peak - later_peak <= gain)
    first = int(np.flatnonzero(ok)[0])
    row = part.iloc[first]
    return {"n": int(row["n"]), "cv_rmse": float(row["cv_rmse"]), "error_of_peak": float(row["error_of_peak"])}


def stop_table(curve: pd.DataFrame, block: dict, cap: int, last_n: int | None) -> pd.DataFrame:
    """The stop point for ``stop_gain`` and then each other gain of ``report_gains``."""
    gains = list(dict.fromkeys([float(block["stop_gain"])] + [float(gain) for gain in block["report_gains"]]))
    area = curve[curve["scope"] == AREA_SCOPE]
    rows = [{"gain": gain, **stop_point(area, gain, cap), "cap": int(cap), "last_n_with_curves": last_n}
            for gain in gains]
    table = pd.DataFrame(rows, columns=STOP_COLUMNS)
    for column in ("n", "last_n_with_curves"):
        table[column] = table[column].astype("Int64")
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="stamp of the output names")
    parser.add_argument("--features-stamp", required=True, help="stamp of features_<stamp>.parquet")
    parser.add_argument("--representatives-stamp", default=None,
                        help="stamp of representatives_<stamp>.parquet and representatives_groups_<stamp>.parquet")
    parser.add_argument("--curves", required=True, type=Path, help="long table of year curves: feeder, timestamp, kw")
    parser.add_argument("--sample", required=True, type=Path, help="CSV with a feeder column: the sample feeders")
    parser.add_argument("--cap", type=int, default=None, help="cap of the stop rule; default error_curve.power_flow_cap")
    parser.add_argument("--on-curves", action="store_true",
                        help="groups of the method on the feeders with curves alone (module note, step 1)")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    started = time.perf_counter()

    settings = load_sample_settings()
    block, representatives_block = settings["error_curve"], settings["representatives"]
    check_block(block, representatives_block)
    runtime = load_runtime()
    suffix, texts_key = runtime["names"]["note_suffix"], runtime["names"]["texts_key"]
    pd.set_option("display.width", runtime["print_width"])
    out = Paths(load_local(), settings).output
    rule = block["order_rule"]
    cap = int(block["power_flow_cap"]) if args.cap is None else int(args.cap)
    features = pd.read_parquet(out / f"features_{args.features_stamp}.parquet")
    names, matrix, hours = curve_matrix(pd.read_parquet(args.curves))
    sample = read_sample(args.sample)
    if args.on_curves:
        table, labels = groups_on_curves(features, names, settings["groups"], representatives_block, rule,
                                         settings["representatives_scout"]["average_feeder"])
    else:
        if args.representatives_stamp is None:
            parser.error("--representatives-stamp is needed unless --on-curves is given")
        table = pd.read_parquet(out / f"representatives_{args.representatives_stamp}.parquet")
        labels = pd.read_parquet(out / f"representatives_groups_{args.representatives_stamp}.parquet")
    assigned = assignments(labels, table, rule, sample)
    load = features.set_index("feeder")["load_kw"].astype("float64")
    built = build_curve(assigned, load, names, matrix, stratum_shares(table, rule), block,
                        int(representatives_block["max_representatives"]), int(runtime["bootstrap_block"]))
    end = built["end"]
    stops = stop_table(built["curve"], block, cap, end["last_n"])
    write_csv(built["curve"], out / f"error_curve_{args.stamp}.csv")
    write_csv(stops, out / f"error_stop_{args.stamp}.csv")
    write_csv(assumption_table(settings, ASSUMPTION_BLOCKS, suffix), out / f"error_curve_{texts_key}_{args.stamp}.csv")

    counts = built["counts"]
    in_grouping = assigned["feeder"].nunique()
    print(f"rule {rule}, choice {representatives_block['representative_choice']}, groups "
          f"{'on the feeders with curves' if args.on_curves else args.representatives_stamp}, hours per step {hours}")
    print(f"sample feeders given {len(sample)}, not in grouping {len(sample) - in_grouping}, "
          f"without a curve {counts['without_curve']} {counts['without_curve_by_stratum']}, strata {counts['strata']}")
    print(f"curve from n {built['curve']['n'].min()} to {end['last_n']}; ended by {end['ended_by']} needed at "
          f"{end['needed_at']}")
    print(stops.to_string(index=False))
    print(f"written to {out} with stamp {args.stamp} in {time.perf_counter() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
