#!/usr/bin/env python3
"""Reject-outcome tracker: fill in post-rejection price/liveness snapshots.

Read-only against ClickHouse's swap archive (already collected by the live
ingest/archiver pipeline — this script makes zero RPC calls). Writes only to
the isolated `app.reject_outcome_tracking` Postgres table.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import ch_query, pg_escape, pg_exec, pg_query_rows, tick_ret_pct  # noqa: E402

HORIZONS = [(1, "1h"), (6, "6h"), (24, "24h"), (72, "72h")]
FINALIZE_HOURS = 72
STILL_TRADING_WINDOW_HOURS = 2


def parse_ts(s: str) -> datetime | None:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("Z") is False and "Z" in s else s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # last resort: fromisoformat
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def tick_at_or_before(pool: str, ts: datetime):
    ts_s = ts.strftime("%Y-%m-%d %H:%M:%S")
    q = (
        "SELECT tick FROM eatlq.uniswap_v3_swaps_decoded "
        f"WHERE pool_address = lower('{pool}') AND block_time <= toDateTime64('{ts_s}', 3) "
        "ORDER BY block_time DESC LIMIT 1"
    )
    out, err = ch_query(q)
    if err or not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def peak_trough(pool: str, ts_from: datetime, ts_to: datetime):
    q = (
        "SELECT max(tick), min(tick) FROM eatlq.uniswap_v3_swaps_decoded "
        f"WHERE pool_address = lower('{pool}') "
        f"AND block_time >= toDateTime64('{ts_from.strftime('%Y-%m-%d %H:%M:%S')}', 3) "
        f"AND block_time <= toDateTime64('{ts_to.strftime('%Y-%m-%d %H:%M:%S')}', 3)"
    )
    out, err = ch_query(q)
    if err or not out:
        return None, None
    parts = out.split("\t")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None, None


def last_swap(pool: str):
    q = f"SELECT max(block_time) FROM eatlq.uniswap_v3_swaps_decoded WHERE pool_address = lower('{pool}')"
    out, err = ch_query(q)
    if err or not out:
        return None
    return parse_ts(out)


def main() -> int:
    rows, err = pg_query_rows(
        "SELECT id, pool_address, reject_ts, entry_tick, token0_is_meme "
        "FROM app.reject_outcome_tracking WHERE status != 'done'"
    )
    if err:
        print(f"query failed: {err}", file=sys.stderr)
        return 1
    if not rows:
        print("nothing to snapshot")
        return 0

    now = datetime.now(timezone.utc)
    updated = 0
    for row in rows:
        if len(row) < 5:
            continue
        rid, pool, reject_ts_s, entry_tick_s, token0_is_meme_s = row[:5]
        reject_dt = parse_ts(reject_ts_s)
        if reject_dt is None:
            continue
        entry_tick = int(entry_tick_s) if entry_tick_s not in ("", None) else None
        token0_is_meme = token0_is_meme_s == "t"
        age_hours = (now - reject_dt).total_seconds() / 3600.0

        sets = []
        for hrs, label in HORIZONS:
            if age_hours + 0.05 < hrs:
                continue
            target = reject_dt + timedelta(hours=hrs)
            tick = tick_at_or_before(pool, target)
            ret = tick_ret_pct(token0_is_meme, entry_tick, tick)
            sets.append(f"tick_{label} = {pg_escape(tick)}")
            sets.append(f"ret_{label}_pct = {pg_escape(ret)}")

        window_end = min(now, reject_dt + timedelta(hours=FINALIZE_HOURS))
        max_tick, min_tick = peak_trough(pool, reject_dt, window_end)
        if token0_is_meme:
            best_tick, worst_tick = max_tick, min_tick
        else:
            best_tick, worst_tick = min_tick, max_tick
        sets.append(f"peak_ret_pct = {pg_escape(tick_ret_pct(token0_is_meme, entry_tick, best_tick))}")
        sets.append(f"trough_ret_pct = {pg_escape(tick_ret_pct(token0_is_meme, entry_tick, worst_tick))}")

        ls = last_swap(pool)
        if ls is not None:
            sets.append(f"last_swap_at = {pg_escape(ls.strftime('%Y-%m-%d %H:%M:%S'))}")
            still_trading = (now - ls) < timedelta(hours=STILL_TRADING_WINDOW_HOURS)
            sets.append(f"still_trading_24h = {pg_escape(still_trading)}")

        status = "done" if age_hours >= FINALIZE_HOURS else "tracking"
        sets.append(f"status = {pg_escape(status)}")
        sets.append("updated_at = now()")

        ok, perr = pg_exec(f"UPDATE app.reject_outcome_tracking SET {', '.join(sets)} WHERE id = {rid};")
        if not ok:
            print(f"update failed for id={rid}: {perr}", file=sys.stderr)
            continue
        updated += 1

    print(f"updated {updated} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
