#!/usr/bin/env python3
"""Same frozen spec, two corrections that change what the numbers mean.

1. Bars are reindexed onto a complete hourly grid. Row indexing made H=4 mean
   "four bars that exist", not four hours, on any pool with gaps. Forward-fill is
   the right fill here: an AMM price only moves when a swap happens, so the price
   during a swapless hour genuinely is the previous close.

2. Every conditional return is reported against the unconditional return of the
   same symbol over the same horizon. Alts drifted against WETH over this window,
   so a long-only rule inherits that drift; edge is the difference, not the level.
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict

import numpy as np

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800"]
VOL_WINDOW = 168
MIN_VOL_OBS = 120
KS = [2.0, 3.0]
HS = [1, 4, 24]
IS_END = "2026-07-16"
OOS_END = "2026-08-18"
BOOT = 10_000
RNG = np.random.default_rng(20260818)


def load() -> dict[str, dict]:
    sql = """
    WITH best AS (
      SELECT symbol AS sym, argMin(pool, cost_bps) AS best_pool,
             min(cost_bps) AS best_cost, any(is_major) AS maj
      FROM edge.bars_1h GROUP BY symbol)
    SELECT b.sym, b.maj, b.best_cost, toString(a.bar), a.open, a.close
    FROM edge.bars_1h a INNER JOIN best b ON a.pool = b.best_pool
    ORDER BY b.sym, a.bar FORMAT TSV
    """
    p = subprocess.run(CH + ["--query", sql], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:600])
    raw: dict[str, dict] = {}
    for ln in p.stdout.decode().strip().split("\n"):
        sym, maj, cost, bar, op, cl = ln.split("\t")
        d = raw.setdefault(sym, {"is_major": int(maj), "cost_bps": float(cost), "rows": []})
        d["rows"].append((bar, float(op), float(cl)))
    return raw


def to_grid(d: dict) -> dict:
    """Complete hourly grid; swapless hours carry the previous close in both fields."""
    import datetime as dt
    rows = d["rows"]
    fmt = "%Y-%m-%d %H:%M:%S"
    t0 = dt.datetime.strptime(rows[0][0], fmt)
    t1 = dt.datetime.strptime(rows[-1][0], fmt)
    n = int((t1 - t0).total_seconds() // 3600) + 1
    have = {int((dt.datetime.strptime(b, fmt) - t0).total_seconds() // 3600): (o, c)
            for b, o, c in rows}
    op = np.full(n, np.nan)
    cl = np.full(n, np.nan)
    bars = [(t0 + dt.timedelta(hours=i)).strftime(fmt) for i in range(n)]
    last = None
    for i in range(n):
        if i in have:
            o, c = have[i]
            op[i], cl[i] = o, c
            last = c
        elif last is not None:
            op[i] = cl[i] = last
    return {"is_major": d["is_major"], "cost_bps": d["cost_bps"], "bar": bars,
            "open": op, "close": cl, "filled": int(n - len(have)), "n": n}


def signals(g: dict) -> np.ndarray:
    c = g["close"]
    lr = np.full(len(c), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr[1:] = np.log(c[1:] / c[:-1])
    z = np.full(len(c), np.nan)
    for t in range(VOL_WINDOW + 1, len(c)):
        w = lr[t - VOL_WINDOW:t]
        w = w[np.isfinite(w)]
        if len(w) < MIN_VOL_OBS:
            continue
        s = w.std(ddof=1)
        if s > 0 and np.isfinite(lr[t]):
            z[t] = lr[t] / s
    return z


def fwd(g: dict, h: int) -> np.ndarray:
    """log(close_{t+1+h} / open_{t+1}) for every t: the trade a signal at t would take."""
    o, c = g["open"], g["close"]
    n = len(o)
    r = np.full(n, np.nan)
    for t in range(n - h - 1):
        ep, xp = o[t + 1], c[t + 1 + h]
        if ep > 0 and xp > 0:
            r[t] = np.log(xp / ep)
    return r


def picks(z: np.ndarray, r: np.ndarray, k: float, h: int, side: str) -> np.ndarray:
    """Non-overlapping trade indices for the frozen rule."""
    idx, busy = [], -1
    for t in range(len(z)):
        if t <= busy or not np.isfinite(z[t]) or not np.isfinite(r[t]):
            continue
        if (z[t] < -k) if side == "long" else (z[t] > k):
            idx.append(t)
            busy = t + 1 + h
    return np.array(idx, dtype=int)


def boot_p(by_day: dict[str, list[float]]) -> tuple[float, float]:
    days = list(by_day.values())
    if len(days) < 5:
        return float("nan"), 1.0
    obs = float(np.mean([x for d in days for x in d]))
    nd = len(days)
    means = np.empty(BOOT)
    for i in range(BOOT):
        pick = RNG.integers(0, nd, nd)
        means[i] = np.mean([x for j in pick for x in days[j]])
    return obs, float(np.mean(np.abs(means - means.mean()) >= abs(obs)))


if __name__ == "__main__":
    raw = load()
    grids = {s: to_grid(d) for s, d in raw.items()}
    nm = {s: g for s, g in grids.items() if g["is_major"] == 0}
    tot = sum(g["n"] for g in nm.values())
    fil = sum(g["filled"] for g in nm.values())
    print(f"[mr2] non_major symbols={len(nm)} grid_hours={tot} "
          f"forward_filled={fil} ({100*fil/tot:.1f}%)", flush=True)

    z = {s: signals(g) for s, g in nm.items()}
    out = {}
    print("\n=== conditional vs unconditional, net of 2x measured cost (bps) ===")
    print(f"{'cell':<16}{'split':<5}{'n':>6}{'uncond':>9}{'cond':>9}{'edge':>9}"
          f"{'net':>9}{'p_edge':>8}")
    for side in ("long", "short"):
        for k in KS:
            for h in HS:
                r = {s: fwd(g, h) for s, g in nm.items()}
                for split, lo, hi in (("IS", "", IS_END), ("OOS", IS_END, OOS_END)):
                    cond_day: dict[str, list[float]] = defaultdict(list)
                    edge_day: dict[str, list[float]] = defaultdict(list)
                    uncond_all, cond_all, net_all = [], [], []
                    for s, g in nm.items():
                        sel = picks(z[s], r[s], k, h, side)
                        if not len(sel):
                            continue
                        bars = np.array(g["bar"])
                        inwin = np.array([(lo <= bars[t + 1] < hi) for t in sel])
                        sel = sel[inwin]
                        if not len(sel):
                            continue
                        base = r[s][np.isfinite(r[s])]
                        bb = np.array(g["bar"])[:len(r[s])]
                        m = np.isfinite(r[s]) & np.array([(lo <= x < hi) for x in bb])
                        base = r[s][m]
                        if not len(base):
                            continue
                        u = float(base.mean()) * (1 if side == "long" else -1) * 1e4
                        for t in sel:
                            c = r[s][t] * (1 if side == "long" else -1) * 1e4
                            day = g["bar"][t + 1][:10]
                            cond_day[day].append(c)
                            edge_day[day].append(c - u)
                            uncond_all.append(u)
                            cond_all.append(c)
                            net_all.append(c - 2 * g["cost_bps"])
                    if not cond_all:
                        continue
                    _, p_edge = boot_p(edge_day)
                    key = f"{side}_k{k:g}_h{h}"
                    rec = {"n": len(cond_all), "uncond_bps": float(np.mean(uncond_all)),
                           "cond_bps": float(np.mean(cond_all)),
                           "edge_bps": float(np.mean(cond_all) - np.mean(uncond_all)),
                           "net_bps": float(np.mean(net_all)), "p_edge": p_edge}
                    out.setdefault(key, {})[split] = rec
                    print(f"{key:<16}{split:<5}{rec['n']:>6}{rec['uncond_bps']:>9.1f}"
                          f"{rec['cond_bps']:>9.1f}{rec['edge_bps']:>9.1f}"
                          f"{rec['net_bps']:>9.1f}{p_edge:>8.4f}")

    json.dump(out, open("out/mr2_results.json", "w"), indent=1)
    print("\n[mr2] wrote out/mr2_results.json")
