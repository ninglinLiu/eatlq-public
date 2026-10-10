#!/usr/bin/env python3
"""
Sprint083 Accumulation Monitor
================================
Reads Redis + sprint055 CSV + bridge state. Outputs current N accounting,
pipeline health, and ACCUMULATING / STALE_NO_SIGNAL / BROKEN verdict.

Run modes:
  --once    Print metrics once and exit (used by cron every 5 min)
  --watch   Print metrics every MONITOR_INTERVAL_SEC (default 300)
  --json    Output as JSON (for machine consumption)

N definitions (never mixed):
  historical_replay_n          — OOS signals (pre-cutover) replayed into executor
  bridge_backfill_n            — sprint055 historical rows published by bridge
                                  (before today's patch, is_0x6ff5 was empty)
  true_live_forward_executor_n — executor-confirmed dry-run buys from signals
                                  published AFTER patch deploy (cutover_ts)
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("sprint083_monitor")

REPO = Path(__file__).resolve().parent.parent
CSV_055 = REPO / "logs/sprint055_paper_shadow.csv"
BRIDGE_STATE = REPO / "logs/sprint082_bridge_state.json"
N_ACCOUNTING = REPO / "mvp/audits/execution_readiness/n_accounting.json"
HEARTBEAT_BRIDGE = Path("/tmp/sprint082_bridge.heartbeat")
HEARTBEAT_ENRICHER = Path("/tmp/sprint083_enricher.heartbeat")

REDIS_URL = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")
MONITOR_INTERVAL_SEC = int(os.environ.get("MONITOR_INTERVAL_SEC", "300"))

# Cutover: when the 0x6ff5 gate patch was deployed to executor
CUTOVER_TS = int(os.environ.get("ACCUMULATION_CUTOVER_TS", "1779005000"))  # 2026-05-17 ~08:03 UTC

# Stale thresholds
BRIDGE_STALE_SEC = int(os.environ.get("BRIDGE_STALE_SEC", "600"))    # 10 min
ENRICHER_STALE_SEC = int(os.environ.get("ENRICHER_STALE_SEC", "300")) # 5 min
SIGNAL_STALE_SEC = int(os.environ.get("SIGNAL_STALE_SEC", "7200"))    # 2h: sprint055 pool discovery is sparse
LIVE_N_GOAL = int(os.environ.get("LIVE_N_GOAL", "100"))


def _get_redis():
    import redis
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    return r


def _heartbeat_age(path: Path) -> int | None:
    """Return seconds since heartbeat was written, or None if not found."""
    if not path.exists():
        return None
    try:
        ts = int(path.read_text().strip())
        return int(time.time()) - ts
    except Exception:
        return None


def _csv_metrics() -> dict:
    """Sprint055 CSV stats."""
    if not CSV_055.exists():
        return {"error": "csv_not_found"}
    rows = list(csv.DictReader(CSV_055.open()))
    from collections import Counter
    total = len(rows)
    has_sender = sum(1 for r in rows if r.get("is_0x6ff5") in ("True", "False"))
    gated = sum(1 for r in rows if r.get("is_0x6ff5") == "True" and r.get("entry_quote_ok") == "True")
    last_ts = None
    for r in reversed(rows):
        st = r.get("signal_time", "")
        if st:
            last_ts = st
            break
    return {
        "total_rows": total,
        "sender_enriched_rows": has_sender,
        "sender_coverage_pct": round(100 * has_sender / total, 1) if total else 0,
        "is_0x6ff5_true_rows": gated,
        "last_signal_time": last_ts,
    }


def _bridge_metrics() -> dict:
    """Bridge published signal counts."""
    result = {"published_total": 0, "error": None}
    if BRIDGE_STATE.exists():
        try:
            data = json.loads(BRIDGE_STATE.read_text())
            published = data.get("published", [])
            result["published_total"] = len(published)
        except Exception as e:
            result["error"] = str(e)
    return result


def _redis_metrics(r) -> dict:
    """Executor state from Redis."""
    now = int(time.time())
    metrics = {
        "signals_stream_len": 0,
        "execution_group_lag": 0,
        "execution_group_pending": 0,
        "execution_group_seen": False,
        "dead_letter_len": 0,
        "open_positions": 0,
        "stuck_positions": 0,
        "exec_trades_total": 0,
        "exec_buys": 0,
        "exec_sells": 0,
        "historical_replay_n": 0,
        "bridge_backfill_n": 0,
        "true_live_forward_executor_n": 0,
        "last_trade_ts": None,
        "last_trade_time": None,
    }

    try:
        metrics["signals_stream_len"] = r.xlen("signals")
        groups = r.xinfo_groups("signals")
        for g in groups:
            if g.get("name") == "execution_group":
                metrics["execution_group_seen"] = True
                metrics["execution_group_lag"] = g.get("lag", 0) or 0
                metrics["execution_group_pending"] = g.get("pending", 0) or 0
    except Exception:
        pass

    try:
        metrics["dead_letter_len"] = r.xlen("signals_dead_letter") or 0
    except Exception:
        pass

    try:
        open_pools = r.smembers("positions:open_pools")
        metrics["open_positions"] = len(open_pools)
        # Stuck: open positions older than 6h
        stuck = 0
        for pool in open_pools:
            pos_data = r.hgetall(f"positions:{pool}")
            entry_ms = int(pos_data.get("entry_time_ms", 0) or 0)
            if entry_ms > 0 and (now * 1000 - entry_ms) > 6 * 3600 * 1000:
                stuck += 1
        metrics["stuck_positions"] = stuck
    except Exception:
        pass

    try:
        trades_raw = r.lrange("exec:trades", 0, 999)
        buys, sells = 0, 0
        historical_n, backfill_n, live_n, live_n_b = 0, 0, 0, 0
        last_ts = None
        for raw in trades_raw:
            try:
                d = json.loads(raw)
                if not d.get("dry_run"):
                    continue
                ts = d.get("ts", 0)
                src = d.get("signal_source", "")
                action = d.get("action", "")
                if action == "buy" and d.get("success"):
                    buys += 1
                    if ts and (last_ts is None or ts > last_ts):
                        last_ts = ts
                    if "S074" in src and ts >= CUTOVER_TS:
                        if "_B" in src:
                            live_n_b += 1
                        else:
                            live_n += 1
                    elif ts < CUTOVER_TS:
                        historical_n += 1
                    else:
                        backfill_n += 1
                elif action == "sell" and d.get("success"):
                    sells += 1
            except Exception:
                pass
        metrics["exec_buys"] = buys
        metrics["exec_sells"] = sells
        metrics["exec_trades_total"] = buys + sells
        metrics["historical_replay_n"] = historical_n
        metrics["bridge_backfill_n"] = backfill_n
        metrics["true_live_forward_executor_n"] = live_n
        metrics["engineering_executor_n_track_b"] = live_n_b
        if last_ts:
            metrics["last_trade_ts"] = last_ts
            metrics["last_trade_time"] = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
    except Exception:
        pass

    return metrics


def _determine_status(csv_m: dict, redis_m: dict, bridge_m: dict, now: int) -> dict:
    """Return status: ACCUMULATING / STALE_NO_SIGNAL / BROKEN."""
    issues = []
    warnings = []
    signal_stale = False

    # Hard failures -> BROKEN. Sparse signal supply is handled separately.
    if csv_m.get("error"):
        issues.append("sprint055_csv_missing")
    if redis_m.get("error"):
        issues.append("redis_unavailable")
    if bridge_m.get("error"):
        issues.append("bridge_state_unreadable")
    if not redis_m.get("error") and not redis_m.get("execution_group_seen"):
        issues.append("executor_group_missing")

    # Signal freshness
    last_sig = csv_m.get("last_signal_time", "")
    if last_sig:
        try:
            dt = datetime.fromisoformat(last_sig.replace("Z", "+00:00"))
            age = now - int(dt.timestamp())
            if age > SIGNAL_STALE_SEC:
                signal_stale = True
                warnings.append(f"signal_stale_{age}s")
        except Exception:
            pass

    # Bridge heartbeat
    bridge_age = _heartbeat_age(HEARTBEAT_BRIDGE)
    if bridge_age is None:
        warnings.append("bridge_heartbeat_missing")
    elif bridge_age > BRIDGE_STALE_SEC:
        warnings.append(f"bridge_stale_{bridge_age}s")

    # Enricher heartbeat
    enricher_age = _heartbeat_age(HEARTBEAT_ENRICHER)
    if enricher_age is None:
        warnings.append("enricher_heartbeat_missing")
    elif enricher_age > ENRICHER_STALE_SEC:
        warnings.append(f"enricher_stale_{enricher_age}s")

    # Accumulation check
    live_n = redis_m.get("true_live_forward_executor_n", 0)
    lag = redis_m.get("execution_group_lag", 0)
    if lag > 200:
        warnings.append(f"executor_lag_{lag}_signals")

    if issues:
        verdict = "BROKEN"
    elif signal_stale:
        verdict = "STALE_NO_SIGNAL"
    elif lag == 0:
        verdict = "ACCUMULATING"
    else:
        verdict = "STALE_NO_SIGNAL"

    return {
        "verdict": verdict,
        "issues": issues,
        "warnings": warnings,
        "signal_stale": signal_stale,
        "true_live_n": live_n,
        "n_goal": LIVE_N_GOAL,
        "pct_to_goal": round(100 * live_n / LIVE_N_GOAL, 1),
    }


def collect_all() -> dict:
    """Collect all metrics into one dict."""
    now = int(time.time())
    csv_m = _csv_metrics()
    bridge_m = _bridge_metrics()

    redis_m = {"error": "redis_unavailable"}
    try:
        r = _get_redis()
        redis_m = _redis_metrics(r)
    except Exception as e:
        redis_m = {"error": str(e)}

    status = _determine_status(csv_m, redis_m, bridge_m, now)

    return {
        "timestamp": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "cutover_ts": CUTOVER_TS,
        "n_accounting": {
            "historical_replay_n": int(redis_m.get("historical_replay_n", 0) or 0),
            "bridge_backfill_n": int(redis_m.get("bridge_backfill_n", 0) or 0),
            "true_live_forward_executor_n": int(redis_m.get("true_live_forward_executor_n", 0) or 0),
            "engineering_executor_n_track_b": int(redis_m.get("engineering_executor_n_track_b", 0) or 0),
            "n_goal": LIVE_N_GOAL,
            "pct_to_goal": status.get("pct_to_goal", 0),
            "source_status": "redis_ok" if not redis_m.get("error") else "redis_unavailable",
            "source_error": redis_m.get("error"),
        },
        "pipeline": {
            "sprint055_total_rows": csv_m.get("total_rows", 0),
            "sprint055_sender_enriched": csv_m.get("sender_enriched_rows", 0),
            "sprint055_is_0x6ff5_eligible": csv_m.get("is_0x6ff5_true_rows", 0),
            "bridge_published_total": bridge_m.get("published_total", 0),
            "signals_stream_len": redis_m.get("signals_stream_len", 0),
            "execution_group_lag": redis_m.get("execution_group_lag", 0),
            "execution_group_pending": redis_m.get("execution_group_pending", 0),
            "dead_letters": redis_m.get("dead_letter_len", 0),
            "open_positions": redis_m.get("open_positions", 0),
            "stuck_positions": redis_m.get("stuck_positions", 0),
            "last_signal_time": csv_m.get("last_signal_time", ""),
            "last_trade_time": redis_m.get("last_trade_time", ""),
            "bridge_heartbeat_age_sec": _heartbeat_age(HEARTBEAT_BRIDGE),
            "enricher_heartbeat_age_sec": _heartbeat_age(HEARTBEAT_ENRICHER),
        },
        "status": status,
    }


def print_readout(data: dict) -> None:
    n = data["n_accounting"]
    p = data["pipeline"]
    s = data["status"]
    ts = data["timestamp"]

    verdict = s["verdict"]
    color = {"ACCUMULATING": "🟢", "STALE_NO_SIGNAL": "🟡", "BROKEN": "🔴"}.get(verdict, "⚪")

    print(f"\n{'='*60}")
    print(f"  EATLQ Accumulation Monitor — {ts[:19]}Z")
    print(f"  Status: {color} {verdict}")
    if verdict == "ACCUMULATING":
        print("  Meaning: infrastructure is up; fresh signals are being consumed normally.")
    elif verdict == "STALE_NO_SIGNAL":
        print("  Meaning: infrastructure is up; waiting for fresh eligible signals.")
    elif verdict == "BROKEN":
        print("  Meaning: Redis, filesystem, or executor infrastructure needs attention.")
    print(f"{'='*60}")
    print(f"\n── N Accounting ──")
    print(f"  historical_replay_n          : {n['historical_replay_n']}")
    print(f"  bridge_backfill_n            : {n['bridge_backfill_n']}")
    print(f"  true_live_forward_executor_n : {n['true_live_forward_executor_n']}  ← TARGET (Track A fee100)")
    print(f"  engineering_executor_n_track_b: {n.get('engineering_executor_n_track_b', 0)}  ← engineering only")
    print(f"  goal: {n['n_goal']} | progress: {n['pct_to_goal']:.1f}%")
    print(f"\n── Pipeline ──")
    print(f"  sprint055 rows               : {p['sprint055_total_rows']}")
    print(f"  sender enriched              : {p['sprint055_sender_enriched']}")
    print(f"  is_0x6ff5=True eligible      : {p['sprint055_is_0x6ff5_eligible']}")
    print(f"  bridge published             : {p['bridge_published_total']}")
    print(f"  signals stream len           : {p['signals_stream_len']}")
    print(f"  executor lag                 : {p['execution_group_lag']}")
    print(f"  pending acks                 : {p['execution_group_pending']}")
    print(f"  dead letters                 : {p['dead_letters']}")
    print(f"  open positions               : {p['open_positions']}")
    print(f"  stuck (>6h)                  : {p['stuck_positions']}")
    print(f"  last signal                  : {p['last_signal_time'] or 'none'}")
    print(f"  last trade                   : {p['last_trade_time'] or 'none'}")
    bha = p.get("bridge_heartbeat_age_sec")
    eha = p.get("enricher_heartbeat_age_sec")
    print(f"  bridge heartbeat             : {bha}s ago" if bha is not None else "  bridge heartbeat             : NOT FOUND ⚠️")
    print(f"  enricher heartbeat           : {eha}s ago" if eha is not None else "  enricher heartbeat           : NOT FOUND ⚠️")
    if s["issues"]:
        print(f"\n── Issues ──")
        for i in s["issues"]:
            print(f"  🔴 {i}")
    if s["warnings"]:
        print(f"\n── Warnings ──")
        for w in s["warnings"]:
            print(f"  🟡 {w}")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human readout")
    args = parser.parse_args()

    if not args.once and not args.watch:
        parser.print_help()
        sys.exit(0)

    def _run() -> dict:
        data = collect_all()
        # Persist n_accounting
        N_ACCOUNTING.parent.mkdir(parents=True, exist_ok=True)
        N_ACCOUNTING.write_text(json.dumps(data["n_accounting"], indent=2))
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print_readout(data)
        return data

    _run()

    if args.watch:
        while True:
            time.sleep(MONITOR_INTERVAL_SEC)
            try:
                _run()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error("Monitor error: %s", e)


if __name__ == "__main__":
    main()
