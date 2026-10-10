"""
Seed Smart Money wallet tiers from Dune Analytics API (free tier).

1. Executes a refined Base-chain wallet profitability query on Dune
2. Polls until results are ready
3. Classifies wallets into Smart Money / Sniper / Diamond Hand / Whale
4. Writes to Redis sorted sets

Usage:
    # Fetch latest cached results (query must have been run on dune.com first):
    DUNE_API_KEY=xxx DUNE_QUERY_ID=123456 python seed_smart_money_dune.py

    # Re-execute the query via API then fetch results:
    DUNE_API_KEY=xxx DUNE_QUERY_ID=123456 DUNE_RERUN=1 python seed_smart_money_dune.py

    Optional env vars:
        REDIS_URL          (default: redis://localhost:6379/0)
        TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  (optional notifications)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dune_seed")

DUNE_BASE = "https://api.dune.com/api/v1"

QUERY_SQL = """
WITH weth_trades AS (
    SELECT
        tx_from AS wallet,
        CASE
            WHEN token_sold_address = 0x4200000000000000000000000000000000000006
            THEN token_bought_address
            ELSE token_sold_address
        END AS token,
        CASE
            WHEN token_sold_address = 0x4200000000000000000000000000000000000006
            THEN 'buy' ELSE 'sell'
        END AS direction,
        amount_usd,
        block_time
    FROM dex.trades
    WHERE blockchain = 'base'
      AND block_time >= NOW() - INTERVAL '{{days}}' day
      AND amount_usd > 10
      AND (token_sold_address = 0x4200000000000000000000000000000000000006
           OR token_bought_address = 0x4200000000000000000000000000000000000006)
),
token_pnl AS (
    SELECT
        wallet, token,
        SUM(CASE WHEN direction = 'buy' THEN amount_usd ELSE 0 END) AS bought_usd,
        SUM(CASE WHEN direction = 'sell' THEN amount_usd ELSE 0 END) AS sold_usd,
        COUNT(*) AS trades,
        MIN(block_time) AS first_trade,
        MAX(block_time) AS last_trade
    FROM weth_trades
    GROUP BY wallet, token
),
round_trips AS (
    SELECT
        wallet, token, bought_usd, sold_usd, trades,
        CASE WHEN bought_usd > 10 AND sold_usd > 10 THEN 1 ELSE 0 END AS has_rt,
        CASE WHEN sold_usd > bought_usd * 1.05 THEN 1 ELSE 0 END AS is_win,
        date_diff('second', first_trade, last_trade) AS hold_sec
    FROM token_pnl
    WHERE date_diff('second', first_trade, last_trade) > 300
),
wallet_agg AS (
    SELECT
        wallet,
        COUNT(DISTINCT token) AS tokens_traded,
        SUM(has_rt) AS round_trips,
        SUM(CASE WHEN has_rt = 1 THEN is_win ELSE 0 END) AS wins,
        SUM(trades) AS total_trades,
        AVG(CASE WHEN has_rt = 1 THEN hold_sec END) AS avg_hold_seconds,
        SUM(bought_usd) AS total_bought_usd,
        SUM(sold_usd) AS total_sold_usd
    FROM round_trips
    GROUP BY wallet
    HAVING SUM(has_rt) >= 5
)
SELECT
    CAST(wallet AS VARCHAR) AS wallet_address,
    tokens_traded, round_trips, wins,
    CAST(wins AS DOUBLE) / NULLIF(round_trips, 0) AS win_rate,
    total_trades,
    COALESCE(avg_hold_seconds, 0) AS avg_hold_seconds,
    total_bought_usd, total_sold_usd,
    total_sold_usd - total_bought_usd AS net_pnl_usd
FROM wallet_agg
WHERE CAST(wins AS DOUBLE) / NULLIF(round_trips, 0) BETWEEN 0.45 AND 0.95
ORDER BY
    (CAST(wins AS DOUBLE) / NULLIF(round_trips, 0))
    * LN(CAST(round_trips AS DOUBLE) + 1) DESC
LIMIT 1000
"""


def dune_request(path: str, api_key: str, method: str = "GET",
                 body: Optional[dict] = None) -> dict:
    url = f"{DUNE_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("X-Dune-API-Key", api_key)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        log.error("Dune API %s %s → %s: %s", method, path, e.code, err_body)
        raise


def execute_saved_query(api_key: str, query_id: str) -> str:
    """Execute a saved Dune query by ID, return execution_id."""
    log.info("executing saved query %s ...", query_id)
    resp = dune_request(f"/query/{query_id}/execute", api_key, "POST")
    execution_id = resp.get("execution_id")
    if not execution_id:
        log.error("no execution_id in response: %s", resp)
        sys.exit(1)
    log.info("execution_id = %s", execution_id)
    return execution_id


def fetch_latest_results(api_key: str, query_id: str) -> List[dict]:
    """Fetch the latest cached results of a saved Dune query (no re-execution)."""
    log.info("fetching latest results for query %s ...", query_id)
    resp = dune_request(f"/query/{query_id}/results?limit=1000", api_key)
    rows = resp.get("result", {}).get("rows", [])
    log.info("received %d wallet rows from Dune", len(rows))
    return rows


def poll_execution(api_key: str, execution_id: str, timeout: int = 600) -> List[dict]:
    """Poll until execution completes, then return rows."""
    deadline = time.time() + timeout
    wait = 5
    while time.time() < deadline:
        resp = dune_request(f"/execution/{execution_id}/status", api_key)
        state = resp.get("state", "")
        log.info("execution state: %s", state)
        if state == "QUERY_STATE_COMPLETED":
            break
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            log.error("query failed: %s", resp)
            sys.exit(1)
        time.sleep(min(wait, 30))
        wait = min(wait * 1.5, 30)

    result = dune_request(f"/execution/{execution_id}/results?limit=1000", api_key)
    rows = result.get("result", {}).get("rows", [])
    log.info("received %d wallet rows from Dune", len(rows))
    return rows


def classify_wallets(rows: List[dict]) -> Dict[str, List[Dict[str, Any]]]:
    tiers: Dict[str, List[Dict[str, Any]]] = {
        "smart_money": [], "sniper": [], "diamond_hand": [], "whale": [],
    }
    for row in rows:
        addr = str(row.get("wallet_address", "")).strip().lower()
        if not addr.startswith("0x"):
            continue

        win_rate = float(row.get("win_rate") or 0)
        round_trips = int(float(row.get("round_trips") or 0))
        total_trades = int(float(row.get("total_trades") or 0))
        avg_hold = float(row.get("avg_hold_seconds") or 0)
        net_pnl = float(row.get("net_pnl_usd") or 0)

        score = win_rate * 0.6 + min(round_trips / 50.0, 1.0) * 0.4

        if win_rate >= 0.55 and round_trips >= 5 and net_pnl > 0:
            tiers["smart_money"].append({"address": addr, "score": round(score, 4)})

        if avg_hold < 600 and win_rate >= 0.5 and round_trips >= 3:
            sniper_score = (1.0 - min(avg_hold / 600.0, 1.0)) * win_rate
            tiers["sniper"].append({"address": addr, "score": round(sniper_score, 4)})

        if avg_hold > 86400 * 7:
            diamond_score = min(avg_hold / (86400 * 30), 1.0) * 0.5 + win_rate * 0.5
            tiers["diamond_hand"].append({"address": addr, "score": round(diamond_score, 4)})

        if total_trades >= 50:
            whale_score = min(total_trades / 500.0, 1.0)
            tiers["whale"].append({"address": addr, "score": round(whale_score, 4)})

    return tiers


def write_to_redis(r: redis.Redis, tiers: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    counts = {}
    for tier, wallets in tiers.items():
        key = f"wallet:{tier}"
        if wallets:
            pipe = r.pipeline()
            pipe.delete(key)
            pipe.zadd(key, {w["address"]: w["score"] for w in wallets})
            pipe.expire(key, 86400 * 14)
            pipe.execute()
        else:
            r.delete(key)
        counts[tier] = len(wallets)
    return counts


def notify_telegram(counts: Dict[str, int], total: int) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    text = (
        "\U0001f4ca <b>Dune Smart Money 种子导入</b>\n"
        "\u2501" * 16 + "\n"
        f"分析钱包数: {total}\n"
        f"Smart Money: {counts.get('smart_money', 0)}\n"
        f"Sniper: {counts.get('sniper', 0)}\n"
        f"Diamond Hand: {counts.get('diamond_hand', 0)}\n"
        f"Whale: {counts.get('whale', 0)}\n"
        "\u2501" * 16
    )
    try:
        body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body, headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=8):
            pass
    except Exception as e:
        log.warning("telegram: %s", e)


def main() -> None:
    api_key = os.environ.get("DUNE_API_KEY", "").strip()
    if not api_key:
        print("Error: set DUNE_API_KEY environment variable")
        print("Get free key at: https://dune.com/settings/api")
        sys.exit(1)

    query_id = os.environ.get("DUNE_QUERY_ID", "").strip()
    if not query_id:
        print("Error: set DUNE_QUERY_ID (the number in https://dune.com/queries/XXXXXX)")
        sys.exit(1)

    rerun = os.environ.get("DUNE_RERUN", "").strip().lower() in ("1", "true", "yes")
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    if rerun:
        execution_id = execute_saved_query(api_key, query_id)
        rows = poll_execution(api_key, execution_id)
    else:
        rows = fetch_latest_results(api_key, query_id)

    if not rows:
        log.error("no results returned, check Dune query")
        sys.exit(1)

    tiers = classify_wallets(rows)
    log.info("classified: %s", {k: len(v) for k, v in tiers.items()})

    r = redis.from_url(redis_url, decode_responses=True)
    counts = write_to_redis(r, tiers)
    log.info("Redis updated: %s", json.dumps(counts))
    notify_telegram(counts, len(rows))

    log.info("done — Smart Money list seeded from Dune query %s", query_id)


if __name__ == "__main__":
    main()
