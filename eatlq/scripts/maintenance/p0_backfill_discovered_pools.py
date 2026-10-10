#!/usr/bin/env python3
"""P0-4: Backfill missing high-volume pools into eatlq.discovered_pools via free RPC."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

import requests

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from lib.rpc_budget_guard import record_rpc_call, require_rpc_budget  # noqa: E402

log = logging.getLogger("p0_backfill_pools")

CHAIN_ID = int(os.environ.get("CHAIN_ID", "8453"))
CH_CONTAINER = os.environ.get("CH_CONTAINER", "eatlq-clickhouse-1")
RPC_URL = (
    os.environ.get("LABEL_RPC_URL")
    or os.environ.get("FREE_RPC_URL")
    or os.environ.get("EVM_HTTP_URL")
    or "https://mainnet.base.org"
).strip()

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
KNOWN_DECIMALS = {WETH: 18, USDC: 6, USDBC: 6}
FACTORY_UNISWAP_V3 = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
RPC_SLEEP_SEC = float(os.environ.get("P0_POOL_RPC_SLEEP_SEC", "0.35"))

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_FEE = "0xddca3f43"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"


def ch_query(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client", "--query", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout)
    return r.stdout


def rpc_call(to: str, data: str) -> str | None:
    require_rpc_budget(1)
    time.sleep(RPC_SLEEP_SEC)
    r = requests.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        },
        timeout=20,
    )
    record_rpc_call(1)
    body = r.json()
    if body.get("error"):
        return None
    return body.get("result")


def decode_addr(raw: str | None) -> str:
    if not raw or not raw.startswith("0x"):
        return ""
    return "0x" + raw[2:].zfill(64)[-40:].lower()


def decode_int(raw: str | None) -> int:
    if not raw or not raw.startswith("0x"):
        return 0
    return int(raw, 16)


def decode_symbol(raw: str | None) -> str:
    if not raw or len(raw) < 130:
        return "UNKNOWN"
    try:
        offset = decode_int(raw[:66]) * 2
        strlen = decode_int("0x" + raw[2 + offset : 2 + offset + 64])
        hexstr = raw[2 + offset + 64 : 2 + offset + 64 + strlen * 2]
        return bytes.fromhex(hexstr).decode("utf-8", errors="replace") or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def find_missing_pools(days: int = 30) -> list[dict[str, Any]]:
    sql = f"""
SELECT a.pool AS pool_address, a.n_swaps
FROM (
  SELECT lower(pool_address) AS pool, count() AS n_swaps
  FROM eatlq.uniswap_v3_swaps
  WHERE chain_id={CHAIN_ID} AND block_time >= now() - INTERVAL {days} DAY
  GROUP BY pool
) a
LEFT JOIN (
  SELECT lower(pool_address) AS pool_address
  FROM eatlq.discovered_pools WHERE chain_id={CHAIN_ID}
) dp ON a.pool = dp.pool_address
WHERE dp.pool_address = ''
ORDER BY a.n_swaps DESC
FORMAT JSONEachRow
"""
    rows = []
    for line in ch_query(sql).splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def fetch_pool_meta(pool: str) -> dict[str, Any] | None:
    token0 = decode_addr(rpc_call(pool, SEL_TOKEN0))
    token1 = decode_addr(rpc_call(pool, SEL_TOKEN1))
    fee_raw = decode_int(rpc_call(pool, SEL_FEE))
    if not token0 or not token1:
        return None
    fee_bps = fee_raw // 100 if fee_raw else 0
    sym0 = decode_symbol(rpc_call(token0, SEL_SYMBOL))
    sym1 = decode_symbol(rpc_call(token1, SEL_SYMBOL))
    dec0 = decode_int(rpc_call(token0, SEL_DECIMALS)) or KNOWN_DECIMALS.get(token0, 18)
    dec1 = decode_int(rpc_call(token1, SEL_DECIMALS)) or KNOWN_DECIMALS.get(token1, 18)
    return {
        "chain_id": CHAIN_ID,
        "pool_address": pool.lower(),
        "factory_address": FACTORY_UNISWAP_V3,
        "pool_type": "v3",
        "token0_address": token0,
        "token0_symbol": sym0,
        "token0_decimals": dec0,
        "token1_address": token1,
        "token1_symbol": sym1,
        "token1_decimals": dec1,
        "fee_bps": fee_bps,
        "fee_raw": fee_raw,
        "has_weth_leg": 1 if WETH in (token0, token1) else 0,
        "created_block": 0,
        "created_tx_hash": "",
    }


def insert_pool(row: dict[str, Any]) -> None:
    sql = (
        "INSERT INTO eatlq.discovered_pools "
        "(chain_id, pool_address, factory_address, pool_type, "
        "token0_address, token0_symbol, token0_decimals, "
        "token1_address, token1_symbol, token1_decimals, "
        "fee_bps, has_weth_leg, created_block, created_tx_hash) "
        f"VALUES ({row['chain_id']}, '{row['pool_address']}', '{row['factory_address']}', "
        f"'{row['pool_type']}', '{row['token0_address']}', '{row['token0_symbol']}', "
        f"{row['token0_decimals']}, '{row['token1_address']}', '{row['token1_symbol']}', "
        f"{row['token1_decimals']}, {row['fee_bps']}, {row['has_weth_leg']}, "
        f"{row['created_block']}, '{row['created_tx_hash']}')"
    )
    ch_query(sql)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    missing = find_missing_pools()
    log.info("missing pools (30d active): %d", len(missing))
    if not missing:
        print(json.dumps({"inserted": 0, "missing": 0}))
        return 0

    inserted = []
    for item in missing:
        pool = item["pool_address"]
        meta = fetch_pool_meta(pool)
        if not meta:
            log.warning("failed to resolve %s", pool)
            continue
        insert_pool(meta)
        meta["n_swaps_30d"] = int(item.get("n_swaps", 0))
        inserted.append(meta)
        log.info(
            "inserted %s fee_bps=%s (%s/%s) swaps_30d=%s",
            pool[:12],
            meta["fee_bps"],
            meta["token0_symbol"],
            meta["token1_symbol"],
            meta["n_swaps_30d"],
        )

    out = {"inserted": len(inserted), "missing": len(missing), "pools": inserted}
    print(json.dumps(out, indent=2, default=str))
    return 0 if inserted else 1


if __name__ == "__main__":
    raise SystemExit(main())
