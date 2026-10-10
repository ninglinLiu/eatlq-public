#!/usr/bin/env python3
"""A3 multi-bar momentum and A5 order-flow imbalance, on the depth-confirmed universe.

Both families share everything except the signal function, so they share this
harness. Pre-registration: ../GOAL.md, written before any return was observed.

The primary statistic is net edge per trade:

    net = (conditional return) - (unconditional return of the same symbol,
                                  same split, same horizon) - (round-trip cost)

Subtracting the unconditional return matters because alts drifted against WETH
over this window; a long-only rule inherits that drift and would otherwise be
credited with it. Subtracting the cost inside the per-trade statistic (rather
than comparing two means afterwards) makes the confidence interval answer the
question that decides the round: does this clear the toll, or not.

Inference is a day-blocked bootstrap -- trades on the same day share order flow
and are not independent, so resampling trades would understate the error.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict

import numpy as np

CH = ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
      "--max_execution_time=1800"]
FMT = "%Y-%m-%d %H:%M:%S"
VOL_WINDOW, MIN_VOL_OBS = 168, 120
MIN_SWAPS_IN_WINDOW = 5          # GOAL.md section 5 amendment
IS_END, OOS_END = "2026-07-16", "2026-08-18"
BOOT = 10_000
N_CELLS = 12                     # per family; Bonferroni divisor
ALPHA = 0.05
RNG = np.random.default_rng(20260818)

A3_GRID = [(L, k, h) for L in (4, 24, 168) for k in (1.5, 2.5) for h in (4, 24)]
A5_GRID = [(W, q, h) for W in (1, 4, 24) for q in (0.3, 0.6) for h in (4, 24)]


def load() -> dict[str, dict]:
    sql = """
    WITH best AS (
      SELECT symbol AS sym, argMin(pool, cost_bps) AS bpool, min(cost_bps) AS bcost
      FROM edge.bars_1h WHERE is_major = 0 GROUP BY symbol)
    SELECT b.sym, b.bcost, toString(a.bar), a.open, a.close,
           f.net_weth, f.gross_weth, f.n_swaps
    FROM edge.bars_1h a
    INNER JOIN best b ON a.pool = b.bpool
    INNER JOIN edge.flow_1h f ON f.pool = a.pool AND f.bar = a.bar
    ORDER BY b.sym, a.bar FORMAT TSV
    """
    p = subprocess.run(CH + ["--query", sql], capture_output=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode()[:2000])
        raise SystemExit("clickhouse failed")
    raw: dict[str, dict] = {}
    for ln in p.stdout.decode().strip().split("\n"):
        sym, cost, bar, op, cl, net, gross, ns = ln.split("\t")
        d = raw.setdefault(sym, {"cost_bps": float(cost), "rows": []})
        d["rows"].append((bar, float(op), float(cl), float(net), float(gross), int(ns)))
    return raw


def to_grid(d: dict) -> dict:
    """Complete hourly grid. Swapless hours carry the previous close and zero flow."""
    rows = d["rows"]
    t0 = dt.datetime.strptime(rows[0][0], FMT)
    t1 = dt.datetime.strptime(rows[-1][0], FMT)
    n = int((t1 - t0).total_seconds() // 3600) + 1
    have = {int((dt.datetime.strptime(r[0], FMT) - t0).total_seconds() // 3600): r
            for r in rows}
    op, cl = np.full(n, np.nan), np.full(n, np.nan)
    net, gross = np.zeros(n), np.zeros(n)
    nsw = np.zeros(n)
    last = None
    for i in range(n):
        if i in have:
            _, o, c, nt, gr, ns = have[i]
            op[i], cl[i], net[i], gross[i], nsw[i] = o, c, nt, gr, ns
            last = c
        elif last is not None:
            op[i] = cl[i] = last
    bars = [(t0 + dt.timedelta(hours=i)).strftime(FMT) for i in range(n)]
    return {"cost_bps": d["cost_bps"], "bar": bars, "open": op, "close": cl,
            "net": net, "gross": gross, "nsw": nsw,
            "n": n, "filled": int(n - len(have))}


def sigma_168(c: np.ndarray) -> np.ndarray:
    lr = np.full(len(c), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr[1:] = np.log(c[1:] / c[:-1])
    s = np.full(len(c), np.nan)
    for t in range(VOL_WINDOW + 1, len(c)):
        w = lr[t - VOL_WINDOW:t]
        w = w[np.isfinite(w)]
        if len(w) >= MIN_VOL_OBS:
            v = w.std(ddof=1)
            if v > 0:
                s[t] = v
    return s


def sig_a3(g: dict, L: int) -> np.ndarray:
    """Cumulative L-hour log return, scaled by sigma_168 * sqrt(L)."""
    c, s = g["close"], sigma_168(g["close"])
    z = np.full(len(c), np.nan)
    for t in range(L, len(c)):
        if c[t] > 0 and c[t - L] > 0 and np.isfinite(s[t]):
            z[t] = np.log(c[t] / c[t - L]) / (s[t] * np.sqrt(L))
    return z


def sig_a5(g: dict, W: int) -> np.ndarray:
    """Net WETH flow over W hours divided by gross flow. Needs >=5 swaps in window."""
    net, gross, nsw = g["net"], g["gross"], g["nsw"]
    cn = np.concatenate([[0.0], np.cumsum(net)])
    cg = np.concatenate([[0.0], np.cumsum(gross)])
    cs = np.concatenate([[0.0], np.cumsum(nsw)])
    z = np.full(len(net), np.nan)
    for t in range(W - 1, len(net)):
        lo = t + 1 - W
        gr = cg[t + 1] - cg[lo]
        ns = cs[t + 1] - cs[lo]
        if gr > 0 and ns >= MIN_SWAPS_IN_WINDOW:
            z[t] = (cn[t + 1] - cn[lo]) / gr
    return z


def fwd(g: dict, h: int) -> np.ndarray:
    """log(close_{t+1+h} / open_{t+1}): the trade a signal at t would actually take."""
    o, c = g["open"], g["close"]
    r = np.full(len(o), np.nan)
    for t in range(len(o) - h - 1):
        if o[t + 1] > 0 and c[t + 1 + h] > 0:
            r[t] = np.log(c[t + 1 + h] / o[t + 1])
    return r


def picks(z: np.ndarray, r: np.ndarray, k: float, h: int) -> np.ndarray:
    """Long-only, non-overlapping: enter when z > k, hold h, ignore signals while in."""
    idx, busy = [], -1
    for t in range(len(z)):
        if t <= busy or not np.isfinite(z[t]) or not np.isfinite(r[t]):
            continue
        if z[t] > k:
            idx.append(t)
            busy = t + 1 + h
    return np.array(idx, dtype=int)


def dayboot(byday: dict[str, list[float]]) -> dict:
    """Point estimate, bootstrap CI at Bonferroni-corrected alpha, se, two-sided p."""
    days = list(byday.values())
    flat = [x for d in days for x in d]
    out = {"n": len(flat), "n_days": len(days), "mean": float(np.mean(flat))}
    if len(days) < 5:
        out.update(lo=None, hi=None, se=None, p=1.0)
        return out
    nd = len(days)
    ms = np.empty(BOOT)
    for i in range(BOOT):
        pick = RNG.integers(0, nd, nd)
        ms[i] = np.mean([x for j in pick for x in days[j]])
    a = ALPHA / N_CELLS
    out["lo"] = float(np.quantile(ms, a / 2))
    out["hi"] = float(np.quantile(ms, 1 - a / 2))
    out["se"] = float(ms.std(ddof=1))
    out["p"] = float(np.mean(np.abs(ms - ms.mean()) >= abs(out["mean"])))
    return out


def evaluate(grids: dict[str, dict], sig: dict[str, np.ndarray],
             k: float, h: int, lo: str, hi: str) -> dict:
    """Aggregate net edge over all symbols for one cell and one window."""
    byday: dict[str, list[float]] = defaultdict(list)
    raw_edge: dict[str, list[float]] = defaultdict(list)
    conds, unconds = [], []
    for s, g in grids.items():
        r = fwd(g, h)
        sel = picks(sig[s], r, k, h)
        if not len(sel):
            continue
        bars = np.array(g["bar"])
        sel = sel[[lo <= bars[t + 1] < hi for t in sel]]
        if not len(sel):
            continue
        m = np.isfinite(r) & np.array([lo <= b < hi for b in bars[:len(r)]])
        if not m.any():
            continue
        u = float(r[m].mean()) * 1e4
        hurdle = 2.0 * g["cost_bps"]
        for t in sel:
            c = r[t] * 1e4
            day = g["bar"][t + 1][:10]
            byday[day].append(c - u - hurdle)
            raw_edge[day].append(c - u)
            conds.append(c)
            unconds.append(u)
    if not conds:
        return {}
    net = dayboot(byday)
    edge = dayboot(raw_edge)
    net.update(cond_bps=float(np.mean(conds)), uncond_bps=float(np.mean(unconds)),
               edge_bps=edge["mean"], edge_lo=edge["lo"], edge_hi=edge["hi"],
               edge_se=edge["se"], n_syms=len({d for d in byday}) and len(conds))
    return net


def run_family(name: str, grid: list, grids: dict, sigfn) -> dict:
    print("\n" + "=" * 118)
    print(f"{name}   long-only, non-overlapping, net = cond - uncond - round_trip_cost "
          f"(bps).  CI at Bonferroni alpha=0.05/{N_CELLS}")
    print("=" * 118)
    print(f"{'cell':<18}{'split':<5}{'n':>6}{'days':>5}{'uncond':>8}{'cond':>8}"
          f"{'edge':>8}{'hurdle':>8}{'net':>9}{'net_lo':>9}{'net_hi':>9}{'se':>7}{'p':>8}")
    res: dict = {}
    cache: dict = {}
    for p1, k, h in grid:
        if p1 not in cache:
            cache[p1] = {s: sigfn(g, p1) for s, g in grids.items()}
        sig = cache[p1]
        key = f"L{p1}_k{k:g}_h{h}" if name.startswith("A3") else f"W{p1}_q{k:g}_h{h}"
        for split, lo, hi in (("ALL", "", OOS_END), ("IS", "", IS_END),
                              ("OOS", IS_END, OOS_END)):
            r = evaluate(grids, sig, k, h, lo, hi)
            if not r:
                continue
            hurdle = r["cond_bps"] - r["uncond_bps"] - r["mean"]
            res.setdefault(key, {})[split] = r | {"hurdle_bps": hurdle}
            print(f"{key:<18}{split:<5}{r['n']:>6}{r['n_days']:>5}"
                  f"{r['uncond_bps']:>8.1f}{r['cond_bps']:>8.1f}{r['edge_bps']:>8.1f}"
                  f"{hurdle:>8.1f}{r['mean']:>9.1f}"
                  f"{(r['lo'] if r['lo'] is not None else float('nan')):>9.1f}"
                  f"{(r['hi'] if r['hi'] is not None else float('nan')):>9.1f}"
                  f"{(r['se'] if r['se'] is not None else float('nan')):>7.1f}"
                  f"{r['p']:>8.4f}")
    return res


def verdict(name: str, res: dict) -> str:
    confirmed = [k for k, v in res.items()
                 if all(sp in v and v[sp]["lo"] is not None and v[sp]["lo"] > 0
                        for sp in ("IS", "OOS"))]
    if confirmed:
        return f"{name}_EDGE_CONFIRMED ({', '.join(confirmed)}) -- recompute hurdle at true $5,000 before claiming"
    alls = [v["ALL"] for v in res.values() if "ALL" in v and v["ALL"]["hi"] is not None]
    if alls and all(v["hi"] < 0 for v in alls):
        worst = max(v["hi"] for v in alls)
        return f"NO_TRADEABLE_{name}_EDGE (powered null; every cell's 95% upper bound < 0, loosest = {worst:.1f} bps)"
    straddle = [k for k, v in res.items()
                if "ALL" in v and v["ALL"]["hi"] is not None and v["ALL"]["hi"] >= 0]
    return f"UNDERPOWERED ({len(straddle)}/{len(res)} cells straddle zero: {', '.join(straddle)})"


def main() -> None:
    raw = load()
    grids = {s: to_grid(d) for s, d in raw.items()}
    tot = sum(g["n"] for g in grids.values())
    fil = sum(g["filled"] for g in grids.values())
    print(f"[universe] non-major symbols={len(grids)} grid_hours={tot} "
          f"forward_filled={fil} ({100 * fil / tot:.1f}%)")
    hur = sorted(2 * g["cost_bps"] for g in grids.values())
    print(f"[hurdle] round trip bps: min={hur[0]:.1f} p50={hur[len(hur) // 2]:.1f} "
          f"max={hur[-1]:.1f}   (R2 worst-direction x2, nominal $5,000 = real ~$2,332)")

    # how much the >=5-swap rule removes, and how correlated the two signals are
    n_all = n_ok = 0
    cors = []
    for s, g in grids.items():
        for W in (1, 4, 24):
            z5 = sig_a5(g, W)
            gr = np.concatenate([[0.0], np.cumsum(g["gross"])])
            has = np.array([(gr[t + 1] - gr[max(t + 1 - W, 0)]) > 0
                            for t in range(len(z5))])
            n_all += int(has.sum())
            n_ok += int(np.isfinite(z5).sum())
        for W in (4, 24):
            a, b = sig_a3(g, W), sig_a5(g, W)
            m = np.isfinite(a) & np.isfinite(b)
            if m.sum() > 200:
                cors.append((W, float(np.corrcoef(a[m], b[m])[0, 1])))
    print(f"[a5 filter] hours with any flow={n_all} kept after >=5 swaps in window="
          f"{n_ok} ({100 * n_ok / max(n_all, 1):.1f}%)")
    for W in (4, 24):
        v = [c for w, c in cors if w == W]
        print(f"[corr] A3 z_L vs A5 ofi_W at L=W={W}: median r={np.median(v):+.3f} "
              f"over {len(v)} symbols")

    out = {}
    a3 = run_family("A3 multi-bar momentum", A3_GRID, grids, sig_a3)
    out["A3"] = {"cells": a3, "verdict": verdict("MOMENTUM", a3)}
    a5 = run_family("A5 order-flow imbalance", A5_GRID, grids, sig_a5)
    out["A5"] = {"cells": a5, "verdict": verdict("OFI", a5)}

    print("\n" + "=" * 118)
    print("VERDICTS (rules frozen in GOAL.md section 6 before any return was observed)")
    print("=" * 118)
    for f in ("A3", "A5"):
        print(f"  {f}: {out[f]['verdict']}")

    print("\nPOWER: the smallest true net edge each family could have detected")
    print(f"{'family':<6}{'cell':<18}{'n':>6}{'se':>7}{'min_detectable':>16}"
          f"{'net_point':>11}{'net_upper':>11}")
    for f, res in (("A3", a3), ("A5", a5)):
        for k, v in res.items():
            a = v.get("ALL")
            if not a or a["se"] is None:
                continue
            mdd = 1.96 * a["se"] * 2
            print(f"{f:<6}{k:<18}{a['n']:>6}{a['se']:>7.1f}{mdd:>16.1f}"
                  f"{a['mean']:>11.1f}{a['hi']:>11.1f}")

    json.dump(out, open("out/predict_results.json", "w"), indent=1)
    print("\n[done] wrote out/predict_results.json")


if __name__ == "__main__":
    main()
