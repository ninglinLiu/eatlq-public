#!/usr/bin/env python3
"""Append a Sprint083 dry-run combat watch snapshot.

Read-only with respect to trading systems: reads local CSV/JSON, Redis via the
redis Python client, and selected process env safety flags. Writes only the
watch markdown artifact.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "mvp/audits/sprint083_fee500_paper_shadow/tonight_combat_watch.md"
N_ACCOUNTING = REPO / "mvp/audits/execution_readiness/n_accounting.json"
SOP_AUDIT = REPO / "mvp/audits/sprint083_fee500_paper_shadow/sop_reality_audit.json"
TRACK_A_CSV = REPO / "logs/sprint055_paper_shadow.csv"
TRACK_B_CSV = REPO / "logs/sprint083_fee500_paper_shadow.csv"
REDIS_URL = os.environ.get("REDIS_URL", "redis://172.18.0.10:6379/0")
SIGNAL_STALE_SEC = int(os.environ.get("SIGNAL_STALE_SEC", "7200"))


def _json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _csv_stats(path: Path) -> dict:
    if not path.exists():
        return {
            "exists": False,
            "rows": 0,
            "eligible_rows": 0,
            "eligible_pools": 0,
            "last_signal_time": "",
        }
    try:
        rows = list(csv.DictReader(path.open()))
    except Exception as exc:
        return {"exists": True, "error": str(exc), "rows": 0, "eligible_rows": 0, "eligible_pools": 0, "last_signal_time": ""}
    eligible = [r for r in rows if r.get("entry_quote_ok") == "True" and r.get("is_0x6ff5") == "True"]
    return {
        "exists": True,
        "rows": len(rows),
        "eligible_rows": len(eligible),
        "eligible_pools": len({r.get("pool") for r in eligible if r.get("pool")}),
        "last_signal_time": max([r.get("signal_time", "") for r in rows if r.get("signal_time")], default=""),
    }


def _redis_stats() -> dict:
    out = {
        "signals_stream_len": None,
        "executor_lag": None,
        "pending_acks": None,
        "dead_letters": None,
        "stuck_positions": None,
        "new_signals": None,
        "error": None,
    }
    try:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        previous = int((OUT.with_suffix(".state.json").exists() and _json(OUT.with_suffix(".state.json")).get("signals_stream_len")) or 0)
        current = int(r.xlen("signals") or 0)
        out["signals_stream_len"] = current
        out["new_signals"] = max(0, current - previous) if previous else 0
        out["dead_letters"] = int(r.xlen("signals_dead_letter") or 0)
        out["stuck_positions"] = 0
        try:
            for g in r.xinfo_groups("signals"):
                if g.get("name") == "execution_group":
                    out["executor_lag"] = int(g.get("lag", 0) or 0)
                    out["pending_acks"] = int(g.get("pending", 0) or 0)
        except Exception:
            pass
        now = int(time.time())
        try:
            for pool in r.smembers("positions:open_pools"):
                pos_data = r.hgetall(f"positions:{pool}")
                entry_ms = int(pos_data.get("entry_time_ms", 0) or 0)
                if entry_ms > 0 and (now * 1000 - entry_ms) > 6 * 3600 * 1000:
                    out["stuck_positions"] += 1
        except Exception:
            pass
        OUT.with_suffix(".state.json").write_text(json.dumps({"signals_stream_len": current}, indent=2))
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _safe_executor_env() -> dict:
    env = {"LIVE_TRADING_MODE": os.environ.get("LIVE_TRADING_MODE", "false"), "EXECUTOR_DRY_RUN": None}
    try:
        ps = subprocess.check_output(["pgrep", "-f", "services/executor/src/main.py"], text=True).split()
        for pid in ps[:1]:
            raw = Path("/proc") / pid / "environ"
            if not raw.exists():
                continue
            parts = raw.read_bytes().split(b"\0")
            for part in parts:
                if part.startswith(b"EXECUTOR_DRY_RUN="):
                    env["EXECUTOR_DRY_RUN"] = part.decode().split("=", 1)[1]
                elif part.startswith(b"LIVE_TRADING_MODE="):
                    env["LIVE_TRADING_MODE"] = part.decode().split("=", 1)[1]
    except Exception:
        pass
    return env


def main() -> None:
    now = datetime.now(timezone.utc)
    n = _json(N_ACCOUNTING)
    sop = _json(SOP_AUDIT)
    a = _csv_stats(TRACK_A_CSV)
    b = _csv_stats(TRACK_B_CSV)
    r = _redis_stats()
    env = _safe_executor_env()

    last_signal = a.get("last_signal_time") or b.get("last_signal_time") or ""
    signal_stale = True
    if last_signal:
        try:
            dt = datetime.fromisoformat(last_signal.replace("Z", "+00:00"))
            signal_stale = (now - dt).total_seconds() > SIGNAL_STALE_SEC
        except Exception:
            signal_stale = True

    toxic = ((sop.get("rolling_toxic_7d") or {}).get("overall") or {}).get("toxic_rate")
    live_n = int(n.get("true_live_forward_executor_n", 0) or 0)
    decision = "CANARY_REVIEW_READY" if live_n >= 25 and not signal_stale else "CANARY_BLOCKED"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if not OUT.exists():
        OUT.write_text(
            "# Tonight Combat Watch\n\n"
            "- Mode: Base V3 highest-intensity paper combat watch.\n"
            "- LIVE_TRADING_MODE must remain false.\n"
            "- EXECUTOR_DRY_RUN must remain 1.\n"
            "- NORMAL_LIVE_ALLOWED=false.\n"
            "- Historical/backfill N never counts as canary N.\n\n"
            "| time_utc | decision | true_live_forward_executor_n | new_signals | signal_stale | executor_lag | dead_letters | stuck_positions | toxic_rate_7d | track_a_eligible_pools | track_b_eligible_pools | track_a_rows | track_b_rows | live_mode | dry_run |\n"
            "|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|\n"
        )

    with OUT.open("a") as f:
        f.write(
            f"| {now.isoformat()} | {decision} | {live_n} | {r.get('new_signals')} | {signal_stale} | "
            f"{r.get('executor_lag')} | {r.get('dead_letters')} | {r.get('stuck_positions')} | "
            f"{toxic if toxic is not None else ''} | {a.get('eligible_pools')} | {b.get('eligible_pools')} | "
            f"{a.get('rows')} | {b.get('rows')} | {env.get('LIVE_TRADING_MODE')} | {env.get('EXECUTOR_DRY_RUN')} |\n"
        )
    print(json.dumps({
        "ok": True,
        "artifact": str(OUT),
        "decision": decision,
        "true_live_forward_executor_n": live_n,
        "new_signals": r.get("new_signals"),
        "signal_stale": signal_stale,
        "executor_lag": r.get("executor_lag"),
        "dead_letters": r.get("dead_letters"),
        "stuck_positions": r.get("stuck_positions"),
        "toxic_rate_7d": toxic,
        "track_a_eligible_pools": a.get("eligible_pools"),
        "track_b_eligible_pools": b.get("eligible_pools"),
        "LIVE_TRADING_MODE": env.get("LIVE_TRADING_MODE"),
        "EXECUTOR_DRY_RUN": env.get("EXECUTOR_DRY_RUN"),
        "NORMAL_LIVE_ALLOWED": False,
    }, indent=2))


if __name__ == "__main__":
    main()
