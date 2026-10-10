#!/usr/bin/env python3
"""Phase 2.5: template-family rollup + weak-label cross-tab (0 RPC)."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from v4_hook_classifier import classify_pools, load_bytecode_cache, load_templates

POOLS = "/tmp/v4_created_pools.jsonl"
LABELS_MERGED = os.path.join(HERE, "_fork_replay_labels_merged.json")
OUT = os.path.join(HERE, "_template_family_stats.json")
CROSSTAB_OUT = os.path.join(HERE, "_template_label_crosstab.json")


def load_pools() -> list[dict]:
    return [json.loads(l) for l in open(POOLS) if l.strip()]


def load_labels() -> dict[str, dict]:
    if not os.path.exists(LABELS_MERGED):
        return {}
    return {p["poolId"]: p for p in json.load(open(LABELS_MERGED)).get("pools", [])}


def family_key(row: dict, hook_profiles: dict) -> str:
    h = row.get("hooks", "").lower()
    if h == "0x" + "0" * 40:
        return "vanilla"
    prof = hook_profiles.get(h, {})
    if prof.get("template_match"):
        return f"template:{prof['template_match']}"
    if prof.get("bucket"):
        return prof["bucket"]
    tier = row.get("hook_profile", {}).get("risk_tier", "unknown")
    return f"unmatched_{tier}"


def main() -> int:
    rows = load_pools()
    report = classify_pools(rows, rpc=None, cache_only=True)
    hook_profiles = {}
    for item in report.get("top_hooks_by_reuse", []):
        hook_profiles[item["hooks"].lower()] = item
    # rebuild full hook_profiles from classification run — use cache + templates
    _, by_addr = load_templates()
    cache = load_bytecode_cache()
    by_hash, _ = load_templates()
    hook_profiles_full = {}
    for h in {r["hooks"].lower() for r in rows}:
        bc = cache.get(h, {})
        tmpl = by_hash.get((bc.get("runtime_hash") or "").lower()) or by_addr.get(h)
        from hook_flags import classify_hook_bucket, decode_hook
        prof = decode_hook(h)
        bucket = classify_hook_bucket(prof, template_match=tmpl)
        hook_profiles_full[h] = {
            "template_match": tmpl,
            "bucket": bucket,
            "risk_tier": prof.risk_tier,
        }

    families: Counter = Counter()
    weth_families: Counter = Counter()
    for r in rows:
        fk = family_key(r, hook_profiles_full)
        families[fk] += 1
        if r.get("has_weth"):
            weth_families[fk] += 1

    labels = load_labels()
    cross: defaultdict[str, list] = defaultdict(list)
    for pid, lab in labels.items():
        row = next((x for x in rows if x["poolId"] == pid), None)
        if not row:
            continue
        fk = family_key(row, hook_profiles_full)
        cross[fk].append(lab.get("label"))

    crosstab = {}
    for fk, lbs in sorted(cross.items(), key=lambda x: -len(x[1])):
        n = len(lbs)
        c = Counter(lbs)
        crosstab[fk] = {
            "n_labeled": n,
            "onchain_swap_ok": c.get("onchain_swap_ok", 0),
            "swap_ok_pct": round(c.get("onchain_swap_ok", 0) / n, 3) if n else None,
            "replay_blocked": c.get("replay_blocked", 0),
            "dead": c.get("dead", 0),
        }

    template_matched = sum(n for k, n in families.items() if k.startswith("template:"))
    out = {
        "n_pools": len(rows),
        "n_unique_hooks": report["n_unique_hooks"],
        "template_matched_pools": template_matched,
        "template_matched_pct": round(template_matched / len(rows), 4) if rows else 0,
        "family_distribution": dict(families.most_common(40)),
        "weth_family_distribution": dict(weth_families.most_common(25)),
        "bucket_distribution": report["bucket_distribution"],
        "rpc_calls": 0,
    }
    json.dump(out, open(OUT, "w"), indent=2)
    json.dump({"crosstab": crosstab, "disclaimer": "weak tx_replay labels; n per family varies"},
              open(CROSSTAB_OUT, "w"), indent=2)

    print(f"=== Template families (0 RPC, n={len(rows)}) ===")
    print(f"  template-matched pools: {template_matched} ({100*template_matched/len(rows):.1f}%)")
    print("  Top families:")
    for fk, n in families.most_common(8):
        print(f"    {fk[:40]:40s} {n:6d}")
    print("\n  Labeled families (weak labels):")
    for fk, s in list(crosstab.items())[:8]:
        print(f"    {fk[:36]:36s} n={s['n_labeled']:3d} ok={s['swap_ok_pct']}")
    print(f"\nWrote {OUT}, {CROSSTAB_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
