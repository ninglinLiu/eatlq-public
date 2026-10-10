"""Backfill eatlq.discovered_pools from Redis pool:discovered:* hashes.

The pool_discovery service persists pools incrementally, but the ClickHouse
table can lag behind Redis (e.g. older keys predating CH sink activation).
swap_poller has been polling swaps for every key in Redis, so many pools with
real swap rows in uniswap_v{2,3}_swaps never made it into discovered_pools,
which in turn causes discovered_swap_trade_events_v1 (INNER JOIN) to drop
them silently.

This script reconciles by dumping Redis pool_discovered_* hashes and
upserting into CH (ReplacingMergeTree collapses dupes by pool_address).

Run once after fixing the swap_poller 0x-prefix bug.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Dict, List
from urllib.parse import urlparse

import clickhouse_connect  # type: ignore
import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("backfill_discovered_pools")

COLUMN_NAMES = [
    "chain_id",
    "pool_address",
    "factory_address",
    "pool_type",
    "token0_address",
    "token0_symbol",
    "token0_decimals",
    "token1_address",
    "token1_symbol",
    "token1_decimals",
    "fee_bps",
    "has_weth_leg",
    "created_block",
    "created_tx_hash",
]


def _decode(d: Dict[bytes, bytes], key: str, default: str = "") -> str:
    return (d.get(key.encode()) or default.encode()).decode()


def _decode_int(d: Dict[bytes, bytes], key: str, default: int = 0) -> int:
    try:
        return int(_decode(d, key, str(default)))
    except (ValueError, TypeError):
        return default


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    p.add_argument("--clickhouse-url", default=os.environ.get("CLICKHOUSE_URL", "http://localhost:8123"))
    p.add_argument("--database", default=os.environ.get("CLICKHOUSE_DB", "eatlq"))
    p.add_argument("--table", default="discovered_pools")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    r = redis.from_url(args.redis_url, decode_responses=False)

    parsed = urlparse(args.clickhouse_url)
    ch_host = parsed.hostname or "clickhouse"
    ch_port = parsed.port or (8443 if parsed.scheme == "https" else 8123)
    client = clickhouse_connect.get_client(
        host=ch_host,
        port=ch_port,
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=args.database,
        interface="http",
        secure=parsed.scheme == "https",
    )
    log.info("clickhouse %s:%d db=%s table=%s", ch_host, ch_port, args.database, args.table)

    scanned = 0
    batch: List[list] = []
    t0 = time.time()

    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=b"pool:discovered:*", count=500)
        for k in keys:
            data = r.hgetall(k)
            if not data:
                continue
            pool_addr = _decode(data, "pool_address")
            if not pool_addr:
                continue
            row = [
                _decode_int(data, "chain_id"),
                pool_addr,
                _decode(data, "factory_address"),
                _decode(data, "pool_type"),
                _decode(data, "token0_address"),
                _decode(data, "token0_symbol"),
                _decode_int(data, "token0_decimals"),
                _decode(data, "token1_address"),
                _decode(data, "token1_symbol"),
                _decode_int(data, "token1_decimals"),
                _decode_int(data, "fee_bps"),
                _decode_int(data, "has_weth_leg"),
                _decode_int(data, "created_block"),
                _decode(data, "created_tx_hash"),
            ]
            batch.append(row)
            scanned += 1

            if len(batch) >= args.batch_size:
                if not args.dry_run:
                    client.insert(table=args.table, data=batch, column_names=COLUMN_NAMES, database=args.database)
                log.info("inserted batch, total=%d elapsed=%.1fs", scanned, time.time() - t0)
                batch = []

        if cursor == 0:
            break

    if batch:
        if not args.dry_run:
            client.insert(table=args.table, data=batch, column_names=COLUMN_NAMES, database=args.database)
        log.info("final batch, total=%d", scanned)

    log.info("done: %d pools upserted in %.1fs (dry_run=%s)", scanned, time.time() - t0, args.dry_run)


if __name__ == "__main__":
    main()
