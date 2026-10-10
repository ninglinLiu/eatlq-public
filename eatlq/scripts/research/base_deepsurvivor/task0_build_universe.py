#!/usr/bin/env python3
"""Task 0: Build deep-survivor universe (point-in-time, shallow filters only)."""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (  # noqa: E402
    BLOCK_SEC,
    DATE_HI,
    DATE_LO,
    HERE,
    MIN_DEPTH_ETH,
    MIN_SWAPS_PRE,
    WETH,
    BudgetRpc,
    ch_query,
    load_json,
    rpc_ledger,
    save_json,
)

OUT = os.path.join(HERE, "_deep_universe.json")
CHECKPOINT = os.path.join(HERE, "_universe_checkpoint.json")
DEPTH_CACHE = os.path.join(HERE, "_depth_cache.json")
ENTRY_AGES_DAYS = [7, 14, 30]

SQL = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0a, lower(token1_address) t1a
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
birth AS (
  SELECT pool_address, min(block_number) AS birth_block, min(block_time) AS birth_time
  FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL
  GROUP BY pool_address
  HAVING birth_time >= '{DATE_LO}' AND birth_time <= '{DATE_HI}'
),
ages AS (
  SELECT 7 AS age_days, 302400 AS age_blocks
  UNION ALL SELECT 14 AS age_days, 604800 AS age_blocks
  UNION ALL SELECT 30 AS age_days, 1296000 AS age_blocks
),
entries AS (
  SELECT b.pool_address AS pool, b.birth_block, b.birth_time,
         a.age_days, b.birth_block + a.age_blocks AS entry_block
  FROM birth b CROSS JOIN ages a
),
active AS (
  SELECT e.pool, e.birth_block, e.birth_time, e.age_days, e.entry_block,
         countIf(s.block_number < e.entry_block AND s.block_number >= e.birth_block) AS ns_pre,
         countIf(s.block_number >= e.entry_block AND s.block_number < e.entry_block + 7200) AS ns_post
  FROM entries e
  INNER JOIN eatlq.uniswap_v3_swaps_decoded s ON s.pool_address = e.pool AND s.chain_id=8453
  GROUP BY e.pool, e.birth_block, e.birth_time, e.age_days, e.entry_block
  HAVING ns_pre >= {MIN_SWAPS_PRE}
),
pre_stats AS (
  SELECT s.pool_address AS pool, a.entry_block,
         count() AS ns_pre,
         uniqExact(s.sender) AS senders_pre,
         uniqExact(s.recipient) AS buyers_pre,
         sum(abs(toFloat64(s.amount0))) + sum(abs(toFloat64(s.amount1))) AS vol_raw
  FROM eatlq.uniswap_v3_swaps_decoded s
  INNER JOIN active a ON s.pool_address = a.pool
  WHERE s.chain_id=8453 AND s.block_number >= a.birth_block AND s.block_number < a.entry_block
  GROUP BY pool, entry_block
)
SELECT
  lower(a.pool) AS pool,
  if(c.t0a='{WETH}', -1, 1) AS orient,
  if(c.t0a='{WETH}', c.t1a, c.t0a) AS alt,
  toUnixTimestamp(a.birth_time) AS birth_ts,
  a.birth_block,
  a.entry_block,
  a.age_days AS age_days_at_entry,
  p.ns_pre, p.senders_pre, p.buyers_pre, p.vol_raw,
  'v3' AS pool_version
FROM active a
INNER JOIN cand c ON a.pool = c.pool_address
INNER JOIN pre_stats p ON a.pool = p.pool AND a.entry_block = p.entry_block
FORMAT TabSeparated
"""


def main():
    print("#### Task 0: Deep universe build ####", flush=True)
    ckpt = load_json(CHECKPOINT, {})
    if ckpt.get("done") and os.path.exists(OUT) and not os.environ.get("FORCE_REBUILD"):
        u = load_json(OUT)
        print(f"  checkpoint: already done, n={u['meta']['n_pools']}")
        return

    print("  querying ClickHouse (entry ages 7/14/30d) ...", flush=True)
    raw = ch_query(SQL, timeout=180)
    rows = []
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        p = ln.split("\t")
        if len(p) < 12:
            continue
        age_d = int(p[6])
        rows.append({
            "pool": p[0], "orient": int(p[1]), "alt": p[2],
            "birth_ts": int(float(p[3])), "birth_block": int(p[4]),
            "entry_block": int(p[5]), "age_days_at_entry": age_d,
            "ns_pre": int(p[7]), "senders_pre": int(p[8]), "buyers_pre": int(p[9]),
            "vol_raw": float(p[10]), "pool_version": p[11],
            "entry_ts": int(float(p[3])) + age_d * 86400,
        })
    rows.sort(key=lambda r: r["ns_pre"], reverse=True)
    print(f"  CH candidates: {len(rows)}")

    depth_cache = load_json(DEPTH_CACHE, {})
    rpc = BudgetRpc()
    depth_budget = 500
    enriched, fetched = [], 0
    seen = set()
    for i, r in enumerate(rows):
        key = f"{r['pool']}:{r['entry_block']}"
        if key in seen:
            continue
        seen.add(key)
        depth = depth_cache.get(key)
        if depth is None and fetched < depth_budget and rpc.budget_remaining() > 0:
            try:
                depth = rpc.weth_depth_eth(r["pool"], r["entry_block"])
                if depth is not None:
                    depth_cache[key] = depth
                    fetched += 1
                    if fetched % 50 == 0:
                        save_json(DEPTH_CACHE, depth_cache)
            except Exception as e:
                print(f"  depth RPC stop: {e}", flush=True)
                break
        if depth is None:
            continue
        if float(depth) >= MIN_DEPTH_ETH:
            r["depth_source"] = "onchain_weth"
            r["depth_eth"] = float(depth)
            enriched.append(r)
        if (i + 1) % 300 == 0:
            print(f"  ...{i+1}/{len(rows)} fetched={fetched} passed={len(enriched)}", flush=True)

    save_json(DEPTH_CACHE, depth_cache)
    ages = [r["age_days_at_entry"] for r in enriched]
    depths = [r["depth_eth"] for r in enriched]
    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "filters": {"min_age_days": min(ENTRY_AGES_DAYS), "entry_ages_days": ENTRY_AGES_DAYS,
                     "min_depth_eth": MIN_DEPTH_ETH, "min_swaps_pre": MIN_SWAPS_PRE},
        "n_ch_candidates": len(rows), "n_pools": len(enriched),
        "depth_rpc_fetched": fetched,
        "age_days": {"min": min(ages) if ages else 0, "median": sorted(ages)[len(ages)//2] if ages else 0},
        "depth_eth": {"min": min(depths) if depths else 0,
                      "median": sorted(depths)[len(depths)//2] if depths else 0,
                      "p90": sorted(depths)[int(len(depths)*0.9)] if depths else 0},
        "v3_count": len(enriched), "v4_count": 0, "rpc": rpc_ledger(),
    }
    save_json(OUT, {"meta": meta, "rows": enriched})
    save_json(CHECKPOINT, {"done": True, "n": len(enriched)})
    print(f"  wrote {OUT} n={len(enriched)} depth_rpc={fetched}")


if __name__ == "__main__":
    main()
