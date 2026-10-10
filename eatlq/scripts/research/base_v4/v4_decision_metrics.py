#!/usr/bin/env python3
"""Phase 5: three v4.md decision metrics (honest partial readouts)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
POOLS = "/tmp/v4_created_pools.jsonl"
LABELS = os.path.join(HERE, "_fork_replay_labels.json")
LABELS_V2 = os.path.join(HERE, "_fork_replay_labels_v2.json")
LABELS_MERGED = os.path.join(HERE, "_fork_replay_labels_merged.json")
LABELS_STRONG = os.path.join(HERE, "_fork_replay_labels_roundtrip.json")
LABELS_MERGED_STRONG = os.path.join(HERE, "_fork_replay_labels_merged_strong.json")
OUT = os.path.join(HERE, "_decision_metrics.json")

FATAL_STRONG = frozenset({
    "instant_hp", "delayed_rug", "hook_block", "delta_tax", "dynamic_fee_spike", "dead",
})


def _roundtrip_stats() -> dict:
    path = LABELS_MERGED_STRONG if os.path.exists(LABELS_MERGED_STRONG) else LABELS_STRONG
    if not os.path.exists(path):
        return {"status": "not_run", "n": 0}
    pools = json.load(open(path)).get("pools", [])
    if not pools:
        return {"status": "not_run", "n": 0}
    labels = Counter(p.get("strong_label") or p.get("label") for p in pools)
    n = len(pools)
    clean = labels.get("clean", 0)
    fatal = sum(labels.get(x, 0) for x in FATAL_STRONG)
    hook_delta = labels.get("hook_block", 0) + labels.get("delta_tax", 0)
    by_tier: dict[str, dict] = {}
    for p in pools:
        tier = p.get("hook_tier") or "unknown"
        lbl = p.get("strong_label") or p.get("label")
        by_tier.setdefault(tier, {"n": 0, "clean": 0, "fatal": 0})
        by_tier[tier]["n"] += 1
        if lbl == "clean":
            by_tier[tier]["clean"] += 1
        if lbl in FATAL_STRONG:
            by_tier[tier]["fatal"] += 1
    tier_rates = {
        t: {
            "n": v["n"],
            "clean_rate": round(v["clean"] / v["n"], 3) if v["n"] else 0,
            "fatal_rate": round(v["fatal"] / v["n"], 3) if v["n"] else 0,
        }
        for t, v in by_tier.items()
    }
    return {
        "status": "partial_roundtrip",
        "n": n,
        "clean_rate": round(clean / n, 3) if n else 0,
        "fatal_rate": round(fatal / n, 3) if n else 0,
        "hook_block_delta_tax_share": round(hook_delta / n, 3) if n else 0,
        "labels": dict(labels),
        "by_tier": tier_rates,
        "label_source": "roundtrip_strong",
        "category": "c",
        "note": "buy→sell roundtrip at offsets [0,30,300,1800,7200]; n=211 WETH universe",
    }


def _tx_replay_stats() -> dict:
    for path in (LABELS_MERGED, LABELS_V2, LABELS):
        if not os.path.exists(path):
            continue
        data = json.load(open(path))
        pools = data.get("pools", [])
        if not pools:
            continue
        labels = Counter(p.get("label") for p in pools)
        n = len(pools)
        ok = labels.get("onchain_swap_ok", 0)
        by_tier: dict[str, dict] = {}
        for p in pools:
            tier = p.get("hook_tier") or "unknown"
            by_tier.setdefault(tier, {"n": 0, "ok": 0})
            by_tier[tier]["n"] += 1
            if p.get("label") == "onchain_swap_ok":
                by_tier[tier]["ok"] += 1
        tier_rates = {
            t: {"n": v["n"], "swap_ok_rate": round(v["ok"] / v["n"], 3) if v["n"] else 0}
            for t, v in by_tier.items()
        }
        return {
            "status": "partial_tx_replay",
            "n": n,
            "swap_replay_ok_rate": round(ok / n, 3) if n else 0,
            "labels": dict(labels),
            "by_tier": tier_rates,
            "label_source": "tx_replay_weak",
            "category": "c",
            "note": "eth_call replay of cached Swap txs; exit proof requires roundtrip",
        }
    return {"status": "not_run", "n": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label-source", default="tx_replay_weak",
                    choices=("tx_replay_weak", "roundtrip_strong"))
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(POOLS) if l.strip()]
    n = len(rows)
    tiers = Counter(r.get("hook_profile", {}).get("risk_tier", "?") for r in rows)
    hooked = n - tiers.get("vanilla", 0)
    rd = tiers.get("returns_delta_high", 0)
    hooked_share = hooked / n if n else 0

    if args.label_source == "roundtrip_strong":
        rt_stats = _roundtrip_stats()
        report = {
            "5.1_sandbox_fidelity_latency": {
                **rt_stats,
                "roundtrip_status": "working" if rt_stats.get("n", 0) >= 200 else "partial",
                "roundtrip_note": "Helper approve fix (Phase 3 C2); batch labels on 211 WETH pools",
                "target": "latency <50ms, MAE <0.0001%",
                "readout": "roundtrip_viable" if rt_stats.get("clean_rate", 0) >= 0.3 else "need_more_data",
            },
            "5.2_aggregator_penetration": {
                "status": "not_run",
                "note": "Phase 5.2 full deferred; H-lite optional only",
                "category": "c",
                "target": ">40% healthy / <15% isolated",
            },
            "5.3_hooked_pool_share_trend": {
                "status": "partial_14d",
                "n_pools": n,
                "hooked_share": round(hooked_share, 4),
                "returns_delta_share": round(rd / n, 4) if n else 0,
                "vanilla_share": round(tiers.get("vanilla", 0) / n, 4) if n else 0,
                "category": "b",
                "target": "hooked >25% stable",
                "readout": "worth_research_hook_filter" if hooked_share > 0.25 else "need_more_data",
            },
        }
    else:
        tx_stats = _tx_replay_stats()
        report = {
            "5.1_sandbox_fidelity_latency": {
                **tx_stats,
                "roundtrip_status": "blocked",
                "roundtrip_note": "superseded by Phase 3 C2 fix; use --label-source roundtrip_strong",
                "target": "latency <50ms, MAE <0.0001%",
                "readout": "tx_replay_viable" if tx_stats.get("swap_replay_ok_rate", 0) >= 0.5 else "need_more_data",
            },
            "5.2_aggregator_penetration": {
                "status": "not_run",
                "note": "requires DEX aggregator volume scrape; deferred to save RPC budget",
                "category": "c",
                "target": ">40% healthy / <15% isolated",
            },
            "5.3_hooked_pool_share_trend": {
                "status": "partial_14d",
                "n_pools": n,
                "hooked_share": round(hooked_share, 4),
                "returns_delta_share": round(rd / n, 4) if n else 0,
                "vanilla_share": round(tiers.get("vanilla", 0) / n, 4) if n else 0,
                "category": "b",
                "target": "hooked >25% stable",
                "readout": "worth_research_hook_filter" if hooked_share > 0.25 else "need_more_data",
            },
        }

    json.dump(report, open(OUT, "w"), indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
