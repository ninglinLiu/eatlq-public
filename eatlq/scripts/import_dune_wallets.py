"""
Import wallet profitability data from Dune Analytics CSV into Redis sorted sets.

Bypasses ClickHouse entirely — uses pre-computed wallet metrics from Dune
to seed Smart Money / Sniper / Diamond Hand / Whale tiers immediately.

Usage:
    python import_dune_wallets.py <path_to_dune_csv>

CSV columns expected (from the Dune query):
    wallet_address, tokens_traded, round_trips, wins, win_rate,
    total_trades, avg_hold_seconds, total_bought_usd, total_sold_usd, net_pnl_usd
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from typing import Any, Dict, List
from urllib.request import Request, urlopen

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dune_import")


def classify_wallets(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    tiers: Dict[str, List[Dict[str, Any]]] = {
        "smart_money": [],
        "sniper": [],
        "diamond_hand": [],
        "whale": [],
    }

    for row in rows:
        addr = row["wallet_address"].strip().lower()
        if not addr.startswith("0x"):
            continue

        win_rate = float(row.get("win_rate", 0) or 0)
        round_trips = int(float(row.get("round_trips", 0) or 0))
        total_trades = int(float(row.get("total_trades", 0) or 0))
        avg_hold = float(row.get("avg_hold_seconds", 0) or 0)
        net_pnl = float(row.get("net_pnl_usd", 0) or 0)

        score = win_rate * 0.6 + min(round_trips / 50.0, 1.0) * 0.4

        if win_rate >= 0.55 and round_trips >= 5 and net_pnl > 0:
            tiers["smart_money"].append({"address": addr, "score": round(score, 4)})

        if avg_hold < 300 and win_rate >= 0.5 and round_trips >= 3:
            sniper_score = (1.0 - min(avg_hold / 300.0, 1.0)) * win_rate
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
            mapping = {w["address"]: w["score"] for w in wallets}
            pipe.zadd(key, mapping)
            pipe.expire(key, 86400 * 7)
            pipe.execute()
        else:
            r.delete(key)
        counts[tier] = len(wallets)
    return counts


def notify_telegram(counts: Dict[str, int], total_rows: int) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    text = (
        "📊 <b>Dune 历史数据导入完成</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"导入钱包数: {total_rows}\n"
        f"Smart Money: {counts.get('smart_money', 0)} 个\n"
        f"Sniper: {counts.get('sniper', 0)} 个\n"
        f"Diamond Hand: {counts.get('diamond_hand', 0)} 个\n"
        f"Whale: {counts.get('whale', 0)} 个\n"
        "━━━━━━━━━━━━━━━━"
    )
    try:
        body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=8):
            pass
    except Exception as e:
        log.warning("telegram notify failed: %s", e)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python import_dune_wallets.py <dune_export.csv>")
        sys.exit(1)

    csv_path = sys.argv[1]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    log.info("reading %s ...", csv_path)
    rows: List[Dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    log.info("loaded %d wallet rows from CSV", len(rows))

    if not rows:
        log.error("CSV is empty, nothing to import")
        sys.exit(1)

    tiers = classify_wallets(rows)
    log.info("classified: %s", {k: len(v) for k, v in tiers.items()})

    r = redis.from_url(redis_url, decode_responses=True)
    counts = write_to_redis(r, tiers)
    log.info("Redis updated: %s", json.dumps(counts))

    notify_telegram(counts, len(rows))
    log.info("done")


if __name__ == "__main__":
    main()
