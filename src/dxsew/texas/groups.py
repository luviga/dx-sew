"""Nested groups of the frame feeders per stratum, typical feeders and starting weights.

Run from the repository::

    uv run python -m dxsew.texas.groups --stamp <stamp> --features-stamp <stamp> \\
        [--sample-stamp <stamp of sample_<stamp>.csv>]

Reads ``<data_root>/texas/features_<features stamp>.parquet`` (``dxsew.texas.features``)
and the settings block ``groups`` of ``configs/texas_sample.yaml``.

1. Features: the columns of ``groups.features`` in order, ``log(x)`` being the
   natural log of x plus 1. Scaling ``frame_z``: (value minus frame mean)
   over frame standard deviation (population), over every frame feeder that
   has the feature. A feeder with an empty feature is left out of grouping.
2. One Ward tree per stratum on the scaled features, Euclidean
   (``scipy.cluster.hierarchy.linkage``, method ``ward``; J. H. Ward,
   "Hierarchical grouping to optimize an objective function", Journal of the
   American Statistical Association 58 (1963) 236-244,
   https://doi.org/10.1080/01621459.1963.10500845), cut at every k from 1 to
   ``groups.cap`` (or the stratum's feeder count) with ``fcluster``,
   criterion ``maxclust``.
3. Numbering stable between k and k+1: when a group splits, its larger part
   (by feeders; on a tie, the part holding the first feeder in name order)
   keeps its number and every other part takes the next free number.
4. Per stratum and k, ``explained`` = 1 - within-group sum of squares over
   total sum of squares, on the scaled features.
5. Per group: feeders, customers, ``customer_share`` in the stratum (over the
   feeders in grouping), centre (mean scaled features), ``starting_weight``
   (= ``customer_share``), ``flagged_customer_share`` (customers on feeders
   that are not ``runnable_alone`` over the group's customers), the typical
   feeder (the ``runnable_alone`` feeder nearest the centre) with its
   distance, and the nearest feeder of any kind with its distance. Ties go to
   the first feeder in name order.
6. Placeholders (decision K62). A group with no typical feeder takes, as
   ``placeholder_group``, the group of the same stratum and k that has a
   typical feeder and whose centre is nearest its own (Euclidean, scaled
   features; ties to the lower group number); ``placeholder_feeder`` is that
   group's typical feeder and ``placeholder_distance`` the distance from this
   group's centre to it. The three are empty for a group with its own typical
   feeder, and for every group of a stratum and k where no group has a
   typical feeder (such cases are listed).
7. Dial, per target of ``groups.dial_targets`` and stratum: ``k_target``, the
   smallest k whose ``explained`` reaches the target (or the largest k);
   ``k_ceiling``, the smallest k whose largest group holds no more than
   ``groups.max_frame_customer_share`` of the customers of all strata (1 when
   the setting is null, the largest k when no k meets it); ``k``, the larger
   of the two.
8. Chosen dial: with ``groups.dial_choice`` set (one of the targets), every
   group at the dial's k for that target, with the feeder to run: its typical
   feeder, or its placeholder feeder when it has none.

Writes, under ``<data_root>/texas/``: ``groups_<stamp>.parquet`` (stratum, k,
feeder, group), ``group_table_<stamp>.csv``, ``explained_<stamp>.csv``,
``dial_<stamp>.csv`` (with the groups that use a placeholder, their
customer share and the ceiling columns), ``small_groups_<stamp>.csv``, with
``groups.dial_choice`` set ``typical_feeders_<stamp>.csv`` and, with
``--sample-stamp``, ``check_set_candidates_<stamp>.csv``.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import numpy as np
import pandas as pd
import scipy
from scipy.cluster.hierarchy import fcluster, linkage

from .config import Paths, load_local, load_sample_settings
from .sample import write_csv, write_parquet

LOG = re.compile(r"^log\((\w+)\)$")


def feature_name(spec: str) -> str:
    """Column-safe name of a feature: ``log(customers)`` gives ``log_customers``."""
    found = LOG.match(spec)
    return f"log_{found.group(1)}" if found else spec


def feature_values(table: pd.DataFrame, specs: list[str]) -> pd.DataFrame:
    """Raw grouping features; ``log(x)`` is ``ln(x + 1)``. Infinite values become empty."""
    values = {}
    for spec in specs:
        found = LOG.match(spec)
        column = table[found.group(1) if found else spec].astype("float64")
        values[feature_name(spec)] = np.log1p(column) if found else column
    return pd.DataFrame(values, index=table.index).replace([np.inf, -np.inf], np.nan)


def frame_z(values: pd.DataFrame) -> pd.DataFrame:
    """Each column minus its mean over its standard deviation (ddof 0), empty values skipped."""
    return (values - values.mean()) / values.std(ddof=0)


def stable_labels(tree: np.ndarray, cap: int, names: list[str]) -> dict[int, np.ndarray]:
    """Group numbers of every cut k = 1..cap, stable between k and k+1 (module note, step 3)."""
    count = len(names)
    first = np.argsort(np.array(names, dtype=object), kind="stable")
    rank = np.empty(count, dtype=np.int64)
    rank[first] = np.arange(count)
    labels = {1: np.ones(count, dtype=np.int64)}
    previous = labels[1]
    for k in range(2, cap + 1):
        raw = fcluster(tree, t=k, criterion="maxclust")
        current = np.zeros(count, dtype=np.int64)
        next_number = int(previous.max()) + 1
        children: dict[int, list[tuple[int, int, int]]] = {}
        for cluster in np.unique(raw):
            members = np.flatnonzero(raw == cluster)
            parents = np.unique(previous[members])
            if len(parents) != 1:
                raise ValueError(f"cut {k} is not nested in cut {k - 1}")
            children.setdefault(int(parents[0]), []).append(
                (-len(members), int(rank[members].min()), int(cluster))
            )
        for parent in sorted(children):
            for position, (_, _, cluster) in enumerate(sorted(children[parent])):
                if position == 0:
                    number = parent
                else:
                    number = next_number
                    next_number += 1
                current[raw == cluster] = number
        labels[k] = current
        previous = current
    return labels


def _sum_of_squares(points: np.ndarray) -> float:
    points = np.ascontiguousarray(points)
    return float(((points - points.mean(axis=0)) ** 2).sum())


def explained(points: np.ndarray, labels: np.ndarray) -> float:
    """1 - within-group sum of squares over total sum of squares.

    Both sums run on contiguous copies, so one group gives exactly 0.
    """
    total = _sum_of_squares(points)
    if total == 0:
        return 0.0
    within = sum(_sum_of_squares(points[labels == group]) for group in np.unique(labels))
    return 1.0 - within / total


def nearest(points: np.ndarray, centre: np.ndarray, names: list[str], allowed: np.ndarray) -> tuple[str | None, float]:
    """Name and distance of the allowed point nearest the centre; ties to the first name."""
    if not allowed.any():
        return None, float("nan")
    distance = np.sqrt(((points - centre) ** 2).sum(axis=1))
    best = float(distance[allowed].min())
    chosen = min(name for name, d, ok in zip(names, distance, allowed) if ok and d == best)
    return chosen, best


def placeholders(
    centres: dict[int, np.ndarray], typical: dict[int, str | None], points: dict[str, np.ndarray]
) -> dict[int, tuple[int, str, float]]:
    """Placeholder of every group without a typical feeder (module note, step 6).

    ``centres`` and ``typical`` are keyed by group number, ``points`` by
    feeder name. Gives ``group -> (placeholder group, placeholder feeder,
    distance from the group's centre to that feeder)``. A group with its own
    typical feeder is not in the result; the result is empty when no group
    has a typical feeder.
    """
    donors = sorted(group for group, feeder in typical.items() if feeder is not None)
    found: dict[int, tuple[int, str, float]] = {}
    if not donors:
        return found
    for group in sorted(centres):
        if typical.get(group) is not None:
            continue
        distance = {donor: float(np.sqrt(((centres[donor] - centres[group]) ** 2).sum())) for donor in donors}
        best = min(distance.values())
        donor = min(number for number, value in distance.items() if value == best)
        feeder = typical[donor]
        found[group] = (donor, feeder, float(np.sqrt(((points[feeder] - centres[group]) ** 2).sum())))
    return found


def nesting_ok(labels: dict[int, np.ndarray]) -> bool:
    """Every group at k+1 lies inside one group at k, for every k below the last."""
    ks = sorted(labels)
    for k in ks[:-1]:
        pairs = pd.DataFrame({"child": labels[k + 1], "parent": labels[k]}).drop_duplicates()
        if pairs["child"].duplicated().any():
            return False
    return True


def group_stratum(
    stratum: str, part: pd.DataFrame, points: np.ndarray, features: list[str], cap: int
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, bool]:
    """Long labels, group rows and explained rows of one stratum, and its nesting result."""
    names = part["feeder"].tolist()
    cap = min(cap, len(names))
    if len(names) > 1:
        tree = linkage(points, method="ward", metric="euclidean")
        labels = stable_labels(tree, cap, names)
    else:
        labels = {1: np.ones(len(names), dtype=np.int64)}
    customers = part["customers"].to_numpy(dtype="float64")
    runnable = part["runnable_alone"].to_numpy(dtype=bool)
    stratum_customers = customers.sum()
    point_of = dict(zip(names, points))
    long_parts, group_rows, explained_rows = [], [], []
    for k, label in labels.items():
        centres: dict[int, np.ndarray] = {}
        typical_of: dict[int, str | None] = {}
        rows_at_k = []
        long_parts.append(pd.DataFrame({"stratum": stratum, "k": k, "feeder": names, "group": label}))
        explained_rows.append(
            {"stratum": stratum, "k": k, "groups": int(len(np.unique(label))), "explained": explained(points, label)}
        )
        for group in np.unique(label):
            inside = label == group
            centre = points[inside].mean(axis=0)
            group_names = [name for name, keep in zip(names, inside) if keep]
            group_customers = customers[inside].sum()
            typical, typical_distance = nearest(points[inside], centre, group_names, runnable[inside])
            any_feeder, any_distance = nearest(points[inside], centre, group_names, np.ones(inside.sum(), bool))
            share = group_customers / stratum_customers
            row = {
                "stratum": stratum,
                "k": k,
                "group": int(group),
                "feeders": int(inside.sum()),
                "customers": int(group_customers),
                "customer_share": share,
                "starting_weight": share,
                "flagged_customer_share": customers[inside & ~runnable].sum() / group_customers,
                "typical_feeder": typical,
                "typical_distance": typical_distance,
                "nearest_feeder": any_feeder,
                "nearest_distance": any_distance,
                "placeholder_group": None,
                "placeholder_feeder": None,
                "placeholder_distance": float("nan"),
            }
            row.update({f"centre_{name}": float(value) for name, value in zip(features, centre)})
            rows_at_k.append(row)
            centres[int(group)] = centre
            typical_of[int(group)] = typical
        chosen = placeholders(centres, typical_of, point_of)
        for row in rows_at_k:
            if row["group"] in chosen:
                donor, feeder, distance = chosen[row["group"]]
                row.update(placeholder_group=donor, placeholder_feeder=feeder, placeholder_distance=distance)
        group_rows.extend(rows_at_k)
    group_table = pd.DataFrame(group_rows)
    group_table["placeholder_group"] = group_table["placeholder_group"].astype("Int64")
    return (
        pd.concat(long_parts, ignore_index=True),
        group_table,
        pd.DataFrame(explained_rows),
        nesting_ok(labels),
    )


def small_groups(table: pd.DataFrame, shares: list[float]) -> pd.DataFrame:
    """Per stratum, k and share: groups under the share and the customer share they hold."""
    rows = []
    for (stratum, k), part in table.groupby(["stratum", "k"], sort=True):
        for share in shares:
            below = part["customer_share"] < share
            rows.append(
                {
                    "stratum": stratum,
                    "k": k,
                    "share_below": share,
                    "groups_below": int(below.sum()),
                    "customer_share_below": float(part.loc[below, "customer_share"].sum()),
                }
            )
    return pd.DataFrame(rows)


def check_ceiling(ceiling: float | None) -> None:
    """A ceiling is None or a share above 0 and at most 1."""
    if ceiling is None:
        return
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling <= 1:
        raise ValueError(f"max_frame_customer_share must be null or above 0 and at most 1, not {ceiling!r}")


def dial(
    explained_table: pd.DataFrame, table: pd.DataFrame, targets: list[float], ceiling: float | None = None
) -> pd.DataFrame:
    """Per target and stratum, the number of groups k of the dial (module note, step 7).

    ``k_target`` is the smallest k reaching the target (or the largest k when
    none does); ``k_ceiling`` the smallest k whose largest group holds no more
    than ``ceiling`` of the customers of every stratum (1 when ``ceiling`` is
    None, the largest k when no k meets it); ``k`` the larger of the two, at
    which every other column is read. ``placeholder_groups`` counts the
    groups at that k that use a placeholder and ``placeholder_customer_share``
    is their customers over the stratum's (in the row ``all``, over the
    customers of every stratum). ``largest_frame_share`` is the largest
    group's customers over the customers of every stratum.
    """
    check_ceiling(ceiling)
    typical = table.dropna(subset=["typical_feeder"]).groupby(["stratum", "k"]).size()
    uses = table["placeholder_feeder"].notna()
    placeholder_count = table[uses].groupby(["stratum", "k"]).size()
    placeholder_customers = table[uses].groupby(["stratum", "k"])["customers"].sum()
    all_customers = table.groupby(["stratum", "k"])["customers"].sum()
    largest = table.groupby(["stratum", "k"])["customers"].max()
    total = float(table.loc[table["k"] == 1, "customers"].sum())

    def frame_share(stratum: str, k: int) -> float:
        value = largest.get((stratum, k))
        return float(value) / total if value is not None and total > 0 else float("nan")

    rows = []
    for target in targets:
        chosen = []
        for stratum, part in explained_table.groupby("stratum", sort=True):
            part = part.sort_values("k")
            ks = [int(k) for k in part["k"]]
            reached = part[part["explained"] >= target]
            k_target = int(reached.iloc[0]["k"]) if len(reached) else ks[-1]
            if ceiling is None:
                k_ceiling, ceiling_reached = 1, True
            else:
                meeting = [k for k in ks if frame_share(stratum, k) <= ceiling]
                k_ceiling, ceiling_reached = (meeting[0], True) if meeting else (ks[-1], False)
            k = max(k_target, k_ceiling)
            pick = part[part["k"] == k].iloc[0]
            chosen.append(
                {
                    "target": target,
                    "stratum": stratum,
                    "k": k,
                    "reached": bool(pick["explained"] >= target),
                    "explained": float(pick["explained"]),
                    "typical_feeders": int(typical.get((stratum, k), 0)),
                    "placeholder_groups": int(placeholder_count.get((stratum, k), 0)),
                    "placeholder_customer_share": float(placeholder_customers.get((stratum, k), 0))
                    / float(all_customers[(stratum, k)]),
                    "k_target": k_target,
                    "k_ceiling": k_ceiling,
                    "ceiling_reached": ceiling_reached,
                    "largest_frame_share": frame_share(stratum, k),
                }
            )
        rows.extend(chosen)
        rows.append(
            {
                "target": target,
                "stratum": "all",
                "k": sum(row["k"] for row in chosen),
                "reached": all(row["reached"] for row in chosen),
                "explained": float("nan"),
                "typical_feeders": sum(row["typical_feeders"] for row in chosen),
                "placeholder_groups": sum(row["placeholder_groups"] for row in chosen),
                "placeholder_customer_share": sum(
                    float(placeholder_customers.get((row["stratum"], row["k"]), 0)) for row in chosen
                )
                / sum(float(all_customers[(row["stratum"], row["k"])]) for row in chosen),
                "k_target": sum(row["k_target"] for row in chosen),
                "k_ceiling": None,
                "ceiling_reached": all(row["ceiling_reached"] for row in chosen),
                "largest_frame_share": max(
                    (row["largest_frame_share"] for row in chosen if not np.isnan(row["largest_frame_share"])),
                    default=float("nan"),
                ),
            }
        )
    out = pd.DataFrame(rows)
    out["k_ceiling"] = out["k_ceiling"].astype("Int64")
    return out


def typical_feeders(table: pd.DataFrame, dial_table: pd.DataFrame, target: float, features: pd.DataFrame) -> pd.DataFrame:
    """One row per group of each stratum at the dial's k for ``target``, with the feeder to run.

    ``run_feeder`` is the group's typical feeder, or its placeholder feeder
    when it has none (``run_is_placeholder`` true); ``run_feeder_loads`` and
    ``run_feeder_customers`` are that feeder's ``loads`` and ``customers``.
    """
    picks = dial_table[(dial_table["target"] == target) & (dial_table["stratum"] != "all")][["stratum", "k"]]
    rows = table.merge(picks, on=["stratum", "k"], how="inner").sort_values(["stratum", "group"], kind="stable")
    columns = [
        "stratum", "k", "group", "feeders", "customers", "customer_share", "starting_weight",
        "typical_feeder", "placeholder_group", "placeholder_feeder",
    ]
    rows = rows[columns].reset_index(drop=True)
    rows["run_is_placeholder"] = rows["typical_feeder"].isna() & rows["placeholder_feeder"].notna()
    rows["run_feeder"] = rows["typical_feeder"].where(rows["typical_feeder"].notna(), rows["placeholder_feeder"])
    sizes = features[["feeder", "loads", "customers"]].rename(
        columns={"feeder": "run_feeder", "loads": "run_feeder_loads", "customers": "run_feeder_customers"}
    )
    rows = rows.merge(sizes, on="run_feeder", how="left")
    for column in ("run_feeder_loads", "run_feeder_customers"):
        rows[column] = rows[column].astype("Int64")
    return rows[
        columns + ["run_feeder", "run_is_placeholder", "run_feeder_loads", "run_feeder_customers"]
    ].reset_index(drop=True)


def check_set_candidates(features: pd.DataFrame, sample: pd.DataFrame) -> pd.DataFrame:
    """One row per sample substation: its flags and whether it can run as one case."""
    keys = sample[["subregion", "substation"]].drop_duplicates()
    part = features.merge(keys, on=["subregion", "substation"], how="inner")
    rows = []
    for (subregion, substation), feeders in part.groupby(["subregion", "substation"], sort=True):
        res = int(feeders["res_customers"].sum())
        com = int(feeders["com_customers"].sum())
        shared = int(feeders["shared_shapes"].sum())
        rows.append(
            {
                "subregion": subregion,
                "substation": substation,
                "stratum": feeders["stratum"].iloc[0],
                "feeders": len(feeders),
                "customers": int(feeders["customers"].sum()),
                "res_customers": res,
                "com_customers": com,
                "feeders_no_res": int(feeders["no_res"].sum()),
                "feeders_no_com": int(feeders["no_com"].sum()),
                "feeders_with_shared_shapes": int((feeders["shared_shapes"] > 0).sum()),
                "shared_shapes": shared,
                "shared_pairs_total": int(feeders["shared_pairs_total"].sum()),
                "feeders_runnable_alone": int(feeders["runnable_alone"].sum()),
                "case_runnable": res > 0 and com > 0 and shared == 0,
            }
        )
    return pd.DataFrame(rows)


def build(features: pd.DataFrame, rule: dict) -> dict:
    """Every output table of the grouping, from the feature table and the ``groups`` settings."""
    if rule["scaling"] != "frame_z":
        raise ValueError(f"unknown scaling {rule['scaling']!r}")
    if rule["starting_weight"] != "customer_share":
        raise ValueError(f"unknown starting weight {rule['starting_weight']!r}")
    ceiling = rule.get("max_frame_customer_share")
    check_ceiling(ceiling)
    choice = rule.get("dial_choice")
    if choice is not None and choice not in rule["dial_targets"]:
        raise ValueError(f"dial_choice {choice!r} is not one of dial_targets {rule['dial_targets']!r}")
    features = features.sort_values("feeder", kind="stable").reset_index(drop=True)
    raw = feature_values(features, rule["features"])
    scaled = frame_z(raw)
    complete = scaled.notna().all(axis=1)
    names = list(scaled.columns)
    left_out = features.loc[~complete, ["stratum", "subregion", "substation", "feeder"]].copy()
    left_out["empty_features"] = [
        " ".join(column for column in names if pd.isna(raw.at[index, column])) for index in left_out.index
    ]
    long_parts, table_parts, explained_parts, nested = [], [], [], {}
    for stratum in sorted(features["stratum"].unique()):
        inside = (features["stratum"] == stratum) & complete
        part = features[inside].reset_index(drop=True)
        points = scaled[inside].to_numpy(dtype="float64")
        long, table, explained_table, ok = group_stratum(stratum, part, points, names, int(rule["cap"]))
        long_parts.append(long)
        table_parts.append(table)
        explained_parts.append(explained_table)
        nested[stratum] = ok
    table = pd.concat(table_parts, ignore_index=True)
    has_typical = table["typical_feeder"].notna().groupby([table["stratum"], table["k"]]).any()
    no_placeholder = table.groupby(["stratum", "k"]).size()[~has_typical].rename("groups").reset_index()
    explained_table = pd.concat(explained_parts, ignore_index=True)
    dial_table = dial(explained_table, table, rule["dial_targets"], ceiling)
    result = {
        "groups": pd.concat(long_parts, ignore_index=True),
        "group_table": table,
        "explained": explained_table,
        "dial": dial_table,
        "small_groups": small_groups(table, rule["small_share_report"]),
        "left_out": left_out.reset_index(drop=True),
        "no_placeholder": no_placeholder,
        "nested": nested,
        "scale": pd.DataFrame({"feature": names, "mean": raw.mean().to_numpy(), "std": raw.std(ddof=0).to_numpy()}),
    }
    if choice is not None:
        result["typical_feeders"] = typical_feeders(table, dial_table, choice, features)
    return result


def gates(result: dict, features: pd.DataFrame) -> dict[str, bool]:
    """Gates of step 4: nesting, explained, weights and one group per feeder per k."""
    explained_table = result["explained"].sort_values(["stratum", "k"])
    at_one = explained_table[explained_table["k"] == 1]["explained"].abs().max() < 1e-12
    rising = all(
        (np.diff(part["explained"].to_numpy()) >= -1e-12).all()
        for _, part in explained_table.groupby("stratum")
    )
    sums = result["group_table"].groupby(["stratum", "k"])["starting_weight"].sum()
    long = result["groups"]
    in_grouping = len(features) - len(result["left_out"])
    per_k = long.groupby(["stratum", "k"])["feeder"].agg(["size", "nunique"])
    per_stratum = long[long["k"] == 1].groupby("stratum").size()
    every_k = (per_k["size"] == per_k["nunique"]).all() and all(
        per_k.loc[stratum, "size"].eq(count).all() for stratum, count in per_stratum.items()
    )
    return {
        "nesting": all(result["nested"].values()),
        "explained_zero_at_k1": bool(at_one),
        "explained_not_decreasing": bool(rising),
        "weights_sum_to_one": bool(((sums - 1).abs() <= 1e-9).all()),
        "one_group_per_feeder_per_k": bool(every_k and per_stratum.sum() == in_grouping),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--stamp", required=True, help="UTC stamp of the output names")
    parser.add_argument("--features-stamp", required=True, help="stamp of features_<stamp>.parquet")
    parser.add_argument("--sample-stamp", default=None, help="stamp of sample_<stamp>.csv for the check-set candidates")
    args = parser.parse_args(argv)
    started = time.perf_counter()

    settings = load_sample_settings()
    rule = settings["groups"]
    paths = Paths(load_local(), settings)
    out = paths.texas
    features = pd.read_parquet(out / f"features_{args.features_stamp}.parquet")
    print(f"scipy {scipy.__version__}")
    result = build(features, rule)

    write_parquet(result["groups"], out / f"groups_{args.stamp}.parquet")
    write_csv(result["group_table"], out / f"group_table_{args.stamp}.csv")
    write_csv(result["explained"], out / f"explained_{args.stamp}.csv")
    write_csv(result["dial"], out / f"dial_{args.stamp}.csv")
    write_csv(result["small_groups"], out / f"small_groups_{args.stamp}.csv")
    if "typical_feeders" in result:
        write_csv(result["typical_feeders"], out / f"typical_feeders_{args.stamp}.csv")
    if args.sample_stamp:
        sample = pd.read_csv(out / f"sample_{args.sample_stamp}.csv")
        candidates = check_set_candidates(features, sample)
        write_csv(candidates, out / f"check_set_candidates_{args.stamp}.csv")
        print(candidates.to_string(index=False))

    pd.set_option("display.width", 220)
    print("scale (frame mean and standard deviation of each feature):")
    print(result["scale"].to_string(index=False))
    left_out = result["left_out"]
    print(f"feeders left out of grouping: {len(left_out)}")
    if len(left_out):
        print(left_out.groupby("stratum").size().to_string())
        print(left_out.to_string(index=False))
    print(result["dial"].to_string(index=False))
    if "typical_feeders" in result:
        chosen = result["typical_feeders"]
        runs = chosen.dropna(subset=["run_feeder"]).drop_duplicates("run_feeder")
        print(
            f"chosen dial {rule['dial_choice']}: groups {len(chosen)}, "
            f"placeholder groups {int(chosen['run_is_placeholder'].sum())}, "
            f"distinct run feeders {len(runs)}, loads of the distinct run feeders {int(runs['run_feeder_loads'].sum())}"
        )
    no_placeholder = result["no_placeholder"]
    print(f"strata and k with no typical feeder in any group, so no placeholder: {len(no_placeholder)}")
    if len(no_placeholder):
        print(no_placeholder.to_string(index=False))
    for name, ok in gates(result, features).items():
        print(f"gate {name}: {'pass' if ok else 'FAIL'}")
    print(f"written to {out} with stamp {args.stamp} in {time.perf_counter() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
