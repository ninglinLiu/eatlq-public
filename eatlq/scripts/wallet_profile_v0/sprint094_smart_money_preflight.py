#!/usr/bin/env python3
"""
Sprint094-A — Smart Money / Wallet Profile preflight (CH-ONLY, ZERO RPC).

Goal: answer pre-flight questions before promising any alpha:
  - How many candidate wallets exist (expanded to 14d, $10k+)?
  - Do they have meaningful pre-signal history?
  - Is the universe still 100% AERO, or does expansion break that?
  - Per-wallet 5m forward-return signal: any wallets persistently beat?
  - Three sample sets: winner / near-miss / control.
  - Four wallet profile classes: SmartMoney / Sniper / Diamond / Paper.
  - W-family of confidence variants ranked by distribution + concentration.

HARD CONSTRAINTS:
  * ZERO RPC calls. All forward labels computed via CH replay of
    `uniswap_v3_swaps_decoded` only.
  * No wallet x day x pool full-history scan; everything is bounded by the
    candidate seed set + a fixed-size control sample.
  * Wallet features are PRE-SIGNAL ONLY — no future return ever feeds back
    into wallet labelling at signal_ts.
  * Does NOT lift FAILED_NO_7D_ALPHA. RESEARCH_ONLY.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, ch_query  # noqa: E402

log = logging.getLogger("sprint094_preflight")

OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sprint094a_smart_money_preflight"
OUT_DIR.mkdir(parents=True, exist_ok=True)

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
BASE_ROUTING = (WETH, USDC, USDBC, CBBTC)

ETH_USD = 3000.0  # rough mid; ok for ranking, not for $ accounting
WHALE_USD_THRESH = 100_000.0
RPC_BUDGET = {"calls": 0, "max": 0}  # hard cap = 0 for this sprint


# ------------------------------------------------------------------------------
# 1. Discovery — single CH query, no loop, 14d alt-pool candidates
# ------------------------------------------------------------------------------

DISCOVERY_SQL_TEMPLATE = r"""
WITH pool_meta_raw AS (
    SELECT
        lower(pool_address) AS pool,
        any(fee_bps) AS fee_bps,
        any(lower(token0_address)) AS token0_address,
        any(lower(token1_address)) AS token1_address,
        any(token0_decimals) AS token0_decimals,
        any(token1_decimals) AS token1_decimals,
        any(token0_symbol) AS symbol0,
        any(token1_symbol) AS symbol1,
        min(discovered_at) AS created_at
    FROM eatlq.discovered_pools
    WHERE chain_id = {chain_id}
    GROUP BY pool
),
pool_meta AS (
    SELECT
        pool, fee_bps, token0_address, token1_address,
        token0_decimals, token1_decimals, symbol0, symbol1, created_at,
        -- quote_side: 1 if token1 is quote, 0 if token0 is quote
        multiIf(
            token1_address IN ({routing_csv}), 1,
            token0_address IN ({routing_csv}), 0,
            -1) AS quote_side,
        multiIf(
            token1_address IN ({routing_csv}), token0_address,
            token0_address IN ({routing_csv}), token1_address,
            '') AS target_address,
        multiIf(
            token1_address IN ({routing_csv}), token0_decimals,
            token0_address IN ({routing_csv}), token1_decimals,
            18) AS target_decimals
    FROM pool_meta_raw
),
pool_meta_alt AS (
    -- whitepaper-faithful: exclude pools where TARGET is itself a routing token
    SELECT * FROM pool_meta
    WHERE target_address != ''
      AND target_address NOT IN ({routing_csv})
),
swaps_in_window AS (
    SELECT
        sd.tx_hash, sd.log_index, sd.block_number, sd.block_time,
        lower(sd.pool_address) AS pool,
        sd.amount0, sd.amount1, sd.tick,
        lower(sb.buyer_address) AS buyer
    FROM eatlq.uniswap_v3_swaps_decoded AS sd
    INNER JOIN eatlq.uniswap_v3_swaps_with_buyer AS sb
        ON sb.tx_hash = sd.tx_hash AND sb.log_index = sd.log_index
    WHERE sd.chain_id = {chain_id}
      AND sd.block_time >= toDateTime('{start_dt}', 'UTC')
      AND sd.block_time <  toDateTime('{end_dt}', 'UTC')
      AND sb.chain_id = {chain_id}
),
candidates AS (
    SELECT DISTINCT
        s.tx_hash, s.log_index, s.block_number,
        toUnixTimestamp(s.block_time) AS signal_ts,
        formatDateTime(s.block_time, '%Y-%m-%dT%H:%i:%SZ') AS signal_time,
        s.pool, s.buyer AS sender, s.tick,
        p.fee_bps, p.token0_address AS token0, p.token1_address AS token1,
        p.token0_decimals, p.token1_decimals,
        p.symbol0, p.symbol1,
        p.target_address, p.target_decimals, p.quote_side,
        toUnixTimestamp(p.created_at) AS pool_created_ts,
        dateDiff('second', p.created_at, s.block_time) AS pool_age_sec,
        -- USD notional via quote leg
        if(p.quote_side = 1,
            -- token1 is quote (e.g. WETH/USDC); amount1 is quote leg
            multiIf(
                p.token1_address = '{usdc}'  OR p.token1_address = '{usdbc}',
                abs(toFloat64(s.amount1)) / pow(10, p.token1_decimals),
                p.token1_address = '{weth}',
                abs(toFloat64(s.amount1)) / pow(10, p.token1_decimals) * {eth_usd},
                p.token1_address = '{cbbtc}',
                abs(toFloat64(s.amount1)) / pow(10, p.token1_decimals) * 60000,
                0.0),
           if(p.quote_side = 0,
            multiIf(
                p.token0_address = '{usdc}'  OR p.token0_address = '{usdbc}',
                abs(toFloat64(s.amount0)) / pow(10, p.token0_decimals),
                p.token0_address = '{weth}',
                abs(toFloat64(s.amount0)) / pow(10, p.token0_decimals) * {eth_usd},
                p.token0_address = '{cbbtc}',
                abs(toFloat64(s.amount0)) / pow(10, p.token0_decimals) * 60000,
                0.0),
            0.0)) AS notional_usd,
        -- direction: target getting bought when target amount goes IN (positive)
        if(p.quote_side = 1,
           if(toFloat64(s.amount0) < 0, 1, -1),  -- target=token0, amount0<0 means out of pool (buy)
           if(toFloat64(s.amount1) < 0, 1, -1)
        ) AS direction
    FROM swaps_in_window AS s
    INNER JOIN pool_meta_alt AS p ON s.pool = p.pool
)
SELECT *
FROM candidates
WHERE notional_usd >= {min_notional}
  AND direction = 1   -- buys only (target being bought)
ORDER BY signal_ts ASC
LIMIT {limit}
FORMAT JSONEachRow
"""


def _ch_data_window_end() -> datetime:
    """Pin our window end to the latest data we actually have (CH may lag)."""
    rows = ch_query(
        f"SELECT max(block_time) AS m FROM eatlq.uniswap_v3_swaps_decoded "
        f"WHERE chain_id = {CHAIN_ID} FORMAT JSONEachRow",
        timeout=30,
    )
    if not rows or not rows[0].get("m"):
        return datetime.now(timezone.utc)
    raw = rows[0]["m"]
    # CH returns "2026-05-26 12:10:25.000"
    try:
        return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def fetch_candidates_bulk(*, days: int, min_notional: float, limit: int) -> list[dict]:
    end_dt_obj = _ch_data_window_end()
    end_dt = end_dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    start_ts = int(end_dt_obj.timestamp()) - days * 86400
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    routing_csv = ", ".join(f"'{a}'" for a in BASE_ROUTING)
    sql = DISCOVERY_SQL_TEMPLATE.format(
        chain_id=CHAIN_ID, routing_csv=routing_csv,
        start_dt=start_dt, end_dt=end_dt,
        usdc=USDC, usdbc=USDBC, weth=WETH, cbbtc=CBBTC,
        eth_usd=ETH_USD,
        min_notional=int(min_notional),
        limit=limit,
    )
    log.info("discovery query: window %s → %s, threshold=$%d", start_dt, end_dt, int(min_notional))
    rows = ch_query(sql, timeout=600)
    log.info("candidates discovered: %d", len(rows))
    return rows


# ------------------------------------------------------------------------------
# 2. Wallet pre-signal history (30d lookback per sender, CH-only)
# ------------------------------------------------------------------------------

WALLET_HISTORY_SQL = r"""
-- Filter swaps_with_buyer by sender + time FIRST (small subset), then enrich.
WITH wb AS (
    SELECT
        lower(buyer_address) AS sender,
        lower(pool_address) AS pool,
        block_time
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {chain_id}
      AND lower(buyer_address) IN ({senders_in_clause})
      AND block_time >= toDateTime('{start_dt}', 'UTC')
      AND block_time <  toDateTime('{end_dt}', 'UTC')
),
pools AS (
    SELECT lower(pool_address) AS pool, min(discovered_at) AS created_at
    FROM eatlq.discovered_pools WHERE chain_id = {chain_id}
    GROUP BY pool
)
SELECT
    wb.sender,
    count() AS n_buys_30d,
    uniqExact(wb.pool) AS n_pools_30d,
    sum(if(dateDiff('second', pools.created_at, wb.block_time) <= 21600, 1, 0)) AS n_early_entries_6h,
    min(wb.block_time) AS first_buy_at,
    max(wb.block_time) AS last_buy_at
FROM wb
LEFT JOIN pools ON pools.pool = wb.pool
GROUP BY sender
FORMAT JSONEachRow
"""


def fetch_wallet_history(senders: list[str], lookback_days: int = 30) -> dict[str, dict]:
    if not senders:
        return {}
    end_dt_obj = _ch_data_window_end()
    end_dt = end_dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    start_dt = datetime.fromtimestamp(int(end_dt_obj.timestamp()) - lookback_days * 86400,
                                       tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    in_clause = ", ".join(f"'{s.lower()}'" for s in senders)
    sql = WALLET_HISTORY_SQL.format(
        chain_id=CHAIN_ID,
        senders_in_clause=in_clause,
        start_dt=start_dt, end_dt=end_dt,
    )
    rows = ch_query(sql, timeout=600)
    return {r["sender"].lower(): r for r in rows}


# ------------------------------------------------------------------------------
# 3. 5m forward replay (CH-only, ZERO RPC) — batched by pool to avoid N queries
# ------------------------------------------------------------------------------

FORWARD_REPLAY_SQL = r"""
WITH candidates AS (
    SELECT
        lower(pool_address) AS pool,
        signal_ts,
        entry_tick,
        cand_id
    FROM (
        SELECT
            arrayJoin({rows_array}) AS row_t
        )
    -- row_t = tuple(pool, signal_ts UInt32, entry_tick Int64, cand_id String)
    ARRAY JOIN [tuple_0=row_t.1 AS pool_address, row_t.2 AS signal_ts,
                row_t.3 AS entry_tick, row_t.4 AS cand_id] AS dummy
)
SELECT 1
"""


def fetch_forward_tick_batched(candidates: list[dict], *, lookahead_sec: int = 300) -> dict[str, dict]:
    """For each candidate, find the last decoded swap tick within signal_ts+lookahead.

    To keep this CH-only and bounded, do it as multiple per-pool grouped queries
    (one per distinct pool to amortize over many candidates in same pool).
    """
    by_pool: dict[str, list[dict]] = {}
    for c in candidates:
        by_pool.setdefault(c["pool"], []).append(c)
    forward_map: dict[str, dict] = {}
    n_pools = len(by_pool)
    log.info("forward replay: %d distinct pools (over %d candidates)", n_pools, len(candidates))

    for idx, (pool, cands) in enumerate(by_pool.items()):
        ts_min = min(int(c["signal_ts"]) for c in cands)
        ts_max = max(int(c["signal_ts"]) for c in cands) + lookahead_sec + 60
        start_dt = datetime.fromtimestamp(ts_min, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        end_dt = datetime.fromtimestamp(ts_max, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        sql = f"""
        SELECT
            toUnixTimestamp(block_time) AS ts,
            tick
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id = {CHAIN_ID}
          AND lower(pool_address) = '{pool}'
          AND block_time >= toDateTime('{start_dt}', 'UTC')
          AND block_time <= toDateTime('{end_dt}', 'UTC')
        ORDER BY block_number ASC, log_index ASC
        FORMAT JSONEachRow
        """
        rows = ch_query(sql, timeout=120)
        # Each row: ts, tick.  For each candidate, find last tick at ts <= signal_ts+lookahead
        ticks = [(int(r["ts"]), int(r["tick"])) for r in rows]
        for c in cands:
            target_ts = int(c["signal_ts"]) + lookahead_sec
            fwd_tick = None; fwd_ts = None
            for ts, tk in ticks:
                if ts > int(c["signal_ts"]) and ts <= target_ts:
                    fwd_tick = tk
                    fwd_ts = ts
            forward_map[f'{c["tx_hash"]}:{c["log_index"]}'] = {
                "forward_tick": fwd_tick, "forward_ts": fwd_ts,
                "n_swaps_in_window": sum(1 for ts, _ in ticks if int(c["signal_ts"]) < ts <= target_ts),
            }
        if (idx + 1) % 25 == 0:
            log.info("  forward replay: %d/%d pools done", idx + 1, n_pools)
    return forward_map


def tick_delta_to_return(delta_tick: float, direction: int) -> float:
    """Δprice/price ≈ direction * (1.0001^Δtick - 1)."""
    if direction == 0:
        return 0.0
    return direction * (math.pow(1.0001, delta_tick) - 1.0)


# ------------------------------------------------------------------------------
# 4. Per-wallet rolling pre-signal scores (Smart/Sniper/Diamond/Paper proxies)
# ------------------------------------------------------------------------------

PAST_BUYS_SQL = r"""
-- Just fetch wallet's past buys (sender, pool, buy_ts, buy_tick) — small.
WITH wb_filtered AS (
    SELECT
        lower(buyer_address) AS sender,
        lower(pool_address) AS pool,
        tx_hash, log_index,
        toUnixTimestamp(block_time) AS buy_ts
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {chain_id}
      AND lower(buyer_address) IN ({senders_in_clause})
      AND block_time >= toDateTime('{start_dt}', 'UTC')
      AND block_time <  toDateTime('{end_dt}', 'UTC')
)
SELECT
    wb.sender,
    wb.pool,
    wb.buy_ts,
    sd.tick AS buy_tick
FROM wb_filtered AS wb
INNER JOIN eatlq.uniswap_v3_swaps_decoded AS sd
    ON sd.tx_hash = wb.tx_hash AND sd.log_index = wb.log_index
WHERE sd.chain_id = {chain_id}
FORMAT JSONEachRow
"""


POOL_TICK_TIMELINE_SQL = r"""
SELECT
    toUnixTimestamp(block_time) AS ts,
    tick
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = {chain_id}
  AND lower(pool_address) = '{pool}'
  AND block_time >= toDateTime('{start_dt}', 'UTC')
  AND block_time <= toDateTime('{end_dt}', 'UTC')
ORDER BY block_number ASC, log_index ASC
FORMAT JSONEachRow
"""


def fetch_wallet_forward_history(senders: list[str], lookback_days: int = 30) -> dict[str, dict]:
    """Compute pre-pump rate per wallet via Python-side forward replay.
    1. Fetch past_buys for all senders (cheap, small).
    2. For each distinct pool, fetch tick timeline once (cheap, indexed).
    3. Match in Python — O(buys × log timeline) trivial.
    Returns dict[sender] = {n_buys, n_with_forward, n_pumped_after, uniq_pools_history}.
    """
    if not senders:
        return {}
    end_dt_obj = _ch_data_window_end()
    end_dt = end_dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    start_dt = datetime.fromtimestamp(int(end_dt_obj.timestamp()) - lookback_days * 86400,
                                       tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    in_clause = ", ".join(f"'{s.lower()}'" for s in senders)
    past_sql = PAST_BUYS_SQL.format(
        chain_id=CHAIN_ID, senders_in_clause=in_clause,
        start_dt=start_dt, end_dt=end_dt,
    )
    past = ch_query(past_sql, timeout=300)
    log.info("  past_buys fetched: %d rows", len(past))
    if not past:
        return {}
    # 2. Per-pool tick timelines
    pools_in_history = sorted({r["pool"].lower() for r in past})
    log.info("  past_buys touch %d distinct pools — fetching tick timelines", len(pools_in_history))
    pool_timelines: dict[str, list[tuple[int, int]]] = {}
    pool_end = end_dt
    for i, pool in enumerate(pools_in_history):
        # Timeline only needs to cover the buy window + 5min
        # Use the same lookback window plus 5min for tail
        ts_sql = POOL_TICK_TIMELINE_SQL.format(
            chain_id=CHAIN_ID, pool=pool,
            start_dt=start_dt, end_dt=pool_end,
        )
        try:
            rows = ch_query(ts_sql, timeout=60)
            pool_timelines[pool] = [(int(r["ts"]), int(r["tick"])) for r in rows]
        except Exception as e:
            log.warning("  timeline fetch failed for %s: %s", pool[:10], str(e)[:80])
            pool_timelines[pool] = []
        if (i + 1) % 50 == 0:
            log.info("    timelines: %d/%d", i + 1, len(pools_in_history))
    # 3. Pre-split each pool's timeline into separate ts/tick lists (for bisect).
    import bisect
    pool_ts: dict[str, list[int]] = {p: [t[0] for t in tl] for p, tl in pool_timelines.items()}
    pool_tk: dict[str, list[int]] = {p: [t[1] for t in tl] for p, tl in pool_timelines.items()}
    # 4. Per-sender aggregation with O(log N) per buy
    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {"n_buys": 0, "n_with_forward": 0,
                                                 "n_pumped_after": 0, "_pools": set()})
    for r in past:
        sender = r["sender"].lower()
        pool = r["pool"].lower()
        buy_ts = int(r["buy_ts"])
        buy_tick = int(r["buy_tick"])
        agg[sender]["n_buys"] += 1
        agg[sender]["_pools"].add(pool)
        ts_list = pool_ts.get(pool, [])
        tk_list = pool_tk.get(pool, [])
        if not ts_list:
            continue
        lo = bisect.bisect_right(ts_list, buy_ts)
        hi = bisect.bisect_right(ts_list, buy_ts + 300)
        if hi <= lo:
            continue  # no swaps in the +5min window
        fwd_tick = tk_list[hi - 1]
        agg[sender]["n_with_forward"] += 1
        if fwd_tick > buy_tick:
            agg[sender]["n_pumped_after"] += 1
    out: dict[str, dict] = {}
    for s, v in agg.items():
        out[s] = {
            "n_buys": v["n_buys"],
            "n_with_forward": v["n_with_forward"],
            "n_pumped_after": v["n_pumped_after"],
            "uniq_pools_history": len(v["_pools"]),
        }
    return out


# ------------------------------------------------------------------------------
# 5. Profile classifiers (continuous scores, sample sizes)
# ------------------------------------------------------------------------------

def classify_wallet(activity: dict, fwd_hist: dict) -> dict:
    """Return continuous proxies for the four whitepaper wallet classes.
    All scores in [0, 1] with sample-size attached. NEVER one-shot tags."""
    n_buys = int(activity.get("n_buys_30d", 0))
    n_pools = int(activity.get("n_pools_30d", 0))
    n_early = int(activity.get("n_early_entries_6h", 0))
    n_pumped = int(fwd_hist.get("n_pumped_after", 0))
    n_with_fwd = int(fwd_hist.get("n_with_forward", 0))
    uniq_pools_hist = int(fwd_hist.get("uniq_pools_history", 0))

    # Smart Money: high pre-pump rate × diversity × non-trivial sample
    pre_pump_rate = (n_pumped / n_with_fwd) if n_with_fwd >= 5 else None
    smart_score = 0.0
    if pre_pump_rate is not None:
        # blend rate × uniq_pools (cap)
        smart_score = max(0.0, min(1.0,
            (pre_pump_rate - 0.5) * 2.0  # rate above 50% counts; 100% → 1.0
            * min(1.0, uniq_pools_hist / 5.0)  # need ≥5 pools for full credit
        ))
    # Sniper: high early-entry rate
    early_rate = (n_early / n_buys) if n_buys >= 5 else None
    sniper_score = 0.0
    if early_rate is not None:
        sniper_score = max(0.0, min(1.0, early_rate))
    # Diamond proxy: ratio of unique pools to buys → patient buyer (low rebuying)
    # Paper proxy: high rebuy density (many buys in few pools) → fast turnover
    # (these are very rough; we don't have sell logs yet to do real holding time)
    if n_buys >= 5:
        rebuy_density = n_buys / max(n_pools, 1)
        paper_score = max(0.0, min(1.0, (rebuy_density - 1.0) / 4.0))  # 1 → 0; 5+ → 1
        diamond_score = max(0.0, min(1.0, 1.0 - paper_score))
    else:
        paper_score = None; diamond_score = None
    return {
        "smart_money_score": smart_score,
        "smart_money_sample_n": n_with_fwd,
        "smart_money_pre_pump_rate": pre_pump_rate,
        "sniper_score": sniper_score,
        "sniper_sample_n": n_buys,
        "sniper_early_rate": early_rate,
        "diamond_score": diamond_score,
        "paper_score": paper_score,
        "rebuy_density": (n_buys / max(n_pools, 1)) if n_buys >= 5 else None,
        "history_n_buys_30d": n_buys,
        "history_n_pools_30d": n_pools,
        "history_uniq_pools_with_5m_fwd": uniq_pools_hist,
    }


# ------------------------------------------------------------------------------
# 6. W-family Whale-score variants
# ------------------------------------------------------------------------------

def w_family(notional_usd: float, profile: dict, pre_velocity: float = 0.0) -> dict:
    """Six W variants. All clipped to [0, 1]."""
    smart = profile.get("smart_money_score") or 0.0
    rebuy = profile.get("rebuy_density") or 1.0
    paper = profile.get("paper_score") or 0.0
    n_buys = profile.get("history_n_buys_30d", 0)

    # W0: strict $100k binary (sprint092 baseline)
    w0 = 1.0 if notional_usd >= WHALE_USD_THRESH else 0.0

    # W1: soft notional — log-scaled sigmoid centered at $50k
    if notional_usd <= 0:
        w1 = 0.0
    else:
        z = math.log10(notional_usd) - math.log10(50_000)
        w1 = 1.0 / (1.0 + math.exp(-2.0 * z))

    # W2: wallet-only (smart_money_score)
    w2 = smart

    # W3: wallet + notional (50/50)
    w3 = 0.5 * w2 + 0.5 * w1

    # W4: wallet + notional + onset context (price velocity)
    onset = max(-1.0, min(1.0, pre_velocity * 50.0))  # scale 2% to 1.0
    w4 = max(0.0, min(1.0, 0.4 * w2 + 0.3 * w1 + 0.3 * max(0.0, onset)))

    # W5: wallet + notional + onset − anti-MEV/anti-bot penalty
    bot_penalty = 0.0
    if n_buys >= 50 and rebuy >= 3.0:  # sprays many buys across few pools → bot-like
        bot_penalty = min(0.4, 0.05 * (rebuy - 3.0))
    if paper >= 0.8:
        bot_penalty += 0.1
    w5 = max(0.0, min(1.0, w4 - bot_penalty))
    return {"w0": w0, "w1": w1, "w2": w2, "w3": w3, "w4": w4, "w5": w5,
            "bot_penalty": bot_penalty, "onset_factor": onset}


# ------------------------------------------------------------------------------
# 7. Control sample (matched same-week buyers in alt pools, NOT in candidates)
# ------------------------------------------------------------------------------

CONTROL_SAMPLE_SQL = r"""
WITH pool_meta AS (
    SELECT
        lower(pool_address) AS pool,
        any(lower(token0_address)) AS token0,
        any(lower(token1_address)) AS token1,
        any(token0_decimals) AS dec0, any(token1_decimals) AS dec1,
        multiIf(
            any(lower(token1_address)) IN ({routing_csv}), 1,
            any(lower(token0_address)) IN ({routing_csv}), 0,
            -1) AS quote_side,
        multiIf(
            any(lower(token1_address)) IN ({routing_csv}), any(lower(token0_address)),
            any(lower(token0_address)) IN ({routing_csv}), any(lower(token1_address)),
            '') AS target_address
    FROM eatlq.discovered_pools
    WHERE chain_id = {chain_id}
    GROUP BY pool
),
pool_meta_alt AS (
    SELECT * FROM pool_meta
    WHERE target_address != '' AND target_address NOT IN ({routing_csv})
)
,
sender_pool AS (
    -- one row per distinct sender in alt pools in window, sender NOT in exclusion
    SELECT
        lower(sb.buyer_address) AS sender,
        any(lower(sb.pool_address)) AS pool,
        any(toUnixTimestamp(sb.block_time)) AS signal_ts,
        any(p.target_address) AS target_address,
        any(p.quote_side) AS quote_side,
        any(p.token0) AS token0, any(p.token1) AS token1,
        any(p.dec0) AS dec0, any(p.dec1) AS dec1,
        count() AS n_alt_swaps
    FROM eatlq.uniswap_v3_swaps_with_buyer AS sb
    INNER JOIN pool_meta_alt AS p ON p.pool = lower(sb.pool_address)
    WHERE sb.chain_id = {chain_id}
      AND sb.block_time >= toDateTime('{start_dt}', 'UTC')
      AND sb.block_time <  toDateTime('{end_dt}', 'UTC')
      AND lower(sb.buyer_address) NOT IN ({exclude_senders})
    GROUP BY sender
    -- bot-like wallets typically have hundreds+ swaps; cap to keep humans
    HAVING n_alt_swaps BETWEEN 1 AND 200
)
SELECT sender, pool, signal_ts, target_address, quote_side, token0, token1, dec0, dec1, n_alt_swaps
FROM sender_pool
ORDER BY rand()
LIMIT {limit}
FORMAT JSONEachRow
"""


def fetch_control_sample(*, days: int, exclude_senders: set[str], limit: int = 500) -> list[dict]:
    end_dt_obj = _ch_data_window_end()
    end_dt = end_dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    start_ts = int(end_dt_obj.timestamp()) - days * 86400
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    routing_csv = ", ".join(f"'{a}'" for a in BASE_ROUTING)
    excl = ", ".join(f"'{s}'" for s in exclude_senders) or "''"
    sql = CONTROL_SAMPLE_SQL.format(
        chain_id=CHAIN_ID, routing_csv=routing_csv,
        start_dt=start_dt, end_dt=end_dt,
        exclude_senders=excl, limit=limit,
    )
    log.info("control sample query (limit=%d)", limit)
    rows = ch_query(sql, timeout=600)
    log.info("control sample rows: %d", len(rows))
    return rows


# ------------------------------------------------------------------------------
# 8. Distribution helpers
# ------------------------------------------------------------------------------

def distribution(name: str, values: list[float]) -> dict:
    import numpy as np
    arr = np.array([v for v in values if v is not None])
    if arr.size == 0:
        return {"name": name, "n": 0}
    return {
        "name": name,
        "n": int(arr.size),
        "min": float(arr.min()),
        "p10": float(np.quantile(arr, 0.10)),
        "p25": float(np.quantile(arr, 0.25)),
        "p50": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
        "p95": float(np.quantile(arr, 0.95)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
    }


# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-notional", type=float, default=2_000)
    ap.add_argument("--lookback-days", type=int, default=14)
    ap.add_argument("--max-candidates", type=int, default=2000)
    ap.add_argument("--control-n", type=int, default=500)
    ap.add_argument("--winner-thresh-pct", type=float, default=0.005,
                    help="forward return %>this → winner (0.005 = 0.5%)")
    ap.add_argument("--near-miss-low-pct", type=float, default=-0.003)
    ap.add_argument("--bot-senders", type=str, default="0x6ff5693b99212da76ad316178a184ab56d299b43",
                    help="comma-separated wallet addresses to flag as bot/MEV")
    args = ap.parse_args()
    BOT_SENDERS = {s.strip().lower() for s in args.bot_senders.split(",") if s.strip()}
    log.info("bot/MEV exclusion set: %s", BOT_SENDERS)

    # 1. discovery
    cands = fetch_candidates_bulk(days=args.days, min_notional=args.min_notional,
                                    limit=args.max_candidates)
    if not cands:
        log.warning("no candidates discovered; exiting")
        return

    # 2. wallet pre-signal activity (lookback per sender)
    #    EXCLUDE bot wallets from the CH query — they would blow up the join.
    senders_all = sorted({c["sender"].lower() for c in cands})
    senders_query = [s for s in senders_all if s not in BOT_SENDERS]
    senders = senders_all  # full set for downstream output
    log.info("distinct senders: total=%d human=%d (bot-excluded from CH query)",
             len(senders_all), len(senders_query))

    activity_map: dict[str, dict] = {}
    fwd_hist_map: dict[str, dict] = {}
    CHUNK = 100  # smaller chunks; CH 24.12 join planner is fragile on large IN
    for i in range(0, len(senders_query), CHUNK):
        chunk = senders_query[i:i+CHUNK]
        log.info("wallet history chunk %d/%d (n=%d)",
                 i//CHUNK+1, (len(senders_query)+CHUNK-1)//CHUNK, len(chunk))
        activity_map.update(fetch_wallet_history(chunk, args.lookback_days))
        fwd_hist_map.update(fetch_wallet_forward_history(chunk, args.lookback_days))
    log.info("activity_map: %d, fwd_hist_map: %d", len(activity_map), len(fwd_hist_map))
    # Bot wallets get a sentinel "skipped" profile so downstream code stays happy.
    for s in senders:
        if s in BOT_SENDERS:
            activity_map.setdefault(s, {"_skipped": "bot"})
            fwd_hist_map.setdefault(s, {"_skipped": "bot"})

    # 3. profile each wallet
    profiles: dict[str, dict] = {}
    for s in senders:
        act = activity_map.get(s, {})
        fwd = fwd_hist_map.get(s, {})
        profiles[s] = classify_wallet(act, fwd)

    # 4. 5m forward replay per candidate (CH only)
    forward_map = fetch_forward_tick_batched(cands)
    log.info("forward labels: %d (of %d candidates)", len(forward_map), len(cands))

    # 5. assemble per-candidate enrichment + W-family + label
    enriched: list[dict] = []
    for c in cands:
        sender = c["sender"].lower()
        prof = profiles.get(sender, {})
        key = f'{c["tx_hash"]}:{c["log_index"]}'
        fwd = forward_map.get(key, {"forward_tick": None})
        entry_tick = float(c.get("tick") or 0)
        direction = int(c.get("direction", 0))
        fwd_pct = None
        if fwd.get("forward_tick") is not None and direction != 0:
            fwd_pct = tick_delta_to_return(float(fwd["forward_tick"]) - entry_tick, direction)
        notional = float(c.get("notional_usd") or 0)
        ws = w_family(notional, prof, pre_velocity=0.0)
        if fwd_pct is None:
            label_set = "no_forward"
        elif fwd_pct >= args.winner_thresh_pct:
            label_set = "winner"
        elif fwd_pct <= args.near_miss_low_pct:
            label_set = "near_miss"
        else:
            label_set = "neutral"
        is_bot = sender in BOT_SENDERS
        enriched.append({
            "tx_hash": c["tx_hash"], "log_index": c["log_index"],
            "signal_ts": c["signal_ts"], "signal_time": c["signal_time"],
            "pool": c["pool"], "sender": sender,
            "is_bot_sender": is_bot,
            "target_address": c["target_address"],
            "symbol0": c.get("symbol0"), "symbol1": c.get("symbol1"),
            "fee_bps": c.get("fee_bps"),
            "pool_age_sec": c.get("pool_age_sec"),
            "notional_usd": notional,
            "direction": direction,
            "entry_tick": entry_tick,
            "forward_tick": fwd.get("forward_tick"),
            "forward_return_pct": fwd_pct,
            "label_set": label_set,
            **{f"prof_{k}": v for k, v in prof.items()},
            **{f"W_{k}": v for k, v in ws.items()},
        })

    # 6. control sample — exclude candidates AND known bots from both selection and profiling
    exclude_for_control = set(senders) | BOT_SENDERS
    control_rows = fetch_control_sample(days=args.days, exclude_senders=exclude_for_control,
                                         limit=args.control_n)
    # Profile control senders too (subset, capped). Drop any that snuck through as bots.
    control_senders = sorted({r["sender"].lower() for r in control_rows if r["sender"].lower() not in BOT_SENDERS})
    log.info("control distinct senders (bot-excluded): %d", len(control_senders))
    control_act_map: dict[str, dict] = {}
    control_fwd_map: dict[str, dict] = {}
    for i in range(0, len(control_senders), CHUNK):
        chunk = control_senders[i:i+CHUNK]
        log.info("control history chunk %d/%d", i//CHUNK+1, (len(control_senders)+CHUNK-1)//CHUNK)
        control_act_map.update(fetch_wallet_history(chunk, args.lookback_days))
        control_fwd_map.update(fetch_wallet_forward_history(chunk, args.lookback_days))
    control_profiles = {s: classify_wallet(control_act_map.get(s, {}), control_fwd_map.get(s, {}))
                          for s in control_senders}

    # 7. write artifacts
    detail_csv = OUT_DIR / "wallet_event_history.csv"
    with detail_csv.open("w", newline="") as f:
        if enriched:
            w = csv.DictWriter(f, fieldnames=list(enriched[0].keys()))
            w.writeheader(); w.writerows(enriched)
    log.info("wallet_event_history: %d rows → %s", len(enriched), detail_csv)

    profile_csv = OUT_DIR / "wallet_profile_scores.csv"
    with profile_csv.open("w", newline="") as f:
        rows_out = []
        for s in senders:
            row = {"sender": s, "group": "candidate", **profiles[s]}
            rows_out.append(row)
        for s in control_senders:
            row = {"sender": s, "group": "control", **control_profiles[s]}
            rows_out.append(row)
        if rows_out:
            w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            w.writeheader(); w.writerows(rows_out)
    log.info("wallet_profile_scores: %d rows → %s", len(rows_out), profile_csv)

    # 8. summary stats
    from collections import Counter
    target_counter = Counter(r["target_address"] for r in enriched)
    target_share_top1 = (target_counter.most_common(1)[0][1] / max(len(enriched), 1)) if enriched else 0.0
    top5 = target_counter.most_common(5)
    sym_lookup = {}
    for r in enriched:
        sym_lookup.setdefault(r["target_address"], r.get("symbol0") if r.get("symbol0") != "WETH" else r.get("symbol1"))

    by_label = Counter(r["label_set"] for r in enriched)
    winners = [r for r in enriched if r["label_set"] == "winner"]
    near = [r for r in enriched if r["label_set"] == "near_miss"]
    neutrals = [r for r in enriched if r["label_set"] == "neutral"]
    control_n_meaningful = sum(1 for s, p in control_profiles.items()
                                if (p.get("history_n_buys_30d") or 0) >= 1)
    # bot-vs-human split
    enriched_human = [r for r in enriched if not r["is_bot_sender"]]
    enriched_bot = [r for r in enriched if r["is_bot_sender"]]
    target_counter_h = Counter(r["target_address"] for r in enriched_human)
    top1_h = (target_counter_h.most_common(1)[0][1] / max(len(enriched_human), 1)) if enriched_human else 0.0

    winners_h = [r for r in enriched_human if r["label_set"] == "winner"]
    near_h = [r for r in enriched_human if r["label_set"] == "near_miss"]
    neutral_h = [r for r in enriched_human if r["label_set"] == "neutral"]

    summary = {
        "sprint": "094A",
        "name": "smart_money_preflight",
        "research_only": True,
        "guard_lifted": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rpc_calls_made": RPC_BUDGET["calls"],
        "rpc_budget_max": RPC_BUDGET["max"],
        "params": vars(args),
        "bot_senders_flagged": sorted(BOT_SENDERS),
        "candidates_n_total": len(enriched),
        "candidates_n_human": len(enriched_human),
        "candidates_n_bot": len(enriched_bot),
        "candidates_distinct_senders_total": len(senders),
        "candidates_distinct_senders_human": len({r["sender"] for r in enriched_human}),
        "candidates_distinct_pools": len({r["pool"] for r in enriched}),
        "candidates_distinct_targets": len(target_counter),
        "candidates_distinct_targets_human": len(target_counter_h),
        "top_token_share_all": target_share_top1,
        "top_token_share_human": top1_h,
        "top5_targets": [{"address": a, "share": n/max(len(enriched),1),
                          "symbol_hint": sym_lookup.get(a)} for a, n in top5],
        "concentration_red_flag_human": top1_h >= 0.50,
        "label_counts_total": dict(by_label),
        "label_counts_human": {
            "winner": len(winners_h), "near_miss": len(near_h),
            "neutral": len(neutral_h),
            "no_forward": len([r for r in enriched_human if r["label_set"] == "no_forward"]),
        },
        "winner_n_human": len(winners_h),
        "near_miss_n_human": len(near_h),
        "neutral_n_human": len(neutral_h),
        "control_distinct_senders": len(control_senders),
        "control_meaningful_history_n": control_n_meaningful,
        "min_holdout_for_verdict": 30,
        "verdict_phase6_predictivity": "INCONCLUSIVE_PREFLIGHT_ONLY",
        # W-family distribution per label set (HUMAN ONLY, bots excluded)
        "w_family_distributions_human": {
            "winners": {f"W_w{i}": distribution(f"W{i}", [r[f"W_w{i}"] for r in winners_h])
                       for i in range(6)} if winners_h else {},
            "near_miss": {f"W_w{i}": distribution(f"W{i}", [r[f"W_w{i}"] for r in near_h])
                          for i in range(6)} if near_h else {},
            "neutral": {f"W_w{i}": distribution(f"W{i}", [r[f"W_w{i}"] for r in neutral_h])
                        for i in range(6)} if neutral_h else {},
            "all_human": {f"W_w{i}": distribution(f"W{i}", [r[f"W_w{i}"] for r in enriched_human])
                          for i in range(6)} if enriched_human else {},
        },
        # Profile score distributions (candidate vs control)
        "profile_distributions": {
            "candidates": {
                "smart_money": distribution("smart", [profiles[s].get("smart_money_score") for s in senders]),
                "sniper":      distribution("sniper", [profiles[s].get("sniper_score") for s in senders]),
                "diamond":     distribution("diamond", [profiles[s].get("diamond_score") for s in senders]),
                "paper":       distribution("paper", [profiles[s].get("paper_score") for s in senders]),
                "history_n_buys": distribution("n_buys_30d", [profiles[s].get("history_n_buys_30d") for s in senders]),
            },
            "control": {
                "smart_money": distribution("smart", [control_profiles[s].get("smart_money_score") for s in control_senders]),
                "sniper":      distribution("sniper", [control_profiles[s].get("sniper_score") for s in control_senders]),
                "diamond":     distribution("diamond", [control_profiles[s].get("diamond_score") for s in control_senders]),
                "paper":       distribution("paper", [control_profiles[s].get("paper_score") for s in control_senders]),
                "history_n_buys": distribution("n_buys_30d", [control_profiles[s].get("history_n_buys_30d") for s in control_senders]),
            },
        },
        "notes": [
            "All wallet features are pre-signal only (lookback window stops at now()).",
            "Forward returns computed via CH replay of uniswap_v3_swaps_decoded; ZERO RPC calls.",
            "Wallet class scores are CONTINUOUS proxies + sample size; no one-shot封神 tagging.",
            "Diamond/Paper proxies use rebuy density (no sell-side decoder available yet).",
            "Concentration flag triggers at top-token-share ≥ 50%; sample weak if dominant.",
            "Verdict is INCONCLUSIVE_PREFLIGHT_ONLY — does NOT lift FAILED_NO_7D_ALPHA.",
        ],
    }
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info("summary written: %s", summary_path)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("w_family_distributions", "profile_distributions")},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
