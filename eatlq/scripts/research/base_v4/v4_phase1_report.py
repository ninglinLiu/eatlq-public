#!/usr/bin/env python3
"""Phase 1 status + hook distribution summary for V4 alpha research."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
POOLS = "/tmp/v4_created_pools.jsonl"
CP = "/tmp/v4_listener_checkpoint.json"
RPC_COUNTER = "/tmp/v4_research_rpc_counter.json"


def main() -> int:
    if not os.path.exists(POOLS):
        print("No pool data yet.")
        return 1
    rows = []
    with open(POOLS) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    tiers = Counter(r.get("hook_profile", {}).get("risk_tier", "?") for r in rows)
    weth = sum(1 for r in rows if r.get("has_weth"))
    rd = sum(1 for r in rows if r.get("hook_profile", {}).get("has_returns_delta"))
    vanilla = sum(1 for r in rows if r.get("hook_profile", {}).get("is_vanilla"))
    blocks = [r["blockNumber"] for r in rows]
    cp = json.load(open(CP)) if os.path.exists(CP) else {}
    rpc = json.load(open(RPC_COUNTER)) if os.path.exists(RPC_COUNTER) else {}

    print("=== V4 Phase-1 Ground Truth Dataset ===")
    print(f"pools_total     : {len(rows)}")
    print(f"weth_pools      : {weth}")
    print(f"vanilla (no hook): {vanilla} ({100*vanilla/len(rows):.1f}%)")
    print(f"returns_delta   : {rd} ({100*rd/len(rows):.1f}%)")
    print(f"block_range     : {min(blocks)} .. {max(blocks)}")
    print(f"checkpoint_block: {cp.get('last_scanned_block')}")
    print(f"rpc_eth_calls   : {rpc.get('eth_calls', 0)}")
    print("\nHook risk tiers:")
    for k, v in tiers.most_common():
        print(f"  {k:25s} {v:6d} ({100*v/len(rows):.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
