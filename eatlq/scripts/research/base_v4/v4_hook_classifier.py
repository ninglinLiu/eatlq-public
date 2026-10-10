#!/usr/bin/env python3
"""Phase 2: Hook clustering, bytecode fingerprint, classification table."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Optional

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hook_flags import classify_hook_bucket, decode_hook
from research_rpc import RpcBudgetExceeded, get_research_rpc

POOLS_PATH = "/tmp/v4_created_pools.jsonl"
OUT_PATH = os.path.join(HERE, "_hook_classification.json")
TEMPLATE_PATH = os.path.join(HERE, "hook_templates.json")
BYTECODE_CACHE = "/tmp/v4_hook_bytecode_cache.json"


def load_pools(path: str = POOLS_PATH) -> list[dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_templates() -> tuple[dict[str, str], dict[str, str]]:
    """Return (runtime_hash->name, address->name)."""
    if not os.path.exists(TEMPLATE_PATH):
        return {}, {}
    data = json.load(open(TEMPLATE_PATH))
    by_hash, by_addr = {}, {}
    for t in data.get("templates", []):
        h = t.get("runtime_hash") or t.get("bytecode_hash")
        if h:
            by_hash[h.lower()] = t["name"]
        if t.get("address"):
            by_addr[t["address"].lower()] = t["name"]
    return by_hash, by_addr


def load_bytecode_cache() -> dict:
    if os.path.exists(BYTECODE_CACHE):
        try:
            return json.load(open(BYTECODE_CACHE))
        except Exception:
            pass
    return {}


def save_bytecode_cache(cache: dict) -> None:
    json.dump(cache, open(BYTECODE_CACHE, "w"))


def runtime_hash(code: bytes) -> str:
    return hashlib.sha256(code).hexdigest()


def fetch_hook_bytecodes(hooks: list[str], rpc=None, *, cache_only: bool = False) -> dict[str, dict]:
    cache = load_bytecode_cache()
    if cache_only:
        return cache
    updated = False
    for addr in hooks:
        key = addr.lower()
        if key in cache and cache[key].get("code_len", 0) > 0:
            continue
        if rpc is None:
            continue
        try:
            code = rpc.eth_get_code(Web3.to_checksum_address(addr))
            cache[key] = {
                "code_len": len(code),
                "runtime_hash": runtime_hash(code) if code else None,
            }
            updated = True
        except RpcBudgetExceeded:
            raise
        except Exception as e:
            cache[key] = {"code_len": 0, "error": str(e)[:80]}
            updated = True
    if updated:
        save_bytecode_cache(cache)
    return cache


def classify_pools(rows: list[dict], rpc=None, *, cache_only: bool = False) -> dict:
    templates_by_hash, templates_by_addr = load_templates()
    unique_hooks = sorted({r["hooks"].lower() for r in rows if r.get("hooks")})
    bytecode_info = fetch_hook_bytecodes(unique_hooks, rpc, cache_only=cache_only)

    hook_pool_count: Counter = Counter()
    hook_buckets: Counter = Counter()
    hook_profiles: dict[str, dict] = {}
    per_hook_pools: defaultdict[str, list] = defaultdict(list)

    for row in rows:
        hooks = row.get("hooks", "").lower()
        hook_pool_count[hooks] += 1
        per_hook_pools[hooks].append(row["poolId"])
        prof = decode_hook(hooks)
        bc = bytecode_info.get(hooks, {})
        tmpl = templates_by_hash.get((bc.get("runtime_hash") or "").lower())
        if not tmpl:
            tmpl = templates_by_addr.get(hooks)
        bucket = classify_hook_bucket(prof, template_match=tmpl)
        hook_buckets[bucket] += 1
        if hooks not in hook_profiles:
            hook_profiles[hooks] = {
                **prof.to_dict(),
                "pool_count": 0,
                "bytecode_len": bc.get("code_len"),
                "runtime_hash": bc.get("runtime_hash"),
                "template_match": tmpl,
                "bucket": bucket,
            }
        hook_profiles[hooks]["pool_count"] = hook_pool_count[hooks]

    top_hooks = hook_pool_count.most_common(30)
    return {
        "n_pools": len(rows),
        "n_unique_hooks": len(unique_hooks),
        "bucket_distribution": dict(hook_buckets),
        "top_hooks_by_reuse": [
            {
                "hooks": h,
                "pool_count": n,
                "reuse_rate": round(n / len(rows), 4) if rows else 0,
                **{k: hook_profiles[h][k] for k in (
                    "risk_tier", "has_returns_delta", "returns_delta_bits",
                    "permissions", "template_match", "bucket", "bytecode_len",
                )},
            }
            for h, n in top_hooks
        ],
        "returns_delta_high_hooks": [
            h for h, p in hook_profiles.items() if p.get("has_returns_delta")
        ],
        "vanilla_pools": hook_pool_count.get(
            "0x0000000000000000000000000000000000000000", 0
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", default=POOLS_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--cache-only", action="store_true", help="use bytecode cache only, 0 RPC")
    args = ap.parse_args()

    rows = load_pools(args.pools)
    if not rows:
        print(f"No pools at {args.pools}; run v4_pool_listener.py first", file=sys.stderr)
        return 1

    rpc = None if args.cache_only else get_research_rpc()
    rpc_start = rpc.count() if rpc else 0
    report = classify_pools(rows, rpc, cache_only=args.cache_only)
    report["rpc_calls"] = (rpc.count() if rpc else rpc_start)
    report["cache_only"] = args.cache_only
    json.dump(report, open(args.out, "w"), indent=2)

    print(f"=== Hook classification (n={report['n_pools']}) ===")
    for bucket, n in sorted(report["bucket_distribution"].items(), key=lambda x: -x[1]):
        pct = 100 * n / report["n_pools"]
        print(f"  {bucket:30s} {n:6d} ({pct:.1f}%)")
    print(f"\nTop-5 hooks by reuse:")
    for item in report["top_hooks_by_reuse"][:5]:
        print(f"  {item['hooks'][:18]}… pools={item['pool_count']} tier={item['risk_tier']}")
    print(f"\nWrote {args.out} | rpc={report['rpc_calls']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
