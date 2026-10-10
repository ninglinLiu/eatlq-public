#!/usr/bin/env python3
"""Fusion shadow — Mode A formula (research only, no signals stream)."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))
sys.path.insert(0, str(REPO / "services" / "strategy" / "src"))

from ch_helper import CHAIN_ID, ch_query, write_csv  # noqa: E402

try:
    import redis
except ImportError:
    redis = None  # type: ignore

log = logging.getLogger("fusion_shadow")

OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "fusion_shadow"
LIVE_CSV = OUT_DIR / "live_scores.csv"
LABELS_PATH = REPO / "mvp" / "audits" / "wallet_profile_v0" / "labels" / "all_labels.csv"
DASHCAM_PATH = Path(os.environ.get("DASHCAM_PATH", str(REPO / "logs" / "dashcam.csv")))


def load_wallet_tags() -> dict[str, dict[str, bool]]:
    tags: dict[str, dict[str, bool]] = {}
    if not LABELS_PATH.exists():
        return tags
    with LABELS_PATH.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            addr = row["sender_address"].lower()
            tags[addr] = {
                "smart_money": row.get("is_smart_money", "").lower() in ("true", "1"),
                "sniper": row.get("is_sniper", "").lower() in ("true", "1"),
                "diamond_hand": row.get("is_diamond_hand", "").lower() in ("true", "1"),
                "paper_hand": row.get("is_paper_hand", "").lower() in ("true", "1"),
                "whale": row.get("is_whale", "").lower() in ("true", "1"),
            }
    return tags


def w_score(sender: str, tags: dict[str, dict[str, bool]]) -> float:
    """Wallet tag score ∈ [0,1] per whitepaper fusion weights."""
    t = tags.get(sender.lower(), {})
    if not any(t.values()):
        return 0.0
    score = 0.0
    if t.get("whale"):
        score += 0.35
    if t.get("smart_money"):
        score += 0.30
    if t.get("sniper"):
        score += 0.20
    if t.get("diamond_hand"):
        score += 0.10
    if t.get("paper_hand"):
        score -= 0.15
    return max(0.0, min(1.0, score))


def compute_final_score(pool: str, sender: str, features: dict[str, float], tags: dict) -> dict[str, Any]:
    whale_score = w_score(sender, tags)
    momentum = float(features.get("price_velocity_5m", 0.0))
    activity = float(features.get("activity_heat", 0)) / 100.0
    risk = max(float(features.get("token_volatility_5m", 0.0)) - 1.0, 0.0)
    final = 0.40 * whale_score + 0.30 * momentum + 0.20 * activity - 0.10 * risk
    return {
        "pool_address": pool.lower(),
        "sender_address": sender.lower(),
        "final_score": round(final, 6),
        "whale_score": round(whale_score, 6),
        "momentum": round(momentum, 6),
        "activity": round(activity, 6),
        "risk": round(risk, 6),
        "token_volatility_5m": float(features.get("token_volatility_5m", 0.0)),
        "unique_buyer_entropy_5m": float(features.get("unique_buyer_entropy_5m", 0.0)),
    }


def fetch_recent_swaps(minutes: int = 10, limit: int = 500) -> list[dict]:
    sql = f"""
    SELECT
        lower(pool_address) AS pool_address,
        lower(buyer_address) AS sender_address,
        block_time,
        tick
    FROM eatlq.uniswap_v3_swaps_with_buyer
    WHERE chain_id = {CHAIN_ID}
      AND block_time >= now() - INTERVAL {minutes} MINUTE
    ORDER BY block_time DESC
    LIMIT {limit}
    """
    return ch_query(sql)


def get_redis_features(r: "redis.Redis", pool: str) -> dict[str, float]:
    """Read live feature store keys when available."""
    pool = pool.lower()
    out: dict[str, float] = {}
    keys = {
        "price_velocity_5m": f"feat:{pool}:price_velocity_5m",
        "buy_pressure_ratio": f"feat:{pool}:buy_pressure_ratio",
        "token_volatility_5m": f"feat:{pool}:token_volatility_5m",
        "activity_heat": f"feat:{pool}:activity_heat",
        "unique_buyer_entropy_5m": f"feat:{pool}:unique_buyer_entropy_5m",
    }
    for name, key in keys.items():
        val = r.get(key)
        if val is not None:
            try:
                out[name] = float(val)
            except (TypeError, ValueError):
                pass
    if not out:
        # Fallback: compute from FeatureStore if keys missing
        try:
            from feature_store import FeatureStore  # noqa: WPS433

            fs = FeatureStore(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
            out = fs.compute(pool)
        except Exception:
            out = {
                "price_velocity_5m": 0.0,
                "buy_pressure_ratio": 0.5,
                "token_volatility_5m": 0.0,
                "activity_heat": 0.0,
                "unique_buyer_entropy_5m": 0.0,
            }
    return out


def run_shadow(*, minutes: int = 10, append: bool = True, limit: int = 500) -> list[dict]:
    tags = load_wallet_tags()
    swaps = fetch_recent_swaps(minutes=minutes, limit=limit)
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.from_url(redis_url, decode_responses=True) if redis else None

    rows: list[dict] = []
    seen: set[str] = set()
    for sw in swaps:
        pool = sw["pool_address"]
        sender = sw["sender_address"]
        bt = str(sw.get("block_time", ""))
        key = f"{pool}|{sender}|{bt}"
        if key in seen:
            continue
        seen.add(key)
        features = get_redis_features(r, pool) if r else {
            "price_velocity_5m": 0.0,
            "activity_heat": 1.0,
            "token_volatility_5m": 0.5,
            "unique_buyer_entropy_5m": 0.0,
        }
        row = compute_final_score(pool, sender, features, tags)
        row["computed_at"] = datetime.now(timezone.utc).isoformat()
        row["block_time"] = str(sw.get("block_time", ""))
        rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "scores.csv"
    fieldnames = [
        "computed_at", "block_time", "pool_address", "sender_address",
        "final_score", "whale_score", "momentum", "activity", "risk",
        "token_volatility_5m", "unique_buyer_entropy_5m",
    ]
    if append and csv_path.exists():
        existing = list(csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
        rows = existing + rows
    write_csv(csv_path, rows, fieldnames)
    return rows


def evaluate_vs_paper(scores_path: Path | None = None, paper_path: Path | None = None) -> str:
    """Compare high final_score entries vs sprint055 paper 30m returns."""
    scores_path = scores_path or OUT_DIR / "scores.csv"
    paper_path = paper_path or REPO / "logs" / "sprint055_paper_shadow.csv"
    if not scores_path.exists():
        return "# Fusion shadow eval\n\nNo scores.csv yet.\n"

    scores = list(csv.DictReader(scores_path.open(newline="", encoding="utf-8")))
    paper: dict[str, float] = {}
    if paper_path.exists():
        with paper_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pool = (row.get("pool") or "").lower()
                ret_key = "net_return_30m_ag"
                if ret_key in row and row[ret_key]:
                    try:
                        paper[pool] = float(row[ret_key])
                    except ValueError:
                        pass

    scored = [s for s in scores if s.get("final_score")]
    scored.sort(key=lambda x: float(x["final_score"]), reverse=True)
    top = scored[: max(1, len(scored) // 5)]
    bottom = scored[-max(1, len(scored) // 5):] if len(scored) >= 5 else []

    def avg_return(subset: list[dict]) -> Optional[float]:
        vals = [paper.get(s["pool_address"].lower()) for s in subset if s["pool_address"].lower() in paper]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    top_avg = avg_return(top)
    bot_avg = avg_return(bottom)
    lines = [
        "# Fusion Shadow Evaluation",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}\n",
        f"Total scores: {len(scores)}",
        f"Paper overlap pools: {len(paper)}",
        "",
        "## Top quintile vs bottom quintile (30m gross return from sprint055 paper)",
        "",
        f"| cohort | n | avg net_return_30m_ag |",
        f"|--------|--:|----------------------:|",
        f"| top 20% by final_score | {len(top)} | {top_avg if top_avg is not None else 'N/A'} |",
        f"| bottom 20% | {len(bottom)} | {bot_avg if bot_avg is not None else 'N/A'} |",
        "",
        "## Interpretation",
        "",
        "Research-only shadow path. Positive spread suggests fusion ranking aligns with paper economics.",
        "Does NOT lift FAILED_NO_7D_ALPHA guard.",
    ]
    md = "\n".join(lines) + "\n"
    (OUT_DIR / "fusion_shadow_eval.md").write_text(md)
    return md


def load_gated_wallets(r: "redis.Redis | None") -> set[str]:
    out: set[str] = set()
    if r is not None:
        for key in ("wallet:smart_money", "wallet:whale"):
            try:
                for m in r.smembers(key):
                    addr = m.decode() if isinstance(m, bytes) else str(m)
                    out.add(addr.lower())
            except Exception:
                pass
    if not out:
        tags = load_wallet_tags()
        for addr, t in tags.items():
            if t.get("smart_money") or t.get("whale"):
                out.add(addr.lower())
    return out


def _normalize_block_time(bt: str) -> str:
    """Normalize CH/dashcam block_time to 'YYYY-MM-DD HH:MM:SS' for SQL."""
    bt = str(bt).strip().replace("T", " ").replace("Z", "")
    if "." in bt:
        bt = bt.split(".")[0]
    return bt


def tail_dashcam_gated_rows(
    gated: set[str],
    *,
    since_hours: int = 24,
    max_rows: int = 2000,
    require_gated: bool = False,
) -> list[dict]:
    """Read recent dashcam rows with sender_address (CH may lag hours)."""
    if not DASHCAM_PATH.exists():
        return []
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=since_hours)).timestamp() * 1000)
    import io
    import subprocess

    try:
        header_line = subprocess.check_output(["head", "-n", "1", str(DASHCAM_PATH)], text=True).strip()
        raw = subprocess.check_output(["tail", "-n", "8000", str(DASHCAM_PATH)], text=True)
    except Exception:
        return []

    lines = raw.strip().splitlines()
    if lines and lines[0].startswith("timestamp"):
        lines = lines[1:]
    if not lines:
        return []

    reader = csv.DictReader(io.StringIO(header_line + "\n" + "\n".join(lines)))
    if not reader.fieldnames or "sender_address" not in reader.fieldnames:
        log.warning("dashcam missing sender_address column")
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for row in reversed(list(reader)):
        sender = (row.get("sender_address") or "").lower().strip()
        if not sender.startswith("0x"):
            continue
        if require_gated and gated and sender not in gated:
            continue
        try:
            ts_ms = int(row["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts_ms < cutoff_ms:
            continue
        pool = (row.get("pool_address") or row.get("token_address") or "").lower().strip()
        if not pool.startswith("0x"):
            continue
        bt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        key = f"{pool}|{sender}|{bt}"
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "pool_address": pool,
            "sender_address": sender,
            "block_time": bt,
            "timestamp_ms": ts_ms,
            "source": "dashcam",
            "wallet_gated": sender in gated if gated else False,
        })
        if len(rows) >= max_rows:
            break
    return rows


def fetch_gated_swaps(minutes: int = 60, limit: int = 1000, gated: set[str] | None = None) -> list[dict]:
    # Prefer dashcam — CH ingest may lag 12–24h on this host
    since_h = max(1, minutes // 60 + 1)
    dash = tail_dashcam_gated_rows(gated or set(), since_hours=since_h, max_rows=limit, require_gated=False)
    if dash:
        tagged = sum(1 for d in dash if d.get("wallet_gated"))
        log.info("dashcam rows=%d wallet_gated=%d (CH fallback skipped)", len(dash), tagged)
        return dash
    try:
        swaps = fetch_recent_swaps(minutes=minutes, limit=limit * 3)
        if not gated:
            return swaps[:limit]
        return [s for s in swaps if s.get("sender_address", "").lower() in gated][:limit]
    except Exception as e:
        log.warning("CH swap fetch failed (dashcam empty): %s", e)
        return []

def evaluate_live_30m(scores_path: Path | None = None) -> str:
    scores_path = scores_path or LIVE_CSV
    if not scores_path.exists():
        return "# Live fusion eval\n\nNo live_scores.csv yet.\n"
    scores = list(csv.DictReader(scores_path.open(newline="", encoding="utf-8")))
    ch_max = None
    try:
        mx = ch_query(
            f"SELECT max(block_time) AS t FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id={CHAIN_ID} FORMAT JSONEachRow",
            timeout=30,
        )
        if mx and mx[0].get("t"):
            ch_max = _normalize_block_time(mx[0]["t"])
    except Exception:
        pass

    enriched: list[dict] = []
    pending_ch = 0
    for s in scores[-200:]:
        pool = s["pool_address"].lower()
        bt = _normalize_block_time(s.get("block_time", ""))
        if not bt:
            continue
        if ch_max and bt > ch_max:
            pending_ch += 1
            continue
        q = f"""
        WITH entry AS (
            SELECT tick AS tick0, block_time AS t0
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE chain_id = {CHAIN_ID} AND lower(pool_address) = '{pool}'
              AND block_time >= toDateTime('{bt}', 'UTC') - INTERVAL 2 MINUTE
              AND block_time <= toDateTime('{bt}', 'UTC') + INTERVAL 2 MINUTE
            ORDER BY abs(toUnixTimestamp(block_time) - toUnixTimestamp(toDateTime('{bt}', 'UTC')))
            LIMIT 1
        ),
        fwd AS (
            SELECT tick AS tick1
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE chain_id = {CHAIN_ID} AND lower(pool_address) = '{pool}'
              AND block_time >= (SELECT t0 FROM entry) + INTERVAL 30 MINUTE
              AND block_time <= (SELECT t0 FROM entry) + INTERVAL 35 MINUTE
            ORDER BY block_time ASC
            LIMIT 1
        )
        SELECT entry.tick0, fwd.tick1 FROM entry, fwd
        FORMAT JSONEachRow
        """
        try:
            rows = ch_query(q, timeout=30)
            if rows and rows[0].get("tick0") and rows[0].get("tick1"):
                t0, t1 = float(rows[0]["tick0"]), float(rows[0]["tick1"])
                enriched.append({**s, "fwd_30m_tick_ret": (t1 - t0) / max(abs(t0), 1)})
        except Exception:
            pass

    top = sorted(enriched, key=lambda x: float(x.get("final_score", 0)), reverse=True)
    top_n = top[: max(1, len(top) // 5)]
    bot_n = top[-max(1, len(top) // 5):] if len(top) >= 5 else []

    def avg(sub: list[dict]) -> Optional[float]:
        v = [float(x.get("fwd_30m_tick_ret", 0)) for x in sub]
        return sum(v) / len(v) if v else None

    md = "\n".join([
        "# Live Fusion Shadow (wallet-gated) — 30m Forward Eval",
        f"\nGenerated: {datetime.now(timezone.utc).isoformat()}\n",
        f"Scored rows: {len(scores)} | With 30m forward: {len(enriched)} | Pending CH ingest: {pending_ch}",
        f"CH max block_time: {ch_max or 'unknown'}",
        "",
        "| cohort | n | avg fwd_30m_tick_ret |",
        "|--------|--:|---------------------:|",
        f"| top 20% final_score | {len(top_n)} | {avg(top_n)} |",
        f"| bottom 20% | {len(bot_n)} | {avg(bot_n)} |",
        "",
        "Research-only. Does NOT lift FAILED_NO_7D_ALPHA guard.",
        "",
    ])
    (OUT_DIR / "live_fusion_eval.md").write_text(md)
    return md


def run_live_gated_shadow(*, minutes: int = 60, limit: int = 500, append: bool = True) -> list[dict]:
    redis_url = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")
    r = None
    gated: set[str] = set()
    if redis:
        try:
            r = redis.from_url(redis_url, decode_responses=True)
            gated = load_gated_wallets(r)
        except Exception as e:
            log.warning("redis unavailable, using label CSV fallback: %s", e)
            r = None
            gated = load_gated_wallets(None)  # type: ignore[arg-type]
    tags = load_wallet_tags()
    swaps = fetch_gated_swaps(minutes=minutes, limit=limit, gated=gated)
    log.info("live gated swaps=%d gated_wallets=%d", len(swaps), len(gated))

    rows: list[dict] = []
    seen: set[str] = set()
    for sw in swaps:
        pool = sw["pool_address"]
        sender = sw["sender_address"]
        is_gated = bool(sw.get("wallet_gated")) or (gated and sender.lower() in gated)
        bt = str(sw.get("block_time", ""))
        key = f"{pool}|{sender}|{bt}"
        if key in seen:
            continue
        seen.add(key)
        features = get_redis_features(r, pool) if r else {}
        row = compute_final_score(pool, sender, features, tags)
        row["computed_at"] = datetime.now(timezone.utc).isoformat()
        row["block_time"] = bt
        row["gated"] = is_gated
        row["source"] = sw.get("source", "ch")
        rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "computed_at", "block_time", "pool_address", "sender_address", "gated", "source",
        "final_score", "whale_score", "momentum", "activity", "risk",
        "token_volatility_5m", "unique_buyer_entropy_5m",
    ]
    if append and LIVE_CSV.exists():
        existing = list(csv.DictReader(LIVE_CSV.open(newline="", encoding="utf-8")))
        rows = existing + rows
    write_csv(LIVE_CSV, rows, fieldnames)
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="Fusion shadow Mode A")
    p.add_argument("--minutes", type=int, default=10)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--eval-only", action="store_true")
    p.add_argument("--no-append", action="store_true")
    p.add_argument("--live", action="store_true", help="Wallet-gated live shadow → live_scores.csv")
    p.add_argument("--eval-live", action="store_true", help="Evaluate live_scores 30m forward")
    p.add_argument("--since-hours", type=int, default=24, help="Dashcam lookback for --live")
    args = p.parse_args()
    if args.eval_live:
        print(evaluate_live_30m())
        return
    if args.live:
        rows = run_live_gated_shadow(
            minutes=args.since_hours * 60,
            append=not args.no_append,
            limit=args.limit,
        )
        log.info("wrote %d live gated rows", len(rows))
        print(evaluate_live_30m())
        return
    if args.eval_only:
        print(evaluate_vs_paper())
        return
    rows = run_shadow(minutes=args.minutes, append=not args.no_append, limit=args.limit)
    log.info("wrote %d fusion shadow rows", len(rows))
    print(evaluate_vs_paper())


if __name__ == "__main__":
    main()
