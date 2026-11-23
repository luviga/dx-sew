"""Order of the representatives, their groups and their weights by load kW (decision K76).

Run from the repository::

    uv run python -m dxsew.frame.representatives --stamp <stamp> --features-stamp <stamp> [--setup <id>]

Reads ``<data_root>/<output_folder>/features_<features stamp>.parquet``
(``dxsew.frame.features``), the settings blocks ``groups`` and
``representatives`` of the setup and, for the head start,
``curves_<representatives.head_start_curves_stamp>/feeder_curves.parquet``
(``dxsew.curves.year``). Method note: docs/representatives.md.

1. Features, scaling and trees as ``dxsew.frame.groups`` makes them, with its
   own functions: the columns of ``groups.features``, ``frame_z`` over every
   frame feeder, one Ward tree per stratum (``scipy.cluster.hierarchy.linkage``,
   method ``ward``, Euclidean) on the feeders with every feature. Feeders with
   an empty feature are left out and counted.
2. Eligible feeders: every feeder in grouping (``all``) or the
   ``runnable_alone`` ones, and in both cases only those whose ``load_kw`` is
   above 0.
3. For each rule of ``order_rules`` one sequence of steps for the whole
   balancing area. The first step has one group per stratum. Each next step
   splits the group, among all groups of all strata that can still be split,
   whose split into the two children of its tree node gives the largest drop:
   ``ward_drop`` is the within-group sum of squares of the parent minus those
   of its two children on the scaled features; ``load_weighted_drop`` is the
   same with each feeder weighted by its ``load_kw``, each sum taken around
   the load-weighted centre of its group. Ties go to the stratum first in name
   order, then to the lower group number. Stop at ``max_representatives``
   groups or when no group can be split.
4. The representative of a group is the eligible feeder nearest the group's
   centre (unweighted mean of the scaled features; ``groups.nearest``, ties to
   the first name). At a split the child that holds the parent's
   representative keeps it and the parent's group number; the other child
   takes the next free number of its stratum and the eligible feeder nearest
   its own centre. No representative is ever dropped.
5. The weight of a group is its ``load_kw`` over its representative's
   ``load_kw`` (``weight: load_kw_share``).
6. ``explained`` at a step: 1 minus the sum over every group of every stratum
   of the within-group sum of squares, over the sum over the strata of each
   stratum's total sum of squares, on the scaled features;
   ``explained_load_weighted`` the same with feeders weighted by ``load_kw``.
7. Checks with no building run: for each quantity of ``check_quantities``,
   the sum over the groups of weight times the representative's value against
   the sum over the feeders in grouping, for the balancing area and each
   stratum.
8. Head start on the feeders that have year curves: their sizes and ratios,
   and the method on those feeders alone (frame scaling, a tree per stratum on
   them, rule ``ward_drop``), its estimate curve against their summed curve
   for each weight of ``head_start_weights``. They were chosen as group
   centres, so they are not a random sample.

Writes, under ``<data_root>/<output_folder>/``: ``representatives_steps_<stamp>.csv``,
``representatives_<stamp>.parquet``, ``representatives_groups_<stamp>.parquet``,
``representatives_errors_<stamp>.csv``, ``representatives_pin_<stamp>.csv``,
``head_start_feeders_<stamp>.csv``, ``head_start_summary_<stamp>.csv`` and
``head_start_errors_<stamp>.csv``.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import scipy
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.stats import spearmanr

from dxsew import config
from dxsew.config import Paths, load_local, load_runtime, load_sample_settings
from dxsew.frame.groups import _sum_of_squares, feature_values, frame_z, nearest
from dxsew.frame.sample import write_csv, write_parquet

ORDER_RULES = ("ward_drop", "load_weighted_drop")
ELIGIBLE_RULES = ("all", "runnable_alone")
WEIGHT_RULES = ("load_kw_share",)
HEAD_START_WEIGHTS = ("load_kw", "customers")
HEAD_START_RULE = "ward_drop"
ALL_SCOPE = "all"
HEAD_START_NOTE = (
    "first look only: these feeders were chosen as group centres, so they are not a random sample of the frame"
)
GROUP_COLUMNS = [
    "rule", "n", "stratum", "group", "feeders", "customers", "load_kw", "load_share", "representative",
    "representative_load_kw", "weight", "distance", "nearest_feeder", "nearest_distance", "runnable_at_pin",
    "since_n",
]
STEP_COLUMNS = [
    "rule", "n", "stratum", "split_group", "new_group", "drop", "new_representative", "explained",
    "explained_load_weighted",
]


@dataclass
class Stratum:
    """The feeders of one stratum in grouping, in name order, with their tree."""

    name: str
    names: list[str]
    points: np.ndarray
    load_kw: np.ndarray
    customers: np.ndarray
    runnable: np.ndarray
    eligible: np.ndarray
    tree: np.ndarray | None = None
    index: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.index = {name: position for position, name in enumerate(self.names)}
        if self.tree is None and len(self.names) > 1:
            self.tree = linkage(self.points, method="ward", metric="euclidean")

    @property
    def count(self) -> int:
        return len(self.names)

    def root(self) -> int:
        """Node number of the whole stratum: the last merge, or the one leaf."""
        return self.count + len(self.tree) - 1 if self.tree is not None else 0

    def children(self, node: int) -> tuple[int, int] | None:
        """The two nodes a node splits into, or None for a single feeder."""
        if self.tree is None or node < self.count:
            return None
        row = self.tree[node - self.count]
        return int(row[0]), int(row[1])

    def leaves(self, node: int) -> np.ndarray:
        """Positions of the feeders under a node, sorted."""
        stack, found = [node], []
        while stack:
            current = stack.pop()
            pair = self.children(current)
            if pair is None:
                found.append(current)
            else:
                stack.extend(pair)
        return np.sort(np.array(found, dtype=np.int64))


def check_settings(block: dict) -> None:
    """Stop on a setting of ``representatives`` the module does not know."""
    unknown = [rule for rule in block["order_rules"] if rule not in ORDER_RULES]
    if unknown or not block["order_rules"]:
        raise ValueError(f"order_rules must be some of {ORDER_RULES}, not {block['order_rules']!r}")
    if block["eligible"] not in ELIGIBLE_RULES:
        raise ValueError(f"eligible must be one of {ELIGIBLE_RULES}, not {block['eligible']!r}")
    if block["weight"] not in WEIGHT_RULES:
        raise ValueError(f"weight must be one of {WEIGHT_RULES}, not {block['weight']!r}")
    if [weight for weight in block["head_start_weights"] if weight not in HEAD_START_WEIGHTS]:
        raise ValueError(f"head_start_weights must be some of {HEAD_START_WEIGHTS}")
    if int(block["max_representatives"]) < 1:
        raise ValueError("max_representatives must be at least 1")


def weighted_sum_of_squares(points: np.ndarray, weights: np.ndarray) -> float:
    """Sum of weight times squared distance to the weighted centre; 0 when the weights add to 0."""
    total = float(weights.sum())
    if total == 0:
        return 0.0
    centre = (weights[:, None] * points).sum(axis=0) / total
    return float((weights * ((points - centre) ** 2).sum(axis=1)).sum())


def prepare(
    features: pd.DataFrame, groups_rule: dict, eligible_rule: str, keep: set[str] | None = None
) -> tuple[list[Stratum], pd.DataFrame, dict]:
    """Strata in name order, the feature rows in grouping and the counts.

    Scaling runs over every row of ``features`` (the frame); ``keep``, when
    given, then limits the strata to those feeders.
    """
    if groups_rule["scaling"] != "frame_z":
        raise ValueError(f"unknown scaling {groups_rule['scaling']!r}")
    if eligible_rule not in ELIGIBLE_RULES:
        raise ValueError(f"eligible must be one of {ELIGIBLE_RULES}, not {eligible_rule!r}")
    features = features.sort_values("feeder", kind="stable").reset_index(drop=True)
    scaled = frame_z(feature_values(features, groups_rule["features"]))
    complete = scaled.notna().all(axis=1)
    chosen = features["feeder"].isin(keep) if keep is not None else pd.Series(True, index=features.index)
    inside_all = complete & chosen
    strata = []
    for name in sorted(features.loc[inside_all, "stratum"].unique()):
        inside = inside_all & (features["stratum"] == name)
        part = features[inside]
        load_kw = part["load_kw"].to_numpy(dtype="float64")
        runnable = part["runnable_alone"].to_numpy(dtype=bool)
        eligible = load_kw > 0
        if eligible_rule == "runnable_alone":
            eligible = eligible & runnable
        strata.append(
            Stratum(
                name=name,
                names=part["feeder"].tolist(),
                points=scaled[inside].to_numpy(dtype="float64"),
                load_kw=load_kw,
                customers=part["customers"].to_numpy(dtype="float64"),
                runnable=runnable,
                eligible=eligible,
            )
        )
    in_grouping = features[inside_all].reset_index(drop=True)
    counts = {
        "feeders": int(chosen.sum()),
        "in_grouping": int(inside_all.sum()),
        "left_out": int((chosen & ~complete).sum()),
        "zero_load_kw": int((chosen & (features["load_kw"] == 0)).sum()),
        "zero_load_kw_in_grouping": int((inside_all & (features["load_kw"] == 0)).sum()),
        "not_eligible_in_grouping": int(sum((~stratum.eligible).sum() for stratum in strata)),
        "strata": len(strata),
    }
    return strata, in_grouping, counts


@dataclass
class Group:
    """One group of a step: its stratum, number, tree node, members and representative."""

    stratum: int
    number: int
    node: int
    members: np.ndarray
    representative: int | None
    since_n: int
    spread: float
    weighted_spread: float
    drop: float | None
    row: dict


def sequence(strata: list[Stratum], rule: str, cap: int) -> dict:
    """Steps, group rows and feeder labels of one order rule (module note, steps 3 to 6).

    Gives ``steps`` (one dict per step), ``rows`` (one dict per step and
    group), ``labels`` (per step, the group of every feeder in grouping, strata
    in order) and ``names`` and ``strata`` of those feeders.
    """
    if rule not in ORDER_RULES:
        raise ValueError(f"unknown order rule {rule!r}")
    measure = "weighted_spread" if rule == "load_weighted_drop" else "spread"
    total_load = float(sum(stratum.load_kw.sum() for stratum in strata))
    cache: dict[tuple[int, int], dict] = {}

    def node_info(s: int, node: int) -> dict:
        key = (s, node)
        if key not in cache:
            stratum = strata[s]
            members = stratum.leaves(node)
            points = stratum.points[members]
            cache[key] = {
                "members": members,
                "spread": _sum_of_squares(points),
                "weighted_spread": weighted_sum_of_squares(points, stratum.load_kw[members]),
            }
        return cache[key]

    def drop_of(s: int, node: int) -> float | None:
        pair = strata[s].children(node)
        if pair is None:
            return None
        return node_info(s, node)[measure] - sum(node_info(s, child)[measure] for child in pair)

    def make_group(s: int, number: int, node: int, representative: int | None, since_n: int) -> Group:
        stratum = strata[s]
        info = node_info(s, node)
        members, spread, weighted_spread = info["members"], info["spread"], info["weighted_spread"]
        points = stratum.points[members]
        centre = points.mean(axis=0)
        member_names = [stratum.names[position] for position in members]
        eligible = stratum.eligible[members]
        near_name, near_distance = nearest(points, centre, member_names, eligible)
        if representative is None and near_name is not None:
            representative = stratum.index[near_name]
        load_kw = float(stratum.load_kw[members].sum())
        row = {
            "stratum": stratum.name,
            "group": number,
            "feeders": len(members),
            "customers": int(stratum.customers[members].sum()),
            "load_kw": load_kw,
            "load_share": load_kw / total_load if total_load > 0 else float("nan"),
            "representative": None,
            "representative_load_kw": float("nan"),
            "weight": float("nan"),
            "distance": float("nan"),
            "nearest_feeder": near_name,
            "nearest_distance": near_distance,
            "runnable_at_pin": None,
            "since_n": since_n,
        }
        if representative is not None:
            own = float(stratum.load_kw[representative])
            row.update(
                representative=stratum.names[representative],
                representative_load_kw=own,
                weight=load_kw / own,
                distance=float(np.sqrt(((stratum.points[representative] - centre) ** 2).sum())),
                runnable_at_pin=bool(stratum.runnable[representative]),
            )
        group = Group(s, number, node, members, representative, since_n, spread, weighted_spread, None, row)
        group.drop = drop_of(s, node)
        return group

    count = len(strata)
    groups = [make_group(s, 1, stratum.root(), None, count) for s, stratum in enumerate(strata)]
    labels = [np.ones(stratum.count, dtype=np.int32) for stratum in strata]
    total = sum(group.spread for group in groups)
    total_weighted = sum(group.weighted_spread for group in groups)
    steps: list[dict] = []
    rows: list[dict] = []
    label_steps: list[np.ndarray] = []

    def record(split: dict) -> None:
        n = len(groups)
        within = sum(group.spread for group in groups)
        within_weighted = sum(group.weighted_spread for group in groups)
        steps.append(
            {
                "rule": rule,
                "n": n,
                **split,
                "explained": 1.0 - within / total if total > 0 else 0.0,
                "explained_load_weighted": 1.0 - within_weighted / total_weighted if total_weighted > 0 else 0.0,
            }
        )
        rows.extend({"rule": rule, "n": n, **group.row} for group in sorted(groups, key=lambda g: (g.stratum, g.number)))
        label_steps.append(np.concatenate(labels) if labels else np.zeros(0, dtype=np.int32))

    record({"stratum": None, "split_group": None, "new_group": None, "drop": float("nan"), "new_representative": None})
    while len(groups) < cap:
        candidates = [group for group in groups if group.drop is not None]
        if not candidates:
            break
        chosen = min(candidates, key=lambda g: (-g.drop, g.stratum, g.number))
        s = chosen.stratum
        stratum = strata[s]
        left, right = stratum.children(chosen.node)
        left_members, right_members = node_info(s, left)["members"], node_info(s, right)["members"]
        if chosen.representative is not None:
            keep_left = bool((left_members == chosen.representative).any())
        else:
            keep_left = int(left_members.min()) < int(right_members.min())
        keeper_node, other_node = (left, right) if keep_left else (right, left)
        number = max(group.number for group in groups if group.stratum == s) + 1
        n = len(groups) + 1
        since = chosen.since_n if chosen.representative is not None else n
        keeper = make_group(s, chosen.number, keeper_node, chosen.representative, since)
        other = make_group(s, number, other_node, None, n)
        labels[s][other.members] = number
        groups.remove(chosen)
        groups.extend([keeper, other])
        record(
            {
                "stratum": stratum.name,
                "split_group": chosen.number,
                "new_group": number,
                "drop": float(chosen.drop),
                "new_representative": other.row["representative"],
            }
        )
    return {
        "steps": steps,
        "rows": rows,
        "labels": label_steps,
        "names": [name for stratum in strata for name in stratum.names],
        "strata": [stratum.name for stratum in strata for _ in stratum.names],
    }


def group_table(rows: list[dict]) -> pd.DataFrame:
    """Rows of every step and group, with the columns of ``GROUP_COLUMNS``."""
    table = pd.DataFrame(rows, columns=GROUP_COLUMNS)
    for column in ("n", "group", "feeders", "customers", "since_n"):
        table[column] = table[column].astype("int64")
    table["runnable_at_pin"] = table["runnable_at_pin"].astype("boolean")
    return table


def step_table(steps: list[dict]) -> pd.DataFrame:
    """Rows of every step, with the columns of ``STEP_COLUMNS``."""
    table = pd.DataFrame(steps, columns=STEP_COLUMNS)
    for column in ("split_group", "new_group"):
        table[column] = table[column].astype("Int64")
    return table


def label_table(rule: str, result: dict) -> pd.DataFrame:
    """One row per step and feeder in grouping: ``rule``, ``n``, ``feeder``, ``stratum``, ``group``."""
    names, strata = result["names"], result["strata"]
    count, steps = len(names), len(result["labels"])
    feeder_codes = np.tile(np.arange(count, dtype=np.int32), steps)
    stratum_names = sorted(set(strata))
    stratum_codes = np.tile(np.array([stratum_names.index(name) for name in strata], dtype=np.int32), steps)
    return pd.DataFrame(
        {
            "rule": pd.Categorical([rule] * (count * steps), categories=list(ORDER_RULES)),
            "n": np.repeat(np.array([step["n"] for step in result["steps"]], dtype=np.int32), count),
            "feeder": pd.Categorical.from_codes(feeder_codes, categories=names),
            "stratum": pd.Categorical.from_codes(stratum_codes, categories=stratum_names),
            "group": np.concatenate(result["labels"]) if steps else np.zeros(0, dtype=np.int32),
        }
    )


def errors(table: pd.DataFrame, in_grouping: pd.DataFrame, quantities: list[str], area: str) -> pd.DataFrame:
    """Per rule, step, scope and quantity: true sum, weighted estimate and error (module note, step 7).

    ``true`` sums the quantity over the feeders in grouping; ``estimate`` sums
    weight times the representative's value over the groups; ``error`` is
    estimate over true minus 1 (0 when both are 0, empty when only true is).
    """
    values = in_grouping.set_index("feeder")[quantities].astype("float64")
    known = table["representative"].notna()
    part = table[known]
    scaled = values.loc[part["representative"]].to_numpy() * part["weight"].to_numpy()[:, None]
    estimate = pd.DataFrame(scaled, columns=quantities)
    for column in ("rule", "n", "stratum"):
        estimate[column] = part[column].to_numpy()
    by_stratum = estimate.groupby(["rule", "n", "stratum"], sort=True)[quantities].sum()
    by_area = estimate.groupby(["rule", "n"], sort=True)[quantities].sum()
    true_stratum = in_grouping.groupby("stratum")[quantities].sum().astype("float64")
    true_area = true_stratum.sum()
    parts = []
    for (rule, n), row in by_area.iterrows():
        parts.append(pd.DataFrame({"rule": rule, "n": n, "scope": area, "quantity": quantities,
                                   "true": true_area[quantities].to_numpy(), "estimate": row[quantities].to_numpy()}))
    long = by_stratum.reset_index().melt(id_vars=["rule", "n", "stratum"], var_name="quantity", value_name="estimate")
    long["true"] = [float(true_stratum.at[s, q]) for s, q in zip(long["stratum"], long["quantity"])]
    long = long.rename(columns={"stratum": "scope"})
    out = pd.concat(parts + [long[["rule", "n", "scope", "quantity", "true", "estimate"]]], ignore_index=True)
    orders = {
        "rule": list(ORDER_RULES),
        "scope": [area] + sorted(true_stratum.index),
        "quantity": list(quantities),
    }
    keys = pd.DataFrame({column: out[column].map({name: rank for rank, name in enumerate(names)})
                         for column, names in orders.items()})
    keys["n"] = out["n"]
    out = out.loc[keys.sort_values(["rule", "n", "scope", "quantity"], kind="stable").index].reset_index(drop=True)
    true, estimate_values = out["true"].to_numpy(), out["estimate"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = estimate_values / true - 1.0
    out["error"] = np.where(true == 0, np.where(estimate_values == 0, 0.0, np.nan), ratio)
    return out


def pin_table(table: pd.DataFrame, report_steps: list[int]) -> pd.DataFrame:
    """Per rule and reported n: representatives the pinned coupling cannot run alone and their load share."""
    rows = []
    for rule, part in table.groupby("rule", sort=False):
        for n in report_steps:
            at = part[part["n"] == n]
            if not len(at):
                continue
            flagged = at["runnable_at_pin"].fillna(False).eq(False) & at["representative"].notna()
            rows.append(
                {
                    "rule": rule,
                    "n": n,
                    "representatives": int(at["representative"].notna().sum()),
                    "not_runnable_at_pin": int(flagged.sum()),
                    "not_runnable_load_share": float(at.loc[flagged, "load_kw"].sum() / at["load_kw"].sum()),
                }
            )
    return pd.DataFrame(rows, columns=["rule", "n", "representatives", "not_runnable_at_pin", "not_runnable_load_share"])


def build(features: pd.DataFrame, groups_rule: dict, block: dict, area: str) -> dict:
    """Every table of steps 3 to 7 of the module note, and the strata and sequences for the gates."""
    check_settings(block)
    strata, in_grouping, counts = prepare(features, groups_rule, block["eligible"])
    sequences = {rule: sequence(strata, rule, int(block["max_representatives"])) for rule in block["order_rules"]}
    table = pd.concat([group_table(result["rows"]) for result in sequences.values()], ignore_index=True)
    steps = pd.concat([step_table(result["steps"]) for result in sequences.values()], ignore_index=True)
    labels = pd.concat([label_table(rule, result) for rule, result in sequences.items()], ignore_index=True)
    return {
        "strata": strata,
        "in_grouping": in_grouping,
        "counts": counts,
        "sequences": sequences,
        "steps": steps,
        "representatives": table,
        "groups": labels,
        "errors": errors(table, in_grouping, list(block["check_quantities"]), area),
        "pin": pin_table(table, [int(n) for n in block["report_steps"]]),
    }


def curve_matrix(curves: pd.DataFrame) -> tuple[list[str], np.ndarray, float]:
    """Feeders in name order, their curves (one row each, float64) and the hours of one step."""
    wide = curves.pivot(index="timestamp", columns="feeder", values="kw").sort_index()
    if wide.isna().any().any():
        raise ValueError("the feeder curves do not share one set of timestamps")
    steps = pd.Series(wide.index).diff().dropna().unique()
    if len(steps) != 1:
        raise ValueError(f"the curves have {len(steps)} step lengths, not one")
    hours = pd.Timedelta(steps[0]) / pd.Timedelta(hours=1)
    names = sorted(wide.columns)
    return names, wide[names].to_numpy(dtype="float64").T, float(hours)


def head_start_sizing(features: pd.DataFrame, names: list[str], matrix: np.ndarray, hours: float) -> pd.DataFrame:
    """Per feeder with a curve: yearly energy, peak, size and the four ratios."""
    sizes = features.set_index("feeder").loc[names, ["stratum", "load_kw", "customers"]]
    table = pd.DataFrame(
        {
            "feeder": names,
            "stratum": sizes["stratum"].to_numpy(),
            "load_kw": sizes["load_kw"].to_numpy(dtype="float64"),
            "customers": sizes["customers"].to_numpy(dtype="int64"),
            "energy_kwh": matrix.sum(axis=1) * hours,
            "peak_kw": matrix.max(axis=1),
        }
    )
    table["energy_per_load_kw"] = table["energy_kwh"] / table["load_kw"]
    table["energy_per_customer"] = table["energy_kwh"] / table["customers"]
    table["peak_per_load_kw"] = table["peak_kw"] / table["load_kw"]
    table["peak_per_customer"] = table["peak_kw"] / table["customers"]
    table["note"] = HEAD_START_NOTE
    return table


def head_start_summary(sizing: pd.DataFrame) -> pd.DataFrame:
    """For all feeders and per stratum: count, median, quartiles and variation of each ratio; rank correlations."""
    ratios = ["energy_per_load_kw", "energy_per_customer", "peak_per_load_kw", "peak_per_customer"]
    pairs = [("energy_kwh", "load_kw"), ("energy_kwh", "customers"), ("peak_kw", "load_kw"), ("peak_kw", "customers")]
    scopes = [(ALL_SCOPE, sizing)] + [(name, part) for name, part in sizing.groupby("stratum", sort=True)]
    rows = []
    for scope, part in scopes:
        for ratio in ratios:
            values = part[ratio].astype("float64")
            described = values.describe()
            mean = float(values.mean())
            statistics = {
                "median": float(values.median()),
                "q1": float(described[described.index[described.index.str.endswith("%")][0]]),
                "q3": float(described[described.index[described.index.str.endswith("%")][-1]]),
                "cv": float(values.std(ddof=0)) / mean if mean != 0 else float("nan"),
            }
            rows.extend(
                {"scope": scope, "feeders": len(part), "quantity": ratio, "statistic": name, "value": value}
                for name, value in statistics.items()
            )
        for first, second in pairs:
            value = float("nan")
            if len(part) > 1 and part[first].nunique() > 1 and part[second].nunique() > 1:
                value = float(spearmanr(part[first], part[second]).statistic)
            rows.append(
                {"scope": scope, "feeders": len(part), "quantity": f"{first}_vs_{second}", "statistic": "spearman",
                 "value": value}
            )
    table = pd.DataFrame(rows)
    table["note"] = HEAD_START_NOTE
    return table


def curve_errors(estimate: np.ndarray, true: np.ndarray) -> dict[str, float]:
    """CV(RMSE), error at the true peak step, error of the estimate's own peak and error in energy."""
    mean = float(true.mean())
    peak_step = int(np.argmax(true))
    return {
        "cv_rmse": float(np.sqrt(((estimate - true) ** 2).mean())) / mean,
        "error_at_true_peak": float(estimate[peak_step] / true[peak_step]) - 1.0,
        "error_of_peak": float(estimate.max() / true.max()) - 1.0,
        "error_energy": float(estimate.sum() / true.sum()) - 1.0,
    }


def head_start(features: pd.DataFrame, curves: pd.DataFrame, groups_rule: dict, block: dict) -> dict:
    """Sizing, summary and the method on the feeders with curves alone (module note, step 8)."""
    names, matrix, hours = curve_matrix(curves)
    sizing = head_start_sizing(features, names, matrix, hours)
    strata, in_grouping, counts = prepare(features, groups_rule, block["eligible"], keep=set(names))
    result = sequence(strata, HEAD_START_RULE, int(counts["in_grouping"]))
    table = group_table(result["rows"])
    position = {name: row for row, name in enumerate(names)}
    kept = [position[name] for name in in_grouping["feeder"]]
    true = matrix[kept].sum(axis=0)
    own = in_grouping.set_index("feeder")
    rows = []
    for weight in block["head_start_weights"]:
        for n, part in table.groupby("n", sort=True):
            group_size = part["load_kw"] if weight == "load_kw" else part["customers"].astype("float64")
            representative_size = own.loc[part["representative"], weight].to_numpy(dtype="float64")
            factors = group_size.to_numpy(dtype="float64") / representative_size
            estimate = factors @ matrix[[position[name] for name in part["representative"]]]
            rows.append({"weight": weight, "n": int(n), **curve_errors(estimate, true)})
    errors_table = pd.DataFrame(rows)
    errors_table["note"] = HEAD_START_NOTE
    return {
        "feeders": sizing,
        "summary": head_start_summary(sizing),
        "errors": errors_table,
        "counts": counts,
        "hours": hours,
        "steps": step_table(result["steps"]),
    }


def ward_matches_fcluster(result: dict, strata: list[Stratum], cap: int) -> dict[str, bool]:
    """Per stratum: under ``ward_drop``, the groups at each k up to ``cap`` equal those of ``fcluster`` at k."""
    strata_of = np.array(result["strata"], dtype=object)
    found = {}
    for stratum in strata:
        inside = strata_of == stratum.name
        ok = True
        seen = set()
        for labels in result["labels"]:
            mine = labels[inside]
            k = len(np.unique(mine))
            if k in seen or k > cap:
                continue
            seen.add(k)
            if stratum.tree is None:
                theirs = np.ones(stratum.count, dtype=np.int64)
            else:
                theirs = fcluster(stratum.tree, t=k, criterion="maxclust")
            pairs = pd.DataFrame({"a": mine, "b": theirs}).drop_duplicates()
            if not (len(pairs) == pairs["a"].nunique() == pairs["b"].nunique()):
                ok = False
        found[stratum.name] = ok
    return found


def gates(built: dict, groups_rule: dict, block: dict) -> dict[str, bool]:
    """Gates of the run: tree cuts, nesting of representatives, weights, one group per feeder, explained."""
    weight_tolerance, zero = float(block["gate_weight_tolerance"]), float(block["gate_zero_tolerance"])
    table, steps, labels = built["representatives"], built["steps"], built["groups"]
    in_grouping = built["in_grouping"]
    found = {}
    if "ward_drop" in built["sequences"]:
        matches = ward_matches_fcluster(built["sequences"]["ward_drop"], built["strata"], int(groups_rule["cap"]))
        found["ward_equals_fcluster"] = all(matches.values())
    nested = True
    for _, part in table.groupby("rule", sort=False):
        sets = [set(at["representative"].dropna()) for _, at in part.groupby("n", sort=True)]
        nested = nested and all(first <= second for first, second in zip(sets, sets[1:]))
    found["representatives_kept"] = nested
    product = (table["weight"] * table["representative_load_kw"]).groupby(
        [table["rule"], table["n"], table["stratum"]]
    ).sum()
    stratum_load = in_grouping.groupby("stratum")["load_kw"].sum()
    expected = stratum_load.reindex(product.index.get_level_values("stratum")).to_numpy()
    found["weights_add_to_load"] = bool((np.abs(product.to_numpy() / expected - 1.0) <= weight_tolerance).all())
    per_step = labels.groupby(["rule", "n"], observed=True)["feeder"].agg(["size", "nunique"])
    found["one_group_per_feeder"] = bool(
        (per_step["size"] == len(in_grouping)).all() and (per_step["nunique"] == len(in_grouping)).all()
    )
    rising = True
    for _, part in steps.groupby("rule", sort=False):
        part = part.sort_values("n")
        for column in ("explained", "explained_load_weighted"):
            rising = rising and bool((np.diff(part[column].to_numpy()) >= -zero).all())
    found["explained_not_falling"] = rising
    return found


def head_start_gate(errors_table: pd.DataFrame, feeders: int, tolerance: float) -> bool:
    """At n equal to the number of feeders every error of the head start is 0 within the tolerance."""
    last = errors_table[errors_table["n"] == feeders]
    columns = ["cv_rmse", "error_at_true_peak", "error_of_peak", "error_energy"]
    return bool(len(last)) and bool((last[columns].abs() <= tolerance).all().all())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="UTC stamp of the output names")
    parser.add_argument("--features-stamp", required=True, help="stamp of features_<stamp>.parquet")
    config.add_setup_argument(parser)
    args = parser.parse_args(argv)
    config.use_setup(args.setup)
    started = time.perf_counter()

    settings = load_sample_settings()
    block, groups_rule = settings["representatives"], settings["groups"]
    check_settings(block)
    runtime = load_runtime()
    suffix = runtime["names"]["note_suffix"]
    pd.set_option("display.width", runtime["print_width"])
    out = Paths(load_local(), settings).output
    print(f"scipy {scipy.__version__}")
    for key, value in block.items():
        if str(key).endswith(suffix):
            print(f"{key}: {value}")
    features = pd.read_parquet(out / f"features_{args.features_stamp}.parquet")
    built = build(features, groups_rule, block, settings["area"])

    write_csv(built["steps"], out / f"representatives_steps_{args.stamp}.csv")
    write_parquet(built["representatives"], out / f"representatives_{args.stamp}.parquet")
    write_parquet(built["groups"], out / f"representatives_groups_{args.stamp}.parquet")
    write_csv(built["errors"], out / f"representatives_errors_{args.stamp}.csv")
    write_csv(built["pin"], out / f"representatives_pin_{args.stamp}.csv")
    for name, value in built["counts"].items():
        print(f"{name}: {value}")
    for rule, part in built["steps"].groupby("rule", sort=False):
        print(f"{rule}: steps {len(part)}, last n {int(part['n'].max())}")
    print(built["pin"].to_string(index=False))
    results = gates(built, groups_rule, block)

    if block["head_start_curves_stamp"] is not None:
        curves_path = out / f"curves_{block['head_start_curves_stamp']}" / "feeder_curves.parquet"
        started_head = head_start(features, pd.read_parquet(curves_path), groups_rule, block)
        write_csv(started_head["feeders"], out / f"head_start_feeders_{args.stamp}.csv")
        write_csv(started_head["summary"], out / f"head_start_summary_{args.stamp}.csv")
        write_csv(started_head["errors"], out / f"head_start_errors_{args.stamp}.csv")
        head_counts = started_head["counts"]
        print(f"head start: feeders with curves {len(started_head['feeders'])}, in grouping "
              f"{head_counts['in_grouping']}, strata {head_counts['strata']}, hours per step {started_head['hours']}")
        results["head_start_zero_at_full"] = head_start_gate(
            started_head["errors"], head_counts["in_grouping"], float(block["gate_zero_tolerance"])
        )
    for name, ok in results.items():
        print(f"gate {name}: {'pass' if ok else 'FAIL'}")
    print(f"written to {out} with stamp {args.stamp} in {time.perf_counter() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
