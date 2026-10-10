#!/usr/bin/env python3
"""Build the drawdown episodes the fork replay will price. See ../GOAL.md sections 2-3.

The episode walk runs in Python rather than SQL because "the first hour after the
peak that breaches -30%" is a sequential scan, and writing it as a window function
would hide the dedupe rule that keeps episodes non-overlapping.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from params import (GAS_BPS, MAX_EPISODES, PEAK_LOOKBACK_H, STOP_DROP,
                    STOP_WINDOW_H, TARGET_NET_BPS)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"


def ch(q: str) -> list[list[str]]:
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--max_execution_time=14400",
                        "--max_memory_usage=30000000000",
                        "--format", "TabSeparatedWithNames", "--query", q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[:4000])
        raise SystemExit("clickhouse failed")
    return [ln.split("\t") for ln in p.stdout.strip().split("\n")[1:] if ln]


def pools() -> dict:
    rows = ch(f"""
    WITH b AS (
      SELECT pool, bar, close,
             max(close) OVER (PARTITION BY pool ORDER BY bar
                              ROWS BETWEEN 1 FOLLOWING AND {STOP_WINDOW_H} FOLLOWING) AS fmax
      FROM edge.bars_1h_year_50 WHERE close > 0
    )
    SELECT u.pool AS pool, u.alt AS alt, u.symbol AS symbol, u.fee AS fee,
           u.weth_is_token1 AS w1, u.cost_p50 AS cost_p50,
           count() AS hit_hours
    FROM b INNER JOIN edge.bars_universe_50 u ON u.pool = b.pool
    WHERE u.venue = 'univ3' AND b.fmax > 0
          AND 1e4 * (b.fmax / b.close - 1) - 2 * u.cost_p50 - {GAS_BPS} >= {TARGET_NET_BPS}
    GROUP BY pool, alt, symbol, fee, w1, cost_p50
    """)
    out = {}
    for pool, alt, sym, fee, w1, cost, hits in rows:
        out[pool] = dict(pool=pool, token=alt, symbol=sym or alt[:10], fee=int(fee),
                         weth_is_token1=int(w1), cost_p50=float(cost),
                         hit_hours=int(hits))
    sys.stderr.write(f"[pools] univ3 pools with >=1 +200% hour: {len(out)}\n")
    return out


def bars(pool_list) -> dict:
    plist = ",".join(f"'{p}'" for p in pool_list)
    rows = ch(f"""
    SELECT pool, toString(bar) AS bar, close
    FROM edge.bars_1h_year_50
    WHERE pool IN ({plist}) AND close > 0
    ORDER BY pool, bar
    """)
    out = defaultdict(list)
    for pool, bar, close in rows:
        out[pool].append((bar, float(close)))
    sys.stderr.write(f"[bars] hours loaded: {sum(len(v) for v in out.values())}\n")
    return out


def walk(series) -> list[dict]:
    """Non-overlapping (peak, stop) pairs.

    A peak is an hour whose close is the highest of the trailing 24 including
    itself; the stop is the first of the next 24 hours closing at or below 70% of
    it. Taking the true local high makes this the worst case for the stop, which
    is the conservative direction for a measurement whose whole point is to find
    out how bad the fill is.
    """
    eps, i, n = [], 0, len(series)
    while i < n:
        lo = max(0, i - PEAK_LOOKBACK_H + 1)
        if series[i][1] < max(series[k][1] for k in range(lo, i + 1)):
            i += 1
            continue
        peak_bar, peak_px = series[i]
        hit = None
        for j in range(i + 1, min(n, i + 1 + STOP_WINDOW_H)):
            if series[j][1] <= STOP_DROP * peak_px:
                hit = j
                break
        if hit is None:
            i += 1
            continue
        eps.append(dict(peak_bar=peak_bar, peak_px=peak_px,
                        stop_bar=series[hit][0], stop_px=series[hit][1],
                        drop=series[hit][1] / peak_px - 1.0,
                        hours_to_stop=hit - i))
        i = hit + 1
    return eps


def blocks(pairs) -> dict:
    """Last swap block of each hour we are going to pin."""
    vals = ",".join(f"('{p}',toDateTime('{h}'))" for p, h in pairs)
    rows = ch(f"""
    SELECT pool, toString(toStartOfHour(block_time)) AS h,
           max(block_number) AS blk, count() AS n_swaps
    FROM census.cl_swaps
    WHERE (pool, toStartOfHour(block_time)) IN ({vals})
    GROUP BY pool, h
    """)
    return {(p, h): dict(block=int(b), n_swaps=int(n)) for p, h, b, n in rows}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    pl = pools()
    bs = bars(list(pl))
    eps = []
    for pool, series in bs.items():
        for e in walk(series):
            eps.append(dict(e, **{k: pl[pool][k] for k in
                                  ("pool", "token", "symbol", "fee",
                                   "weth_is_token1", "cost_p50")}))
    eps.sort(key=lambda e: (e["pool"], e["peak_bar"]))
    sys.stderr.write(f"[episodes] total={len(eps)} pools={len({e['pool'] for e in eps})}\n")

    total = len(eps)
    if total > MAX_EPISODES:
        step = total / MAX_EPISODES
        eps = [eps[int(i * step)] for i in range(MAX_EPISODES)]
        sys.stderr.write(f"[episodes] deterministic every-{step:.2f}nd sample -> {len(eps)}\n")

    want = {(e["pool"], e["peak_bar"]) for e in eps} | {(e["pool"], e["stop_bar"]) for e in eps}
    blk = blocks(sorted(want))
    keep, missing = [], 0
    for e in eps:
        a = blk.get((e["pool"], e["peak_bar"]))
        b = blk.get((e["pool"], e["stop_bar"]))
        if not a or not b or b["block"] <= a["block"]:
            missing += 1
            continue
        keep.append(dict(e, peak_block=a["block"], stop_block=b["block"],
                         peak_n_swaps=a["n_swaps"], stop_n_swaps=b["n_swaps"]))
    sys.stderr.write(f"[episodes] priced={len(keep)} dropped_no_block={missing}\n")

    bysym = defaultdict(int)
    for e in keep:
        bysym[e["symbol"]] += 1
    sys.stderr.write("  " + "  ".join(f"{k}:{v}" for k, v in
                                      sorted(bysym.items(), key=lambda x: -x[1])) + "\n")
    (OUT / "episodes.json").write_text(json.dumps(
        dict(n_pools=len(pl), n_episodes_total=total, n_priced=len(keep),
             episodes=keep), indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
