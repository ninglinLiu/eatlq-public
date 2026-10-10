#!/usr/bin/env python3
"""
Sprint092 pool-meta backfill — RPC-based.

Fixes the data gap discovered in sprint092 v2 readout:
  - 5 active Base pools (~791k swaps/21d) are NOT_IN_DISCOVERED
  - 3 active pools (~100k swaps/21d) have EMPTY_TOKEN_ADDR
  - 45 pools have BAD_FEE_BPS (fee in {1,2,3,5} non-standard encodings, but token meta OK)

For NOT_IN_DISCOVERED + EMPTY_TOKEN_ADDR, calls Base RPC for token0()/token1()/fee()
and decimals() per token, then writes to a new CH table:
    eatlq.discovered_pools_sprint092_backfill

Sprint092 SQL UNIONs the production discovered_pools with this backfill view,
and relaxes the strict fee_bps tier filter to include the BAD_FEE_BPS pools whose
token meta is already valid in discovered_pools.

NOT a production fix to discovered_pools — sprint092-scoped only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query  # noqa: E402

log = logging.getLogger("sprint092_backfill")

BACKFILL_TABLE = "eatlq.discovered_pools_sprint092_backfill"

# Solidity selectors (keccak256 first 4 bytes)
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_FEE = "0xddca3f43"
SEL_DECIMALS = "0x313ce567"

# Quote tokens (need these resolved for USD notional)
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"


def find_pools_needing_backfill(days: int = 21) -> list[dict[str, Any]]:
    """Pools active in last N days but missing/broken meta in discovered_pools."""
    sql = f"""
    WITH active AS (
        SELECT lower(pool_address) AS pool, count() AS n_swaps
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = {CHAIN_ID} AND block_time >= now() - INTERVAL {days} DAY
        GROUP BY pool
    )
    SELECT
        a.pool AS pool,
        a.n_swaps AS n_swaps,
        multiIf(
            dp.pool_address = '', 'NOT_IN_DISCOVERED',
            length(dp.token0_address) != 42 OR length(dp.token1_address) != 42, 'EMPTY_TOKEN_ADDR',
            'OTHER'
        ) AS reason
    FROM active a
    LEFT JOIN eatlq.discovered_pools dp FINAL
        ON a.pool = lower(dp.pool_address) AND dp.chain_id = {CHAIN_ID}
    WHERE reason IN ('NOT_IN_DISCOVERED', 'EMPTY_TOKEN_ADDR')
    ORDER BY a.n_swaps DESC
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=60)


def rpc_call(rpc_url: str, to: str, data: str, timeout: int = 15) -> str | None:
    try:
        r = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            return None
        return body.get("result")
    except Exception as exc:
        log.debug("rpc_call %s %s: %s", to[:10], data[:10], exc)
        return None


def hex_to_address(hex_val: str | None) -> str | None:
    if not hex_val or not hex_val.startswith("0x"):
        return None
    h = hex_val[2:].lower().zfill(64)
    if len(h) < 64:
        return None
    return "0x" + h[-40:]


def hex_to_int(hex_val: str | None) -> int | None:
    if not hex_val or not hex_val.startswith("0x"):
        return None
    try:
        return int(hex_val, 16)
    except ValueError:
        return None


def fetch_pool_meta(rpc_url: str, pool: str) -> dict[str, Any] | None:
    """Resolve token0/token1/fee for a V3-style pool via eth_call."""
    t0_raw = rpc_call(rpc_url, pool, SEL_TOKEN0)
    t1_raw = rpc_call(rpc_url, pool, SEL_TOKEN1)
    fee_raw = rpc_call(rpc_url, pool, SEL_FEE)
    token0 = hex_to_address(t0_raw)
    token1 = hex_to_address(t1_raw)
    fee = hex_to_int(fee_raw)
    if not token0 or not token1:
        return None
    has_weth = (token0 == WETH) or (token1 == WETH)
    return {
        "pool_address": pool.lower(),
        "token0_address": token0,
        "token1_address": token1,
        "fee_raw": fee,
        "fee_bps": (fee // 100) if fee is not None else 0,  # protocol fee → discovered_pools meta (1/5/30/100)
        "has_weth_leg": 1 if has_weth else 0,
    }


def fetch_decimals_batch(rpc_url: str, tokens: set[str], workers: int = 8) -> dict[str, int]:
    out: dict[str, int] = {}

    def worker(t: str) -> tuple[str, int | None]:
        raw = rpc_call(rpc_url, t, SEL_DECIMALS)
        return t, hex_to_int(raw)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t, d in pool.map(worker, sorted(tokens)):
            if d is not None and 0 < d <= 36:
                out[t] = d
            else:
                # WETH/USDC/USDbC sane defaults
                if t == WETH:
                    out[t] = 18
                elif t in (USDC, USDBC):
                    out[t] = 6
                else:
                    out[t] = 18  # ERC20 typical default
    return out


def create_backfill_table() -> None:
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {BACKFILL_TABLE} (
        chain_id UInt64,
        pool_address String,
        token0_address String,
        token1_address String,
        token0_decimals UInt8,
        token1_decimals UInt8,
        fee_bps UInt32,
        fee_raw UInt32,
        has_weth_leg UInt8,
        backfilled_at DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(backfilled_at)
    ORDER BY (chain_id, pool_address)
    """
    ch_query(ddl, timeout=30)


def insert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    values = []
    for r in rows:
        values.append(
            f"({CHAIN_ID}, '{r['pool_address']}', '{r['token0_address']}', '{r['token1_address']}', "
            f"{r['token0_decimals']}, {r['token1_decimals']}, {r['fee_bps']}, {r['fee_raw']}, "
            f"{r['has_weth_leg']})"
        )
    sql = (
        f"INSERT INTO {BACKFILL_TABLE} "
        f"(chain_id, pool_address, token0_address, token1_address, "
        f"token0_decimals, token1_decimals, fee_bps, fee_raw, has_weth_leg) "
        f"VALUES {','.join(values)}"
    )
    ch_query(sql, timeout=60)


def run(*, days: int = 21, workers: int = 6) -> dict[str, Any]:
    rpc = os.environ.get("EVM_HTTP_URL") or os.environ.get("LABEL_RPC_URL")
    if not rpc:
        raise RuntimeError("Set EVM_HTTP_URL or LABEL_RPC_URL")

    create_backfill_table()
    pending = find_pools_needing_backfill(days)
    log.info("pools needing backfill: %d", len(pending))

    if not pending:
        return {"resolved": 0, "pending": 0}

    pool_metas: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_pool_meta, rpc, row["pool"]): row for row in pending}
        for fut in as_completed(futs):
            row = futs[fut]
            meta = fut.result()
            if meta:
                meta["n_swaps_21d"] = int(row.get("n_swaps") or 0)
                pool_metas.append(meta)
                log.info(
                    "pool %s: token0=%s token1=%s fee_raw=%s weth=%d",
                    meta["pool_address"][:10],
                    meta["token0_address"][:10],
                    meta["token1_address"][:10],
                    meta["fee_raw"],
                    meta["has_weth_leg"],
                )

    # Collect all unique tokens
    tokens: set[str] = set()
    for m in pool_metas:
        tokens.add(m["token0_address"])
        tokens.add(m["token1_address"])
    log.info("unique tokens to resolve decimals: %d", len(tokens))

    decimals_map = fetch_decimals_batch(rpc, tokens, workers=workers)
    for m in pool_metas:
        m["token0_decimals"] = decimals_map.get(m["token0_address"], 18)
        m["token1_decimals"] = decimals_map.get(m["token1_address"], 18)

    insert_rows(pool_metas)
    return {
        "resolved": len(pool_metas),
        "pending": len(pending),
        "with_weth_leg": sum(1 for m in pool_metas if m["has_weth_leg"]),
        "tokens_resolved": len(decimals_map),
        "table": BACKFILL_TABLE,
        "pools": [
            {
                "pool": m["pool_address"],
                "n_swaps_21d": m["n_swaps_21d"],
                "fee_raw": m["fee_raw"],
                "has_weth_leg": m["has_weth_leg"],
                "token0_dec": m["token0_decimals"],
                "token1_dec": m["token1_decimals"],
            }
            for m in sorted(pool_metas, key=lambda x: -x["n_swaps_21d"])
        ],
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Sprint092 pool-meta backfill (one-shot RPC)")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()
    result = run(days=args.days, workers=args.workers)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
