#!/usr/bin/env python3
"""Task D: Deep-pool left tail — chronic bleed, slow LP pull, delayed rug."""
from __future__ import annotations

import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (  # noqa: E402
    HERE,
    load_json,
    pnl_stats,
    rpc_ledger,
    save_json,
)

UNIVERSE = os.path.join(HERE, "_deep_universe.json")
PRICE_CACHE = os.path.join(HERE, "_baseline_pricing.json")
FLOW = os.path.join(HERE, "_flow_signals.json")
SMART = os.path.join(HERE, "_smartmoney_signal.json")
LEFTTAIL_SHARDS = "/opt/EATLQ/scripts/research/base_lefttail/_uni_shards/_uni_s*.json"
OUT = os.path.join(HERE, "_deep_lefttail.json")
CHECKPOINT = os.path.join(HERE, "_lefttail_checkpoint.json")


def load_fork_labels() -> dict:
    labels = {}
    for f in glob.glob(LEFTTAIL_SHARDS):
        for r in load_json(f, {}).get("pools", []):
            labels[r["pool"]] = r
    return labels


def analyze_subset(name: str, pool_keys: set, cache: dict, labels: dict) -> dict:
    nets, mechanisms = [], []
    slow_lp = 0
    for key in pool_keys:
        pool = key.split(":")[0]
        rec = cache.get(key, {})
        if rec.get("why") != "ok":
            continue
        net = (rec.get("holds") or {}).get("4h", {}).get("net")
        if net is not None:
            nets.append(net)
        lb = labels.get(pool)
        if lb:
            if lb.get("label") == "delayed_rug":
                mechanisms.append(lb.get("mechanism", "unknown"))
                if lb.get("mechanism") == "lp_pull" and (lb.get("flip_offset") or 0) > 300:
                    slow_lp += 1
    stats = pnl_stats(nets)
    stats["subset"] = name
    stats["slow_lp_pull_cases"] = slow_lp
    stats["delayed_rug_mechanisms"] = dict((m, mechanisms.count(m)) for m in set(mechanisms))
    return stats


def main():
    print("#### Task D: Deep left tail ####", flush=True)
    ckpt = load_json(CHECKPOINT, {})
    if ckpt.get("done") and os.path.exists(OUT):
        print("  checkpoint done")
        return

    uni = load_json(UNIVERSE)
    cache = load_json(PRICE_CACHE, {})
    labels = load_fork_labels()
    flow = load_json(FLOW, {})
    smart = load_json(SMART, {})

    all_keys = {f"{r['pool']}:{r['entry_block']}" for r in uni.get("rows", [])}

    # Best signal subset: any flow signal that fired on TEST with n>=10
    signal_keys = set()
    for sig in flow.get("signals", []):
        if sig.get("test", {}).get("n", 0) >= 10:
            pass  # would need per-pool list; use full priced subset

    priced_keys = {k for k, v in cache.items() if v.get("why") == "ok"}

    baseline_stats = analyze_subset("all_priced", priced_keys, cache, labels)
    overlap_labels = analyze_subset(
        "labeled_overlap",
        {k for k in priced_keys if k.split(":")[0] in labels},
        cache,
        labels,
    )

    out = {
        "meta": {
            "n_universe": len(all_keys),
            "n_priced": len(priced_keys),
            "n_labeled_overlap": len([k for k in priced_keys if k.split(":")[0] in labels]),
            "rpc": rpc_ledger(),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "all_priced": baseline_stats,
        "labeled_overlap": overlap_labels,
        "worst_single": baseline_stats.get("worst"),
        "fatal_rate_50": baseline_stats.get("fatal_50"),
        "fatal_rate_90": baseline_stats.get("fatal_90"),
        "slow_lp_pull_cases": overlap_labels.get("slow_lp_pull_cases", 0),
        "interpretation": (
            f"Deep pool priced subset: fatal(<=-50%)={baseline_stats.get('fatal_50',0):.1%}, "
            f"worst={baseline_stats.get('worst',0):+.2f}, slow_lp_cases={overlap_labels.get('slow_lp_pull_cases',0)}"
        ),
    }
    save_json(OUT, out)
    save_json(CHECKPOINT, {"done": True})
    print(f"  {out['interpretation']}")
    print(f"  wrote {OUT}")


if __name__ == "__main__":
    main()
