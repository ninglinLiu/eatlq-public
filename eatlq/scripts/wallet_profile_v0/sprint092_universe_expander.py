#!/usr/bin/env python3
"""Sprint092 universe expansion v2 — single-buy $100k event, target-aware, day-paginated.

Fixes vs v1:
  1. tick→return uses 1.0001^Δtick (logarithmic, V3 native)
  2. tick direction = +1 if target=token0, -1 if target=token1 (per pool meta)
  3. WHALE_USD_THRESH = single swap notional ≥$100k (NOT cumulative wallet tag)
  5. day-paginated discovery (no LIMIT truncation)
  6. is_buy = target-leg outflow (amount0<0 OR amount1<0 depending on quote side)
  8. USD-normalized flows so volatility/activity are quote-leg invariant
"""

from __future__ import annotations

import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from ch_helper import CHAIN_ID, ch_query

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from fee_bps_encoding import FEE_BPS_META_STANDARD, fee_bps_sql_in  # noqa: E402

log = logging.getLogger("sprint092_universe")

# Whitepaper / design constants
SIGNAL_THRESHOLD = 0.52
DASHCAM_THRESHOLD = 0.40
BASE_CAPITAL_USD = 25.0
WHALE_USD_THRESH = 100_000  # design §3: single buy proxy
POOL_MAX_AGE_SEC = 72 * 3600
ETH_USD = float(os.environ.get("WALLET_PROFILE_ETH_USD", "3500"))
# discovered_pools.fee_bps uses meta encoding (1/5/30/100), NOT protocol (100/500/3000/10000)
FEE_TIERS_BPS: tuple[int, ...] = FEE_BPS_META_STANDARD

# Honeypot pre-filter window (backtest-safe: only uses data prior to signal_ts)
HONEYPOT_LOOKBACK_SEC = 6 * 3600  # 6h pre-signal sell-path proxy

# Quote tokens on Base (anchor for USD notional + direction sign).
# Mirrors services/strategy/src/feature_store.py::_QUOTE_PRIORITY_BASE.
# Whitepaper-faithful: a pool whose TARGET (non-quote) leg is itself in this set
# is treated as a base-routing/pricing pool and must be excluded from signal
# discovery (e.g. WETH/USDC, WETH/cbBTC, USDC/USDbC).
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
BASE_ROUTING_TOKENS = (WETH, USDC, USDBC, CBBTC)

UNISWAP_V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
UNISWAP_V2_FACTORY = "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"

LOG_FILTER_FACTORY_JSON = {
    "v3_factory": UNISWAP_V3_FACTORY,
    "v2_factory": UNISWAP_V2_FACTORY,
    "mode": "factory_subscription",
}

# Routers / known non-real-buyer sender addresses (exclude from gate)
SENDER_BLACKLIST = {
    "0x6ff5693b99212da76ad316178a184ab56d299b43",  # Universal Router
}


def fee_tier_sql_in() -> str:
    return fee_bps_sql_in(tiers=FEE_TIERS_BPS)


def volatility_adjustment(token_volatility_5m: float) -> float:
    """Whitepaper Part 4.3: max(0.3, 1.0 − vol × 0.5)."""
    return max(0.3, 1.0 - token_volatility_5m * 0.5)


def position_size_usd(final_score: float, token_volatility_5m: float) -> float:
    return BASE_CAPITAL_USD * final_score * volatility_adjustment(token_volatility_5m)


def tick_delta_to_return(delta_tick: float, direction: int) -> float:
    """V3 native: price_ratio = 1.0001^Δtick. direction = +1 (target=token0) or -1 (target=token1)."""
    if direction == 0:
        return 0.0
    # cap delta_tick to avoid math overflow for extreme rugs (|Δtick| > 800k ≈ ±10^35 ratio)
    capped = max(-700_000.0, min(700_000.0, float(delta_tick)))
    price_ratio = math.pow(1.0001, capped)
    raw_ret = price_ratio - 1.0
    return raw_ret * direction


def _quote_filter_sql(prefix: str = "pm") -> str:
    """Determine quote_side: 1 if token1 is quote, 0 if token0 is quote, -1 otherwise."""
    dot = f"{prefix}." if prefix else ""
    return f"""multiIf(
        lower({dot}token1_address) IN (lower('{WETH}'), lower('{USDC}'), lower('{USDBC}')), 1,
        lower({dot}token0_address) IN (lower('{WETH}'), lower('{USDC}'), lower('{USDBC}')), 0,
        -1
    )"""


def _notional_usd_sql(amount0: str, amount1: str, pm: str = "pm") -> str:
    """USD notional from the QUOTE leg (so it's a true USD amount, not target amount)."""
    return f"""multiIf(
        {pm}.quote_side = 1 AND lower({pm}.token1_address) = lower('{WETH}'),
            abs({amount1}) / pow(10, {pm}.token1_decimals) * {ETH_USD},
        {pm}.quote_side = 0 AND lower({pm}.token0_address) = lower('{WETH}'),
            abs({amount0}) / pow(10, {pm}.token0_decimals) * {ETH_USD},
        {pm}.quote_side = 1 AND lower({pm}.token1_address) IN (lower('{USDC}'), lower('{USDBC}')),
            abs({amount1}) / pow(10, {pm}.token1_decimals),
        {pm}.quote_side = 0 AND lower({pm}.token0_address) IN (lower('{USDC}'), lower('{USDBC}')),
            abs({amount0}) / pow(10, {pm}.token0_decimals),
        0.0
    )"""


def _discover_day(
    day_offset: int,
    *,
    max_pool_age_hours: int,
    whale_usd_thresh: float,
    fee_in: str,
) -> list[dict[str, Any]]:
    """Fetch single-buy ≥$100k events for one UTC day."""
    end_dt = datetime.now(timezone.utc) - timedelta(days=day_offset)
    start_dt = end_dt - timedelta(days=1)
    start = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    max_age_sec = max_pool_age_hours * 3600
    blk_list = ", ".join(f"'{a}'" for a in SENDER_BLACKLIST)

    notional_expr = _notional_usd_sql("s.amount0", "s.amount1", "pm")

    sql = f"""
    WITH first_swap AS (
        SELECT
            lower(pool_address) AS pool,
            toUnixTimestamp(min(block_time)) AS first_ts
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = {CHAIN_ID}
          AND block_time >= toDateTime('{start}', 'UTC') - INTERVAL {max_pool_age_hours + 2} HOUR
          AND block_time <  toDateTime('{end}', 'UTC')
        GROUP BY pool
    ),
    pool_meta_src AS (
        SELECT
            lower(pool_address)   AS pool,
            fee_bps               AS src_fee_bps,
            lower(token0_address) AS src_token0,
            lower(token1_address) AS src_token1,
            token0_decimals       AS src_dec0,
            token1_decimals       AS src_dec1,
            has_weth_leg          AS src_weth_leg
        FROM eatlq.discovered_pools FINAL
        WHERE chain_id = {CHAIN_ID}
          AND length(token0_address) = 42
          AND length(token1_address) = 42
          AND token0_decimals > 0
          AND token1_decimals > 0
        UNION ALL
        SELECT
            lower(pool_address),
            fee_bps,
            lower(token0_address),
            lower(token1_address),
            token0_decimals,
            token1_decimals,
            has_weth_leg
        FROM eatlq.discovered_pools_sprint092_backfill FINAL
        WHERE chain_id = {CHAIN_ID}
    ),
    pool_meta_agg AS (
        SELECT
            pool,
            max(src_fee_bps) AS fee_bps,
            argMax(src_token0, src_fee_bps) AS token0_address,
            argMax(src_token1, src_fee_bps) AS token1_address,
            argMax(src_dec0,   src_fee_bps) AS token0_decimals,
            argMax(src_dec1,   src_fee_bps) AS token1_decimals,
            max(src_weth_leg)               AS has_weth_leg
        FROM pool_meta_src
        GROUP BY pool
    ),
    pool_meta AS (
        SELECT
            pool, fee_bps, token0_address, token1_address,
            token0_decimals, token1_decimals, has_weth_leg,
            -- quote_side: 1 if token1 is quote, 0 if token0 is quote, -1 otherwise
            multiIf(
                token1_address IN (lower('{WETH}'), lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}')), 1,
                token0_address IN (lower('{WETH}'), lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}')), 0,
                -1) AS quote_side,
            -- target = non-quote leg address (the "altcoin/memecoin" side)
            multiIf(
                token1_address IN (lower('{WETH}'), lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}')), token0_address,
                token0_address IN (lower('{WETH}'), lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}')), token1_address,
                '') AS target_address
        FROM pool_meta_agg
        WHERE has_weth_leg = 1
            OR token1_address IN (lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}'))
            OR token0_address IN (lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}'))
    ),
    pool_meta_whitepaper AS (
        -- Whitepaper-faithful: reject pools where TARGET is itself a base routing
        -- token (WETH/USDC/USDbC/cbBTC). These are pricing/routing pools, not
        -- altcoin signal pools. Mirrors feature_store::_resolve_quote_target intent.
        SELECT *
        FROM pool_meta
        WHERE target_address != ''
          AND target_address NOT IN (
              lower('{WETH}'), lower('{USDC}'), lower('{USDBC}'), lower('{CBBTC}')
          )
    )
    SELECT DISTINCT
        lower(s.pool_address) AS pool,
        lower(b.buyer_address) AS sender,
        toUnixTimestamp(s.block_time) AS signal_ts,
        toString(s.block_time)        AS signal_time,
        s.block_number                AS block_number,
        s.tick                        AS tick,
        pm.fee_bps                    AS fee_bps,
        pm.token0_address             AS token0,
        pm.token1_address             AS token1,
        pm.token0_decimals            AS token0_decimals,
        pm.token1_decimals            AS token1_decimals,
        pm.quote_side                 AS quote_side,
        pm.target_address             AS target_address,
        multiIf(pm.target_address = pm.token0_address, pm.token0_decimals, pm.token1_decimals) AS target_decimals,
        multiIf(pm.quote_side = 1, 1, pm.quote_side = 0, -1, 0) AS direction,
        fs.first_ts                   AS pool_first_ts,
        toUnixTimestamp(s.block_time) - fs.first_ts AS pool_age_sec,
        {notional_expr}               AS notional_usd
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    INNER JOIN (
        SELECT
            lower(tx_hash) AS tx_hash,
            log_index,
            lower(buyer_address) AS buyer_address
        FROM eatlq.uniswap_v3_swaps_with_buyer
        WHERE chain_id = {CHAIN_ID}
          AND length(buyer_address) = 42
          AND block_time >= toDateTime('{start}', 'UTC')
          AND block_time <  toDateTime('{end}', 'UTC')
          AND lower(buyer_address) NOT IN ({blk_list})
    ) AS b
        ON lower(s.tx_hash) = b.tx_hash
       AND s.log_index = b.log_index
    INNER JOIN first_swap AS fs
        ON lower(s.pool_address) = fs.pool
    INNER JOIN pool_meta_whitepaper AS pm
        ON lower(s.pool_address) = pm.pool
    WHERE s.chain_id = {CHAIN_ID}
      AND s.block_time >= toDateTime('{start}', 'UTC')
      AND s.block_time <  toDateTime('{end}', 'UTC')
      AND pm.quote_side >= 0
      AND pool_age_sec >= 0
      AND pool_age_sec <= {max_age_sec}
      AND (
            (pm.quote_side = 1 AND s.amount0 < 0)
         OR (pm.quote_side = 0 AND s.amount1 < 0)
      )
      AND {notional_expr} >= {whale_usd_thresh}
    ORDER BY signal_ts ASC
    LIMIT 20000
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=240)


def discover_whale_buy_events(
    *,
    days: int = 21,
    max_pool_age_hours: int = POOL_MAX_AGE_SEC // 3600,
    whale_usd_thresh: float = WHALE_USD_THRESH,
) -> list[dict[str, Any]]:
    """Whitepaper-faithful trigger: single buy ≥$100k on young pool. No wallet labels."""
    fee_in = fee_tier_sql_in()
    rows: list[dict[str, Any]] = []
    for d in range(days):
        try:
            day_rows = _discover_day(
                d,
                max_pool_age_hours=max_pool_age_hours,
                whale_usd_thresh=whale_usd_thresh,
                fee_in=fee_in,
            )
            rows.extend(day_rows)
            log.info("day -%d: %d whale-buy events", d, len(day_rows))
        except Exception as exc:
            log.warning("day -%d failed: %s", d, exc)
    return rows


def fetch_5m_feature_window(
    pool: str,
    signal_ts: int,
    *,
    token0: str,
    token1: str,
    token0_dec: int,
    token1_dec: int,
    quote_side: int,
    window_sec: int = 300,
) -> list[dict[str, Any]]:
    """USD-normalized + target-aware flow points for the pre-signal 5-min window."""
    start_dt = datetime.fromtimestamp(signal_ts - window_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(signal_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if quote_side == 1:
        is_buy_expr = "amount0 < 0"
        if token1.lower() == WETH:
            notional_inline = f"abs(amount1) / pow(10, {token1_dec}) * {ETH_USD}"
        elif token1.lower() in (USDC, USDBC):
            notional_inline = f"abs(amount1) / pow(10, {token1_dec})"
        else:
            notional_inline = "0.0"
    elif quote_side == 0:
        is_buy_expr = "amount1 < 0"
        if token0.lower() == WETH:
            notional_inline = f"abs(amount0) / pow(10, {token0_dec}) * {ETH_USD}"
        elif token0.lower() in (USDC, USDBC):
            notional_inline = f"abs(amount0) / pow(10, {token0_dec})"
        else:
            notional_inline = "0.0"
    else:
        return []

    sql = f"""
    SELECT
        block_number,
        lower(buyer_address) AS sender_address,
        {notional_inline}    AS abs_usd,
        {is_buy_expr}        AS is_buy
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool.lower()}'
      AND block_time >  toDateTime('{start_dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
    ORDER BY block_number ASC
    LIMIT 5000
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=60)


def check_pre_signal_sell_path(
    pool: str,
    signal_ts: int,
    quote_side: int,
    *,
    lookback_sec: int = HONEYPOT_LOOKBACK_SEC,
) -> bool:
    """
    HISTORICAL-PROXY honeypot check. NOT the whitepaper's Quoter V2 simulation.

    The whitepaper specifies a live `quoteExactInputSingle(token → WETH, 0.001)`
    eth_call to detect honeypots. That requires RPC + the contract's *current*
    state. For batch backtests where calling RPC per candidate is impractical,
    this function falls back to a backtest-safe proxy: "did the pool have ≥1
    sell of the target leg in the past lookback_sec hours?". A pool with zero
    historical sells is a strong honeypot suspicion. A pool with ≥1 historical
    sell is *not* a guarantee against honeypotting (the contract may have been
    upgraded since), but is the strongest passive check we can do offline.

    For the whitepaper-faithful Quoter V2 check, use `check_quoter_v2_sell_path`
    below (slower, requires RPC, uses current pool state).
    """
    if quote_side not in (0, 1):
        return False
    is_sell_expr = "amount0 > 0" if quote_side == 1 else "amount1 > 0"
    start_dt = datetime.fromtimestamp(signal_ts - lookback_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(signal_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT count() AS sells
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool.lower()}'
      AND block_time >= toDateTime('{start_dt}', 'UTC')
      AND block_time <  toDateTime('{end_dt}', 'UTC')
      AND {is_sell_expr}
    FORMAT JSONEachRow
    """
    try:
        rows = ch_query(sql, timeout=30)
        return bool(rows) and int(rows[0].get("sells", 0)) > 0
    except Exception:
        return False


def check_quoter_v2_sell_path(
    target_token: str,
    target_decimals: int,
    rpc_url: str,
    *,
    fee_tiers: tuple[int, ...] = (3000, 10000, 500, 100),
    test_units: float = 0.001,
    block_identifier: int | str | None = None,
) -> dict[str, Any]:
    """Whitepaper-faithful honeypot check via Uniswap V3 QuoterV2.

    Simulates selling `test_units` (default 0.001) of target_token back to WETH
    across each fee tier. If ALL tiers revert or return amountOut == 0, the
    token is flagged as honeypot.

    NOTE on backtest semantics: this uses the *current* (or specified) block
    state, not the block at signal_ts. Calling at historical blocks would
    require an archive RPC. For sprint092 we accept current-state as a proxy
    for historical state under the assumption that:
      - non-honeypot tokens generally remain non-honeypot
      - honeypot tokens that pumped-and-trapped are now even more likely to revert
    This is documented as a deviation in the readout.

    Returns dict with: {ok, amount_in_wei, fee_tier_used, amount_out, reason}.
    """
    _scripts_lib = str(Path(__file__).resolve().parents[1] / "lib")
    if _scripts_lib not in sys.path:
        sys.path.insert(0, _scripts_lib)
    from v3_quoterv2_struct import (  # type: ignore[import-not-found]
        JsonRpcClient,
        quote_exact_input_single_struct,
    )

    rpc = JsonRpcClient(rpc_url)
    amount_in_wei = int(test_units * (10 ** int(target_decimals)))
    last_reason = ""
    for fee in fee_tiers:
        try:
            res = quote_exact_input_single_struct(
                rpc=rpc,
                token_in=target_token,
                token_out=WETH,
                amount_in=amount_in_wei,
                fee=int(fee),
                block_identifier=block_identifier,
                timeout=10,
            )
        except Exception as exc:
            last_reason = f"call_error:{str(exc)[:80]}"
            continue
        amount_out = int(res.get("amount_out") or 0)
        if amount_out > 0:
            return {
                "ok": True,
                "amount_in_wei": amount_in_wei,
                "fee_tier_used": int(fee),
                "amount_out": amount_out,
                "reason": "",
            }
        last_reason = res.get("reason", "zero_out")
    return {
        "ok": False,
        "amount_in_wei": amount_in_wei,
        "fee_tier_used": -1,
        "amount_out": 0,
        "reason": last_reason or "all_tiers_failed",
    }


def fetch_tick_path(
    pool: str,
    entry_ts: int,
    *,
    max_horizon_sec: int = 7200,
) -> list[dict[str, Any]]:
    """Post-entry tick series for exit simulation."""
    start = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.fromtimestamp(entry_ts + max_horizon_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT
        toUnixTimestamp(block_time) AS ts,
        tick
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool.lower()}'
      AND block_time >= toDateTime('{start}', 'UTC')
      AND block_time <= toDateTime('{end}', 'UTC')
    ORDER BY block_time ASC
    LIMIT 5000
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=60)


def log_filter_env_patch() -> dict[str, str]:
    import json

    return {
        "LOG_FILTER_JSON": json.dumps(LOG_FILTER_FACTORY_JSON),
        "SWAP_POLLER_FEE_TIERS": ",".join(str(f) for f in FEE_TIERS_BPS),
        "POOL_MAX_AGE_SEC": str(POOL_MAX_AGE_SEC),
        "STRATEGY_MODE": "sprint092_null_overlay",
    }
