#!/usr/bin/env python3
"""Task 4 — convexity basket vs random baseline (zero RPC)."""
from __future__ import annotations

import random

import numpy as np

from common import (
    OUT,
    TEST_DAYS,
    TRAIN_DAYS,
    load_json,
    summarize_rets,
    write_json,
)


def basket_stats(mints: list[str], rets_map: dict, split_days: set[str], labels: dict) -> dict:
    rets = []
    for m in mints:
        lab = labels.get(m, {})
        if lab.get("day") not in split_days:
            continue
        r = rets_map.get(m)
        if r is not None:
            rets.append(r)
    s = summarize_rets(rets)
    s["split_days"] = sorted(split_days)
    return s


def main():
    raw_rc = load_json(OUT / "_sol_realcost_labels.json")
    raw_lt = load_json(OUT / "_sol_lefttail_model.json")
    labels = {x["mint"]: x for x in raw_rc["labels"]}
    preds = {x["mint"]: x for x in raw_lt["predictions"]}
    cut = raw_lt["prune_cut_p60"]

    # primary hold: 30m exit (best grid coverage); fallback 5m
    def primary_ret(m):
        lab = labels[m]
        return lab["returns"].get("30m") or lab["returns"].get("5m")

    rets_map = {m: primary_ret(m) for m in labels if primary_ret(m) is not None}
    all_mints = list(rets_map.keys())

    pruned = [m for m in all_mints if preds.get(m, {}).get("keep_pruned")]

    rng = random.Random(42)
    baseline_draws = []
    for _ in range(500):
        sample = rng.sample(all_mints, min(len(pruned), len(all_mints)))
        baseline_draws.append(np.mean([rets_map[m] for m in sample]))
    baseline_mean = float(np.mean(baseline_draws))

    convexity = {
        "hold_horizon": "30m_from_entry",
        "cost_rt": 0.03,
        "pruned_basket_equal_weight": {
            "TRAIN": basket_stats(pruned, rets_map, TRAIN_DAYS, labels),
            "TEST": basket_stats(pruned, rets_map, TEST_DAYS, labels),
            "ALL": basket_stats(pruned, rets_map, TRAIN_DAYS | TEST_DAYS, labels),
        },
        "all_coins_equal_weight": {
            "TRAIN": basket_stats(all_mints, rets_map, TRAIN_DAYS, labels),
            "TEST": basket_stats(all_mints, rets_map, TEST_DAYS, labels),
            "ALL": basket_stats(all_mints, rets_map, TRAIN_DAYS | TEST_DAYS, labels),
        },
    }
    baseline = {
        "method": "random_equal_weight_same_n_as_pruned",
        "n_per_draw": len(pruned),
        "n_draws": 500,
        "mean_return_baseline": baseline_mean,
        "random_basket_TEST": basket_stats(
            rng.sample(all_mints, min(len(pruned), len(all_mints))), rets_map, TEST_DAYS, labels
        ),
        "all_coins_TEST": convexity["all_coins_equal_weight"]["TEST"],
    }
    write_json("_sol_convexity.json", convexity)
    write_json("_sol_baseline.json", baseline)
    te = convexity["pruned_basket_equal_weight"]["TEST"]
    print(f"convexity TEST pruned mean={te.get('mean', 'n/a')} n={te.get('n')} baseline={baseline_mean:.4f}")


if __name__ == "__main__":
    main()
