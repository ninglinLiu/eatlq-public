#!/usr/bin/env python3
"""Reject-outcome tracker: capture pretrade-rejection events from executor logs.

Read-only against the live trading path: reads `docker logs eatlq-executor-1`
(no RPC) and the `pool:discovered:*` Redis hash (no RPC). Writes only to the
isolated `app.reject_outcome_tracking` Postgres table. Does not touch executor
code, config, or the trading decision path in any way.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    EXECUTOR_CONTAINER,
    REDIS_CONTAINER,
    WETH_BASE,
    ch_query,
    pg_escape,
    pg_exec,
    redis_hgetall,
    run,
    run_merged,
)

STATE_FILE = Path("/opt/EATLQ/mvp/state/reject_outcome_tracker_checkpoint.json")
LOG_LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+\S+\s+executor BUY blocked by pretrade gate "
    r"pool=(?P<pool>0x[0-9a-fA-F]{40})\s+reason=(?P<reason>\S+)"
)
DEFAULT_LOOKBACK = "2h"


def classify(reason: str) -> str:
    r = reason.lower()
    if "closed_source" in r:
        return "closed_source_high"
    if r.startswith("sim_unavailable"):
        return "sim_unavailable"
    if r.startswith("external_risk"):
        return "external_risk_other"
    if r.startswith("cluster_"):
        return "cluster_blacklist"
    if r.startswith("friction"):
        return "friction"
    if "depth" in r or "shallow_pool" in r:
        return "depth"
    if r.startswith("honeypot"):
        return "honeypot_other"
    return "other"


def load_checkpoint() -> str | None:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("last_ts")
        except Exception:
            return None
    return None


def save_checkpoint(ts: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_ts": ts}))


def to_ch_ts(iso_ts: str) -> str:
    """Executor logs use RFC3339 (e.g. 2026-07-04T21:03:29Z); ClickHouse's
    toDateTime64() wants 'YYYY-MM-DD HH:MM:SS'."""
    dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fetch_entry_tick(pool: str, ts: str):
    ch_ts = to_ch_ts(ts)
    q = (
        "SELECT tick FROM eatlq.uniswap_v3_swaps_decoded "
        f"WHERE pool_address = lower('{pool}') AND block_time <= toDateTime64('{ch_ts}', 3) "
        "ORDER BY block_time DESC LIMIT 1"
    )
    out, err = ch_query(q)
    if err or not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def fetch_bridge_signal(pool: str) -> dict:
    """Best-effort enrichment: scan recent `signals` stream for this pool's p_fatal."""
    out, _, rc = run(["docker", "exec", REDIS_CONTAINER, "redis-cli", "XREVRANGE", "signals", "+", "-", "COUNT", "1000"])
    if rc != 0:
        return {}
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("{") and pool in line.lower():
            try:
                return json.loads(line)
            except Exception:
                continue
    return {}


def main() -> int:
    since = load_checkpoint()
    cmd = ["docker", "logs", EXECUTOR_CONTAINER, "--since", since or DEFAULT_LOOKBACK]
    out, rc = run_merged(cmd, timeout=60)
    if rc != 0:
        print("docker logs failed", file=sys.stderr)
        return 1

    new_rows = []
    latest_ts = since
    for line in out.split("\n"):
        m = LOG_LINE_RE.search(line)
        if not m:
            continue
        ts, pool, reason = m.group("ts"), m.group("pool").lower(), m.group("reason")
        latest_ts = ts
        meta = redis_hgetall(f"pool:discovered:{pool}")
        token0_addr = meta.get("token0_address", "")
        token1_addr = meta.get("token1_address", "")
        token0_is_meme = None
        token_symbol = None
        token_address = None
        if token0_addr and token1_addr:
            token0_is_meme = token1_addr.lower() == WETH_BASE.lower()
            token_symbol = meta.get("token0_symbol") if token0_is_meme else meta.get("token1_symbol")
            token_address = token0_addr if token0_is_meme else token1_addr
        entry_tick = fetch_entry_tick(pool, ts)
        sig = fetch_bridge_signal(pool)
        new_rows.append(
            dict(
                pool_address=pool,
                token_address=token_address,
                token_symbol=token_symbol,
                reject_reason=reason,
                reject_category=classify(reason),
                reject_ts=ts,
                entry_tick=entry_tick,
                token0_is_meme=token0_is_meme,
                p_fatal=sig.get("p_fatal"),
                confidence=sig.get("confidence"),
            )
        )

    if not new_rows:
        print("no new rejections")
        if latest_ts:
            save_checkpoint(latest_ts)
        return 0

    cols = (
        "pool_address, token_address, token_symbol, reject_reason, reject_category, "
        "reject_ts, entry_tick, token0_is_meme, p_fatal, confidence"
    )
    values_sql = ",\n".join(
        "("
        + ", ".join(
            [
                pg_escape(r["pool_address"]),
                pg_escape(r["token_address"]),
                pg_escape(r["token_symbol"]),
                pg_escape(r["reject_reason"]),
                pg_escape(r["reject_category"]),
                pg_escape(r["reject_ts"]),
                pg_escape(r["entry_tick"]),
                pg_escape(r["token0_is_meme"]),
                pg_escape(r["p_fatal"]),
                pg_escape(r["confidence"]),
            ]
        )
        + ")"
        for r in new_rows
    )
    sql = (
        f"INSERT INTO app.reject_outcome_tracking ({cols}) VALUES\n{values_sql}\n"
        "ON CONFLICT (pool_address, reject_ts) DO NOTHING;\n"
    )
    ok, err = pg_exec(sql)
    if not ok:
        print(f"insert failed: {err}", file=sys.stderr)
        return 1

    print(f"ingested {len(new_rows)} rejection(s)")
    if latest_ts:
        save_checkpoint(latest_ts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
