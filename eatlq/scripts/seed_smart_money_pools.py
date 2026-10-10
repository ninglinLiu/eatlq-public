"""
Seed Smart Money Pools into Redis pool:discovered:*

背景：
swap_poller 只监控工厂事件 PairCreated/PoolCreated 新发现的池子（pool:discovered:*
且 has_weth_leg=1）。Smart money 大量在 Aerodrome / Uniswap 主流池 (WETH/USDC,
AERO/WETH, AERO/USDC, AERO/cbBTC ...) 交易，这些老池子没被发现流程登记，swap_poller
收不到 → market_ticks 没数据 → strategy 零信号。

修复：查询 ClickHouse 获取 wallet:smart_money 名单最近 N 天交易最多的 top-K venue，
把这些 venue 以 pool:discovered:{addr} hash 形式补录到 Redis（伪造 created_at_sec=now
让其通过 SMART_MONEY_POOL_MAX_AGE_H 年龄过滤；统一打 has_weth_leg=1 通过 poller 过滤）。

幂等；可重复执行。swap_poller 会在下一次 poll 时自动把这些池子纳入 eth_getLogs 批次。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("seed_smart_money_pools")


def ch_query(url: str, db: str, user: str, password: str, sql: str) -> List[Dict[str, Any]]:
    params = urlencode({
        "database": db,
        "user": user,
        "password": password,
        "default_format": "JSONEachRow",
        "max_bytes_before_external_group_by": "3000000000",
        "max_memory_usage": "5000000000",
    })
    full_url = f"{url}/?{params}"
    req = Request(full_url, data=sql.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8").strip()
    if not body:
        return []
    rows = []
    for line in body.split("\n"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


VENUE_LABEL_TO_POOL_TYPE = (
    ("Aerodrome CL", "aero_cl"),
    ("Aerodrome vAMM", "aero_vamm"),
)


def infer_pool_type(venue_label: str) -> str:
    """venue_label 形如 'WETH/USDC 0.30%' 或 'AERO/WETH [Aerodrome CL]'。"""
    for needle, pool_type in VENUE_LABEL_TO_POOL_TYPE:
        if needle in venue_label:
            return pool_type
    # Aerodrome 未命中 → Uniswap；带 fee 的默认 v3，其余按 v2
    if "%" in venue_label:
        return "v3"
    return "v2"


def _parse_blacklist(raw: str) -> set:
    out = set()
    for item in (raw or "").split(","):
        item = item.strip().lower()
        if item:
            out.add(item)
    return out


def run() -> int:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    ch_url = (
        os.environ.get("CLICKHOUSE_URL")
        or os.environ.get("CLICKHOUSE_HTTP_URL")
        or "http://clickhouse:8123"
    )
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))

    lookback_days = int(os.environ.get("SEED_SM_POOLS_LOOKBACK_DAYS", "3"))
    top_k = int(os.environ.get("SEED_SM_POOLS_TOP_K", "20"))
    min_trades = int(os.environ.get("SEED_SM_POOLS_MIN_TRADES", "20"))
    # strategy 的黑名单池子 seed 了也会被跳过（浪费 swap-poller RPC + market_ticks 带宽）
    blacklist = _parse_blacklist(os.environ.get("STRATEGY_POOL_BLACKLIST", ""))

    r = redis.from_url(redis_url, decode_responses=True)
    wallets = r.zrange("wallet:smart_money", 0, -1)
    if not wallets:
        log.error("wallet:smart_money is empty — nothing to seed")
        return 1

    log.info("smart_money wallets=%d lookback_days=%d top_k=%d", len(wallets), lookback_days, top_k)

    wallets_sql = ",".join(f"'{w.lower()}'" for w in wallets)
    sql = f"""
        SELECT
            venue_address,
            anyHeavy(venue_label) AS venue_label,
            count() AS trades,
            uniq(sender_address) AS wallets
        FROM eatlq.swap_trade_events_all
        WHERE chain_id = {chain_id}
          AND block_time >= now() - INTERVAL {lookback_days} DAY
          AND sender_address IN ({wallets_sql})
        GROUP BY venue_address
        HAVING trades >= {min_trades}
        ORDER BY trades DESC
        LIMIT {top_k}
    """

    rows = ch_query(ch_url, ch_db, ch_user, ch_password, sql)
    if not rows:
        log.warning("no smart money venues found — nothing to seed")
        return 0

    log.info("clickhouse returned %d candidate venues", len(rows))

    now_sec = int(time.time())
    now_ms = now_sec * 1000
    seeded = 0
    updated = 0
    skipped_blacklist = 0
    for row in rows:
        addr = (row.get("venue_address") or "").lower()
        if not addr or not addr.startswith("0x"):
            continue
        if addr in blacklist:
            skipped_blacklist += 1
            log.info("skip blacklisted venue %s (%s)", addr, row.get("venue_label", ""))
            # 把之前 seed 过的黑名单池子从 Redis 清掉，避免残留 swap-poller 还在轮询
            existing = r.hgetall(f"pool:discovered:{addr}")
            if existing.get("seed_source") == "smart_money_seed":
                r.delete(f"pool:discovered:{addr}")
                log.info("deleted stale seed for blacklisted %s", addr)
            continue
        label = row.get("venue_label", "")
        pool_type = infer_pool_type(label)
        key = f"pool:discovered:{addr}"

        existing = r.hgetall(key)
        mapping = {
            "pool_address": addr,
            "pool_type": pool_type,
            "chain_id": str(chain_id),
            "has_weth_leg": "1",
            "created_at_sec": str(now_sec),
            "discovered_at_ms": str(now_ms),
            "seed_source": "smart_money_seed",
            "seed_label": label,
            "seed_trades_3d": str(row.get("trades", 0)),
            "seed_wallets": str(row.get("wallets", 0)),
            "seed_last_refreshed": str(now_sec),
        }
        r.hset(key, mapping=mapping)
        if existing:
            updated += 1
        else:
            seeded += 1
        log.info("seeded %s pool_type=%s trades=%s wallets=%s label=%s",
                 addr, pool_type, row.get("trades"), row.get("wallets"), label)

    log.info("done: new=%d refreshed=%d skipped_blacklist=%d total_candidates=%d",
             seeded, updated, skipped_blacklist, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(run())
