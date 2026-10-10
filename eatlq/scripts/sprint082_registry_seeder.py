#!/usr/bin/env python3
"""
Sprint082 Registry Seeder
==========================
Seeds Redis pool:discovered:{pool} hashes for all 49 sender-gated pools discovered
in sprint078_shadow.csv + sprint080 sender map. This allows the executor's dynamic
_lookup_discovered() to route signals for pools not hardcoded in registry.py.

Run once (or re-run — it's idempotent):
    python3 scripts/sprint082_registry_seeder.py

Pool metadata priority:
  1. Token symbol/decimals from RPC (ERC20.symbol / .decimals)
  2. Sprint080 sender_map.json for dominant_sender info
  3. UNKNOWN / 18 as safe defaults

Safety: read-only RPC calls only. No transactions.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from typing import Dict, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("sprint082_registry_seeder")

WETH = "0x4200000000000000000000000000000000000006"
UNISWAP_V3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
BASE_CHAIN_ID = 8453

_ERC20_ABI = json.loads(
    '[{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},'
    '{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]'
)

# Known token metadata (supplement RPC where possible)
_KNOWN_TOKENS: Dict[str, Tuple[str, int]] = {
    WETH.lower(): ("WETH", 18),
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC", 6),
    "0x940181a94a35a4569e4529a3cdfb74e38fd98631": ("AERO", 18),
    "0x4ed4e862860be2ec1f4896764335d5942baadf07": ("DEGEN", 18),
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": ("cbBTC", 8),
    "0x2ae3f1ec7f1f5012cfeab0185bfcb5aa7ac6e90f": ("cbETH", 18),
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("DAI", 18),
}


def _get_redis():
    import redis as _redis
    url = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")
    r = _redis.from_url(url, decode_responses=True)
    r.ping()
    return r


def _get_w3():
    try:
        from web3 import Web3
        rpc = (
            os.environ.get("EXECUTOR_RPC_URL")
            or os.environ.get("EVM_HTTP_URL")
            or "https://mainnet.base.org"
        )
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        if w3.is_connected():
            return w3
    except Exception:
        pass
    return None


def resolve_token(w3, addr: str) -> Tuple[str, int]:
    addr_l = addr.lower()
    if addr_l in _KNOWN_TOKENS:
        return _KNOWN_TOKENS[addr_l]
    if w3 is None:
        return ("UNKNOWN", 18)
    try:
        from web3 import Web3
        c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=_ERC20_ABI)
        sym = c.functions.symbol().call()[:16]
        dec = int(c.functions.decimals().call())
        _KNOWN_TOKENS[addr_l] = (sym, dec)
        return (sym, dec)
    except Exception:
        return ("UNKNOWN", 18)


def load_pools() -> list:
    """Load sender-gated pool metadata from sprint078_shadow.csv."""
    csv_path = os.environ.get("SPRINT078_CSV", "/opt/EATLQ/logs/sprint078_shadow.csv")
    pools: Dict[str, dict] = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("is_0x6ff5") != "True":
                continue
            pool = row["pool"].lower()
            if pool in pools:
                continue
            fee_ppm = int(float(row.get("fee_bps", 0)))
            weth_is_t1 = row.get("weth_is_token1", "").lower() in ("true", "1")
            token_other = row["token_other"].lower()
            pools[pool] = {
                "pool": pool,
                "token_other": token_other,
                "fee_ppm": fee_ppm,
                "fee_bps_reg": max(1, fee_ppm // 100),
                "weth_is_t1": weth_is_t1,
                "dominant_sender": row.get("dominant_sender", ""),
                "dom_sender_share": row.get("dom_sender_share", ""),
            }
    return list(pools.values())


def seed_pool(r, pool_meta: dict, w3) -> bool:
    """Write pool:discovered:{pool} hash to Redis. Idempotent."""
    pool = pool_meta["pool"]
    key = f"pool:discovered:{pool}"

    token_other = pool_meta["token_other"]
    weth_is_t1 = pool_meta["weth_is_t1"]
    fee_bps_reg = pool_meta["fee_bps_reg"]

    # Resolve symbols
    sym_other, dec_other = resolve_token(w3, token_other)
    weth_sym, weth_dec = "WETH", 18

    if weth_is_t1:
        t0_addr, t0_sym, t0_dec = token_other, sym_other, dec_other
        t1_addr, t1_sym, t1_dec = WETH.lower(), weth_sym, weth_dec
    else:
        t0_addr, t0_sym, t0_dec = WETH.lower(), weth_sym, weth_dec
        t1_addr, t1_sym, t1_dec = token_other, sym_other, dec_other

    now_ms = int(time.time() * 1000)
    fields = {
        "pool_address": pool,
        "pool_type": "v3",
        "chain_id": str(BASE_CHAIN_ID),
        "token0_address": t0_addr,
        "token0_symbol": t0_sym,
        "token0_decimals": str(t0_dec),
        "token1_address": t1_addr,
        "token1_symbol": t1_sym,
        "token1_decimals": str(t1_dec),
        "fee_bps": str(fee_bps_reg),
        "has_weth_leg": "1",
        "factory_address": "0x33128a8fc17869897dce68ed026d694621f6fdfd",  # Uniswap V3 factory Base
        "discovered_at_ms": str(now_ms),
        "seeded_by": "sprint082_registry_seeder",
        "dominant_sender": pool_meta.get("dominant_sender", ""),
        "dom_sender_share": pool_meta.get("dom_sender_share", ""),
        "is_0x6ff5": "1",
    }

    try:
        r.hset(key, mapping=fields)
        return True
    except Exception as e:
        log.error("hset failed for %s: %s", pool, e)
        return False


def main() -> None:
    log.info("Connecting to Redis...")
    try:
        r = _get_redis()
        log.info("Redis connected")
    except Exception as e:
        log.error("Redis connection failed: %s", e)
        sys.exit(1)

    log.info("Loading sender-gated pools from sprint078...")
    pools = load_pools()
    log.info("Loaded %d sender-gated pools", len(pools))

    w3 = _get_w3()
    if w3:
        log.info("RPC connected for token resolution")
    else:
        log.warning("No RPC — token symbols will default to UNKNOWN")

    seeded = 0
    already_existed = 0
    for i, pool_meta in enumerate(pools):
        key = f"pool:discovered:{pool_meta['pool']}"
        existed = r.exists(key)
        ok = seed_pool(r, pool_meta, w3)
        if ok:
            if existed:
                already_existed += 1
            else:
                seeded += 1
        # Brief rate-limit pause every 10 pools when resolving via RPC
        if w3 and i > 0 and i % 10 == 0:
            time.sleep(0.3)

    log.info(
        "Done: %d newly seeded, %d updated (existed), %d total",
        seeded, already_existed, len(pools),
    )

    # Verify one
    sample = pools[0]["pool"] if pools else None
    if sample:
        check = r.hgetall(f"pool:discovered:{sample}")
        log.info(
            "Verification sample pool=%s token0=%s/%s token1=%s/%s fee_bps=%s",
            sample[:20],
            check.get("token0_symbol", "?"),
            check.get("token0_decimals", "?"),
            check.get("token1_symbol", "?"),
            check.get("token1_decimals", "?"),
            check.get("fee_bps", "?"),
        )


if __name__ == "__main__":
    main()
