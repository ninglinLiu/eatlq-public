#!/usr/bin/env python3
"""Build the point-in-time predictor panel for C1b, per GOAL.md section 3.

For every (pool, entry date, lookback L) it computes, using ONLY hourly bars
strictly before the entry date:

    sigma_rob = 1.4826 * MAD(hourly log returns)     robust, discards no bar
    Rbar      = sum(vol_weth * fee_rate) / L         fee revenue per hour
    FV        = Rbar / sigma_rob**2                  the selection variable

MAD rather than a return-magnitude filter is deliberate: GOAL.md section 3
records that the deep-universe filter MAX_ABS_LR=2.3 flags 10.8% of bars in the
$50 universe, so neutralising them may discard signal rather than noise.

Zero RPC. Reads ClickHouse only, writes only under this workspace.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path("/root/base_condmm_r0")
POSITIONS = Path("/root/base_fee_tier_r0/out/stage3_pnl_50.json")
LOOKBACKS = (24, 168, 720)
MIN_RETURNS = 12          # a window with fewer usable returns yields no predictor
TICK_LO, TICK_HI = 1e-30, 1e30   # CLMM tick-limit validity bound, GOAL.md 3


def ch(query: str) -> str:
    out = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_memory_usage", "8000000000", "--query", query],
        capture_output=True, text=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:2000])
    return out.stdout


def load_bars():
    """pool -> (ts[int64], vol_weth[f8], close[f8]), plus pool -> fee_rate."""
    print("[s01] pulling bars ...", flush=True)
    txt = ch(
        "SELECT b.pool, toUnixTimestamp(b.bar), b.vol_weth, b.close, u.fee "
        "FROM edge.bars_1h_year_50 b "
        "INNER JOIN edge.bars_universe_50 u ON u.pool = b.pool "
        "ORDER BY b.pool, b.bar FORMAT TabSeparated")
    pools, ts, vol, close, fee = [], [], [], [], []
    for line in txt.rstrip("\n").split("\n"):
        a, b, c, d, e = line.split("\t")
        pools.append(a)
        ts.append(int(b))
        vol.append(float(c))
        close.append(float(d))
        fee.append(int(e))
    pools = np.array(pools)
    ts = np.array(ts, dtype=np.int64)
    vol = np.array(vol, dtype=np.float64)
    close = np.array(close, dtype=np.float64)
    fee = np.array(fee, dtype=np.int64)
    print(f"[s01] bars={len(ts):,} pools={len(set(pools.tolist())):,}", flush=True)

    order = np.lexsort((ts, pools))
    pools, ts, vol, close, fee = (x[order] for x in (pools, ts, vol, close, fee))

    out, fee_rate = {}, {}
    edges = np.flatnonzero(pools[1:] != pools[:-1]) + 1
    for lo, hi in zip(np.r_[0, edges], np.r_[edges, len(pools)]):
        p = pools[lo]
        out[p] = (ts[lo:hi], vol[lo:hi], close[lo:hi])
        fee_rate[p] = fee[lo] / 1e6
    return out, fee_rate


def entry_dates() -> list[str]:
    rows = json.loads(POSITIONS.read_text())
    return sorted({r["entry"] for r in rows})


def to_ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def predictors(bars, fee_rate, days):
    """One record per (pool, day, L). Uses bars strictly before day-start."""
    day_ts = np.array([to_ts(d) for d in days], dtype=np.int64)
    recs = []
    for p, (ts, vol, close) in bars.items():
        fr = fee_rate[p]
        # index of first bar at or after each day start -> window is [.. idx)
        cut = np.searchsorted(ts, day_ts, side="left")
        for di, day in enumerate(days):
            hi = int(cut[di])
            if hi < MIN_RETURNS + 1:
                continue
            for L in LOOKBACKS:
                lo = max(0, hi - L)
                w_close = close[lo:hi]
                w_vol = vol[lo:hi]
                if len(w_close) < MIN_RETURNS + 1:
                    continue
                ok = np.isfinite(w_close) & (w_close > TICK_LO) & (w_close < TICK_HI)
                # log returns only between consecutive valid marks
                pair = ok[1:] & ok[:-1]
                if pair.sum() < MIN_RETURNS:
                    continue
                r = np.log(w_close[1:][pair] / w_close[:-1][pair])
                r = r[np.isfinite(r)]
                if r.size < MIN_RETURNS:
                    continue
                med = np.median(r)
                sigma_rob = 1.4826 * np.median(np.abs(r - med))
                rbar = float(np.nansum(w_vol) * fr / L)
                recs.append({
                    "pool": p, "entry": day, "L": L,
                    "sigma_rob": float(sigma_rob),
                    "rbar": rbar,
                    "fv": (rbar / sigma_rob ** 2) if sigma_rob > 0 else None,
                    "n_ret": int(r.size),
                })
        # progress
    return recs


def main():
    bars, fee_rate = load_bars()
    days = entry_dates()
    print(f"[s01] entry days={len(days)} {days[0]} -> {days[-1]}", flush=True)
    recs = predictors(bars, fee_rate, days)
    n_fv = sum(1 for r in recs if r["fv"] is not None)
    print(f"[s01] records={len(recs):,}  with finite FV={n_fv:,}", flush=True)
    (ROOT / "out").mkdir(exist_ok=True)
    with open(ROOT / "out" / "panel.json", "w") as fh:
        json.dump(recs, fh)
    zero_sigma = len(recs) - n_fv
    meta = {
        "records": len(recs),
        "finite_fv": n_fv,
        "zero_sigma_dropped": zero_sigma,
        "lookbacks": list(LOOKBACKS),
        "min_returns": MIN_RETURNS,
        "tick_bound": [TICK_LO, TICK_HI],
        "entry_days": len(days),
    }
    with open(ROOT / "out" / "panel_meta.json", "w") as fh:
        json.dump(meta, fh, indent=1)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
