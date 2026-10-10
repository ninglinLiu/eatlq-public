#!/usr/bin/env python3
"""Forced-flow SHORT: crowded longs (extreme positive funding) rolling over ->
long-liquidation cascade. The ONE idea where the counterparty is STRUCTURALLY
forced (levered longs get liquidated), unlike DEX spot where the only forced
player is you.

DATA: OKX 1h price (_cache_okx) + 8h funding history (_cache_fund), 20-coin
intersection. Short COLLECTS positive funding while held (added to PnL).

THESIS (binned, least p-hackable): among "rolled-over" candidates (was up, now
closing below EMA12), does short EV RISE and catastrophe FALL as funding
crowdedness rises? Plus TRAIN(first60%)->holdout(last40%) time split.

CANDIDATE: ret24>0 (recent uptrend) AND close < EMA12 (rolling over).
ENTRY short next bar; hard stop +HARD_STOP (catastrophe); trail after favorable;
HOLD cap. cost = 0.10% RT. funding_pnl = sum of positive funding over hold.
GATE METRIC #1 = catastrophe tail (a short's left tail is everything).
"""
from __future__ import annotations
import glob
import json
import os
import math
import statistics as st
from bisect import bisect_right

OKX = "/opt/EATLQ/scripts/research/sol/_cache_okx"
FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund"
RT_COST = 0.0010
HARD_STOP = 0.15
TRAIL_ARM, TRAIL_ATR = 0.12, 1.5
HOLD = 48               # hours
MIN_HOURS = 1500
FUND_LOOKBACK = 9       # settlements (~3 days) for crowdedness


def load_pair(sym):
    pf = f"{OKX}/{sym}_1h.json"
    ff = f"{FUND}/{sym}.json"
    if not (os.path.exists(pf) and os.path.exists(ff)):
        return None
    price = json.load(open(pf))           # [ts,o,h,l,c,v]
    fund = json.load(open(ff))            # [[ts,rate],...]
    if len(price) < MIN_HOURS or len(fund) < 30:
        return None
    fund.sort()
    fts = [f[0] for f in fund]
    frate = [f[1] for f in fund]
    return price, fts, frate


def atr(series, n=14):
    tr = [0.0]
    for i in range(1, len(series)):
        h, l, pc = series[i][2], series[i][3], series[i - 1][4]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [tr[0]] * len(series)
    a = sum(tr[1:n + 1]) / n if len(tr) > n else tr[0]
    for i in range(len(series)):
        if i >= n:
            a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def ema(vals, span):
    k = 2 / (span + 1)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def funding_crowd(fts, frate, ts):
    """sum of last FUND_LOOKBACK funding rates strictly before ts (causal)."""
    i = bisect_right(fts, ts)
    lo = max(0, i - FUND_LOOKBACK)
    seg = frate[lo:i]
    return sum(seg) if seg else 0.0


def funding_over(fts, frate, t0, t1):
    """sum of funding rates settled in (t0, t1] (short collects positive)."""
    i0 = bisect_right(fts, t0)
    i1 = bisect_right(fts, t1)
    return sum(frate[i0:i1])


def sim_short(series, at, entry_i, fts, frate):
    n = len(series)
    entry = series[entry_i][1]
    t_entry = series[entry_i][0]
    if entry <= 0:
        return None
    best_low = entry
    armed = False
    end = min(entry_i + HOLD, n)
    for j in range(entry_i, end):
        hi, lo = series[j][2], series[j][3]
        adverse = (hi - entry) / entry
        if adverse >= HARD_STOP:
            fpnl = funding_over(fts, frate, t_entry, series[j][0])
            return (-HARD_STOP - RT_COST + fpnl, adverse, 1, j)
        best_low = min(best_low, lo)
        if not armed and (entry - lo) / entry >= TRAIL_ARM:
            armed = True
        if armed and hi >= best_low + TRAIL_ATR * at[j]:
            ret = (entry - (best_low + TRAIL_ATR * at[j])) / entry - RT_COST
            fpnl = funding_over(fts, frate, t_entry, series[j][0])
            mae = max((series[k][2] - entry) / entry for k in range(entry_i, j + 1))
            return (ret + fpnl, mae, 0, j)
    ex_i = end - 1
    ret = (entry - series[ex_i][4]) / entry - RT_COST
    fpnl = funding_over(fts, frate, t_entry, series[ex_i][0])
    mae = max((series[j][2] - entry) / entry for j in range(entry_i, ex_i + 1))
    return (ret + fpnl, mae, 0, ex_i)


def collect(syms):
    sigs = []
    for sym in syms:
        pair = load_pair(sym)
        if not pair:
            continue
        series, fts, frate = pair
        c = [r[4] for r in series]
        at = atr(series)
        e12 = ema(c, 12)
        tmin, tmax = series[0][0], series[-1][0]
        cut = tmin + 0.6 * (tmax - tmin)
        t = 60
        last_exit = -1                       # non-overlapping: one position per coin
        while t < len(series) - HOLD - 1:
            if t <= last_exit:
                t += 1
                continue
            ret24 = c[t] / c[t - 24] - 1
            rolling_over = c[t] < e12[t] and ret24 > 0.0
            if not rolling_over:
                t += 1
                continue
            crowd = funding_crowd(fts, frate, series[t][0])
            sim = sim_short(series, at, t + 1, fts, frate)
            if not sim:
                t += 1
                continue
            sigs.append({"sym": sym, "ts": series[t][0], "crowd": crowd,
                         "ret": sim[0], "mae": sim[1], "cat": sim[2],
                         "seg": "train" if series[t][0] <= cut else "test"})
            last_exit = sim[3]               # block re-entry until this trade exits
            t = last_exit + 1
    return sigs


def qbins(sigs, key, qs=(0.6, 0.85)):
    vals = sorted(s[key] for s in sigs)
    if not vals:
        return []
    th = [vals[int(q * len(vals))] for q in qs]
    return th


def report(sigs, name, th):
    print(f"\n--- {name}: short PnL & catastrophe by funding-crowd bin ---")
    def b_of(c):
        if c < th[0]:
            return "low"
        if c < th[1]:
            return "mid"
        return "high(crowded)"
    bins = {}
    for s in sigs:
        bins.setdefault(b_of(s["crowd"]), []).append(s)
    for b in ["low", "mid", "high(crowded)"]:
        g = bins.get(b, [])
        if not g:
            print(f"  {b:14s}: (none)"); continue
        rets = [s["ret"] for s in g]
        maes = sorted(s["mae"] for s in g)
        cat = sum(s["cat"] for s in g) / len(g)
        mae95 = maes[int(0.95 * len(maes)) - 1] if maes else 0
        print(f"  {b:14s}: n={len(g):4d} EV={100*st.mean(rets):+6.2f}% "
              f"med={100*st.median(rets):+6.2f}% win={100*sum(1 for r in rets if r>0)/len(rets):3.0f}% "
              f"catastrophe={100*cat:5.1f}% mae_p95={100*mae95:5.1f}%")


def main():
    syms = sorted({os.path.basename(f).split("_")[0] for f in glob.glob(f"{OKX}/*_1h.json")})
    sigs = collect(syms)
    print("#### FORCED-FLOW SHORT: crowded longs rolling over -> liquidation cascade ####")
    print(f"coins used={len({s['sym'] for s in sigs})} | candidates={len(sigs)} | "
          f"funding earned by short is INCLUDED in PnL | #1 metric = catastrophe tail")
    if not sigs:
        print("no candidates"); return
    allret = [s["ret"] for s in sigs]
    print(f"ALL short PnL: EV={100*st.mean(allret):+.2f}% med={100*st.median(allret):+.2f}% "
          f"catastrophe={100*sum(s['cat'] for s in sigs)/len(sigs):.1f}%")
    th = qbins(sigs, "crowd")
    print(f"funding-crowd thresholds (median/p80): {[round(x,4) for x in th]}")
    report(sigs, "ALL", th)
    tr = [s for s in sigs if s["seg"] == "train"]
    te = [s for s in sigs if s["seg"] == "test"]
    th_tr = qbins(tr, "crowd")
    report(tr, "TRAIN (first 60%)", th_tr)
    report(te, "TEST (holdout, last 40%)", th_tr)  # use TRAIN thresholds on holdout

    print("\n=== VERDICT (thesis: short EV RISES & catastrophe FALLS as funding crowd rises, on HOLDOUT) ===")
    def b_of(c, t):
        return "low" if c < t[0] else ("mid" if c < t[1] else "high")
    bins = {"low": [], "mid": [], "high": []}
    for s in te:
        bins[b_of(s["crowd"], th_tr)].append(s)
    seq = [b for b in ["low", "mid", "high"] if bins[b]]
    evs = [st.mean([x["ret"] for x in bins[b]]) for b in seq]
    cats = [sum(x["cat"] for x in bins[b]) / len(bins[b]) for b in seq]
    h_ev = len(evs) >= 2 and all(evs[i] <= evs[i + 1] for i in range(len(evs) - 1)) and evs[-1] > 0
    h_cat = len(cats) >= 2 and all(cats[i] >= cats[i + 1] for i in range(len(cats) - 1))
    print(f"  holdout EV seq {seq}={[round(100*x,2) for x in evs]} -> {'PASS' if h_ev else 'FAIL'}")
    print(f"  holdout catastrophe seq={[round(100*x,1) for x in cats]} -> {'PASS' if h_cat else 'FAIL'}")
    print(f"  >>> {'SURVIVES (crowded-long short has a real edge) — tiny canary only' if (h_ev and h_cat) else 'no monotone forced-flow edge on holdout'}")


if __name__ == "__main__":
    main()
