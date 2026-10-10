#!/usr/bin/env python3
"""P1-9: Backfill token metadata for Aerodrome pools in discovered_pools (5 rows, all empty)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

import requests

try:
    from eth_utils import function_signature_to_4byte_selector
except ImportError:
    function_signature_to_4byte_selector = None  # type: ignore

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from lib.fee_bps_encoding import protocol_to_meta_fee  # noqa: E402

log = logging.getLogger("p1_aero_backfill")

CHAIN_ID = int(os.environ.get("CHAIN_ID", "8453"))
CH_CONTAINER = os.environ.get("CH_CONTAINER", "eatlq-clickhouse-1")
RPC_URL = (
    os.environ.get("LABEL_RPC_URL")
    or os.environ.get("FREE_RPC_URL")
    or "https://mainnet.base.org"
).strip()
RPC_SLEEP = float(os.environ.get("P0_POOL_RPC_SLEEP_SEC", "0.5"))

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AERO = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
KNOWN_SYMBOLS = {WETH: "WETH", USDC: "USDC", AERO: "AERO"}
KNOWN_DECIMALS = {WETH: 18, USDC: 6, AERO: 18}
FACTORY_AERO_VAMM = "0x420dd381b31aef6683db6b902084cb0ffece40da"
FACTORY_AERO_CL = "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a"

SEL = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "decimals": "0x313ce567",
    "symbol": "0x95d89b41",
    "stable": "0x22be3de1",
}


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
    time.sleep(RPC_SLEEP)
    r = requests.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        },
        timeout=20,
        headers={"User-Agent": os.environ.get("RPC_USER_AGENT", "eatlq-aero-backfill/1.0")},
    )
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


def list_aero_gaps() -> list[dict[str, str]]:
    sql = f"""
SELECT lower(pool_address) AS pool, pool_type
FROM eatlq.discovered_pools FINAL
WHERE chain_id={CHAIN_ID}
  AND pool_type IN ('aero_vamm','aero_cl')
  AND (length(token0_address)!=42 OR length(token1_address)!=42)
FORMAT JSONEachRow
"""
    rows = []
    for line in ch_query(sql).splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def fetch_meta(pool: str, pool_type: str) -> dict[str, Any] | None:
    token0 = decode_addr(rpc_call(pool, SEL["token0"]))
    token1 = decode_addr(rpc_call(pool, SEL["token1"]))
    if not token0 or not token1:
        return None

    if pool_type == "aero_vamm":
        stable_raw = rpc_call(pool, SEL["stable"])
        is_stable = decode_int(stable_raw) == 1
        fee_bps = 5 if is_stable else 30
        factory = FACTORY_AERO_VAMM
    else:
        fee_raw = decode_int(rpc_call(pool, SEL["fee"]))
        fee_bps = protocol_to_meta_fee(fee_raw) if fee_raw else 30
        factory = FACTORY_AERO_CL

    sym0 = KNOWN_SYMBOLS.get(token0) or decode_symbol(rpc_call(token0, SEL["symbol"]))
    sym1 = KNOWN_SYMBOLS.get(token1) or decode_symbol(rpc_call(token1, SEL["symbol"]))
    dec0 = decode_int(rpc_call(token0, SEL["decimals"])) or KNOWN_DECIMALS.get(token0, 18)
    dec1 = decode_int(rpc_call(token1, SEL["decimals"])) or KNOWN_DECIMALS.get(token1, 18)

    return {
        "chain_id": CHAIN_ID,
        "pool_address": pool.lower(),
        "factory_address": factory,
        "pool_type": pool_type,
        "token0_address": token0,
        "token0_symbol": sym0,
        "token0_decimals": dec0,
        "token1_address": token1,
        "token1_symbol": sym1,
        "token1_decimals": dec1,
        "fee_bps": fee_bps,
        "has_weth_leg": 1 if WETH in (token0, token1) else 0,
        "created_block": 0,
        "created_tx_hash": "",
    }


def insert_row(row: dict[str, Any]) -> None:
    sql = (
        "INSERT INTO eatlq.discovered_pools "
        "(chain_id,pool_address,factory_address,pool_type,"
        "token0_address,token0_symbol,token0_decimals,"
        "token1_address,token1_symbol,token1_decimals,"
        "fee_bps,has_weth_leg,created_block,created_tx_hash) "
        f"VALUES ({row['chain_id']},'{row['pool_address']}','{row['factory_address']}',"
        f"'{row['pool_type']}','{row['token0_address']}','{row['token0_symbol']}',"
        f"{row['token0_decimals']},'{row['token1_address']}','{row['token1_symbol']}',"
        f"{row['token1_decimals']},{row['fee_bps']},{row['has_weth_leg']},"
        f"{row['created_block']},'{row['created_tx_hash']}')"
    )
    ch_query(sql)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gaps = list_aero_gaps()
    log.info("aerodrome pools needing metadata: %d", len(gaps))
    inserted = []
    for g in gaps:
        meta = fetch_meta(g["pool"], g["pool_type"])
        if not meta:
            log.warning("failed %s (%s)", g["pool"], g["pool_type"])
            continue
        insert_row(meta)
        inserted.append(meta)
        log.info(
            "inserted %s %s/%s fee_bps=%s",
            g["pool"][:12],
            meta["token0_symbol"],
            meta["token1_symbol"],
            meta["fee_bps"],
        )
    print(json.dumps({"inserted": len(inserted), "gaps": len(gaps), "pools": inserted}, indent=2))
    return 0 if len(inserted) == len(gaps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
