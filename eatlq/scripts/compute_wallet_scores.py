"""
Wallet Scoring Pipeline — queries ClickHouse sender views (016-027) to classify
wallets into Smart Money / Sniper / Diamond Hand / Whale tiers, writes to Redis
sorted sets. Runs periodically (default every 6 hours).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.request import Request, urlopen
from urllib.parse import urlencode

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("wallet_scorer")


def load_mev_blacklist(path: Path, chain_id: int) -> set[str]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return set()
        payload = json.loads(raw)
    except Exception:
        return set()
    chain_payload = payload.get(str(chain_id)) or payload.get(chain_id) or {}
    out: set[str] = set()
    if not isinstance(chain_payload, dict):
        return out
    for values in chain_payload.values():
        if not isinstance(values, list):
            continue
        for addr in values:
            if isinstance(addr, str):
                addr = addr.lower().strip()
                if addr.startswith("0x") and len(addr) == 42:
                    out.add(addr)
    return out


def ch_query(url: str, db: str, user: str, password: str, sql: str) -> List[Dict[str, Any]]:
    """Execute a ClickHouse query and return rows as list of dicts."""
    params = urlencode({
        "database": db,
        "user": user,
        "password": password,
        "default_format": "JSONEachRow",
        # Allow disk spill for large GROUP BY / ORDER BY (avoids OOM on 7GB server)
        "max_bytes_before_external_group_by": "5368709120",   # 5 GB
        "max_bytes_before_external_sort": "5368709120",       # 5 GB
        "max_memory_usage": "7000000000",                     # 7 GB hard cap
        # Deep view chains can exceed the default 10000 optimisation limit
        "query_plan_max_optimizations_to_apply": "0",         # 0 = unlimited
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


def load_wallet_universe(
    ch_url: str,
    ch_db: str,
    ch_user: str,
    ch_password: str,
    chain_id: int,
) -> List[Dict[str, Any]]:
    """Load the merged wallet scoring universe from ClickHouse."""
    win_rate_source = os.environ.get("WALLET_SCORE_WIN_RATE_SOURCE", "holding").strip().lower()
    if win_rate_source == "holding":
        sql_summary = f"""
            SELECT
                sender_address,
                n_clean_buys AS tokens_traded,
                n_clean_buys AS round_trips,
                wins_2h AS wins,
                holding_win_rate_2h AS win_rate,
                avg_hold_seconds,
                active_span_days
            FROM eatlq.sender_pnl_holding_summary_v1
            WHERE chain_id = {chain_id}
              AND n_clean_buys >= 5
            ORDER BY holding_win_rate_2h DESC, n_clean_buys DESC
            LIMIT 5000
        """
    else:
        sql_summary = f"""
            SELECT
                sender_address,
                tokens_traded,
                round_trips,
                wins,
                win_rate,
                avg_hold_seconds,
                active_span_days
            FROM eatlq.sender_pnl_summary_v1
            WHERE chain_id = {chain_id}
              AND round_trips >= 3
            ORDER BY win_rate DESC, round_trips DESC
            LIMIT 5000
        """

    try:
        pnl_rows = ch_query(ch_url, ch_db, ch_user, ch_password, sql_summary)
    except Exception as e:
        log.warning("wallet summary query failed (%s source): %s", win_rate_source, e)
        if win_rate_source == "holding":
            try:
                pnl_rows = ch_query(ch_url, ch_db, ch_user, ch_password, f"""
                    SELECT
                        sender_address,
                        tokens_traded,
                        round_trips,
                        wins,
                        win_rate,
                        avg_hold_seconds,
                        active_span_days
                    FROM eatlq.sender_pnl_summary_v1
                    WHERE chain_id = {chain_id}
                      AND round_trips >= 3
                    ORDER BY win_rate DESC, round_trips DESC
                    LIMIT 5000
                """)
                log.warning("fallback to sender_pnl_summary_v1 succeeded")
            except Exception:
                pnl_rows = []
        else:
            pnl_rows = []

    try:
        feature_rows = ch_query(ch_url, ch_db, ch_user, ch_password, f"""
            SELECT
                sender_address,
                trade_count,
                distinct_venues,
                distinct_bought_assets,
                avg_trades_per_active_day,
                cross_protocol_flag,
                multi_venue_flag
            FROM eatlq.sender_feature_store_v1
            WHERE chain_id = {chain_id}
              AND trade_count >= 5
            ORDER BY trade_count DESC
            LIMIT 5000
        """)
    except Exception as e:
        log.warning("sender_feature_store_v1 query failed: %s", e)
        feature_rows = []

    feature_map = {r["sender_address"]: r for r in feature_rows}
    wallets: List[Dict[str, Any]] = []
    for row in pnl_rows:
        addr = row["sender_address"]
        feat = feature_map.get(addr, {})
        round_trips = int(row.get("round_trips", 0))
        wallets.append(
            {
                "address": addr,
                "tokens_traded": int(row.get("tokens_traded", 0)),
                "round_trips": round_trips,
                "wins": int(row.get("wins", 0)),
                "win_rate": float(row.get("win_rate", 0.0)),
                "avg_hold_seconds": float(row.get("avg_hold_seconds", 0.0) or 0.0),
                "active_span_days": float(row.get("active_span_days", 0.0) or 0.0),
                "trade_count": int(feat.get("trade_count", round_trips)),
                "distinct_venues": int(feat.get("distinct_venues", 0)),
                "distinct_bought_assets": int(feat.get("distinct_bought_assets", 0)),
                "avg_trades_per_active_day": float(feat.get("avg_trades_per_active_day", 0.0) or 0.0),
                "cross_protocol_flag": int(feat.get("cross_protocol_flag", 0) or 0),
                "multi_venue_flag": int(feat.get("multi_venue_flag", 0) or 0),
            }
        )
    log.info("wallet universe loaded: %d rows", len(wallets))
    return wallets


def compute_scores(ch_url: str, ch_db: str, ch_user: str, ch_password: str,
                   chain_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """Query ClickHouse and classify wallets into tiers."""
    results: Dict[str, List[Dict[str, Any]]] = {
        "smart_money": [],
        "sniper": [],
        "diamond_hand": [],
        "whale": [],
    }

    wallet_rows = load_wallet_universe(ch_url, ch_db, ch_user, ch_password, chain_id)
    for row in wallet_rows:
        addr = row["address"]
        win_rate = float(row["win_rate"])
        round_trips = int(row["round_trips"])
        avg_hold = float(row["avg_hold_seconds"])
        trade_count = int(row["trade_count"])
        distinct_assets = int(row["distinct_bought_assets"])
        distinct_venues = int(row["distinct_venues"])

        score = win_rate * 0.6 + min(round_trips / 50.0, 1.0) * 0.4

        # Require diversity: must trade >=3 distinct assets across >=2 venues.
        # This filters out single-pool bots and market makers.
        if win_rate >= 0.55 and round_trips >= 5 and distinct_assets >= 3 and distinct_venues >= 2:
            results["smart_money"].append({"address": addr, "score": round(score, 4)})

        if avg_hold < 300 and win_rate >= 0.5 and round_trips >= 3:
            sniper_score = (1.0 - min(avg_hold / 300.0, 1.0)) * win_rate
            results["sniper"].append({"address": addr, "score": round(sniper_score, 4)})

        if avg_hold > 86400 * 7:
            diamond_score = min(avg_hold / (86400 * 30), 1.0) * 0.5 + win_rate * 0.5
            results["diamond_hand"].append({"address": addr, "score": round(diamond_score, 4)})

        if trade_count >= 50:
            whale_score = min(trade_count / 500.0, 1.0)
            results["whale"].append({"address": addr, "score": round(whale_score, 4)})

    return results


def rank_smart_money_candidates(
    wallet_rows: List[Dict[str, Any]],
    profile: str,
    target_count: int,
    mev_blacklist: set[str],
) -> List[Dict[str, Any]]:
    """Build medium/broad candidate pools without touching production smart_money."""
    if profile == "medium":
        min_win_rate = 0.48
        min_round_trips = 4
        min_trade_count = 8
        min_assets = 2
        min_venues = 1
    elif profile == "broad":
        min_win_rate = 0.42
        min_round_trips = 3
        min_trade_count = 6
        min_assets = 1
        min_venues = 1
    else:
        raise ValueError(f"unsupported profile: {profile}")

    ranked: List[Dict[str, Any]] = []
    fallback_ranked: List[Dict[str, Any]] = []
    for row in wallet_rows:
        addr = str(row["address"]).lower()
        if addr in mev_blacklist:
            continue
        win_rate = float(row["win_rate"])
        round_trips = int(row["round_trips"])
        trade_count = int(row["trade_count"])
        distinct_assets = int(row["distinct_bought_assets"])
        distinct_venues = int(row["distinct_venues"])
        avg_hold = float(row["avg_hold_seconds"])
        active_span = float(row["active_span_days"])

        score = (
            win_rate * 0.45
            + min(round_trips / 50.0, 1.0) * 0.20
            + min(trade_count / 100.0, 1.0) * 0.10
            + min(distinct_assets / 8.0, 1.0) * 0.10
            + min(distinct_venues / 4.0, 1.0) * 0.05
            + min(active_span / 30.0, 1.0) * 0.05
            + (0.05 if avg_hold >= 300 else 0.0)
        )
        candidate = {
            "address": addr,
            "score": round(score, 6),
            "win_rate": round(win_rate, 6),
            "round_trips": round_trips,
            "trade_count": trade_count,
            "distinct_bought_assets": distinct_assets,
            "distinct_venues": distinct_venues,
        }
        fallback_ranked.append(candidate)

        if win_rate < min_win_rate:
            continue
        if round_trips < min_round_trips or trade_count < min_trade_count:
            continue
        if distinct_assets < min_assets or distinct_venues < min_venues:
            continue
        ranked.append(candidate)

    def _sort_key(r: Dict[str, Any]) -> tuple:
        return (
            -float(r["score"]),
            -float(r["win_rate"]),
            -int(r["round_trips"]),
            -int(r["trade_count"]),
            str(r["address"]),
        )

    ranked.sort(key=_sort_key)
    fallback_ranked.sort(key=_sort_key)
    if len(ranked) < target_count:
        seen = {str(r["address"]).lower() for r in ranked}
        for row in fallback_ranked:
            addr = str(row["address"]).lower()
            if addr in seen:
                continue
            ranked.append(row)
            seen.add(addr)
            if len(ranked) >= target_count:
                break

    ranked.sort(
        key=lambda r: (
            -float(r["score"]),
            -float(r["win_rate"]),
            -int(r["round_trips"]),
            -int(r["trade_count"]),
            str(r["address"]),
        )
    )
    return ranked[:target_count]


def write_to_redis(
    r: redis.Redis, scores: Dict[str, List[Dict[str, Any]]], mev_blacklist: set[str]
) -> Dict[str, int]:
    """Merge wallet scores into Redis sorted sets (never deletes existing Dune-seeded data)."""
    counts = {}
    for tier, wallets in scores.items():
        key = f"wallet:{tier}"
        if mev_blacklist:
            r.zrem(key, *list(mev_blacklist))
        existing = r.zcard(key)
        clean_wallets = [
            row for row in wallets if row["address"].lower() not in mev_blacklist
        ]
        if clean_wallets:
            mapping = {w["address"]: w["score"] for w in clean_wallets}
            r.zadd(key, mapping)
            r.expire(key, 86400 * 14)
            new_total = r.zcard(key)
            log.info("%s: merged %d from ClickHouse (was %d, now %d)",
                     key, len(clean_wallets), existing, new_total)
            counts[tier] = new_total
        else:
            if existing > 0:
                log.info("keeping %d existing entries in %s (ClickHouse returned empty)", existing, key)
            counts[tier] = existing
    return counts


def write_exact_key(
    r: redis.Redis,
    key: str,
    wallets: List[Dict[str, Any]],
    mev_blacklist: set[str],
) -> int:
    """Replace a candidate key exactly; safe for offline-only candidate pools."""
    clean_wallets = [row for row in wallets if row["address"].lower() not in mev_blacklist]
    pipe = r.pipeline()
    pipe.delete(key)
    if clean_wallets:
        mapping = {w["address"]: float(w["score"]) for w in clean_wallets}
        pipe.zadd(key, mapping)
        pipe.expire(key, 86400 * 14)
    pipe.execute()
    return int(r.zcard(key) or 0)


def snapshot_existing_key(
    r: redis.Redis,
    source_key: str,
    target_key: str,
    mev_blacklist: set[str],
) -> int:
    items = r.zrange(source_key, 0, -1, withscores=True)
    wallets = [
        {
            "address": (addr if isinstance(addr, str) else addr.decode()),
            "score": float(score),
        }
        for addr, score in items
        if (addr if isinstance(addr, str) else addr.decode()).lower() not in mev_blacklist
    ]
    return write_exact_key(r, target_key, wallets, mev_blacklist)


def dump_candidate_file(path: Path, wallets: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": int(time.time()),
        "count": len(wallets),
        "wallets": wallets,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def notify_telegram(counts: Dict[str, int]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    text = (
        "📊 <b>钱包评分更新</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"Smart Money: {counts.get('smart_money', 0)} 个\n"
        f"Sniper: {counts.get('sniper', 0)} 个\n"
        f"Diamond Hand: {counts.get('diamond_hand', 0)} 个\n"
        f"Whale: {counts.get('whale', 0)} 个\n"
        "━━━━━━━━━━━━━━━━"
    )
    try:
        import urllib.request
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


def run_once() -> None:
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    blacklist_path = Path(os.environ.get("MEV_BLACKLIST_PATH", "infra/config/mev_blacklist.yaml"))

    r = redis.from_url(redis_url, decode_responses=True)
    mev_blacklist = load_mev_blacklist(blacklist_path, chain_id)
    log.info("loaded mev/router blacklist entries=%d", len(mev_blacklist))

    if os.environ.get("WALLET_SCORE_CANDIDATE_MODE", "0") == "1":
        wallet_rows = load_wallet_universe(ch_url, ch_db, ch_user, ch_password, chain_id)
        medium_target = int(os.environ.get("SMART_MONEY_MEDIUM_TARGET", "200"))
        broad_target = int(os.environ.get("SMART_MONEY_BROAD_TARGET", "500"))
        strict_count = snapshot_existing_key(
            r, "wallet:smart_money", "wallet:smart_money_strict", mev_blacklist
        )
        medium = rank_smart_money_candidates(wallet_rows, "medium", medium_target, mev_blacklist)
        broad = rank_smart_money_candidates(wallet_rows, "broad", broad_target, mev_blacklist)
        medium_count = write_exact_key(
            r, "wallet:smart_money_medium_candidate", medium, mev_blacklist
        )
        broad_count = write_exact_key(
            r, "wallet:smart_money_broad_candidate", broad, mev_blacklist
        )
        dump_candidate_file(Path("logs/smart_money_strict_snapshot.json"), [
            {"address": addr, "score": float(score)}
            for addr, score in r.zrange("wallet:smart_money_strict", 0, -1, withscores=True)
        ])
        dump_candidate_file(Path("logs/smart_money_medium_candidate.json"), medium)
        dump_candidate_file(Path("logs/smart_money_broad_candidate.json"), broad)
        log.info(
            "candidate pools updated: %s",
            json.dumps(
                {
                    "wallet:smart_money_strict": strict_count,
                    "wallet:smart_money_medium_candidate": medium_count,
                    "wallet:smart_money_broad_candidate": broad_count,
                }
            ),
        )
        return

    log.info("computing wallet scores from ClickHouse...")
    scores = compute_scores(ch_url, ch_db, ch_user, ch_password, chain_id)
    counts = write_to_redis(r, scores, mev_blacklist)
    log.info("wallet scores updated: %s", json.dumps(counts))
    notify_telegram(counts)


def main() -> None:
    interval = int(os.environ.get("SCORING_INTERVAL_SEC", "21600"))
    if os.environ.get("WALLET_SCORE_RUN_ONCE", "0") == "1":
        run_once()
        return
    log.info("wallet scorer started, interval=%ds", interval)

    while True:
        try:
            run_once()
        except Exception as e:
            log.error("scoring cycle failed: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
