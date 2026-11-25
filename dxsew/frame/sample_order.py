"""Seeded random order of the feeders of each stratum and the list of feeders to match (decision K80).

Run from the repository::

    uv run python -m dxsew.frame.sample_order --stamp <stamp> --features-stamp <stamp> --representatives-stamp <stamp> [--setup <id>]

Reads ``<data_root>/<output_folder>/features_<features stamp>.parquet``
(``dxsew.frame.features``) and ``representatives_<representatives stamp>.parquet``
and ``representatives_groups_<representatives stamp>.parquet``
(``dxsew.frame.representatives``), and the settings blocks ``representatives``
and ``error_curve`` of the setup. Method note: docs/error_curve.md.

1. The feeders in grouping are those of the first step of rule
   ``error_curve.order_rule`` in the groups table.
2. Sample order: for each stratum, its feeders in grouping sorted by name,
   then put in a random order by ``numpy.random.default_rng(seed).permutation``,
   where the seed is ``error_curve.sample_seed`` plus the stratum's position
   (from 0) in the sorted list of strata. A stratum's order depends only on
   its own feeders and that position.
3. Match list: the representatives of rule ``error_curve.order_rule`` at
   n = ``representatives.max_representatives`` (or the last step, when the
   order stops before it), by ``since_n`` then name, role ``candidate``;
   then, per stratum in name order, the first ``error_curve.sample_per_stratum``
   feeders of the sample order (all of them when the stratum has fewer), role
   ``sample``. A feeder in both lists appears once, where it first appears,
   with role ``both``.

Writes, under ``<data_root>/<output_folder>/``: ``sample_order_<stamp>.csv``
(``stratum``, ``rank`` from 1, ``feeder``, ``load_kw``, ``customers``,
``runnable_alone``) and ``match_list_<stamp>.csv`` (``feeder``, ``role``,
``stratum``, ``rank``, empty for candidates, ``since_n``, empty for sample
feeders, ``runnable_alone``).
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np
import pandas as pd

from dxsew import config
from dxsew.config import Paths, load_local, load_runtime, load_sample_settings
from dxsew.frame.representatives import ORDER_RULES, check_settings
from dxsew.frame.sample import write_csv

ORDER_COLUMNS = ["stratum", "rank", "feeder", "load_kw", "customers", "runnable_alone"]
MATCH_COLUMNS = ["feeder", "role", "stratum", "rank", "since_n", "runnable_alone"]
ROLES = ("candidate", "sample", "both")


def check_block(block: dict, representatives_block: dict) -> None:
    """Stop on a setting of ``error_curve`` that the method cannot use."""
    check_settings(representatives_block)
    if block["order_rule"] not in representatives_block["order_rules"] or block["order_rule"] not in ORDER_RULES:
        raise ValueError(
            f"error_curve.order_rule must be one of representatives.order_rules "
            f"{representatives_block['order_rules']}, not {block['order_rule']!r}"
        )
    if int(block["sample_per_stratum"]) < 1 or int(block["bootstrap_draws"]) < 1:
        raise ValueError("sample_per_stratum and bootstrap_draws must be at least 1")
    if int(block["power_flow_cap"]) < 1:
        raise ValueError("power_flow_cap must be at least 1")
    low, high = (float(value) for value in block["band"])
    if not 0 <= low < high <= 1:
        raise ValueError(f"band must be two fractions, low before high, not {block['band']!r}")
    gains = [float(block["stop_gain"])] + [float(gain) for gain in block["report_gains"]]
    if any(gain < 0 for gain in gains):
        raise ValueError("stop_gain and report_gains must not be negative")


def in_grouping_feeders(labels: pd.DataFrame, rule: str) -> pd.DataFrame:
    """``feeder`` and ``stratum`` of every feeder in grouping: the rows of the first step of ``rule``."""
    part = labels[labels["rule"].astype(str) == rule]
    if not len(part):
        raise ValueError(f"the groups table has no rows of rule {rule!r}")
    first = part[part["n"] == part["n"].min()]
    return pd.DataFrame(
        {"feeder": first["feeder"].astype(str).to_numpy(), "stratum": first["stratum"].astype(str).to_numpy()}
    )


def sample_order(feeders: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Seeded random order of each stratum's feeders (module note, step 2).

    ``feeders`` holds ``feeder`` and ``stratum`` and any of ``load_kw``,
    ``customers`` and ``runnable_alone``; those are carried to the output.
    """
    if feeders["feeder"].duplicated().any():
        raise ValueError("a feeder appears twice")
    parts = []
    for position, name in enumerate(sorted(feeders["stratum"].unique())):
        part = feeders[feeders["stratum"] == name].sort_values("feeder", kind="stable").reset_index(drop=True)
        order = np.random.default_rng(int(seed) + position).permutation(len(part))
        part = part.iloc[order].reset_index(drop=True)
        part.insert(0, "rank", np.arange(len(part), dtype=np.int64) + 1)
        parts.append(part)
    table = pd.concat(parts, ignore_index=True)
    columns = [column for column in ORDER_COLUMNS if column in table.columns]
    return table[columns]


def first_of_order(order: pd.DataFrame, per_stratum: int) -> pd.DataFrame:
    """The first ``per_stratum`` feeders of each stratum's order (all of them when it has fewer)."""
    return order[order["rank"] <= int(per_stratum)].reset_index(drop=True)


def candidates(table: pd.DataFrame, rule: str, n: int) -> pd.DataFrame:
    """Representatives of ``rule`` at the step ``n`` (or the last step before it), by ``since_n`` then name."""
    part = table[table["rule"].astype(str) == rule]
    at = part[part["n"] == min(int(n), int(part["n"].max()))]
    found = at[at["representative"].notna()][["representative", "stratum", "since_n"]]
    found = found.rename(columns={"representative": "feeder"})
    return found.sort_values(["since_n", "feeder"], kind="stable").reset_index(drop=True)


def match_list(chosen: pd.DataFrame, sample: pd.DataFrame, runnable: pd.Series) -> pd.DataFrame:
    """Candidates then sample feeders, a feeder in both once with role ``both`` (module note, step 3)."""
    sample_rank = dict(zip(sample["feeder"], sample["rank"]))
    since = dict(zip(chosen["feeder"], chosen["since_n"]))
    rows = []
    for feeder, stratum, since_n in zip(chosen["feeder"], chosen["stratum"], chosen["since_n"]):
        role = "both" if feeder in sample_rank else "candidate"
        rows.append({"feeder": feeder, "role": role, "stratum": stratum, "rank": sample_rank.get(feeder),
                     "since_n": since_n})
    sample = sample.sort_values(["stratum", "rank"], kind="stable")
    for feeder, stratum, rank in zip(sample["feeder"], sample["stratum"], sample["rank"]):
        if feeder in since:
            continue
        rows.append({"feeder": feeder, "role": "sample", "stratum": stratum, "rank": rank, "since_n": None})
    table = pd.DataFrame(rows, columns=MATCH_COLUMNS[:-1])
    for column in ("rank", "since_n"):
        table[column] = table[column].astype("Int64")
    table["runnable_alone"] = runnable.reindex(table["feeder"]).to_numpy()
    return table


def runnable_counts(order: pd.DataFrame, sample: pd.DataFrame, chosen: pd.DataFrame, runnable: pd.Series) -> pd.DataFrame:
    """Per stratum: feeders in grouping, sample feeders and candidates, and how many of each run alone at the pin."""
    rows = []
    for name in sorted(order["stratum"].unique()):
        mine, picked = sample[sample["stratum"] == name], chosen[chosen["stratum"] == name]
        rows.append(
            {
                "stratum": name,
                "feeders": int((order["stratum"] == name).sum()),
                "sample": len(mine),
                "sample_runnable_alone": int(runnable.reindex(mine["feeder"]).fillna(False).astype(bool).sum()),
                "candidates": len(picked),
                "candidates_runnable_alone": int(runnable.reindex(picked["feeder"]).fillna(False).astype(bool).sum()),
            }
        )
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="UTC stamp of the output names")
    parser.add_argument("--features-stamp", required=True, help="stamp of features_<stamp>.parquet")
    parser.add_argument("--representatives-stamp", required=True,
                        help="stamp of representatives_<stamp>.parquet and representatives_groups_<stamp>.parquet")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    started = time.perf_counter()

    settings = load_sample_settings()
    block, representatives_block = settings["error_curve"], settings["representatives"]
    check_block(block, representatives_block)
    pd.set_option("display.width", load_runtime()["print_width"])
    out = Paths(load_local(), settings).output
    rule = block["order_rule"]
    features = pd.read_parquet(out / f"features_{args.features_stamp}.parquet")
    labels = pd.read_parquet(out / f"representatives_groups_{args.representatives_stamp}.parquet",
                             columns=["rule", "n", "feeder", "stratum"])
    table = pd.read_parquet(out / f"representatives_{args.representatives_stamp}.parquet")

    feeders = in_grouping_feeders(labels, rule)
    own = features.set_index("feeder")
    for column in ("load_kw", "customers", "runnable_alone"):
        feeders[column] = own.loc[feeders["feeder"], column].to_numpy()
    order = sample_order(feeders, int(block["sample_seed"]))
    sample = first_of_order(order, int(block["sample_per_stratum"]))
    chosen = candidates(table, rule, int(representatives_block["max_representatives"]))
    runnable = own["runnable_alone"].astype(bool)
    matches = match_list(chosen, sample, runnable)
    write_csv(order, out / f"sample_order_{args.stamp}.csv")
    write_csv(matches, out / f"match_list_{args.stamp}.csv")

    print(f"rule {rule}, seed {block['sample_seed']}, feeders in grouping {len(order)}, candidates {len(chosen)}")
    print(runnable_counts(order, sample, chosen, runnable).to_string(index=False))
    print(matches["role"].value_counts().reindex(list(ROLES), fill_value=0).to_string())
    print(f"written to {out} with stamp {args.stamp} in {time.perf_counter() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
