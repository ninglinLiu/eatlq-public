"""
Shadow Closer -- independently scans shadow:signals for entries older than 30 minutes,
computes exit quotes, and marks them closed. Runs as a standalone loop, decoupled from
the executor lifecycle.

Idempotent: each entry is closed exactly once (status field transitions open -> closed).

Usage:
    python scripts/shadow_closer.py

Env vars:
    REDIS_URL           (default: redis://redis:6379/0)
    EVM_HTTP_URL        (RPC for exit quote computation)
    SHADOW_HOLD_MIN     (default: 30)
    ROUND_TRIP_COST     (default: 0.006)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import redis
from web3 import Web3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("shadow_closer")

WETH_BASE = "0x4200000000000000000000000000000000000006"
QUOTER_V2_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
SHADOW_LIST_KEY = "shadow:signals"

PAIR_ABI = json.loads(
    '[{"constant":true,"inputs":[],"name":"getReserves",'
    '"outputs":[{"name":"","type":"uint112"},{"name":"","type":"uint112"},'
    '{"name":"","type":"uint32"}],"stateMutability":"view","type":"function"},'
    '{"constant":true,"inputs":[],"name":"token0",'
    '"outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}]'
)

QUOTER_ABI = json.loads(
    '[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},'
    '{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},'
    '{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],'
    '"name":"params","type":"tuple"}],"name":"quoteExactInputSingle",'
    '"outputs":[{"name":"amountOut","type":"uint256"},'
    '{"name":"sqrtPriceX96After","type":"uint160"},'
    '{"name":"initializedTicksCrossed","type":"uint32"},'
    '{"name":"gasEstimate","type":"uint256"}],'
    '"stateMutability":"nonpayable","type":"function"}]'
)


def _exit_quote_v2(w3: Web3, pool_address: str, token_amount: int) -> int:
    """Compute WETH received for selling token_amount via V2 CPMM."""
    try:
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pool_address), abi=PAIR_ABI,
        )
        t0 = pair.functions.token0().call().lower()
        r0, r1, _ = pair.functions.getReserves().call()
        if t0 == WETH_BASE.lower():
            reserve_out, reserve_in = r0, r1
        else:
            reserve_out, reserve_in = r1, r0
        if reserve_in == 0 or reserve_out == 0:
            return 0
        amt_fee = token_amount * 997
        return (amt_fee * reserve_out) // (reserve_in * 1000 + amt_fee)
    except Exception:
        return 0


def _exit_quote_v3(w3: Web3, pool_address: str, token_amount: int,
                   fee_bps: int = 30, r_conn: "redis.Redis | None" = None) -> int:
    """Compute WETH received for selling token_amount via V3 QuoterV2."""
    try:
        pool_meta_key = f"pool:discovered:{pool_address.lower()}"
        if r_conn is None:
            r_conn = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                    decode_responses=True)
        meta = r_conn.hgetall(pool_meta_key)
        weth_is_t0 = meta.get("token0_address", "").lower() == WETH_BASE.lower()
        token_addr = meta.get("token1_address" if weth_is_t0 else "token0_address", "")
        if not token_addr:
            return 0
        quoter = w3.eth.contract(
            address=Web3.to_checksum_address(QUOTER_V2_BASE), abi=QUOTER_ABI,
        )
        token_cs = Web3.to_checksum_address(token_addr)
        weth_cs = Web3.to_checksum_address(WETH_BASE)
        fee = fee_bps * 100
        result = quoter.functions.quoteExactInputSingle(
            (token_cs, weth_cs, token_amount, fee, 0)
        ).call()
        return int(result[0])
    except Exception:
        return 0


def _compute_exit(w3: Web3, record: dict, r_conn: "redis.Redis | None" = None) -> dict:
    """Compute exit quote for a shadow record. Returns updated record."""
    pool_address = record["pool_address"]
    token_amount = int(record.get("expected_token_out", 0))
    notional = int(record.get("notional_in_wei", 0))
    pool_type = record.get("pool_type", "v2")
    round_trip_cost = float(os.environ.get("ROUND_TRIP_COST", "0.006"))

    if token_amount <= 0 or notional <= 0:
        record["exit_weth_out_wei"] = "0"
        record["net_return_pct"] = -100.0
        record["status"] = "closed"
        record["closed_at_ms"] = int(time.time() * 1000)
        return record

    if pool_type in ("v2", "aero_vamm"):
        exit_wei = _exit_quote_v2(w3, pool_address, token_amount)
    else:
        fee_bps = 30
        try:
            if r_conn is None:
                r_conn = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                        decode_responses=True)
            meta = r_conn.hgetall(f"pool:discovered:{pool_address.lower()}")
            fee_bps = int(meta.get("fee_bps", "30"))
        except Exception:
            pass
        exit_wei = _exit_quote_v3(w3, pool_address, token_amount, fee_bps, r_conn)

    if exit_wei > 0 and notional > 0:
        gross_return = exit_wei / notional - 1.0
        net_return = gross_return - round_trip_cost
    else:
        net_return = -1.0

    record["exit_weth_out_wei"] = str(exit_wei)
    record["net_return_pct"] = round(net_return * 100, 4)
    record["status"] = "closed"
    record["closed_at_ms"] = int(time.time() * 1000)
    return record


def run() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    rpc_url = os.environ.get("EVM_HTTP_URL") or os.environ.get("EXECUTOR_RPC_URL", "")
    hold_min = int(os.environ.get("SHADOW_HOLD_MIN", "30"))
    poll_sec = int(os.environ.get("SHADOW_CLOSER_POLL_SEC", "60"))

    r = redis.from_url(redis_url, decode_responses=True)
    w3 = Web3(Web3.HTTPProvider(rpc_url)) if rpc_url else None

    if w3 and not w3.is_connected():
        log.error("cannot connect RPC %s", rpc_url)
        w3 = None

    log.info("shadow_closer started (hold=%dmin, poll=%ds, rpc=%s)",
             hold_min, poll_sec, "connected" if w3 else "none")

    while True:
        try:
            now_ms = int(time.time() * 1000)
            cutoff_ms = now_ms - hold_min * 60 * 1000
            total = r.llen(SHADOW_LIST_KEY)
            closed_count = 0

            for i in range(total - 1, -1, -1):
                raw = r.lindex(SHADOW_LIST_KEY, i)
                if raw is None:
                    continue
                try:
                    record = json.loads(raw)
                except Exception:
                    continue

                if record.get("status") == "closed":
                    continue

                signal_ts = int(record.get("signal_ts_ms", 0))
                if signal_ts == 0 or signal_ts > cutoff_ms:
                    continue

                if w3 is not None:
                    record = _compute_exit(w3, record, r)
                else:
                    record["exit_weth_out_wei"] = "0"
                    record["net_return_pct"] = 0.0
                    record["status"] = "closed"
                    record["closed_at_ms"] = now_ms

                r.lset(SHADOW_LIST_KEY, i, json.dumps(record, ensure_ascii=True))
                closed_count += 1

            if closed_count > 0:
                log.info("closed %d shadow signals", closed_count)

        except Exception as e:
            log.error("shadow_closer cycle error: %s", e)

        time.sleep(poll_sec)


if __name__ == "__main__":
    run()
