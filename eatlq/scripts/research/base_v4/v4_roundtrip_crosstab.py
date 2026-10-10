#!/usr/bin/env python3
"""Phase 4 Task B: roundtrip strong-label crosstabs (0 RPC)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WEAK = os.path.join(HERE, "_fork_replay_labels_merged.json")
STRONG = os.path.join(HERE, "_fork_replay_labels_roundtrip.json")
MERGED = os.path.join(HERE, "_fork_replay_labels_merged_strong.json")
POOLS = "/tmp/v4_created_pools.jsonl"
OUT_TIER = os.path.join(HERE, "_roundtrip_crosstab.json")
OUT_TMPL = os.path.join(HERE, "_roundtrip_template_crosstab.json")
OUT_GAP = os.path.join(HERE, "_weak_strong_gap.json")


def _load(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    return {p["poolId"]: p for p in json.load(open(path)).get("pools", [])}


def _pool_meta() -> dict[str, dict]:
    m = {}
    for line in open(POOLS):
        if line.strip():
            r = json.loads(line)
            m[r["poolId"]] = r
    return m


def _rate(ok: int, n: int):
    return round(ok / n, 3) if n else None


def _crosstab(rows: list[dict], key_fn) -> dict:
    out = {}
    groups: defaultdict[str, list] = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    for k, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        n = len(items)
        labels = Counter(r.get("strong_label") or r.get("label") for r in items)
        clean = labels.get("clean", 0)
        fatal = sum(labels.get(x, 0) for x in (
            "instant_hp", "delayed_rug", "hook_block", "delta_tax", "dynamic_fee_spike", "dead"
        ))
        out[k] = {
            "n": n,
            "clean": clean,
            "clean_rate": _rate(clean, n),
            "fatal": fatal,
            "fatal_rate": _rate(fatal, n),
            "labels": dict(labels),
            "note": "descriptive only; not statistically significant",
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roundtrip-crosstab", action="store_true")
    args = ap.parse_args()
    if not args.roundtrip_crosstab:
        ap.print_help()
        return 1

    weak = _load(WEAK)
    strong = _load(STRONG)
    meta = _pool_meta()
    rows = []
    for pid, s in strong.items():
        w = weak.get(pid, {})
        p = meta.get(pid, {})
        ht = p.get("hook_template") or {}
        rows.append({
            "poolId": pid,
            "weak_label": w.get("label"),
            "strong_label": s.get("label"),
            "label": s.get("label"),
            "hook_tier": s.get("hook_tier") or p.get("hook_profile", {}).get("risk_tier"),
            "hook_family": ht.get("hook_family"),
            "template_name": ht.get("template_name"),
        })

    tier_tab = _crosstab(rows, lambda r: r.get("hook_tier") or "unknown")
    tmpl_tab = _crosstab(rows, lambda r: r.get("hook_family") or r.get("template_name") or "unmatched")

    gaps = []
    for r in rows:
        if r.get("weak_label") == "onchain_swap_ok" and r.get("strong_label") != "clean":
            gaps.append(r)
    gaps.sort(key=lambda x: x.get("strong_label", ""))
    gap_out = {
        "n_total_strong": len(rows),
        "n_weak_ok_strong_not_clean": len(gaps),
        "rate": _rate(len(gaps), sum(1 for r in rows if r.get("weak_label") == "onchain_swap_ok")),
        "top20": gaps[:20],
        "disclaimer": "onchain_swap_ok != clean proves weak labels are insufficient for exit proof",
    }

    json.dump({"by_tier": tier_tab, "n": len(rows)}, open(OUT_TIER, "w"), indent=2)
    json.dump({"by_template": tmpl_tab, "n": len(rows)}, open(OUT_TMPL, "w"), indent=2)
    json.dump(gap_out, open(OUT_GAP, "w"), indent=2)

    print(f"=== Roundtrip crosstab (n={len(rows)}) ===")
    for tier, s in tier_tab.items():
        print(f"  {tier:22s} n={s['n']:3d} clean={s['clean_rate']} fatal={s['fatal_rate']}")
    print(f"  weak_ok≠clean: {len(gaps)}/{len(rows)}")
    print(f"Wrote {OUT_TIER}, {OUT_TMPL}, {OUT_GAP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
