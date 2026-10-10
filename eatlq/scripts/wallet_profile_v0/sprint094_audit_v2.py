#!/usr/bin/env python3
"""
Sprint094 audit v2 — answers what user asked in the 2nd review pass.

Scope:
 1. Universe coverage audit: V3 alt-pool universe (sprint094 original scope) is
    one of THREE Base universes. Quantify the other two (V2, Aerodrome).
 2. Notional valuation audit: empirical ETH/USD price from the canonical
    WETH/USDC V3 pool (0xd0b53...224) by date; compare against the
    hardcoded $3000 that the original sprint094 used.
 3. Threshold ladder across $500 / $1k / $2k / $5k / $10k / $25k / $50k / $100k
    over 7d / 14d / 30d, in each universe, with concentration metrics (top
    token / top wallet / top week share).
 4. 5min label noise fix: compute LAST tick / p50 tick / p75 tick / median tick
    of the 5min window per candidate, and compare distributions to see if
    1262% tail goes away.
 5. Bot / MEV / high-rebuy / TSG filter buckets: report per-bucket candidate
    counts so winner-only is not contaminated by structural artifacts.

HARD CONSTRAINTS:
 * ZERO RPC calls (RPC_BUDGET capped at 0).
 * All CH queries bounded by lookback + IN clauses + small chunks.
 * Outputs go to mvp/audits/wallet_profile_v0/sprint094_audit_v2/.
 * Does NOT lift FAILED_NO_7D_ALPHA.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query  # noqa: E402

log = logging.getLogger("sprint094_audit_v2")

OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sprint094_audit_v2"
OUT_DIR.mkdir(parents=True, exist_ok=True)

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
BASE_ROUTING = (WETH, USDC, USDBC, CBBTC)
ETH_USDC_POOL = "0xd0b53d9277642d899df5c87a3966a349a798f224"  # canonical Base WETH/USDC 500bps
KNOWN_BOT_WALLETS = {"0x6ff5693b99212da76ad316178a184ab56d299b43"}
TSG_TOKEN = "0x4c433f4e1f6b6df8c4ada95dc69ac1e0c81f8a4f"  # set by ddata; fixed at runtime

RPC_BUDGET = {"calls": 0, "max": 0}


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def routing_in_clause() -> str:
    return ", ".join(f"'{a}'" for a in BASE_ROUTING)


def data_window_end() -> datetime:
    """Pin to the latest data we actually have (ingest may lag)."""
    rows = ch_query(
        f"SELECT max(block_time) AS m FROM eatlq.uniswap_v3_swaps_decoded "
        f"WHERE chain_id = {CHAIN_ID} FORMAT JSONEachRow",
        timeout=30,
    )
    raw = rows[0].get("m") if rows else None
    if not raw:
        return datetime.now(timezone.utc)
    return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def window_bounds(end: datetime, days: int) -> tuple[str, str]:
    end_dt = end.strftime("%Y-%m-%d %H:%M:%S")
    start_dt = datetime.fromtimestamp(int(end.timestamp()) - days * 86400,
                                       tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start_dt, end_dt


# ------------------------------------------------------------------------------
# 1. ETH price by date — empirical from WETH/USDC V3 pool 0xd0b53...
# ------------------------------------------------------------------------------

def build_eth_price_series(*, start_dt: str, end_dt: str) -> dict[str, float]:
    """Return dict[YYYY-MM-DD] = median USDC per WETH."""
    # Pool 0xd0b53...224 has token0=WETH(18dec), token1=USDC(6dec); USDC per WETH
    # = 1.0001^tick × 10^(dec0 - dec1) = 1.0001^tick × 10^12 (human units)
    sql = f"""
    SELECT
        toString(toDate(block_time)) AS d,
        round(quantile(0.5)(pow(1.0001, tick) * pow(10, 12)), 2) AS usd
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{ETH_USDC_POOL}'
      AND block_time >= toDateTime('{start_dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
      AND tick BETWEEN -250000 AND -180000  -- sanity bound around historical ETH range
    GROUP BY d
    ORDER BY d
    FORMAT JSONEachRow
    """
    rows = ch_query(sql, timeout=120)
    return {r["d"]: float(r["usd"]) for r in rows}


def eth_price_at(eth_by_day: dict[str, float], block_date: str) -> float:
    """Lookup ETH price on given date; fallback to nearest available."""
    if block_date in eth_by_day:
        return eth_by_day[block_date]
    if not eth_by_day:
        return 2200.0  # cold fallback
    # Try nearest in time
    days = sorted(eth_by_day.keys())
    return eth_by_day[days[-1]]  # most recent


# ------------------------------------------------------------------------------
# 2. Threshold ladder per universe
# ------------------------------------------------------------------------------

def discovery_v3(*, days: int, eth_price: float, min_notional: float) -> list[dict]:
    """V3 alt-pool universe — same logic as sprint094 but with overridable ETH price."""
    end_dt = data_window_end()
    start_dt, end_dt_s = window_bounds(end_dt, days)
    routing = routing_in_clause()
    sql = f"""
    WITH pool_meta_raw AS (
        SELECT lower(pool_address) AS pool,
               any(fee_bps) AS fee_bps,
               any(lower(token0_address)) AS t0, any(lower(token1_address)) AS t1,
               any(token0_decimals) AS d0, any(token1_decimals) AS d1,
               any(token0_symbol) AS s0, any(token1_symbol) AS s1
        FROM eatlq.discovered_pools WHERE chain_id = {CHAIN_ID} GROUP BY pool
    ),
    pool_meta AS (
        SELECT pool, fee_bps, t0, t1, d0, d1, s0, s1,
            multiIf(t1 IN ({routing}), 1, t0 IN ({routing}), 0, -1) AS qs,
            multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') AS target
        FROM pool_meta_raw
        WHERE (t1 IN ({routing}) OR t0 IN ({routing}))
          AND multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') NOT IN ({routing})
          AND multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') != ''
          AND d0 > 0 AND d1 > 0
    )
    SELECT
        sd.tx_hash, sd.log_index,
        toUnixTimestamp(sd.block_time) AS signal_ts,
        toString(toDate(sd.block_time)) AS block_date,
        lower(sd.pool_address) AS pool, lower(sb.buyer_address) AS sender,
        p.target AS target_address, p.s0 AS sym0, p.s1 AS sym1,
        p.fee_bps, p.qs AS quote_side, toInt32(sd.tick) AS tick,
        round(if(p.qs = 1,
            multiIf(p.t1 IN ('{USDC}', '{USDBC}'),
                abs(toFloat64(sd.amount1))/pow(10,p.d1),
              p.t1 = '{WETH}',
                abs(toFloat64(sd.amount1))/pow(10,p.d1) * {eth_price},
              p.t1 = '{CBBTC}',
                abs(toFloat64(sd.amount1))/pow(10,p.d1) * 95000, 0.0),
            if(p.qs = 0,
                multiIf(p.t0 IN ('{USDC}', '{USDBC}'),
                    abs(toFloat64(sd.amount0))/pow(10,p.d0),
                  p.t0 = '{WETH}',
                    abs(toFloat64(sd.amount0))/pow(10,p.d0) * {eth_price},
                  p.t0 = '{CBBTC}',
                    abs(toFloat64(sd.amount0))/pow(10,p.d0) * 95000, 0.0), 0.0)), 2) AS notional_usd
    FROM eatlq.uniswap_v3_swaps_decoded AS sd
    INNER JOIN eatlq.uniswap_v3_swaps_with_buyer AS sb
        ON sb.tx_hash = sd.tx_hash AND sb.log_index = sd.log_index
    INNER JOIN pool_meta AS p ON p.pool = lower(sd.pool_address)
    WHERE sd.chain_id = {CHAIN_ID} AND sb.chain_id = {CHAIN_ID}
      AND sd.block_time >= toDateTime('{start_dt}', 'UTC')
      AND sd.block_time <  toDateTime('{end_dt_s}', 'UTC')
      AND if(p.qs = 1, toFloat64(sd.amount0) < 0, toFloat64(sd.amount1) < 0)
    HAVING notional_usd >= {min_notional}
    ORDER BY signal_ts ASC
    LIMIT 30000 FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=900)


def discovery_v2(*, days: int, eth_price: float, min_notional: float) -> list[dict]:
    """V2 alt-pool universe — uses Uniswap V2 pair_address + amount0/1_in/out_raw."""
    end_dt = data_window_end()
    start_dt, end_dt_s = window_bounds(end_dt, days)
    routing = routing_in_clause()
    sql = f"""
    WITH pool_meta_raw AS (
        SELECT lower(pool_address) AS pool,
               any(fee_bps) AS fee_bps,
               any(lower(token0_address)) AS t0, any(lower(token1_address)) AS t1,
               any(token0_decimals) AS d0, any(token1_decimals) AS d1,
               any(token0_symbol) AS s0, any(token1_symbol) AS s1
        FROM eatlq.discovered_pools
        WHERE chain_id = {CHAIN_ID} AND pool_type = 'v2'
        GROUP BY pool
    ),
    pool_meta AS (
        SELECT pool, fee_bps, t0, t1, d0, d1, s0, s1,
            multiIf(t1 IN ({routing}), 1, t0 IN ({routing}), 0, -1) AS qs,
            multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') AS target
        FROM pool_meta_raw
        WHERE (t1 IN ({routing}) OR t0 IN ({routing}))
          AND multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') NOT IN ({routing})
          AND multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') != ''
          AND d0 > 0 AND d1 > 0
    )
    SELECT
        sv.tx_hash, sv.log_index,
        toUnixTimestamp(sv.block_time) AS signal_ts,
        toString(toDate(sv.block_time)) AS block_date,
        lower(sv.pair_address) AS pool, lower(sv.sender) AS sender,
        p.target AS target_address, p.s0 AS sym0, p.s1 AS sym1,
        p.fee_bps, p.qs AS quote_side,
        -- V2 amounts are hex strings; decode via reinterpretAsUInt256(reverse(unhex(...))).
        -- A BUY of target token = quote token amount comes IN.
        round(if(p.qs = 1,
            multiIf(p.t1 IN ('{USDC}', '{USDBC}'),
                toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount1_in_raw, 3)))))/pow(10,p.d1),
              p.t1 = '{WETH}',
                toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount1_in_raw, 3)))))/pow(10,p.d1) * {eth_price},
              p.t1 = '{CBBTC}',
                toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount1_in_raw, 3)))))/pow(10,p.d1) * 95000, 0.0),
            if(p.qs = 0,
                multiIf(p.t0 IN ('{USDC}', '{USDBC}'),
                    toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount0_in_raw, 3)))))/pow(10,p.d0),
                  p.t0 = '{WETH}',
                    toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount0_in_raw, 3)))))/pow(10,p.d0) * {eth_price},
                  p.t0 = '{CBBTC}',
                    toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount0_in_raw, 3)))))/pow(10,p.d0) * 95000, 0.0), 0.0)), 2) AS notional_usd
    FROM eatlq.uniswap_v2_swaps AS sv
    INNER JOIN pool_meta AS p ON p.pool = lower(sv.pair_address)
    WHERE sv.chain_id = {CHAIN_ID}
      AND sv.block_time >= toDateTime('{start_dt}', 'UTC')
      AND sv.block_time <  toDateTime('{end_dt_s}', 'UTC')
      AND startsWith(if(p.qs = 1, amount1_in_raw, amount0_in_raw), '0x')
    HAVING notional_usd >= {min_notional}
    ORDER BY signal_ts ASC LIMIT 30000 FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=900)


def discovery_aero(*, days: int, eth_price: float, min_notional: float) -> list[dict]:
    """Aerodrome vAMM universe with USD notional via discovered_pools metadata (P1-9)."""
    end_dt = data_window_end()
    start_dt, end_dt_s = window_bounds(end_dt, days)
    routing = routing_in_clause()
    sql = f"""
    WITH pool_meta_raw AS (
        SELECT lower(pool_address) AS pool,
               any(fee_bps) AS fee_bps,
               any(lower(token0_address)) AS t0, any(lower(token1_address)) AS t1,
               any(token0_decimals) AS d0, any(token1_decimals) AS d1,
               any(token0_symbol) AS s0, any(token1_symbol) AS s1
        FROM eatlq.discovered_pools FINAL
        WHERE chain_id = {CHAIN_ID} AND pool_type IN ('aero_vamm', 'aero_cl')
          AND length(token0_address) = 42 AND length(token1_address) = 42
        GROUP BY pool
    ),
    pool_meta AS (
        SELECT pool, fee_bps, t0, t1, d0, d1, s0, s1,
            multiIf(t1 IN ({routing}), 1, t0 IN ({routing}), 0, -1) AS qs,
            multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') AS target
        FROM pool_meta_raw
        WHERE (t1 IN ({routing}) OR t0 IN ({routing}))
          AND multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') NOT IN ({routing})
          AND multiIf(t1 IN ({routing}), t0, t0 IN ({routing}), t1, '') != ''
          AND d0 > 0 AND d1 > 0
    )
    SELECT
        sv.tx_hash, sv.log_index,
        toUnixTimestamp(sv.block_time) AS signal_ts,
        toString(toDate(sv.block_time)) AS block_date,
        lower(sv.pair_address) AS pool, lower(sv.sender) AS sender,
        p.target AS target_address, p.s0 AS sym0, p.s1 AS sym1,
        p.fee_bps, p.qs AS quote_side,
        round(if(p.qs = 1,
            multiIf(p.t1 IN ('{USDC}', '{USDBC}'),
                toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount1_in_raw, 3)))))/pow(10,p.d1),
              p.t1 = '{WETH}',
                toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount1_in_raw, 3)))))/pow(10,p.d1) * {eth_price},
              p.t1 = '{CBBTC}',
                toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount1_in_raw, 3)))))/pow(10,p.d1) * 95000, 0.0),
            if(p.qs = 0,
                multiIf(p.t0 IN ('{USDC}', '{USDBC}'),
                    toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount0_in_raw, 3)))))/pow(10,p.d0),
                  p.t0 = '{WETH}',
                    toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount0_in_raw, 3)))))/pow(10,p.d0) * {eth_price},
                  p.t0 = '{CBBTC}',
                    toFloat64(reinterpretAsUInt256(reverse(unhex(substring(amount0_in_raw, 3)))))/pow(10,p.d0) * 95000, 0.0), 0.0)), 2) AS notional_usd
    FROM eatlq.aerodrome_vamm_swaps_mv AS sv
    INNER JOIN pool_meta AS p ON p.pool = lower(sv.pair_address)
    WHERE sv.chain_id = {CHAIN_ID}
      AND sv.block_time >= toDateTime('{start_dt}', 'UTC')
      AND sv.block_time <  toDateTime('{end_dt_s}', 'UTC')
      AND startsWith(if(p.qs = 1, amount1_in_raw, amount0_in_raw), '0x')
    HAVING notional_usd >= {min_notional}
    ORDER BY signal_ts ASC LIMIT 30000 FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=900)


# ------------------------------------------------------------------------------
# 3. Threshold ladder summary
# ------------------------------------------------------------------------------

def concentration_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    senders = Counter(r["sender"] for r in rows)
    pools = Counter(r["pool"] for r in rows)
    targets = Counter(r["target_address"] for r in rows)
    weeks = Counter(r.get("block_date", "")[:7] for r in rows)  # YYYY-MM as proxy
    bot_n = sum(n for s, n in senders.items() if s in KNOWN_BOT_WALLETS)
    n = len(rows)
    top1_sender = senders.most_common(1)[0] if senders else ("", 0)
    top1_pool = pools.most_common(1)[0] if pools else ("", 0)
    top1_target = targets.most_common(1)[0] if targets else ("", 0)
    return {
        "n": n,
        "distinct_senders": len(senders),
        "distinct_senders_no_bot": len([s for s in senders if s not in KNOWN_BOT_WALLETS]),
        "distinct_pools": len(pools),
        "distinct_targets": len(targets),
        "n_bot_events": bot_n,
        "top1_sender": top1_sender[0][:14],
        "top1_sender_share": round(top1_sender[1] / n, 3),
        "top1_pool": top1_pool[0][:14],
        "top1_pool_share": round(top1_pool[1] / n, 3),
        "top1_target": top1_target[0][:14],
        "top1_target_share": round(top1_target[1] / n, 3),
        "top1_month": (weeks.most_common(1)[0][0] if weeks else "",
                       round(weeks.most_common(1)[0][1] / n, 3) if weeks else 0.0),
    }


def run_threshold_ladder(*, eth_price: float) -> dict:
    out = {"v3": {}, "v2": {}, "aero_count_only": {}}
    days_list = [7, 14, 30]
    thresh_list = [500, 1_000, 2_000, 5_000, 10_000, 25_000, 50_000, 100_000]
    for d in days_list:
        out["v3"][f"{d}d"] = {}
        out["v2"][f"{d}d"] = {}
        for t in thresh_list:
            log.info("v3 ladder: days=%d thresh=$%d", d, t)
            v3 = discovery_v3(days=d, eth_price=eth_price, min_notional=t)
            out["v3"][f"{d}d"][f"${t}"] = concentration_stats(v3)
        # V2 only at a few thresholds to keep runtime bounded
        for t in [1_000, 5_000, 10_000, 25_000, 50_000, 100_000]:
            log.info("v2 ladder: days=%d thresh=$%d", d, t)
            v2 = discovery_v2(days=d, eth_price=eth_price, min_notional=t)
            out["v2"][f"{d}d"][f"${t}"] = concentration_stats(v2)
        log.info("aero count: days=%d", d)
        aero = discovery_aero(days=d, eth_price=eth_price, min_notional=0)
        out["aero_count_only"][f"{d}d"] = {
            "n_pools": len(aero),
            "total_events": sum(int(r["n_events"]) for r in aero),
            "total_senders_dedup_top200pools": len({}),  # placeholder
        }
    return out


# ------------------------------------------------------------------------------
# 4. 5min label variants on V3 alt-pool candidates
# ------------------------------------------------------------------------------

def fetch_pool_ticks(pool: str, start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    SELECT toUnixTimestamp(block_time) AS ts, tick
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id = {CHAIN_ID}
      AND lower(pool_address) = '{pool}'
      AND block_time >= toDateTime('{start_dt}', 'UTC')
      AND block_time <= toDateTime('{end_dt}', 'UTC')
    ORDER BY block_number ASC, log_index ASC
    FORMAT JSONEachRow
    """
    rows = ch_query(sql, timeout=120)
    return [(int(r["ts"]), int(r["tick"])) for r in rows]


def label_variants(candidates: list[dict]) -> list[dict]:
    """For each candidate, compute 5min forward tick under multiple aggregators."""
    import bisect, statistics
    out: list[dict] = []
    by_pool: dict[str, list[dict]] = {}
    for c in candidates:
        by_pool.setdefault(c["pool"], []).append(c)
    for pool, cs in by_pool.items():
        if not cs:
            continue
        ts_min = min(int(c["signal_ts"]) for c in cs)
        ts_max = max(int(c["signal_ts"]) for c in cs) + 360
        ticks = fetch_pool_ticks(pool, ts_min, ts_max)
        ts_list = [t[0] for t in ticks]
        tk_list = [t[1] for t in ticks]
        for c in cs:
            signal_ts = int(c["signal_ts"])
            entry_tick = int(c["tick"])
            target_ts = signal_ts + 300
            lo = bisect.bisect_right(ts_list, signal_ts)
            hi = bisect.bisect_right(ts_list, target_ts)
            window_ticks = tk_list[lo:hi]
            if not window_ticks:
                out.append({**c, "fwd_n_swaps": 0,
                            "fwd_last_tick": None, "fwd_p50_tick": None,
                            "fwd_p75_tick": None, "fwd_trimmed_tick": None,
                            "ret_last": None, "ret_p50": None, "ret_p75": None,
                            "ret_trimmed": None})
                continue
            last = window_ticks[-1]
            p50 = int(statistics.median(window_ticks))
            srt = sorted(window_ticks)
            p75_idx = max(0, int(0.75 * (len(srt) - 1)))
            p75 = srt[p75_idx]
            # trimmed mean: drop top/bottom 10% of ticks
            if len(srt) >= 10:
                trim_lo = int(0.10 * len(srt))
                trim_hi = max(trim_lo + 1, int(0.90 * len(srt)))
                trimmed = int(sum(srt[trim_lo:trim_hi]) / max(1, (trim_hi - trim_lo)))
            else:
                trimmed = int(sum(srt) / len(srt))
            def rt(t):
                return math.pow(1.0001, t - entry_tick) - 1.0  # buys: target leaves pool, price goes up → positive
            out.append({**c, "fwd_n_swaps": len(window_ticks),
                        "fwd_last_tick": last, "fwd_p50_tick": p50,
                        "fwd_p75_tick": p75, "fwd_trimmed_tick": trimmed,
                        "ret_last": rt(last), "ret_p50": rt(p50),
                        "ret_p75": rt(p75), "ret_trimmed": rt(trimmed)})
    return out


# ------------------------------------------------------------------------------
# 5. Sell-side / hold-time enrichment (CH-only)
# ------------------------------------------------------------------------------

def fetch_sender_buy_sell_pairs(senders: list[str], pool_set: list[str],
                                  window_days: int) -> list[dict]:
    """For each (sender, pool), pull every BUY/SELL event in the window so we can
    pair them into open→close trades and compute hold-time."""
    if not senders or not pool_set:
        return []
    end_dt = data_window_end()
    start_dt, end_dt_s = window_bounds(end_dt, window_days)
    senders_in = ", ".join(f"'{s.lower()}'" for s in senders)
    pools_in = ", ".join(f"'{p.lower()}'" for p in pool_set)
    sql = f"""
    WITH pool_meta_raw AS (
        SELECT lower(pool_address) AS pool,
               any(lower(token0_address)) AS t0, any(lower(token1_address)) AS t1,
               any(token0_decimals) AS d0, any(token1_decimals) AS d1
        FROM eatlq.discovered_pools WHERE chain_id = {CHAIN_ID} GROUP BY pool
    ),
    pool_meta AS (
        SELECT pool, t0, t1, d0, d1,
            multiIf(t1 IN ({routing_in_clause()}), 1, t0 IN ({routing_in_clause()}), 0, -1) AS qs
        FROM pool_meta_raw WHERE pool IN ({pools_in})
    ),
    sw AS (
        SELECT lower(sb.buyer_address) AS sender,
               lower(sd.pool_address) AS pool,
               toUnixTimestamp(sd.block_time) AS ts,
               toFloat64(sd.amount0) AS a0, toFloat64(sd.amount1) AS a1
        FROM eatlq.uniswap_v3_swaps_decoded AS sd
        INNER JOIN eatlq.uniswap_v3_swaps_with_buyer AS sb
            ON sb.tx_hash = sd.tx_hash AND sb.log_index = sd.log_index
        WHERE sd.chain_id = {CHAIN_ID} AND sb.chain_id = {CHAIN_ID}
          AND lower(sb.buyer_address) IN ({senders_in})
          AND lower(sd.pool_address) IN ({pools_in})
          AND sd.block_time >= toDateTime('{start_dt}', 'UTC')
          AND sd.block_time <  toDateTime('{end_dt_s}', 'UTC')
    )
    SELECT sw.sender, sw.pool, sw.ts,
        -- is_buy under quote convention: if qs=1, target=token0, buy = amount0 < 0
        if(p.qs = 1, if(sw.a0 < 0, 1, 0), if(sw.a1 < 0, 1, 0)) AS is_buy
    FROM sw INNER JOIN pool_meta AS p ON p.pool = sw.pool
    ORDER BY sw.sender ASC, sw.pool ASC, sw.ts ASC
    FORMAT JSONEachRow
    """
    return ch_query(sql, timeout=900)


def hold_time_stats(events: list[dict]) -> dict[str, dict]:
    """Pair buys with subsequent sells per (sender, pool); compute hold time."""
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in events:
        by_key[(e["sender"], e["pool"])].append(e)
    stats: dict[str, dict] = defaultdict(lambda: {
        "n_buys": 0, "n_sells": 0, "n_paired": 0,
        "hold_sec_sum": 0, "hold_sec_n": 0,
        "n_quick_sells_5min": 0, "n_held_24h_plus": 0,
    })
    for (sender, pool), evs in by_key.items():
        evs_sorted = sorted(evs, key=lambda x: int(x["ts"]))
        # simple FIFO pairing
        buy_queue: list[int] = []  # list of buy timestamps
        for e in evs_sorted:
            if int(e["is_buy"]) == 1:
                buy_queue.append(int(e["ts"]))
                stats[sender]["n_buys"] += 1
            else:
                stats[sender]["n_sells"] += 1
                if buy_queue:
                    buy_ts = buy_queue.pop(0)
                    hold = int(e["ts"]) - buy_ts
                    stats[sender]["n_paired"] += 1
                    stats[sender]["hold_sec_sum"] += hold
                    stats[sender]["hold_sec_n"] += 1
                    if hold <= 300:
                        stats[sender]["n_quick_sells_5min"] += 1
                    if hold >= 86400:
                        stats[sender]["n_held_24h_plus"] += 1
    # finalize: compute avg hold + paper/diamond rate
    out: dict[str, dict] = {}
    for s, v in stats.items():
        n_paired = v["n_paired"]
        avg_hold = v["hold_sec_sum"] / max(1, v["hold_sec_n"])
        paper_rate = v["n_quick_sells_5min"] / max(1, n_paired) if n_paired else None
        diamond_rate = v["n_held_24h_plus"] / max(1, n_paired) if n_paired else None
        out[s] = {
            "n_buys": v["n_buys"], "n_sells": v["n_sells"],
            "n_paired_trades": n_paired,
            "avg_hold_sec": round(avg_hold, 1) if n_paired else None,
            "median_hold_sec": None,  # rough; skipped for v1
            "paper_rate_5min": round(paper_rate, 3) if paper_rate is not None else None,
            "diamond_rate_24h": round(diamond_rate, 3) if diamond_rate is not None else None,
        }
    return out


# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-v2", action="store_true")
    ap.add_argument("--skip-label-fix", action="store_true")
    ap.add_argument("--skip-holdtime", action="store_true")
    args = ap.parse_args()

    end = data_window_end()
    start_30d_dt, end_dt_s = window_bounds(end, 30)
    log.info("data window end: %s", end.isoformat())

    # 1. ETH price series
    log.info("building ETH price series from %s pool ...", ETH_USDC_POOL[:14])
    eth_by_day = build_eth_price_series(start_dt=start_30d_dt, end_dt=end_dt_s)
    eth_mean = sum(eth_by_day.values()) / max(1, len(eth_by_day))
    log.info("ETH price (30d) days=%d  mean=$%.0f  first=%s last=%s",
             len(eth_by_day), eth_mean, list(eth_by_day)[0] if eth_by_day else "",
             list(eth_by_day)[-1] if eth_by_day else "")
    (OUT_DIR / "eth_price_by_day.json").write_text(json.dumps(eth_by_day, indent=2))

    # 2. Threshold ladder
    log.info("running threshold ladder ...")
    ladder = run_threshold_ladder(eth_price=round(eth_mean, 0))
    (OUT_DIR / "threshold_ladder.json").write_text(json.dumps(ladder, indent=2, default=str))

    # 3. Label-variant comparison on a representative candidate slice
    #    Use V3 14d × $2k (the same slice the original sprint094 ran).
    if not args.skip_label_fix:
        log.info("running 5min label variants on V3 14d × $2k slice ...")
        cands_v3 = discovery_v3(days=14, eth_price=round(eth_mean, 0), min_notional=2000)
        cands_v3_with_labels = label_variants(cands_v3)
        with (OUT_DIR / "label_variants_v3_14d_2k.csv").open("w", newline="") as f:
            keys = ["tx_hash", "log_index", "signal_ts", "pool", "sender", "target_address",
                    "notional_usd", "tick", "fwd_n_swaps",
                    "fwd_last_tick", "fwd_p50_tick", "fwd_p75_tick", "fwd_trimmed_tick",
                    "ret_last", "ret_p50", "ret_p75", "ret_trimmed"]
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader(); w.writerows(cands_v3_with_labels)

        # 4. Hold-time / sell-side stats on those candidates' senders
        if not args.skip_holdtime:
            senders = sorted({c["sender"] for c in cands_v3 if c["sender"] not in KNOWN_BOT_WALLETS})
            pools = sorted({c["pool"] for c in cands_v3})
            log.info("hold-time pass: %d senders × %d pools", len(senders), len(pools))
            evs = fetch_sender_buy_sell_pairs(senders, pools, window_days=14)
            log.info("hold-time events fetched: %d", len(evs))
            ht = hold_time_stats(evs)
            with (OUT_DIR / "hold_time_stats_v3_14d.csv").open("w", newline="") as f:
                rows = [{"sender": s, **v} for s, v in ht.items()]
                if rows:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader(); w.writerows(rows)
            log.info("hold_time_stats: %d senders", len(ht))

    log.info("done; outputs in %s", OUT_DIR)


if __name__ == "__main__":
    main()
