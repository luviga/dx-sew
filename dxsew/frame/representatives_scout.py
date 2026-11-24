"""Scout of the representative choice, the order rule and a regulator feature (decision K77).

Run from the repository::

    uv run python -m dxsew.frame.representatives_scout --stamp <stamp> --features-stamp <stamp> --reference-stamp <stamp> [--setup <id>]

Reads ``<data_root>/<output_folder>/features_<features stamp>.parquet``
(``dxsew.frame.features``), the blocks ``groups``, ``representatives`` and
``representatives_scout`` of the setup, the outputs of
``dxsew.frame.representatives`` of the reference stamp
(``representatives_<reference stamp>.parquet``,
``representatives_errors_<reference stamp>.csv`` and
``head_start_errors_<reference stamp>.csv``) and
``curves_<representatives.head_start_curves_stamp>/feeder_curves.parquet``
(``dxsew.curves.year``). Method note: docs/representatives.md.

It measures and changes no default. For every combination of
``representatives.order_rules``, ``representatives_scout.choices`` and
``representatives_scout.feature_sets`` it runs one sequence of
``dxsew.frame.representatives`` from the number of strata to
``representatives.max_representatives``, with ``eligible``, ``weight``,
``check_quantities`` and ``report_steps`` of the block ``representatives``.
A feature set adds its features to ``groups.features`` for its sequences. The
head start runs for every combination with the weight
``representatives_scout.head_start_weight`` only.

Writes, under ``<data_root>/<output_folder>/``, each with the columns
``rule``, ``choice`` and ``feature_set`` first: ``scout_representatives_<stamp>.parquet``,
``scout_errors_<stamp>.csv``, ``scout_steps_<stamp>.csv``,
``scout_head_start_<stamp>.csv``, ``scout_repick_<stamp>.csv`` (report only)
and ``scout_<texts key>_<stamp>.csv`` (``block``, ``key`` and ``sentence`` of
every key of the blocks ``groups``, ``representatives`` and
``representatives_scout`` that ends in the note suffix).
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import pandas as pd
import scipy

from dxsew import config
from dxsew.config import Paths, load_local, load_runtime, load_sample_settings
from dxsew.frame.representatives import (
    DEFAULT_CHOICE,
    HEAD_START_WEIGHTS,
    REPRESENTATIVE_CHOICES,
    assumption_table,
    build_prepared,
    check_average_rule,
    check_settings,
    curve_matrix,
    errors,
    gates,
    method_on_curves,
    prepare,
)
from dxsew.frame.sample import write_csv, write_parquet

SCOUT_BLOCK = "representatives_scout"
SCOUT_ASSUMPTION_BLOCKS = ("groups", "representatives", SCOUT_BLOCK)
KEYS = ["rule", "choice", "feature_set"]
NAME_SEPARATOR = ";"
REFERENCE_RULE = "ward_drop"
HEAD_START_COLUMNS = ["n", "cv_rmse", "error_at_true_peak", "error_of_peak", "error_energy", "note"]


def check_scout(scout: dict, block: dict, groups_rule: dict) -> None:
    """Stop on a setting of ``representatives_scout`` the module does not know."""
    unknown = [choice for choice in scout["choices"] if choice not in REPRESENTATIVE_CHOICES]
    if unknown or not scout["choices"]:
        raise ValueError(f"choices must be some of {REPRESENTATIVE_CHOICES}, not {scout['choices']!r}")
    if not isinstance(scout["feature_sets"], dict) or not scout["feature_sets"]:
        raise ValueError("feature_sets must map a name to a list of features")
    if scout["head_start_weight"] not in HEAD_START_WEIGHTS:
        raise ValueError(f"head_start_weight must be one of {HEAD_START_WEIGHTS}")
    if "average_feeder" in scout["choices"]:
        for extra in scout["feature_sets"].values():
            check_average_rule(scout["average_feeder"], list(groups_rule["features"]) + list(extra or []))
    check_settings(block)


def labelled(table: pd.DataFrame, rule: str, choice: str, feature_set: str) -> pd.DataFrame:
    """The rows of one sequence with ``rule``, ``choice`` and ``feature_set`` first."""
    part = table[table["rule"] == rule].drop(columns="rule").reset_index(drop=True)
    front = pd.DataFrame({"rule": rule, "choice": choice, "feature_set": feature_set}, index=part.index)
    return pd.concat([front, part], axis=1)


def partition_codes(result: dict) -> list[np.ndarray]:
    """Per step, the group of every feeder in grouping as codes in order of first appearance."""
    strata = pd.factorize(pd.Series(result["strata"]))[0].astype(np.int64)
    found = []
    for labels in result["labels"]:
        key = strata * (int(labels.max()) + 1) + labels.astype(np.int64)
        found.append(pd.factorize(key)[0])
    return found


def area_errors(errors_table: pd.DataFrame, area: str, quantities: list[str], prefix: str) -> pd.DataFrame:
    """Per n: one column ``<prefix><quantity>`` with the balancing-area error of each quantity."""
    part = errors_table[errors_table["scope"] == area]
    wide = part.pivot(index="n", columns="quantity", values="error")[quantities]
    wide.columns = [f"{prefix}{quantity}" for quantity in quantities]
    return wide


def weights_over_feeders(table: pd.DataFrame, feeders: int) -> pd.Series:
    """Per n: the sum of the groups' weights over the number of feeders in grouping."""
    return table.groupby("n", sort=True)["weight"].sum() / feeders


def step_rows(
    table: pd.DataFrame,
    steps: pd.DataFrame,
    errors_table: pd.DataFrame,
    feeders: int,
    area: str,
    quantities: list[str],
    reference: dict[int, set[str]],
) -> pd.DataFrame:
    """One row per step of one sequence: explained, weights, strata with one group, overlap and errors."""
    out = steps.set_index("n")[["explained", "explained_load_weighted"]].copy()
    out["weights_over_feeders"] = weights_over_feeders(table, feeders)
    sizes = table.groupby(["n", "stratum"], sort=True).size()
    single = sizes[sizes == 1].reset_index()
    names = single.groupby("n", sort=True)["stratum"].agg(lambda found: NAME_SEPARATOR.join(sorted(found)))
    out["strata_one_group"] = single.groupby("n", sort=True).size().reindex(out.index, fill_value=0)
    out["strata_one_group_names"] = names.reindex(out.index).fillna("")
    chosen = table.dropna(subset=["representative"]).groupby("n", sort=True)["representative"].agg(set)
    out["shared_with_reference"] = [
        len(chosen.get(n, set()) & reference[n]) if n in reference else pd.NA for n in out.index
    ]
    out["shared_with_reference"] = out["shared_with_reference"].astype("Int64")
    out = out.join(area_errors(errors_table, area, quantities, "error_"))
    strata_rows = errors_table[(errors_table["scope"] != area) & errors_table["error"].notna()].copy()
    strata_rows["absolute"] = strata_rows["error"].abs()
    largest = strata_rows.loc[strata_rows.groupby("n", sort=True)["absolute"].idxmax()].set_index("n")
    out["max_abs_stratum_error"] = largest["absolute"]
    out["max_abs_stratum_where"] = largest["scope"] + " " + largest["quantity"]
    return out.reset_index()


def repick_rows(
    table: pd.DataFrame,
    errors_table: pd.DataFrame,
    in_grouping: pd.DataFrame,
    area: str,
    quantities: list[str],
    report_steps: list[int],
) -> pd.DataFrame:
    """Per reported n: groups whose kept representative is not the feeder nearest the target, and both sets' checks."""
    feeders = len(in_grouping)
    own_load = in_grouping.set_index("feeder")["load_kw"].astype("float64")
    kept_errors = area_errors(errors_table, area, quantities, "kept_error_")
    rows = []
    for n in report_steps:
        at = table[table["n"] == n]
        if not len(at):
            continue
        known = at["representative"].notna()
        moved = known & (at["representative"] != at["nearest_feeder"])
        repicked = at[at["nearest_feeder"].notna()].copy()
        repicked["representative"] = repicked["nearest_feeder"]
        repicked["representative_load_kw"] = own_load.loc[repicked["representative"]].to_numpy()
        repicked["weight"] = repicked["load_kw"] / repicked["representative_load_kw"]
        repicked_errors = area_errors(errors(repicked, in_grouping, quantities, area), area, quantities,
                                      "repicked_error_")
        rows.append(
            {
                "n": n,
                "kept_not_target": int(moved.sum()),
                "kept_not_target_load_share": float(at.loc[moved, "load_kw"].sum() / at["load_kw"].sum()),
                "kept_weights_over_feeders": float(at["weight"].sum() / feeders),
                "repicked_weights_over_feeders": float(repicked["weight"].sum() / feeders),
                **kept_errors.loc[n].to_dict(),
                **repicked_errors.loc[n].to_dict(),
            }
        )
    return pd.DataFrame(rows)


def reference_sets(reference: pd.DataFrame) -> dict[int, set[str]]:
    """Per n: the representatives of the reference sequence (rule ``ward_drop`` of the reference run)."""
    part = reference[(reference["rule"] == REFERENCE_RULE) & reference["representative"].notna()]
    return {int(n): set(at["representative"]) for n, at in part.groupby("n", sort=True)}


def same_rows(mine: pd.DataFrame, theirs: pd.DataFrame, tolerance: float) -> bool:
    """Same columns and rows; text and integer columns equal, float columns equal within ``tolerance`` relative."""
    if list(mine.columns) != list(theirs.columns) or len(mine) != len(theirs):
        return False
    mine, theirs = mine.reset_index(drop=True), theirs.reset_index(drop=True)
    for column in mine.columns:
        first, second = mine[column], theirs[column]
        if pd.api.types.is_float_dtype(first) and pd.api.types.is_float_dtype(second):
            a, b = first.to_numpy(dtype="float64"), second.to_numpy(dtype="float64")
            both_empty = np.isnan(a) & np.isnan(b)
            close = np.abs(a - b) <= tolerance * np.maximum(np.abs(a), np.abs(b))
            if not (both_empty | close).all():
                return False
        elif not first.astype(object).where(first.notna(), None).equals(
            second.astype(object).where(second.notna(), None)
        ):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="UTC stamp of the output names")
    parser.add_argument("--features-stamp", required=True, help="stamp of features_<stamp>.parquet")
    parser.add_argument("--reference-stamp", required=True, help="stamp of the outputs of dxsew.frame.representatives")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    started = time.perf_counter()

    settings = load_sample_settings()
    block, groups_rule, scout = settings["representatives"], settings["groups"], settings[SCOUT_BLOCK]
    check_scout(scout, block, groups_rule)
    runtime = load_runtime()
    suffix, texts_key = runtime["names"]["note_suffix"], runtime["names"]["texts_key"]
    pd.set_option("display.width", runtime["print_width"])
    out = Paths(load_local(), settings).output
    area = settings["area"]
    print(f"scipy {scipy.__version__}")
    assumptions = assumption_table(settings, SCOUT_ASSUMPTION_BLOCKS, suffix)
    for row in assumptions.itertuples(index=False):
        print(f"{row.block}.{row.key}: {row.sentence}")

    features = pd.read_parquet(out / f"features_{args.features_stamp}.parquet")
    reference = pd.read_parquet(out / f"representatives_{args.reference_stamp}.parquet")
    reference_errors = pd.read_csv(out / f"representatives_errors_{args.reference_stamp}.csv")
    reference_head = pd.read_csv(out / f"head_start_errors_{args.reference_stamp}.csv")
    shared = reference_sets(reference)
    quantities = list(block["check_quantities"])
    report_steps = [int(n) for n in block["report_steps"]]
    tolerance = float(block["gate_zero_tolerance"])
    rules, choices, feature_sets = list(block["order_rules"]), list(scout["choices"]), dict(scout["feature_sets"])
    average_rule = scout["average_feeder"]

    pieces: dict[tuple[str, str, str], dict] = {}
    results: dict[str, bool] = {}
    partitions_equal = True
    for feature_set, extra in feature_sets.items():
        prepared = prepare(features, groups_rule, block["eligible"], extra_features=list(extra or []))
        _, in_grouping, counts = prepared
        feeders = counts["in_grouping"]
        first_codes: dict[str, list[np.ndarray]] = {}
        for choice in choices:
            tick = time.perf_counter()
            built = build_prepared(prepared, block, area, choice, average_rule)
            for name, ok in gates(built, groups_rule, block).items():
                results[name] = results.get(name, True) and ok
                if not ok:
                    print(f"gate {name} FAIL for {choice}, {feature_set}")
            for rule in rules:
                codes = partition_codes(built["sequences"][rule])
                if rule not in first_codes:
                    first_codes[rule] = codes
                else:
                    same = len(codes) == len(first_codes[rule]) and all(
                        np.array_equal(a, b) for a, b in zip(codes, first_codes[rule])
                    )
                    partitions_equal = partitions_equal and same
                table = built["representatives"][built["representatives"]["rule"] == rule]
                errors_table = built["errors"][built["errors"]["rule"] == rule]
                steps = built["steps"][built["steps"]["rule"] == rule]
                pieces[(rule, choice, feature_set)] = {
                    "representatives": labelled(built["representatives"], rule, choice, feature_set),
                    "errors": labelled(built["errors"], rule, choice, feature_set),
                    "steps": step_rows(table, steps, errors_table, feeders, area, quantities, shared),
                    "repick": repick_rows(table, errors_table, in_grouping, area, quantities, report_steps),
                }
            print(f"{feature_set}, {choice}: feeders in grouping {feeders}, {time.perf_counter() - tick:.0f} s")
            del built
    results["partitions_equal_across_choices"] = partitions_equal

    head_pieces = {}
    curves_stamp = block["head_start_curves_stamp"]
    if curves_stamp is not None:
        names, matrix, _ = curve_matrix(pd.read_parquet(out / f"curves_{curves_stamp}" / "feeder_curves.parquet"))
        for rule in rules:
            for choice in choices:
                for feature_set, extra in feature_sets.items():
                    method = method_on_curves(
                        features, names, matrix, groups_rule, block, rule=rule, weights=[scout["head_start_weight"]],
                        choice=choice, average_rule=average_rule, extra_features=list(extra or []),
                    )
                    part = method["errors"][HEAD_START_COLUMNS].copy()
                    part.insert(0, "feature_set", feature_set)
                    part.insert(0, "choice", choice)
                    part.insert(0, "rule", rule)
                    head_pieces[(rule, choice, feature_set)] = part

    order = [(rule, choice, feature_set) for rule in rules for choice in choices for feature_set in feature_sets]

    def joined(kind: str) -> pd.DataFrame:
        parts = []
        for key in order:
            part = pieces[key][kind]
            if kind in ("steps", "repick"):
                part = part.copy()
                for position, (column, value) in enumerate(zip(KEYS, key)):
                    part.insert(position, column, value)
            parts.append(part)
        return pd.concat(parts, ignore_index=True)

    scout_representatives = joined("representatives")
    scout_errors = joined("errors")
    write_parquet(scout_representatives, out / f"scout_representatives_{args.stamp}.parquet")
    write_csv(scout_errors, out / f"scout_errors_{args.stamp}.csv")
    write_csv(joined("steps"), out / f"scout_steps_{args.stamp}.csv")
    write_csv(joined("repick"), out / f"scout_repick_{args.stamp}.csv")
    if head_pieces:
        write_csv(pd.concat([head_pieces[key] for key in order], ignore_index=True),
                  out / f"scout_head_start_{args.stamp}.csv")
    write_csv(assumptions, out / f"scout_{texts_key}_{args.stamp}.csv")

    first_set = next(iter(feature_sets))
    if DEFAULT_CHOICE in choices:
        for rule in rules:
            mine =scout_representatives[(scout_representatives["rule"] == rule)
                                         & (scout_representatives["choice"] == DEFAULT_CHOICE)
                                         & (scout_representatives["feature_set"] == first_set)]
            theirs = reference[reference["rule"] == rule]
            results[f"reference_representatives_{rule}"] = same_rows(
                mine.drop(columns=["choice", "feature_set"]), theirs, tolerance
            )
            mine_errors = scout_errors[(scout_errors["rule"] == rule) & (scout_errors["choice"] == DEFAULT_CHOICE)
                                       & (scout_errors["feature_set"] == first_set)]
            results[f"reference_errors_{rule}"] = same_rows(
                mine_errors.drop(columns=["choice", "feature_set"]), reference_errors[reference_errors["rule"] == rule],
                tolerance,
            )
        key = (REFERENCE_RULE, DEFAULT_CHOICE, first_set)
        if key in head_pieces:
            theirs = reference_head[reference_head["weight"] == scout["head_start_weight"]].drop(columns="weight")
            results["reference_head_start"] = same_rows(head_pieces[key].drop(columns=KEYS), theirs, tolerance)
    for name, ok in results.items():
        print(f"gate {name}: {'pass' if ok else 'FAIL'}")
    print(f"written to {out} with stamp {args.stamp} in {time.perf_counter() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
