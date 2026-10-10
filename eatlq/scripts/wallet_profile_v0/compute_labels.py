#!/usr/bin/env python3
"""
Wallet Profile v0 — whitepaper 4 labels + Whale.

Reads ClickHouse offline data only (no new RPC).
Outputs audit CSVs + optional Redis wallet:* sets.

Does NOT touch production labeler, models/, or guard flags.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import (  # noqa: E402
    CHAIN_ID,
    USDBC,
    USDC,
    WETH,
    ch_query,
    sql_hash,
    write_csv,
)

from sender_semantics import buyer_address_expr, buyer_join, FORBIDDEN_BUYERS  # noqa: E402

log = logging.getLogger("wallet_profile_v0")

AUDIT_ROOT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels"
ETH_USD_DEFAULT = float(os.environ.get("WALLET_PROFILE_ETH_USD", "3500"))

LABEL_KEYS = ("sniper", "smart_money", "diamond_hand", "paper_hand", "whale")


def _forbidden_buyer_filter(expr: str) -> str:
    items = ", ".join(f"'{a}'" for a in FORBIDDEN_BUYERS)
    return f"{expr} NOT IN ({items}) AND length({expr}) = 42"


def _buyer_col(s_alias: str = "s", tf_alias: str = "tf") -> str:
    return f"{buyer_address_expr(s_alias, tf_alias)} AS sender_address"


def compute_sniper(ch_chain: int = CHAIN_ID, lookback_days: int = 90, limit: int = 5000) -> list[dict]:
    """Sender buys in pool within created_block+0..+3; WETH-leg pools only."""
    sql = f"""
    WITH pool_meta AS (
        SELECT
            lower(pool_address) AS pool_address,
            created_block
        FROM eatlq.discovered_pools FINAL
        WHERE chain_id = {ch_chain}
          AND has_weth_leg = 1
          AND created_block > 0
    ),
    early_buys AS (
        SELECT
            {_buyer_col('s', 'tf')},
            lower(s.pool_address) AS pool_address,
            min(s.block_number) AS entry_block,
            pm.created_block,
            count() AS buy_count
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        {buyer_join('s', 'tf')}
        INNER JOIN pool_meta AS pm ON lower(s.pool_address) = pm.pool_address
        WHERE s.chain_id = {ch_chain}
          AND s.block_time >= now() - INTERVAL {lookback_days} DAY
          AND s.block_number >= pm.created_block
          AND s.block_number <= pm.created_block + 3
          AND s.amount0 < 0
          AND {_forbidden_buyer_filter(buyer_address_expr('s', 'tf'))}
        GROUP BY sender_address, pool_address, pm.created_block
    )
    SELECT
        sender_address,
        count() AS sniper_pools,
        min(entry_block - created_block) AS min_blocks_after_create,
        max(buy_count) AS max_buys_in_window
    FROM early_buys
    GROUP BY sender_address
    HAVING sniper_pools >= 1
    ORDER BY sniper_pools DESC
    LIMIT {limit}
    """
    return ch_query(sql)


def compute_smart_money(ch_chain: int = CHAIN_ID, lookback_days: int = 90, limit: int = 5000) -> list[dict]:
    """Buy when token tick < per-token p10; sell when tick > p90."""
    return compute_smart_money_as_of(
        as_of_date=None,
        ch_chain=ch_chain,
        lookback_days=lookback_days,
        limit=limit,
    )


def _ticks_light_available(min_rows: int = 10_000, lookback_days: int = 90) -> bool:
    """MV must have enough rows AND span the requested lookback window."""
    try:
        rows = ch_query(
            f"""
            SELECT count() AS n
            FROM eatlq.sender_token_ticks_light
            WHERE day >= today() - INTERVAL {lookback_days + 7} DAY
            FORMAT JSONEachRow
            """,
            timeout=15,
        )
        if not rows or int(rows[0]["n"]) < min_rows:
            return False
        span = ch_query(
            """
            SELECT dateDiff('day', min(day), today()) AS span_days
            FROM eatlq.sender_token_ticks_light
            FORMAT JSONEachRow
            """,
            timeout=15,
        )
        return bool(span and int(span[0].get("span_days") or 0) >= lookback_days - 7)
    except Exception:
        return False


def compute_smart_money_as_of(
    as_of_date: str | None = None,
    ch_chain: int = CHAIN_ID,
    lookback_days: int = 90,
    limit: int = 5000,
    *,
    use_ticks_light: bool = True,
) -> list[dict]:
    """Point-in-time smart_money; as_of_date=None uses now(). Prefers sender_token_ticks_light MV."""
    if as_of_date:
        day_lo = f"toDate('{as_of_date}') - INTERVAL {lookback_days} DAY"
        day_hi = f"toDate('{as_of_date}')"
        time_lo = f"toDateTime('{as_of_date}', 'UTC') - INTERVAL {lookback_days} DAY"
        time_hi = f"toDateTime('{as_of_date}', 'UTC')"
    else:
        day_lo = f"today() - INTERVAL {lookback_days} DAY"
        day_hi = "today() + 1"
        time_lo = f"now() - INTERVAL {lookback_days} DAY"
        time_hi = "now()"

    if use_ticks_light and _ticks_light_available(lookback_days=lookback_days):
        sql = f"""
        WITH token_ticks AS (
            SELECT
                token_address,
                sender_address,
                side,
                tick
            FROM eatlq.sender_token_ticks_light
            WHERE chain_id = {ch_chain}
              AND day >= {day_lo}
              AND day < {day_hi}
        ),
        bounds AS (
            SELECT
                token_address,
                quantile(0.10)(tick) AS tick_p10,
                quantile(0.90)(tick) AS tick_p90
            FROM token_ticks
            GROUP BY token_address
            HAVING count() >= 20
        ),
        labeled AS (
            SELECT
                tt.sender_address,
                tt.side,
                tt.tick,
                b.tick_p10,
                b.tick_p90
            FROM token_ticks AS tt
            INNER JOIN bounds AS b ON tt.token_address = b.token_address
        )
        SELECT
            sender_address,
            countIf(side = 'buy' AND tick <= tick_p10) AS smart_buys,
            countIf(side = 'sell' AND tick >= tick_p90) AS smart_sells,
            countIf(side = 'buy') AS total_buys,
            countIf(side = 'sell') AS total_sells
        FROM labeled
        GROUP BY sender_address
        HAVING smart_buys >= 1 AND smart_sells >= 1
           AND (smart_buys + smart_sells) >= 2
        ORDER BY smart_buys + smart_sells DESC
        LIMIT {limit}
        """
        return ch_query(sql, timeout=180)

    sql = f"""
    WITH token_ticks AS (
        SELECT
            if(s.amount0 < 0,
               if(lower(dp.token0_address) = lower('{WETH}'), lower(dp.token1_address), lower(dp.token0_address)),
               if(lower(dp.token1_address) = lower('{WETH}'), lower(dp.token0_address), lower(dp.token1_address))
            ) AS token_address,
            s.tick AS tick,
            {_buyer_col('s', 'tf')},
            if(s.amount0 < 0, 'buy', 'sell') AS side
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        {buyer_join('s', 'tf')}
        INNER JOIN eatlq.discovered_pools AS dp FINAL
            ON lower(s.pool_address) = lower(dp.pool_address) AND s.chain_id = dp.chain_id
        WHERE s.chain_id = {ch_chain}
          AND s.block_time >= {time_lo}
          AND s.block_time < {time_hi}
          AND dp.has_weth_leg = 1
          AND s.tick != 0
          AND {_forbidden_buyer_filter(buyer_address_expr('s', 'tf'))}
    ),
    bounds AS (
        SELECT
            token_address,
            quantile(0.10)(tick) AS tick_p10,
            quantile(0.90)(tick) AS tick_p90
        FROM token_ticks
        GROUP BY token_address
        HAVING count() >= 20
    ),
    labeled AS (
        SELECT
            tt.sender_address,
            tt.side,
            tt.tick,
            b.tick_p10,
            b.tick_p90
        FROM token_ticks AS tt
        INNER JOIN bounds AS b ON tt.token_address = b.token_address
    )
    SELECT
        sender_address,
        countIf(side = 'buy' AND tick <= tick_p10) AS smart_buys,
        countIf(side = 'sell' AND tick >= tick_p90) AS smart_sells,
        countIf(side = 'buy') AS total_buys,
        countIf(side = 'sell') AS total_sells
    FROM labeled
    GROUP BY sender_address
    HAVING smart_buys >= 1 AND smart_sells >= 1
       AND (smart_buys + smart_sells) >= 2
    ORDER BY smart_buys + smart_sells DESC
    LIMIT {limit}
    """
    return ch_query(sql, timeout=180)


def compute_smart_money_strict(
    as_of_date: str | None = None,
    ch_chain: int = CHAIN_ID,
    lookback_days: int = 90,
    limit: int = 5000,
    *,
    early_entry_hours: int = 24,
    hold_days_max: int = 7,
    pnl_sigma: float = 1.0,
) -> list[dict]:
    """Whitepaper strict smart_money: early entry + PnL > σ + timely exit."""
    effective_lb = lookback_days
    if not _ticks_light_available(lookback_days=lookback_days):
        for lb in (30, 14):
            if _ticks_light_available(lookback_days=lb, min_rows=1000):
                effective_lb = lb
                log.info("strict labels using reduced lookback=%dd (MV span)", lb)
                break
        else:
            log.warning("sender_token_ticks_light unavailable; strict labels empty")
            return []
    lookback_days = effective_lb

    if as_of_date:
        day_lo = f"toDate('{as_of_date}') - INTERVAL {lookback_days} DAY"
        day_hi = f"toDate('{as_of_date}')"
        as_of_ts = f"toDateTime('{as_of_date}', 'UTC')"
    else:
        day_lo = f"today() - INTERVAL {lookback_days} DAY"
        day_hi = "today() + 1"
        as_of_ts = "now()"

    sql = f"""
    WITH token_ticks AS (
        SELECT token_address, sender_address, side, tick, block_time
        FROM eatlq.sender_token_ticks_light
        WHERE chain_id = {ch_chain}
          AND day >= {day_lo}
          AND day < {day_hi}
          AND block_time < {as_of_ts}
    ),
    token_first AS (
        SELECT token_address, min(block_time) AS first_seen
        FROM token_ticks
        GROUP BY token_address
    ),
    round_trips AS (
        SELECT
            tt.sender_address,
            tt.token_address,
            minIf(tt.tick, tt.side = 'buy') AS entry_tick,
            maxIf(tt.tick, tt.side = 'sell') AS exit_tick,
            minIf(tt.block_time, tt.side = 'buy') AS first_buy,
            minIf(tt.block_time, tt.side = 'sell') AS first_sell
        FROM token_ticks AS tt
        GROUP BY tt.sender_address, tt.token_address
        HAVING first_buy > toDateTime('1970-01-01')
           AND first_sell > first_buy
           AND entry_tick != 0
           AND exit_tick != 0
    ),
    pnl AS (
        SELECT
            rt.sender_address,
            rt.token_address,
            (rt.exit_tick - rt.entry_tick) / greatest(abs(rt.entry_tick), 1) AS pnl_proxy,
            dateDiff('hour', tf.first_seen, rt.first_buy) AS entry_lag_hours,
            dateDiff('day', rt.first_buy, rt.first_sell) AS hold_days,
            rt.exit_tick >= rt.entry_tick * 1.5 AS timely_profit_exit
        FROM round_trips AS rt
        INNER JOIN token_first AS tf ON rt.token_address = tf.token_address
    ),
    stats AS (
        SELECT avg(pnl_proxy) AS mu, stddevPop(pnl_proxy) AS sigma
        FROM pnl
        WHERE isFinite(pnl_proxy)
    ),
    strict_rows AS (
        SELECT
            p.sender_address,
            count() AS strict_tokens,
            avg(p.pnl_proxy) AS avg_pnl_proxy
        FROM pnl AS p
        CROSS JOIN stats AS s
        WHERE p.entry_lag_hours <= {early_entry_hours}
          AND p.hold_days <= {hold_days_max}
          AND (p.timely_profit_exit = 1 OR p.hold_days <= {hold_days_max})
          AND p.pnl_proxy > s.mu + {pnl_sigma} * greatest(s.sigma, 0.01)
        GROUP BY p.sender_address
        HAVING strict_tokens >= 1
    )
    SELECT
        sender_address,
        strict_tokens,
        avg_pnl_proxy
    FROM strict_rows
    ORDER BY strict_tokens DESC, avg_pnl_proxy DESC
    LIMIT {limit}
    """
    return ch_query(sql, timeout=240)


def compute_diamond_hand(ch_chain: int = CHAIN_ID, lookback_days: int = 180, limit: int = 5000) -> list[dict]:
    """Hold > 30 days between first buy and first sell on same token."""
    sql = f"""
    WITH token_flows AS (
        SELECT
            {_buyer_col('s', 'tf')},
            if(s.amount0 < 0,
               if(lower(dp.token0_address) = lower('{WETH}'), lower(dp.token1_address), lower(dp.token0_address)),
               if(lower(dp.token1_address) = lower('{WETH}'), lower(dp.token0_address), lower(dp.token1_address))
            ) AS token_address,
            s.block_time AS block_time,
            if(s.amount0 < 0, 1, 0) AS is_buy
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        {buyer_join('s', 'tf')}
        INNER JOIN eatlq.discovered_pools AS dp FINAL
            ON lower(s.pool_address) = lower(dp.pool_address) AND s.chain_id = dp.chain_id
        WHERE s.chain_id = {ch_chain}
          AND s.block_time >= now() - INTERVAL {lookback_days} DAY
          AND dp.has_weth_leg = 1
          AND {_forbidden_buyer_filter(buyer_address_expr('s', 'tf'))}
    ),
    agg AS (
        SELECT
            sender_address,
            token_address,
            minIf(block_time, is_buy = 1) AS first_buy,
            minIf(block_time, is_buy = 0) AS first_sell_any
        FROM token_flows
        GROUP BY sender_address, token_address
    ),
    round_trips AS (
        SELECT
            sender_address,
            token_address,
            first_buy,
            first_sell_any AS first_sell,
            dateDiff('day', first_buy, first_sell_any) AS hold_days
        FROM agg
        WHERE first_buy > toDateTime('1970-01-01')
          AND first_sell_any > first_buy
          AND dateDiff('day', first_buy, first_sell_any) >= 14
    )
    SELECT
        sender_address,
        count() AS diamond_tokens,
        max(hold_days) AS max_hold_days,
        avg(hold_days) AS avg_hold_days
    FROM round_trips
    GROUP BY sender_address
    HAVING diamond_tokens >= 1
    ORDER BY max_hold_days DESC
    LIMIT {limit}
    """
    return ch_query(sql)


def compute_paper_hand(ch_chain: int = CHAIN_ID, lookback_days: int = 90, limit: int = 5000) -> list[dict]:
    """Exit within 1h at price < entry×1.10 for >60% of round trips."""
    sql = f"""
    WITH token_flows AS (
        SELECT
            {_buyer_col('s', 'tf')},
            lower(s.pool_address) AS pool_address,
            s.block_time AS block_time,
            s.tick AS tick,
            if(s.amount0 < 0, 1, 0) AS is_buy
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        {buyer_join('s', 'tf')}
        INNER JOIN eatlq.discovered_pools AS dp FINAL
            ON lower(s.pool_address) = lower(dp.pool_address) AND s.chain_id = dp.chain_id
        WHERE s.chain_id = {ch_chain}
          AND s.block_time >= now() - INTERVAL {lookback_days} DAY
          AND dp.has_weth_leg = 1
          AND {_forbidden_buyer_filter(buyer_address_expr('s', 'tf'))}
    ),
    buys AS (
        SELECT sender_address, pool_address, block_time AS buy_time, tick AS buy_tick
        FROM token_flows WHERE is_buy = 1
    ),
    sells AS (
        SELECT sender_address, pool_address, block_time AS sell_time, tick AS sell_tick
        FROM token_flows WHERE is_buy = 0
    ),
    pairs AS (
        SELECT
            b.sender_address,
            b.pool_address,
            b.buy_time,
            s.sell_time,
            b.buy_tick,
            s.sell_tick,
            dateDiff('second', b.buy_time, s.sell_time) AS hold_sec,
            if(s.sell_tick <= b.buy_tick * 1.10, 1, 0) AS paper_exit
        FROM buys AS b
        INNER JOIN sells AS s
            ON b.sender_address = s.sender_address
           AND b.pool_address = s.pool_address
           AND s.sell_time > b.buy_time
        WHERE dateDiff('second', b.buy_time, s.sell_time) <= 3600
    )
    SELECT
        sender_address,
        count() AS paper_trades,
        sum(paper_exit) AS paper_pattern_count,
        paper_pattern_count / count() AS paper_rate
    FROM pairs
    GROUP BY sender_address
    HAVING paper_trades >= 3 AND paper_rate >= 0.6
    ORDER BY paper_rate DESC, paper_trades DESC
    LIMIT {limit}
    """
    return ch_query(sql)


def compute_whale(
    ch_chain: int = CHAIN_ID,
    lookback_days: int = 30,
    threshold_usd: float = 100_000,
    eth_usd: float = ETH_USD_DEFAULT,
    limit: int = 5000,
) -> list[dict]:
    """Cumulative WETH leg × ETH_USD or stable leg ≥ threshold."""
    sql = f"""
    WITH swaps AS (
        SELECT
            {_buyer_col('s', 'tf')},
            lower(dp.token0_address) AS token0,
            lower(dp.token1_address) AS token1,
            abs(s.amount0) / pow(10, dp.token0_decimals) AS amt0,
            abs(s.amount1) / pow(10, dp.token1_decimals) AS amt1
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        {buyer_join('s', 'tf')}
        INNER JOIN eatlq.discovered_pools AS dp FINAL
            ON lower(s.pool_address) = lower(dp.pool_address) AND s.chain_id = dp.chain_id
        WHERE s.chain_id = {ch_chain}
          AND s.block_time >= now() - INTERVAL {lookback_days} DAY
          AND {_forbidden_buyer_filter(buyer_address_expr('s', 'tf'))}
    ),
    notionals AS (
        SELECT
            sender_address,
            sum(
                multiIf(
                    token0 = lower('{WETH}'), amt0 * {eth_usd},
                    token1 = lower('{WETH}'), amt1 * {eth_usd},
                    token0 IN (lower('{USDC}'), lower('{USDBC}')), amt0,
                    token1 IN (lower('{USDC}'), lower('{USDBC}')), amt1,
                    0.0
                )
            ) AS total_usd_proxy
        FROM swaps
        GROUP BY sender_address
    )
    SELECT
        sender_address,
        total_usd_proxy
    FROM notionals
    WHERE total_usd_proxy >= {threshold_usd}
    ORDER BY total_usd_proxy DESC
    LIMIT {limit}
    """
    return ch_query(sql)


def merge_all_labels(label_sets: dict[str, list[dict]]) -> list[dict[str, Any]]:
    senders: dict[str, dict[str, Any]] = {}
    for label, rows in label_sets.items():
        col = f"is_{label}" if label != "smart_money" else "is_smart_money"
        if label == "smart_money":
            col = "is_smart_money"
        elif label == "diamond_hand":
            col = "is_diamond_hand"
        elif label == "paper_hand":
            col = "is_paper_hand"
        elif label == "sniper":
            col = "is_sniper"
        elif label == "whale":
            col = "is_whale"
        for row in rows:
            addr = str(row["sender_address"]).lower()
            if addr not in senders:
                senders[addr] = {
                    "sender_address": addr,
                    "is_smart_money": False,
                    "is_sniper": False,
                    "is_diamond_hand": False,
                    "is_paper_hand": False,
                    "is_whale": False,
                }
            senders[addr][col] = True
    return sorted(senders.values(), key=lambda r: r["sender_address"])


def publish_redis(
    all_labels: list[dict[str, Any]],
    redis_url: str | None = None,
    redis_suffix: str = "",
) -> dict[str, int]:
    import redis

    url = redis_url or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.from_url(url, decode_responses=True)
    mapping = {
        "smart_money": "is_smart_money",
        "sniper": "is_sniper",
        "diamond_hand": "is_diamond_hand",
        "paper_hand": "is_paper_hand",
        "whale": "is_whale",
    }
    counts: dict[str, int] = {}
    for tier, col in mapping.items():
        key = f"wallet:{tier}{redis_suffix}"
        addrs = [row["sender_address"] for row in all_labels if row.get(col)]
        pipe = r.pipeline()
        if addrs:
            pipe.delete(key)
            pipe.sadd(key, *addrs)
            pipe.expire(key, 86400 * 14)
        pipe.execute()
        counts[key] = int(r.scard(key) or 0)
    return counts


def write_metadata(
    label_sets: dict[str, list[dict]],
    query_hashes: dict[str, str],
    eth_usd: float,
    redis_counts: dict[str, int] | None = None,
) -> None:
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": CHAIN_ID,
        "eth_usd_assumption": eth_usd,
        "whale_threshold_usd": 100_000,
        "lookback_days": {
            "sniper": 90,
            "smart_money": 90,
            "diamond_hand": 180,
            "paper_hand": 90,
            "whale": 30,
        },
        "counts": {k: len(v) for k, v in label_sets.items()},
        "query_hashes": query_hashes,
        "redis_counts": redis_counts or {},
        "research_only": True,
        "guard_flags_touched": False,
    }
    (AUDIT_ROOT / "labels_metadata.json").write_text(json.dumps(meta, indent=2))


def run_all(
    *,
    publish: bool = False,
    limit: int = 5000,
    eth_usd: float = ETH_USD_DEFAULT,
    only: list[str] | None = None,
    lookback_days: dict[str, int] | None = None,
    output_suffix: str = "",
    redis_suffix: str = "",
) -> dict[str, Any]:
    AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    label_sets: dict[str, list[dict]] = {}
    query_hashes: dict[str, str] = {}
    lb = lookback_days or {}

    compute_fns = {
        "sniper": lambda: compute_sniper(limit=limit, lookback_days=lb.get("sniper", 90)),
        "smart_money": lambda: compute_smart_money(limit=limit, lookback_days=lb.get("smart_money", 90)),
        "diamond_hand": lambda: compute_diamond_hand(limit=limit, lookback_days=lb.get("diamond_hand", 180)),
        "paper_hand": lambda: compute_paper_hand(limit=limit, lookback_days=lb.get("paper_hand", 90)),
        "whale": lambda: compute_whale(eth_usd=eth_usd, limit=limit, lookback_days=lb.get("whale", 30)),
    }

    selected = only if only else list(compute_fns.keys())
    for name in selected:
        fn = compute_fns[name]
        log.info("computing label: %s lookback=%s", name, lb.get(name, "default"))
        try:
            rows = fn()
            label_sets[name] = rows
            query_hashes[name] = sql_hash(name)
            out_name = f"{name}{output_suffix}.csv"
            write_csv(
                AUDIT_ROOT / out_name,
                rows,
                list(rows[0].keys()) if rows else ["sender_address"],
            )
            log.info("  %s: %d senders -> %s", name, len(rows), out_name)
        except Exception as e:
            log.error("  %s failed: %s", name, e)
            label_sets[name] = []

    all_labels = merge_all_labels(label_sets)
    all_name = f"all_labels{output_suffix}.csv"
    write_csv(
        AUDIT_ROOT / all_name,
        all_labels,
        ["sender_address", "is_smart_money", "is_sniper", "is_diamond_hand", "is_paper_hand", "is_whale"],
    )

    redis_counts = None
    if publish:
        try:
            redis_counts = publish_redis(all_labels, redis_suffix=redis_suffix)
            log.info("redis published: %s", redis_counts)
        except Exception as e:
            log.warning("redis publish failed (audit CSVs still written): %s", e)

    write_metadata(label_sets, query_hashes, eth_usd, redis_counts)
    return {
        "label_sets": {k: len(v) for k, v in label_sets.items()},
        "all_labels": len(all_labels),
        "all_labels_csv": str((AUDIT_ROOT / all_name).relative_to(REPO)),
        "redis": redis_counts,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Wallet Profile v0 label computation")
    p.add_argument("--publish", action="store_true", help="Write Redis wallet:* sets")
    p.add_argument("--redis-out", action="store_true", help="Alias for --publish")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--eth-usd", type=float, default=ETH_USD_DEFAULT)
    p.add_argument("--only", type=str, default=None, help="Comma-separated label(s), e.g. smart_money")
    p.add_argument("--lookback-days", type=int, default=None, help="Lookback for selected label(s)")
    p.add_argument("--output-suffix", type=str, default="", help="Suffix for output CSVs, e.g. _lb14d")
    p.add_argument("--redis-suffix", type=str, default="", help="Suffix for Redis keys, e.g. _lb14d")
    p.add_argument("--strict-smart-money", action="store_true", help="Export smart_money_strict.csv (v2)")
    args = p.parse_args()

    if args.strict_smart_money:
        rows = compute_smart_money_strict(lookback_days=args.lookback_days or 90, limit=args.limit)
        out = AUDIT_ROOT / "smart_money_strict.csv"
        write_csv(out, rows, ["sender_address", "strict_tokens", "avg_pnl_proxy"])
        all_labels = merge_all_labels({"smart_money": [{**r, "is_smart_money": True} for r in rows]})
        write_csv(
            AUDIT_ROOT / "all_labels_strict.csv",
            all_labels,
            ["sender_address", "is_smart_money", "is_sniper", "is_diamond_hand", "is_paper_hand", "is_whale"],
        )
        print(json.dumps({"strict_smart_money_n": len(rows), "output": str(out.relative_to(REPO))}, indent=2))
        return

    only = [x.strip() for x in args.only.split(",")] if args.only else None
    lookback: dict[str, int] = {}
    if args.lookback_days is not None:
        targets = only or ["smart_money"]
        for t in targets:
            lookback[t] = args.lookback_days

    result = run_all(
        publish=args.publish or args.redis_out,
        limit=args.limit,
        eth_usd=args.eth_usd,
        only=only,
        lookback_days=lookback or None,
        output_suffix=args.output_suffix,
        redis_suffix=args.redis_suffix,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
