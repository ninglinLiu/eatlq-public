"""Read-only CH lookup of the 4 frozen F03 shadow signals after their hour."""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

from ch_readonly import query_tsv
from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
WILDCAT = Path("/root/base_wildcat_alpha")
OUT = ROOT / "out"
LN_TICK = float(np.log(1.0001))
POOLS = [
    "0x82321f3beb69f503380d6b233857d5c43562e2d0",
    "0xbe00ff35af70e8415d0eb605a286d8a45466a4c1",
    "0xdfe5f275020def30993f042174fc2d335678b626",
    "0xe49b3f5614a926b5f468c9ee340343d1bb59c507",
]
SIGNAL_HOUR = "2026-08-14 13:00:00"


def main() -> None:
    research_entry_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    pool_list = ", ".join(f"'{p}'" for p in POOLS)
    freshness = query_tsv(
        "SELECT min(block_time) AS min_t, max(block_time) AS max_t, count() AS n "
        "FROM eatlq.uniswap_v3_swaps WHERE chain_id = 8453 AND removed = 0"
    )
    hours = query_tsv(
        f"""
SELECT
    lower(pool_address) AS pool_address,
    toStartOfHour(block_time) AS block_hour,
    argMin(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8)))), tuple(block_number, log_index)) AS open_tick,
    argMax(reinterpretAsInt32(reverse(unhex(right(tick_raw, 8)))), tuple(block_number, log_index)) AS close_tick,
    uniqExact(tuple(tx_hash, log_index)) AS swap_count,
    uniqExact(sender) AS unique_senders
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0 AND length(tick_raw) = 66
  AND lower(pool_address) IN ({pool_list})
  AND block_time >= toDateTime64('2026-08-14 13:00:00', 3, 'UTC')
  AND block_time < toDateTime64('2026-08-16 14:00:00', 3, 'UTC')
GROUP BY pool_address, block_hour
ORDER BY pool_address, block_hour
"""
    )
    signals = pd.read_csv(WILDCAT / "SHADOW/signals.csv")
    meta = pd.read_csv(WILDCAT / "data/pool_meta.tsv", sep="\t").drop_duplicates("pool_address", keep="last")
    h = pd.read_csv(StringIO(hours), sep="\t")
    h["block_hour"] = pd.to_datetime(h.block_hour, utc=True)
    h["pool_address"] = h.pool_address.str.lower()
    t0 = pd.Timestamp(SIGNAL_HOUR, tz="UTC")
    rows = []
    for _, s in signals.iterrows():
        p = str(s.pool_address).lower()
        m = meta[meta.pool_address.str.lower() == p]
        pair = "?"
        if len(m):
            pair = f"{m.iloc[0].token0_symbol}/{m.iloc[0].token1_symbol}"
        ph = h[h.pool_address == p].sort_values("block_hour")
        at = ph[ph.block_hour == t0]
        close0 = float(at.iloc[0].close_tick) if len(at) else None

        def ret_at(hours_ahead: int):
            target = t0 + pd.Timedelta(hours=hours_ahead)
            hit = ph[ph.block_hour == target]
            if close0 is None or not len(hit):
                return {"exact_hour": False, "ret_bps": None, "close_tick": None, "swap_count": None}
            close1 = float(hit.iloc[0].close_tick)
            return {
                "exact_hour": True,
                "ret_bps": float((close1 - close0) * LN_TICK * 10_000),
                "close_tick": close1,
                "swap_count": int(hit.iloc[0].swap_count),
            }

        path = []
        for _, r in ph.iterrows():
            hrs = int((r.block_hour - t0).total_seconds() // 3600)
            path.append(
                {
                    "hours_ahead": hrs,
                    "block_hour": r.block_hour.isoformat(),
                    "close_tick": int(r.close_tick),
                    "ret_from_signal_bps": (
                        None if close0 is None else float((float(r.close_tick) - close0) * LN_TICK * 10_000)
                    ),
                    "swap_count": int(r.swap_count),
                }
            )
        rows.append(
            {
                "pool_address": p,
                "pair": pair,
                "signal_side": s.signal_side,
                "direction_persistence": float(s.direction_persistence),
                "signal_hour_swaps": int(s.swap_count),
                "signal_close_tick": close0,
                "ret_4h": ret_at(4),
                "ret_24h": ret_at(24),
                "hours_observed_after": int(max((r["hours_ahead"] for r in path), default=-1)),
                "path": path,
            }
        )
    payload = {
        "status": "SHADOW_DESCRIPTIVE",
        "n": len(rows),
        "statistical_verdict": "FORBIDDEN",
        "freshness_raw": freshness,
        "signals": rows,
        "usd_capacity": "UNVERIFIED",
        "note": "tick space only; no executable quotes; n=4 cannot change F03 status",
    }
    (OUT / "r0_shadow_mature.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (OUT / "r0_shadow_hours.tsv").write_text(hours, encoding="utf-8")
    print(json.dumps({"freshness": freshness.splitlines()[:3], "n_hour_rows": int(len(h)), "signals": [
        {k: v for k, v in r.items() if k != "path"} for r in rows
    ]}, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
