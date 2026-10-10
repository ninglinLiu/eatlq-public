#!/usr/bin/env python3
"""A5 order-flow imbalance and A3 momentum at $50, on the full year.

Pre-registration: ../GOAL_R1_SIZE50.md, written before any $50 number was seen.
Signals, grids, statistic, inference and trade construction are copied unchanged
from round 38. Three things differ, and only these three: the panel is 366 days
instead of 3 months, the cost is point-in-time per (pool, date) at $50 instead of
one static number at $5,000, and gas is charged.

Round 38's result was that the A5 signal is real but smaller than the toll. The
toll has now fallen by a factor of roughly three relative to volatility, so this
run asks whether the same signal clears the toll this account actually pays.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict

import numpy as np

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=3600", "--max_memory_usage=12000000000"]
FMT = "%Y-%m-%d %H:%M:%S"
VOL_WINDOW, MIN_VOL_OBS = 168, 120
MIN_SWAPS_IN_WINDOW = 5
PANEL_START = "2025-08-17"
IS_END, OOS_END = "2026-05-17", "2026-08-18"   # GOAL_R1_SIZE50 sec.4
BOOT = 10_000
N_CELLS = 12
ALPHA = 0.05
RNG = np.random.default_rng(20260819)

SIZE_USD = 50.0
GAS_USD_PER_SWAP = 0.0359          # mean of 286 real Base receipts
GAS_USD_PER_SWAP_TRIM = 0.0159     # same, 99% trimmed
GAS_BPS = 2.0 * GAS_USD_PER_SWAP / SIZE_USD * 1e4
GAS_BPS_TRIM = 2.0 * GAS_USD_PER_SWAP_TRIM / SIZE_USD * 1e4

A3_GRID = [(L, k, h) for L in (4, 24, 168) for k in (1.5, 2.5) for h in (4, 24)]
A5_GRID = [(W, q, h) for W in (1, 4, 24) for q in (0.3, 0.6) for h in (4, 24)]


def q(sql: str) -> str:
    p = subprocess.run(CH + ["--query", sql], capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:3000])
        raise SystemExit("clickhouse failed")
    return p.stdout.decode()


def load() -> dict[str, dict]:
    """One pool per symbol: the one with the lowest median $50 cost.

    Correlated pools on the same token would otherwise enter as independent
    evidence, which is how round 40's false positive was manufactured.
    """
    # Same exclusion list as build_bars_year.py: WETH derivatives, stablecoins and
    # BTC wrappers move with WETH by construction and their return against WETH is
    # not an alt return.
    pat = " OR ".join(f"position(upper(u.symbol), '{s.upper()}') > 0" for s in (
        "ETH", "USD", "USDC", "USDT", "DAI", "EUR", "BTC", "WBTC", "cbBTC",
        "USDS", "GHO", "crvUSD", "sUSD", "USR", "USDe"))
    sql = f"""
    WITH c AS (
      SELECT pool, quantile(0.5)(cost_bps_med) AS cost, count() AS days
      FROM census.pit_cost_50 WHERE feasible = 1 AND cost_bps_med > 0
      GROUP BY pool HAVING days >= 180),
    best AS (
      SELECT u.symbol AS sym, argMin(u.pool, c.cost) AS bpool, min(c.cost) AS bcost
      FROM edge.bars_universe_50 u INNER JOIN c ON c.pool = u.pool
      WHERE NOT ({pat}) GROUP BY u.symbol)
    SELECT b.sym, b.bcost, toString(a.bar), a.open, a.close,
           f.net_weth, f.gross_weth, f.n_swaps
    FROM edge.bars_1h_year_50 a
    INNER JOIN best b ON a.pool = b.bpool
    INNER JOIN edge.flow_1h_year_50 f ON f.pool = a.pool AND f.bar = a.bar
    ORDER BY b.sym, a.bar FORMAT TSV
    """
    raw: dict[str, dict] = {}
    for ln in q(sql).strip().split("\n"):
        if not ln:
            continue
        sym, cost, bar, op, cl, net, gross, ns = ln.split("\t")
        d = raw.setdefault(sym, {"cost_bps": float(cost), "rows": []})
        d["rows"].append((bar, float(op), float(cl), float(net),
                          float(gross), int(ns)))
    return raw


def to_grid(d: dict) -> dict:
    rows = d["rows"]
    t0 = dt.datetime.strptime(rows[0][0], FMT)
    t1 = dt.datetime.strptime(rows[-1][0], FMT)
    n = int((t1 - t0).total_seconds() // 3600) + 1
    have = {int((dt.datetime.strptime(r[0], FMT) - t0).total_seconds() // 3600): r
            for r in rows}
    op, cl = np.full(n, np.nan), np.full(n, np.nan)
    net, gross, nsw = np.zeros(n), np.zeros(n), np.zeros(n)
    last = None
    for i in range(n):
        if i in have:
            _, o, c, nt, gr, ns = have[i]
            op[i], cl[i], net[i], gross[i], nsw[i] = o, c, nt, gr, ns
            last = c
        elif last is not None:
            op[i] = cl[i] = last
    bars = [(t0 + dt.timedelta(hours=i)).strftime(FMT) for i in range(n)]
    return {"cost_bps": d["cost_bps"], "bar": bars,
            # numpy string array so split masks are vectorised instead of being
            # rebuilt as a Python comprehension once per cell per split
            "barr": np.array(bars), "open": op, "close": cl,
            "net": net, "gross": gross, "nsw": nsw, "n": n}


_sigma_cache: dict[int, np.ndarray] = {}


def sigma_168(c: np.ndarray) -> np.ndarray:
    """Trailing 168-hour sample sd of hourly log returns, ignoring gaps.

    Rolling sums over the finite-masked series, so it is one pass instead of one
    slice per hour. Identical to the round 38 definition including the
    MIN_VOL_OBS floor and the ddof=1 convention.
    """
    key = id(c)
    if key in _sigma_cache:
        return _sigma_cache[key]
    lr = np.full(len(c), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr[1:] = np.log(c[1:] / c[:-1])
    ok = np.isfinite(lr)
    x = np.where(ok, lr, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(x)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x)])
    cn = np.concatenate([[0], np.cumsum(ok.astype(np.int64))])
    out = np.full(len(c), np.nan)
    t = np.arange(VOL_WINDOW + 1, len(c))
    if len(t):
        lo = t - VOL_WINDOW
        m = cn[t] - cn[lo]
        s1 = cs[t] - cs[lo]
        s2 = cs2[t] - cs2[lo]
        with np.errstate(divide="ignore", invalid="ignore"):
            var = (s2 - s1 * s1 / np.maximum(m, 1)) / np.maximum(m - 1, 1)
        good = (m >= MIN_VOL_OBS) & (var > 0)
        out[t[good]] = np.sqrt(var[good])
    _sigma_cache[key] = out
    return out


def sig_a3(g: dict, L: int) -> np.ndarray:
    c, s = g["close"], sigma_168(g["close"])
    z = np.full(len(c), np.nan)
    for t in range(L, len(c)):
        if c[t] > 0 and c[t - L] > 0 and np.isfinite(s[t]):
            z[t] = np.log(c[t] / c[t - L]) / (s[t] * np.sqrt(L))
    return z


def sig_a5(g: dict, W: int) -> np.ndarray:
    net, gross, nsw = g["net"], g["gross"], g["nsw"]
    cn = np.concatenate([[0.0], np.cumsum(net)])
    cg = np.concatenate([[0.0], np.cumsum(gross)])
    cs = np.concatenate([[0.0], np.cumsum(nsw)])
    z = np.full(len(net), np.nan)
    for t in range(W - 1, len(net)):
        lo = t + 1 - W
        gr, ns = cg[t + 1] - cg[lo], cs[t + 1] - cs[lo]
        if gr > 0 and ns >= MIN_SWAPS_IN_WINDOW:
            z[t] = (cn[t + 1] - cn[lo]) / gr
    return z


def fwd(g: dict, h: int) -> np.ndarray:
    """log(close_{t+1+h} / open_{t+1}): the trade a signal at t would take."""
    o, c = g["open"], g["close"]
    n = len(o)
    r = np.full(n, np.nan)
    m = n - h - 1
    if m > 0:
        num, den = c[1 + h:1 + h + m], o[1:1 + m]
        with np.errstate(divide="ignore", invalid="ignore"):
            v = np.log(num / den)
        v[(num <= 0) | (den <= 0)] = np.nan
        r[:m] = v
    return r


def picks(z: np.ndarray, r: np.ndarray, k: float, h: int) -> np.ndarray:
    """Long-only, non-overlapping. Walks only the hours that actually signal."""
    with np.errstate(invalid="ignore"):
        cand = np.flatnonzero(np.isfinite(z) & np.isfinite(r) & (z > k))
    idx, busy = [], -1
    for t in cand:
        if t <= busy:
            continue
        idx.append(int(t))
        busy = t + 1 + h
    return np.array(idx, dtype=int)


def dayboot(byday: dict[str, list[float]]) -> dict:
    """Day-blocked bootstrap of the mean, vectorised.

    Resampling whole days and then averaging every trade inside the resampled
    days is exactly sum(day_sums[pick]) / sum(day_counts[pick]), so the day-level
    sums and counts are all that is needed. Round 38's version rebuilt the flat
    trade list inside all 10,000 iterations, which is fine on a 3-month panel and
    intractable on a 366-day one with 179 symbols.
    """
    days = list(byday.values())
    sums = np.array([float(np.sum(d)) for d in days])
    cnts = np.array([len(d) for d in days], dtype=np.int64)
    n = int(cnts.sum())
    out = {"n": n, "n_days": len(days),
           "mean": float(sums.sum() / n) if n else 0.0}
    if len(days) < 5 or n == 0:
        out.update(lo=None, hi=None, se=None, p=1.0)
        return out
    nd = len(days)
    idx = RNG.integers(0, nd, size=(BOOT, nd))
    ms = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    a = ALPHA / N_CELLS
    out["lo"] = float(np.quantile(ms, a / 2))
    out["hi"] = float(np.quantile(ms, 1 - a / 2))
    out["se"] = float(ms.std(ddof=1))
    out["p"] = float(np.mean(np.abs(ms - ms.mean()) >= abs(out["mean"])))
    return out


def evaluate(grids, sig, k, h, lo, hi):
    byday, raw_edge = defaultdict(list), defaultdict(list)
    bysym = defaultdict(list)
    conds, unconds, hurdles = [], [], []
    for s, g in grids.items():
        r = fwd(g, h)
        sel = picks(sig[s], r, k, h)
        if not len(sel):
            continue
        barr = g["barr"]
        entry = barr[sel + 1]
        sel = sel[(entry >= lo) & (entry < hi)]
        if not len(sel):
            continue
        inwin = (barr[:len(r)] >= lo) & (barr[:len(r)] < hi)
        m = np.isfinite(r) & inwin
        if not m.any():
            continue
        u = float(r[m].mean()) * 1e4
        hurdle = 2.0 * g["cost_bps"] + GAS_BPS
        for t in sel:
            c = r[t] * 1e4
            day = g["bar"][t + 1][:10]
            byday[day].append(c - u - hurdle)
            raw_edge[day].append(c - u)
            bysym[s].append(c - u - hurdle)
            conds.append(c)
            unconds.append(u)
            hurdles.append(hurdle)
    if not conds:
        return {}
    net = dayboot(byday)
    edge = dayboot(raw_edge)
    net.update(cond_bps=float(np.mean(conds)), uncond_bps=float(np.mean(unconds)),
               hurdle_bps=float(np.mean(hurdles)),
               edge_bps=edge["mean"], edge_lo=edge["lo"], edge_hi=edge["hi"],
               n_syms=len(bysym),
               net_trimgas=float(np.mean([x for d in byday.values() for x in d]))
               + (GAS_BPS - GAS_BPS_TRIM),
               top_sym=max(bysym.items(), key=lambda kv: sum(kv[1]))[0]
               if bysym else None)
    net["_bysym"] = {s: (len(v), float(np.sum(v))) for s, v in bysym.items()}
    net["_byday"] = {d: v for d, v in byday.items()}
    return net


def run(name, grid, sigf, grids):
    print("=" * 132)
    print(f"{name} at ${SIZE_USD:.0f} per trade  (gas {GAS_BPS:.1f} bps round trip, "
          f"trimmed {GAS_BPS_TRIM:.1f})   CI at alpha={ALPHA}/{N_CELLS}")
    print("=" * 132)
    print(f"{'p1':>5}{'k':>6}{'h':>5}{'split':>6}{'n':>8}{'days':>6}{'syms':>6}"
          f"{'cond':>9}{'uncond':>9}{'edge':>8}{'edge_lo':>9}{'hurdle':>8}"
          f"{'net':>9}{'net_lo':>9}{'net_hi':>9}{'p':>8}")
    res = {}
    sigcache: dict[float, dict] = {}
    for p1, k, h in grid:
        if p1 not in sigcache:
            sigcache[p1] = {s: sigf(g, p1) for s, g in grids.items()}
        sig = sigcache[p1]
        for split, lo, hi in (("IS", PANEL_START, IS_END), ("OOS", IS_END, OOS_END)):
            r = evaluate(grids, sig, k, h, lo, hi)
            if not r:
                continue
            res.setdefault((p1, k, h), {})[split] = r
            print(f"{p1:>5}{k:>6}{h:>5}{split:>6}{r['n']:>8}{r['n_days']:>6}"
                  f"{r['n_syms']:>6}{r['cond_bps']:>9.1f}{r['uncond_bps']:>9.1f}"
                  f"{r['edge_bps']:>8.1f}"
                  f"{(r['edge_lo'] if r['edge_lo'] is not None else 0):>9.1f}"
                  f"{r['hurdle_bps']:>8.1f}{r['mean']:>9.1f}"
                  f"{(r['lo'] if r['lo'] is not None else 0):>9.1f}"
                  f"{(r['hi'] if r['hi'] is not None else 0):>9.1f}{r['p']:>8.4f}")
    return res


def main() -> None:
    sys.stderr.write("[load] ...\n")
    raw = load()
    grids = {s: to_grid(d) for s, d in raw.items() if len(d["rows"]) >= 2000}
    sys.stderr.write(f"[load] symbols {len(grids)} of {len(raw)} "
                     f"(>=2000 hours of data)\n")
    hur = sorted(2 * g["cost_bps"] + GAS_BPS for g in grids.values())
    sys.stderr.write(f"[hurdle] round trip bps: min={hur[0]:.1f} "
                     f"p10={hur[len(hur) // 10]:.1f} p50={hur[len(hur) // 2]:.1f} "
                     f"max={hur[-1]:.1f}\n")
    out = {}
    for name, grid, sigf in (("A5 order flow imbalance", A5_GRID, sig_a5),
                             ("A3 multi-bar momentum", A3_GRID, sig_a3)):
        r = run(name, grid, sigf, grids)
        out[name] = {f"{a}_{b}_{c}": {k: {kk: vv for kk, vv in v.items()
                                          if not kk.startswith("_")}
                                      for k, v in d.items()}
                     for (a, b, c), d in r.items()}
        wins = [(key, d) for key, d in r.items()
                if d.get("IS", {}).get("lo") is not None
                and d["IS"]["lo"] > 0 and d.get("OOS", {}).get("mean", -1) > 0]
        print(f"\ncells clearing zero in IS with positive OOS: "
              f"{[k for k, _ in wins]}")
        if wins:
            print("concentration guard (GOAL_R1_SIZE50 sec.6) on each winner:")
            for key, d in wins:
                bysym = d["IS"]["_bysym"]
                top = max(bysym.items(), key=lambda kv: kv[1][1])
                tot = sum(v[1] for v in bysym.values())
                print(f"  {key}: {len(bysym)} symbols, largest is {top[0]} "
                      f"with {top[1][0]} trades and "
                      f"{100 * top[1][1] / tot:.1f}% of the total effect")
    json.dump(out, open("out/a5_year_50.json", "w"), indent=1)


if __name__ == "__main__":
    main()
