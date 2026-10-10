#!/usr/bin/env python3
"""Phase 2 Task A: zero-RPC offline analysis on existing artifacts."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hook_flags import decode_hook

POOLS = "/tmp/v4_created_pools.jsonl"
CLASSIFICATION = os.path.join(HERE, "_hook_classification.json")
TEMPLATES = os.path.join(HERE, "hook_templates.json")
LABELS_V1 = os.path.join(HERE, "_fork_replay_labels.json")
LABELS_V2 = os.path.join(HERE, "_fork_replay_labels_v2.json")
LABELS_MERGED = os.path.join(HERE, "_fork_replay_labels_merged.json")
SELFTEST = os.path.join(HERE, "_fork_replay_selftest.json")
TREND_OUT = os.path.join(HERE, "_hook_trend_by_day.json")
TOP30_OUT = os.path.join(HERE, "_hook_top30.json")
CROSSTAB_OUT = os.path.join(HERE, "_weak_label_crosstab.json")
V3CMP_OUT = os.path.join(HERE, "_v3_v4_static_detect_compare.json")
BLOCKS_PER_DAY = 43200  # ~2s Base blocks


def _flags_summary(permissions: list[str]) -> str:
    if not permissions:
        return "vanilla"
    head = permissions[:5]
    return ";".join(head)


def load_pools() -> list[dict]:
    return [json.loads(l) for l in open(POOLS) if l.strip()]


def load_fork_labels() -> list[dict]:
    rows = []
    seen = set()
    for path in (LABELS_MERGED, LABELS_V2, LABELS_V1, SELFTEST):
        if not os.path.exists(path):
            continue
        data = json.load(open(path))
        for key in ("pools", "cases"):
            for r in data.get(key, []):
                pid = r.get("poolId")
                if pid in seen:
                    continue
                seen.add(pid)
                lbl = r.get("label") or r.get("label_entry")
                if lbl:
                    rows.append({**r, "label": lbl})
    return rows


def load_template_addrs() -> set[str]:
    if not os.path.exists(TEMPLATES):
        return set()
    return {t["address"].lower() for t in json.load(open(TEMPLATES)).get("templates", [])}


def task_a1_trend(rows: list[dict]) -> dict:
    min_blk = min(r["blockNumber"] for r in rows)
    max_blk = max(r["blockNumber"] for r in rows)
    by_day: defaultdict[int, list] = defaultdict(list)
    for r in rows:
        d = (r["blockNumber"] - min_blk) // BLOCKS_PER_DAY
        by_day[d].append(r)

    days = []
    for d in sorted(by_day):
        chunk = by_day[d]
        n = len(chunk)
        hooked = sum(1 for r in chunk if not r.get("hook_profile", {}).get("is_vanilla", True))
        rd = sum(1 for r in chunk if r.get("hook_profile", {}).get("risk_tier") == "returns_delta_high")
        days.append({
            "day_index": d,
            "block_lo": min_blk + d * BLOCKS_PER_DAY,
            "block_hi": min_blk + (d + 1) * BLOCKS_PER_DAY - 1,
            "n_pools": n,
            "hooked_pct": round(hooked / n, 4) if n else 0,
            "returns_delta_pct": round(rd / n, 4) if n else 0,
        })

    if len(days) >= 2:
        h0, h1 = days[0]["hooked_pct"], days[-1]["hooked_pct"]
        r0, r1 = days[0]["returns_delta_pct"], days[-1]["returns_delta_pct"]
        trend_h = "rising" if h1 > h0 + 0.02 else ("falling" if h1 < h0 - 0.02 else "flat")
        trend_r = "rising" if r1 > r0 + 0.02 else ("falling" if r1 < r0 - 0.02 else "flat")
    else:
        trend_h = trend_r = "insufficient_days"

    conclusions = [
        f"14d window spans {len(days)} day-buckets ({min_blk}–{max_blk}); hooked% trend={trend_h} ({days[0]['hooked_pct']:.1%} → {days[-1]['hooked_pct']:.1%}).",
        f"RETURNS_DELTA% trend={trend_r} ({days[0]['returns_delta_pct']:.1%} → {days[-1]['returns_delta_pct']:.1%}); not TVL-weighted.",
        "Initialize-count proxy only — worth hook filter research if trend flat/rising; causal rug link still unproven.",
    ]
    out = {"min_block": min_blk, "max_block": max_blk, "n_days": len(days), "days": days, "conclusions": conclusions}
    json.dump(out, open(TREND_OUT, "w"), indent=2)
    return out


def task_a2_top30(rows: list[dict], labels: list[dict]) -> dict:
    tpl = load_template_addrs()
    by_hook: Counter = Counter()
    hook_flags: dict[str, dict] = {}
    for r in rows:
        h = r.get("hooks", "").lower()
        if not h or h == "0x" + "0" * 40:
            continue
        by_hook[h] += 1
        if h not in hook_flags:
            hp = r.get("hook_profile") or decode_hook(r["hooks"]).to_dict()
            hook_flags[h] = hp

    label_by_hook: defaultdict[str, list] = defaultdict(list)
    for lb in labels:
        h = lb.get("hooks", "").lower()
        if h:
            label_by_hook[h].append(lb.get("label"))

    total = sum(by_hook.values())
    table = []
    for rank, (h, cnt) in enumerate(by_hook.most_common(30), 1):
        hp = hook_flags.get(h, {})
        lbs = label_by_hook.get(h, [])
        ok = sum(1 for x in lbs if x == "onchain_swap_ok")
        blocked = sum(1 for x in lbs if x == "replay_blocked")
        n_lbl = len(lbs)
        table.append({
            "rank": rank,
            "hook": h,
            "pool_count": cnt,
            "share_of_hooked_pools": round(cnt / total, 4) if total else 0,
            "flags_summary": _flags_summary(hp.get("permissions", [])),
            "risk_tier": hp.get("risk_tier"),
            "has_template": h in tpl,
            "tx_replay_labeled_n": n_lbl,
            "tx_replay_ok_rate": round(ok / n_lbl, 3) if n_lbl else None,
            "replay_blocked_n": blocked,
        })
    out = {"n_hooked_pools": total, "unique_hooks": len(by_hook), "top30": table}
    json.dump(out, open(TOP30_OUT, "w"), indent=2)
    return out


def task_a3_crosstab(labels: list[dict]) -> dict:
    tiers = ["vanilla", "returns_delta_high", "standard", "unknown"]
    table = {}
    for tier in tiers:
        subset = [r for r in labels if (r.get("hook_tier") or r.get("hook_profile", {}).get("risk_tier")) == tier]
        if not subset and tier == "standard":
            subset = [r for r in labels if r.get("hook_tier") == "standard"]
        n = len(subset)
        ok = sum(1 for r in subset if r.get("label") == "onchain_swap_ok")
        blocked = sum(1 for r in subset if r.get("label") == "replay_blocked")
        dead = sum(1 for r in subset if r.get("label") == "dead")
        table[tier] = {
            "n": n,
            "onchain_swap_ok": ok,
            "onchain_swap_ok_pct": round(ok / n, 3) if n else None,
            "replay_blocked": blocked,
            "replay_blocked_pct": round(blocked / n, 3) if n else None,
            "dead": dead,
            "note": "NOT statistically significant; descriptive only",
        }
    # merge other hooked = standard + unknown non-vanilla
    other = [r for r in labels if r.get("hook_tier") in ("standard", "unknown")]
    n = len(other)
    out = {
        "total_labeled_n": len(labels),
        "by_tier": table,
        "other_hooked_combined": {
            "n": n,
            "onchain_swap_ok_pct": round(sum(1 for r in other if r.get("label") == "onchain_swap_ok") / n, 3) if n else None,
            "replay_blocked_pct": round(sum(1 for r in other if r.get("label") == "replay_blocked") / n, 3) if n else None,
        },
        "disclaimer": "Weak labels from tx_replay only; n small; no p-values",
    }
    json.dump(out, open(CROSSTAB_OUT, "w"), indent=2)
    return out


def task_a4_v3_compare() -> dict:
    """Logic-only comparison: V3 644 fork-replay taxonomy vs V4 hook static + weak tx replay."""
    rows = [
        {
            "failure_class": "instant_hp / transfer toggle",
            "v3_mechanism": "Token sell path reverts; buy succeeds at entry block",
            "v3_static_pre_entry": "Partial — bytecode/heuristics; fork-replay needed for ground truth",
            "v4_analog": "RETURNS_DELTA hook may revert swap in callback (hook_block)",
            "v4_static_pre_entry": "Stronger — hook address LSB flags visible at Initialize",
            "detection_gap": "V4 can flag permission matrix before first swap; V3 needs token contract inspect",
        },
        {
            "failure_class": "delayed_rug / lp_pull",
            "v3_mechanism": "Pool WETH depth collapses after entry (deployer removes LP)",
            "v3_static_pre_entry": "Weak — depth OK at entry; multi-point fork-replay catches flip",
            "v4_analog": "AFTER_REMOVE_LIQUIDITY hooks + same LP pull dynamic",
            "v4_static_pre_entry": "Partial — hook flags show liquidity hooks; timing still needs replay",
            "detection_gap": "Both need temporal replay for lp_pull; V4 adds hook capability hint",
        },
        {
            "failure_class": "delayed_rug / transfer_toggle",
            "v3_mechanism": "Sell reverts later while depth may remain",
            "v3_static_pre_entry": "Weak pre-entry",
            "v4_analog": "Hook-gated swap deltas / blacklist patterns",
            "v4_static_pre_entry": "Hook flags + template cluster may help",
            "detection_gap": "V3 644 labels from full roundtrip; V4 tx-only cannot see exit",
        },
        {
            "failure_class": "dead (no entry)",
            "v3_mechanism": "Buy fails at entry block",
            "v3_static_pre_entry": "N/A — discovered at replay",
            "v4_analog": "No swap in index / tx replay fails",
            "v4_static_pre_entry": "tx_replay_weak only proves past swap existence",
            "detection_gap": "V4 weak label ≠ V3 dead semantics",
        },
        {
            "failure_class": "clean (roundtrip OK)",
            "v3_mechanism": "Buy+sell succeed across hold grid",
            "v3_static_pre_entry": "Requires fork roundtrip (644 universe)",
            "v4_analog": "onchain_swap_ok — someone swapped successfully",
            "v4_static_pre_entry": "Weaker — does NOT prove our exit path",
            "detection_gap": "V4 cannot replace V3 clean label until roundtrip unblocked",
        },
    ]
    summary = (
        "V3 644 = full roundtrip multi-point labels (clean/instant_hp/delayed_rug/dead). "
        "V4 Phase2 = hook structure + tx_replay_weak. "
        "V4 wins on pre-entry static hook screening; V3 wins on exit/rug ground truth."
    )
    out = {"v3_universe_n": 644, "v3_label_source": "fork_replay_backtest.py roundtrip grid",
           "v4_label_source": "tx_replay eth_call weak", "comparison_rows": rows, "summary": summary}
    json.dump(out, open(V3CMP_OUT, "w"), indent=2)
    return out


def main() -> int:
    rows = load_pools()
    labels = load_fork_labels()
    print("=== Task A1: hook trend by day ===")
    t1 = task_a1_trend(rows)
    for c in t1["conclusions"]:
        print(f"  {c}")

    print("\n=== Task A2: Top-30 hooks ===")
    t2 = task_a2_top30(rows, labels)
    for r in t2["top30"][:5]:
        print(f"  #{r['rank']} {r['hook'][:14]}… pools={r['pool_count']} tpl={r['has_template']} replay_ok={r['tx_replay_ok_rate']}")

    print("\n=== Task A3: weak label crosstab ===")
    t3 = task_a3_crosstab(labels)
    for tier, s in t3["by_tier"].items():
        if s["n"]:
            print(f"  {tier:22s} n={s['n']:3d} ok={s['onchain_swap_ok_pct']} blocked={s['replay_blocked_pct']}")

    print("\n=== Task A4: V3 644 vs V4 static detect ===")
    task_a4_v3_compare()
    print(f"  Wrote {V3CMP_OUT}")

    print(f"\nOutputs: {TREND_OUT}, {TOP30_OUT}, {CROSSTAB_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
