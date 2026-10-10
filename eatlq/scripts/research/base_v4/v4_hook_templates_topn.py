#!/usr/bin/env python3
"""Phase 2 Task D: fetch eth_getCode for Top-N hooks missing templates (≤20 RPC)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from research_rpc import RpcBudgetExceeded, get_research_rpc

POOLS = "/tmp/v4_created_pools.jsonl"
TEMPLATES = os.path.join(HERE, "hook_templates.json")
TOP30 = os.path.join(HERE, "_hook_top30.json")
RPC_WARN = 18_000


def runtime_hash(code: bytes) -> str | None:
    return hashlib.sha256(code).hexdigest() if code else None


def top_hooks(n: int) -> list[dict]:
    if os.path.exists(TOP30):
        return json.load(open(TOP30)).get("top30", [])[:n]
    by: Counter = Counter()
    for line in open(POOLS):
        r = json.loads(line)
        h = r.get("hooks", "").lower()
        if h and h != "0x" + "0" * 40:
            by[h] += 1
    return [{"hook": h, "pool_count": c} for h, c in by.most_common(n)]


def load_templates() -> dict:
    if os.path.exists(TEMPLATES):
        return json.load(open(TEMPLATES))
    return {"templates": []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tpl_data = load_templates()
    known = {t["address"].lower(): t for t in tpl_data.get("templates", [])}
    known_hashes = {t.get("runtime_hash") for t in tpl_data.get("templates", []) if t.get("runtime_hash")}

    candidates = []
    for row in top_hooks(args.top):
        h = row["hook"].lower()
        if h in known:
            continue
        candidates.append(row)

    if args.dry_run:
        print(json.dumps({"would_fetch": len(candidates), "hooks": [c["hook"] for c in candidates]}, indent=2))
        return 0

    rpc = get_research_rpc()
    if rpc.count() >= RPC_WARN:
        print(f"STOP: RPC {rpc.count()} >= {RPC_WARN}")
        return 2

    added = []
    for row in candidates[: args.top]:
        if rpc.count() >= RPC_WARN:
            print(f"STOP mid-run at RPC {rpc.count()}")
            break
        addr = Web3.to_checksum_address(row["hook"])
        code = rpc.eth_get_code(addr)
        rh = runtime_hash(code)
        name = f"top_hook_{addr[2:10]}"
        entry = {
            "name": name,
            "source": "phase2_topn_getCode",
            "address": addr,
            "notes": f"pools={row.get('pool_count', '?')}; auto-fetched Phase2",
            "bytecode_len": len(code),
            "runtime_hash": rh,
        }
        if rh and rh in known_hashes:
            entry["notes"] += "; hash matches existing template family"
        tpl_data.setdefault("templates", []).append(entry)
        known[addr.lower()] = entry
        if rh:
            known_hashes.add(rh)
        added.append(entry)
        print(f"  + {name} len={len(code)} hash={str(rh)[:16]}… rpc={rpc.count()}")

    tpl_data["phase2_rpc_added"] = len(added)
    tpl_data["rpc_calls"] = rpc.count()
    json.dump(tpl_data, open(TEMPLATES, "w"), indent=2)
    print(f"Added {len(added)} templates; total={len(tpl_data['templates'])} rpc={rpc.count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
