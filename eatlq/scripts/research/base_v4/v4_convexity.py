#!/usr/bin/env python3
"""Phase 4: convexity + fatal rate by hook bucket (requires fork-replay labels)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = os.path.join(HERE, "_fork_replay_labels.json")
LABELS_V2 = os.path.join(HERE, "_fork_replay_labels_v2.json")
LABELS_MERGED = os.path.join(HERE, "_fork_replay_labels_merged.json")
LABELS_STRONG = os.path.join(HERE, "_fork_replay_labels_roundtrip.json")
LABELS_MERGED_STRONG = os.path.join(HERE, "_fork_replay_labels_merged_strong.json")
SELFTEST = os.path.join(HERE, "_fork_replay_selftest.json")
POOLS = "/tmp/v4_created_pools.jsonl"
OUT = os.path.join(HERE, "_convexity_report.json")

FATAL_STRONG = frozenset({
    "instant_hp", "delayed_rug", "hook_block", "delta_tax", "dynamic_fee_spike", "dead",
})


def _label_of(r: dict, *, strong: bool = False) -> str:
    if strong:
        return r.get("strong_label") or r.get("label") or "unknown"
    return r.get("label") or r.get("label_entry") or "unknown"


def load_labels(*, source: str = "tx_replay_weak") -> list[dict]:
    pool_meta = {}
    if os.path.exists(POOLS):
        for line in open(POOLS):
            if not line.strip():
                continue
            row = json.loads(line)
            pool_meta[row["poolId"]] = row.get("hook_profile", {})

    if source == "roundtrip_strong":
        path = LABELS_MERGED_STRONG if os.path.exists(LABELS_MERGED_STRONG) else LABELS_STRONG
        if not os.path.exists(path):
            return []
        rows = []
        for r in json.load(open(path)).get("pools", []):
            if not r.get("hook_tier"):
                r["hook_tier"] = pool_meta.get(r.get("poolId"), {}).get("risk_tier")
            rows.append(r)
        return rows

    rows = []
    seen = set()
    for path in (LABELS_MERGED, LABELS_V2, LABELS, SELFTEST):
        if not os.path.exists(path):
            continue
        data = json.load(open(path))
        for key in ("pools", "cases"):
            for r in data.get(key, []):
                pid = r.get("poolId")
                if pid in seen:
                    continue
                seen.add(pid)
                if not r.get("hook_tier"):
                    r["hook_tier"] = pool_meta.get(pid, {}).get("risk_tier")
                rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label-source", default="tx_replay_weak",
                    choices=("tx_replay_weak", "roundtrip_strong"))
    args = ap.parse_args()
    strong = args.label_source == "roundtrip_strong"
    labels = load_labels(source=args.label_source)
    if not labels:
        print("No fork-replay labels yet — run v4_fork_replay.py --selftest first")
        return 1

    by_tier: defaultdict[str, list] = defaultdict(list)
    for r in labels:
        tier = r.get("hook_tier") or r.get("hook_profile", {}).get("risk_tier", "unknown")
        by_tier[tier].append(r)

    report = {
        "n_labeled": len(labels),
        "label_source": args.label_source,
        "label_mix": dict(Counter(_label_of(r, strong=strong) for r in labels)),
        "by_tier": {},
    }
    for tier, rows in by_tier.items():
        n = len(rows)
        lbls = [_label_of(r, strong=strong) for r in rows]
        if strong:
            clean = sum(1 for x in lbls if x == "clean")
            fatal = sum(1 for x in lbls if x in FATAL_STRONG)
            hook_block = sum(1 for x in lbls if x in ("hook_block", "delta_tax"))
            rt_err = sum(1 for x in lbls if x == "roundtrip_error")
            report["by_tier"][tier] = {
                "n": n,
                "clean_rate": round(clean / n, 3) if n else 0,
                "fatal_rate": round(fatal / n, 3) if n else 0,
                "hook_block_delta_tax_rate": round(hook_block / n, 3) if n else 0,
                "roundtrip_error_rate": round(rt_err / n, 3) if n else 0,
                "labels": dict(Counter(lbls)),
                "note": "roundtrip buy→sell at offsets [0,30,300,1800,7200]",
            }
            continue
        swap_ok = sum(1 for x in lbls if x == "onchain_swap_ok")
        tradeable = sum(1 for r in rows if r.get("sell_ok") or _label_of(r) == "clean")
        instant = sum(1 for x in lbls if x == "instant_hp")
        dead = sum(1 for x in lbls if x == "dead")
        blocked = sum(1 for x in lbls if x == "replay_blocked")
        slips = [r["slippage"] for r in rows if r.get("slippage") is not None]
        fatal50 = sum(1 for s in slips if s <= -0.5)
        report["by_tier"][tier] = {
            "n": n,
            "swap_replay_ok_rate": round(swap_ok / n, 3) if n else 0,
            "tradeable_rate": round(tradeable / n, 3) if n else 0,
            "instant_hp_rate": round(instant / n, 3) if n else 0,
            "dead_rate": round(dead / n, 3) if n else 0,
            "replay_blocked_rate": round(blocked / n, 3) if n else 0,
            "fatal_le50pct": round(fatal50 / max(len(slips), 1), 3),
            "median_slippage": sorted(slips)[len(slips) // 2] if slips else None,
            "note": "swap_replay_ok=tx eth_call; tradeable=full roundtrip only",
        }

    json.dump(report, open(OUT, "w"), indent=2)
    print(f"=== Phase 4 (n={len(labels)}, source={args.label_source}) ===")
    print(f"  labels: {report['label_mix']}")
    for tier, stats in sorted(report["by_tier"].items()):
        if strong:
            print(
                f"  {tier:22s} n={stats['n']:3d} clean={stats['clean_rate']:.1%} "
                f"fatal={stats['fatal_rate']:.1%} hook/delta={stats['hook_block_delta_tax_rate']:.1%}"
            )
        else:
            print(
                f"  {tier:22s} n={stats['n']:3d} swap_ok={stats['swap_replay_ok_rate']:.1%} "
                f"tradeable={stats['tradeable_rate']:.1%} dead={stats['dead_rate']:.1%} "
                f"blocked={stats['replay_blocked_rate']:.1%}"
            )
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
