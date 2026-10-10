#!/usr/bin/env python3
"""
Sprint082 signal bridge — tail paper shadow CSV → Redis signals stream (dry-run path).

Default: writes to Redis `signals` stream; executor runs with EXECUTOR_DRY_RUN=1.
Does NOT bypass tiny_live_runner.py — both paths coexist.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    import redis
except ImportError:
    redis = None  # type: ignore

from tiny_live_filters import check_paper_safety  # noqa: E402

log = logging.getLogger("signal_bridge")

DEFAULT_CSVS = [
    REPO / "logs" / "sprint055_paper_shadow.csv",
    REPO / "logs" / "sprint087_expanded_paper_shadow.csv",
]


def _parse_ts(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def row_to_signal(row: dict, *, min_confidence: float = 0.52) -> Optional[dict]:
    pool = (row.get("pool") or "").strip().lower()
    if not pool.startswith("0x") or len(pool) != 42:
        return None
    conf_raw = row.get("confidence") or row.get("final_score") or "0.55"
    try:
        conf = float(conf_raw)
    except (TypeError, ValueError):
        conf = 0.55
    if conf < min_confidence:
        return None
    return {
        "action": "BUY",
        "pool_address": pool,
        "token_address": pool,
        "chain_id": int(os.environ.get("CHAIN_ID", "8453")),
        "confidence": conf,
        "signal_source": "smart_money",
        "signal_time": row.get("signal_time", ""),
        "fee_bps": row.get("fee_bps", ""),
        "position_size_usd": float(os.environ.get("BRIDGE_POSITION_USD", "10")),
        "bridge": True,
        "timestamp": int(time.time()),
    }


def tail_and_bridge(
    csv_paths: list[Path],
    *,
    redis_url: str,
    stream: str = "signals",
    poll_sec: float = 5.0,
    dry_run: bool = False,
    once: bool = False,
    tail_lines: int = 0,
) -> int:
    if redis is None:
        raise RuntimeError("redis package required")
    r = redis.from_url(redis_url, decode_responses=True)
    seen: set[str] = set()
    sent = 0

    def process_file(path: Path) -> None:
        nonlocal sent
        if not path.exists():
            return
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if tail_lines > 0:
            rows = rows[-tail_lines:]
        for row in rows:
            key = f"{row.get('signal_time','')}|{row.get('pool','')}"
            if key in seen:
                continue
            safe, reason = check_paper_safety(row)
            if not safe:
                log.debug("filtered %s: %s", key, reason)
                continue
            sig = row_to_signal(row)
            if sig is None:
                continue
            seen.add(key)
            if dry_run:
                log.info("[dry-run] would XADD %s", json.dumps(sig)[:120])
            else:
                r.xadd(stream, {"payload": json.dumps(sig, ensure_ascii=True)})
                log.info("XADD signal pool=%s conf=%.3f", sig["pool_address"], sig["confidence"])
            sent += 1

    if once:
        for p in csv_paths:
            process_file(p)
        return sent

    log.info("bridge tailing %d CSVs → %s (dry_run=%s)", len(csv_paths), stream, dry_run)
    while True:
        for p in csv_paths:
            process_file(p)
        time.sleep(poll_sec)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Paper shadow → Redis signals bridge")
    p.add_argument("--csv", action="append", default=[])
    p.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    p.add_argument("--stream", default=os.environ.get("SIGNALS_STREAM", "signals"))
    p.add_argument("--dry-run", action="store_true", help="Log only, no XADD")
    p.add_argument("--once", action="store_true")
    p.add_argument("--tail", type=int, default=50, help="Process last N rows per CSV when --once")
    args = p.parse_args()
    paths = [Path(x) for x in args.csv] if args.csv else DEFAULT_CSVS
    if args.once:
        n = tail_and_bridge(
            paths,
            redis_url=args.redis_url,
            stream=args.stream,
            dry_run=args.dry_run,
            once=True,
            tail_lines=args.tail,
        )
        print(json.dumps({"signals_sent": n, "dry_run": args.dry_run}))
        return
    tail_and_bridge(paths, redis_url=args.redis_url, stream=args.stream, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
