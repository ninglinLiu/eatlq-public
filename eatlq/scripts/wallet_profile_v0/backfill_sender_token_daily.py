#!/usr/bin/env python3
"""Backfill sender_token_ticks_light by day/hour/15min shard (OOM-safe)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from ch_helper import CHAIN_ID, WETH, ch_query  # noqa: E402

log = logging.getLogger("backfill_sender_token_daily")

DDL_PATH = REPO / "infra" / "clickhouse" / "init" / "039_sender_token_daily_mv.sql"
AUDIT = REPO / "mvp" / "audits" / "wallet_profile_v0" / "infra"
LOG_JSON = AUDIT / "sender_token_ticks_backfill.json"

_TOKEN_EXPR = f"""
if(
    s.amount0 < 0,
    if(lower(dp.token0_address) = lower('{WETH}'), lower(dp.token1_address), lower(dp.token0_address)),
    if(lower(dp.token1_address) = lower('{WETH}'), lower(dp.token0_address), lower(dp.token1_address))
)
"""


def apply_ddl() -> None:
    if not DDL_PATH.exists():
        raise FileNotFoundError(DDL_PATH)
    sql = DDL_PATH.read_text(encoding="utf-8")
    import subprocess

    r = subprocess.run(
        ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client", "--multiquery"],
        input=sql,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:500])


def _insert_window(start_ts: str, end_ts: str, chain_id: int = CHAIN_ID, *, shard: int = 0, shards: int = 4) -> None:
    shard_filter = f"AND cityHash64(s.pool_address) % {shards} = {shard}" if shards > 1 else ""
    sql = f"""
    INSERT INTO eatlq.sender_token_ticks_light
    SELECT
        toDate(s.block_time) AS day,
        s.chain_id AS chain_id,
        lower(
            if(
                length(tf.tx_from) = 42 AND startsWith(tf.tx_from, '0x'),
                tf.tx_from,
                s.sender
            )
        ) AS sender_address,
        if(
            s.amount0 < 0,
            if(lower(dp.token0_address) = lower('{WETH}'), lower(dp.token1_address), lower(dp.token0_address)),
            if(lower(dp.token1_address) = lower('{WETH}'), lower(dp.token0_address), lower(dp.token1_address))
        ) AS token_address,
        if(s.amount0 < 0, 'buy', 'sell') AS side,
        s.tick AS tick,
        s.block_time AS block_time
    FROM eatlq.uniswap_v3_swaps_decoded AS s
    LEFT JOIN (
        SELECT chain_id, tx_hash, anyLast(tx_from) AS tx_from
        FROM eatlq.tx_from_by_hash
        GROUP BY chain_id, tx_hash
    ) AS tf ON s.chain_id = tf.chain_id AND lower(s.tx_hash) = lower(tf.tx_hash)
    ANY INNER JOIN (
        SELECT pool_address, token0_address, token1_address
        FROM eatlq.discovered_pools
        WHERE chain_id = {chain_id} AND has_weth_leg = 1
    ) AS dp ON lower(s.pool_address) = lower(dp.pool_address)
    WHERE s.chain_id = {chain_id}
      AND s.tick != 0
      AND s.block_time >= toDateTime('{start_ts}', 'UTC')
      AND s.block_time < toDateTime('{end_ts}', 'UTC')
      AND if(
            s.amount0 < 0,
            if(lower(dp.token0_address) = lower('{WETH}'), lower(dp.token1_address), lower(dp.token0_address)),
            if(lower(dp.token1_address) = lower('{WETH}'), lower(dp.token0_address), lower(dp.token1_address))
        ) != ''
      {shard_filter}
    SETTINGS max_memory_usage = 8000000000, max_threads = 4
    """
    ch_query(sql, timeout=1200)


def backfill_window(
    start: datetime,
    end: datetime,
    chain_id: int = CHAIN_ID,
    *,
    shards: int = 4,
) -> None:
    for shard in range(shards):
        _insert_window(
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
            chain_id,
            shard=shard,
            shards=shards,
        )


def backfill_hour(day: date, hour: int, chain_id: int = CHAIN_ID) -> dict:
    start = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    try:
        backfill_window(start, end, chain_id)
    except Exception as e:
        log.warning("%s h%02d failed: %s", day, hour, str(e)[:120])
    cnt = ch_query(
        f"SELECT count() AS n FROM eatlq.sender_token_ticks_light "
        f"WHERE chain_id={chain_id} AND day=toDate('{day.isoformat()}') "
        f"AND block_time >= toDateTime('{start.strftime('%Y-%m-%d %H:%M:%S')}', 'UTC') "
        f"AND block_time < toDateTime('{(start + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')}', 'UTC') "
        f"FORMAT JSONEachRow",
        timeout=60,
    )
    return {"day": day.isoformat(), "hour": hour, "rows": int(cnt[0]["n"]) if cnt else 0}


def backfill_day(day: date, chain_id: int = CHAIN_ID, *, hourly: bool = True) -> dict:
    if not hourly:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        backfill_window(start, start + timedelta(days=1), chain_id)
        cnt = ch_query(
            f"SELECT count() AS n FROM eatlq.sender_token_ticks_light "
            f"WHERE chain_id={chain_id} AND day=toDate('{day.isoformat()}') FORMAT JSONEachRow",
            timeout=60,
        )
        return {"day": day.isoformat(), "rows": int(cnt[0]["n"]) if cnt else 0}

    total = 0
    for hour in range(24):
        row = backfill_hour(day, hour, chain_id)
        total += row["rows"]
    return {"day": day.isoformat(), "rows": total, "hourly": True}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Backfill sender_token_ticks_light")
    p.add_argument("--days", type=int, default=120, help="Days of history to backfill")
    p.add_argument("--apply-ddl", action="store_true")
    p.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--daily", action="store_true", help="Use single INSERT per day (may OOM)")
    args = p.parse_args()

    AUDIT.mkdir(parents=True, exist_ok=True)
    if args.apply_ddl:
        log.info("Applying DDL from %s", DDL_PATH)
        apply_ddl()

    today = datetime.now(timezone.utc).date()
    results: list[dict] = []
    for i in range(args.days):
        d = today - timedelta(days=i)
        if args.skip_existing:
            ex = ch_query(
                f"SELECT count() AS n FROM eatlq.sender_token_ticks_light "
                f"WHERE chain_id={CHAIN_ID} AND day=toDate('{d.isoformat()}') FORMAT JSONEachRow",
                timeout=30,
            )
            if ex and int(ex[0]["n"]) > 1000:
                log.info("skip %s rows=%s", d, ex[0]["n"])
                results.append({"day": d.isoformat(), "rows": int(ex[0]["n"]), "skipped": True})
                continue
        try:
            row = backfill_day(d, hourly=not args.daily)
            row["skipped"] = False
            results.append(row)
            log.info("backfilled %s rows=%s", d, row["rows"])
        except Exception as e:
            log.error("failed %s: %s", d, e)
            results.append({"day": d.isoformat(), "error": str(e)[:200]})

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_requested": args.days,
        "hourly_mode": not args.daily,
        "results": results,
    }
    LOG_JSON.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
