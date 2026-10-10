#!/usr/bin/env python3
"""Does EXIT management convert the (holdout-surviving) win-rate signal into money?

The wide discriminant search found a REPRODUCIBLE win-rate lift (low early selling
+ size, e.g. n_sells<=1) but negative realized EV due to a left-skewed payoff
(small wins, fat -50/-90% loss tail). The entry is real; the killer is the exit.

This tests stop-loss / take-profit / hold-horizon on that subset.
PROTOCOL (no p-hack): grid the exit on TRAIN, pick best by TRAIN EV, judge ONCE on
the untouched TEST holdout with full gates (EV>0, bootstrap p5>0, PF_top1_removed>=1.3).
Outcome uses the bonding-curve price grid; stop/tp checked at grid points in order.
"""
from __future__ import annotations
import glob
import json
import random
import statistics as st
import datetime as dt
from collections import defaultdict

SB_GLOB = "/opt/EATLQ/mvp/diagnostics/sol/smart_buyers*.jsonl"
W, ENTRY, COST = 30.0, "180", 0.03
PATH_KEYS = ["600", "1800", "3600", "7200"]   # forward grid points after entry
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}


def day_of(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%m-%d")


def gp(grid, key):
    order = ["60", "180", "600", "1800", "3600", "7200"]
    for k in order[order.index(key)::-1]:
        if grid.get(k):
            return grid[k]
    return None


def load():
    seen = {}
    for f in sorted(glob.glob(SB_GLOB)):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("mint")
            if m and m not in seen and d.get("early_buys") is not None:
                seen[m] = d
    out = []
    for d in seen.values():
        grid = d.get("grid") or {}
        pe = gp(grid, ENTRY)
        if not pe:
            continue
        eb = [e for e in d["early_buys"] if (e.get("t_rel") or 9e9) <= W]
        if not eb:
            continue
        nsell = sum(1 for e in eb if not e.get("is_buy"))
        buy = defaultdict(float)
        for e in eb:
            if e.get("is_buy"):
                buy[e["buyer"]] += float(e.get("sol") or 0)
        sizes = [float(e.get("sol") or 0) for e in eb if e.get("is_buy")]
        d["_day"] = day_of(d["created_ms"])
        d["_pe"] = pe
        d["_path"] = [(int(k), grid.get(k)) for k in PATH_KEYS if grid.get(k)]
        d["_nsell"] = nsell
        d["_mean_buy"] = (sum(sizes) / len(sizes)) if sizes else 0.0
        d["_nw"] = len(set(e["buyer"] for e in eb))
        out.append(d)
    return out


def sim_exit(r, stop, tp, hold_s):
    pe = r["_pe"]
    for t, px in r["_path"]:
        if t > hold_s:
            break
        ret = px / pe - 1
        if stop is not None and ret <= -stop:
            return (px / pe) * (1 - COST) - 1
        if tp is not None and ret >= tp:
            return (px / pe) * (1 - COST) - 1
    # exit at last path point <= hold_s (or first available)
    cand = [(t, px) for t, px in r["_path"] if t <= hold_s] or r["_path"]
    if not cand:
        return None
    px = cand[-1][1]
    return (px / pe) * (1 - COST) - 1


def gates(rets):
    if len(rets) < 10:
        return None
    ev = st.mean(rets)
    srt = sorted(rets, reverse=True)
    g = sum(x for x in srt[1:] if x > 0); l = -sum(x for x in srt if x < 0)
    pft = g / l if l > 0 else 0.0
    evs = sorted(st.mean([random.choice(rets) for _ in rets]) for _ in range(1500))
    p5 = evs[int(0.05 * 1500)]
    wr = sum(1 for x in rets if x > 0) / len(rets)
    return dict(n=len(rets), ev=ev, wr=wr, pft=pft, p5=p5, med=st.median(rets),
                best=max(rets), worst=min(rets))


def subset(rows, name):
    if name == "nsell<=1":
        return [r for r in rows if r["_nsell"] <= 1]
    if name == "nsell<=1 & meanbuy>=0.19":
        return [r for r in rows if r["_nsell"] <= 1 and r["_mean_buy"] >= 0.19]
    if name == "nsell<=1 & nw>=5":
        return [r for r in rows if r["_nsell"] <= 1 and r["_nw"] >= 5]
    return rows


def main():
    rows = load()
    train = [r for r in rows if r["_day"] in TRAIN_DAYS]
    test = [r for r in rows if r["_day"] in TEST_DAYS]
    print("#### EXIT MANAGEMENT on the holdout-surviving win-rate subset ####")
    print(f"coins={len(rows)} train={len(train)} test={len(test)}\n")

    stops = [None, 0.2, 0.35, 0.5, 0.7]
    tps = [None, 0.3, 0.5, 1.0, 2.0]
    holds = [600, 1800, 3600, 7200]

    for sub_name in ["nsell<=1", "nsell<=1 & meanbuy>=0.19", "nsell<=1 & nw>=5"]:
        tr = subset(train, sub_name)
        te = subset(test, sub_name)
        print(f"=== subset '{sub_name}': train n={len(tr)} test n={len(te)} ===")
        # grid the exit on TRAIN, pick best by TRAIN EV (with a sanity n floor)
        best = None
        for stop in stops:
            for tp in tps:
                for hold in holds:
                    rets = [x for x in (sim_exit(r, stop, tp, hold) for r in tr) if x is not None]
                    if len(rets) < 20:
                        continue
                    ev = st.mean(rets)
                    if best is None or ev > best[0]:
                        best = (ev, stop, tp, hold)
        if not best:
            print("  insufficient train sample\n"); continue
        _, stop, tp, hold = best
        tr_rets = [x for x in (sim_exit(r, stop, tp, hold) for r in tr) if x is not None]
        te_rets = [x for x in (sim_exit(r, stop, tp, hold) for r in te) if x is not None]
        gtr, gte = gates(tr_rets), gates(te_rets)
        print(f"  best TRAIN exit: stop={stop} tp={tp} hold={hold}s")
        if gtr:
            print(f"  TRAIN: n={gtr['n']} EV={100*gtr['ev']:+.1f}% WR={100*gtr['wr']:.0f}% "
                  f"PFtop1rm={gtr['pft']:.2f} p5={100*gtr['p5']:+.1f}%")
        if gte:
            surv = gte['ev'] > 0 and gte['p5'] > 0 and gte['pft'] >= 1.3 and gte['n'] >= 20
            print(f"  TEST : n={gte['n']} EV={100*gte['ev']:+.1f}% WR={100*gte['wr']:.0f}% "
                  f"PFtop1rm={gte['pft']:.2f} p5={100*gte['p5']:+.1f}% "
                  f"med={100*gte['med']:+.1f}% best={100*gte['best']:+.0f}% worst={100*gte['worst']:+.0f}%")
            print(f"  >>> {'SURVIVES holdout (candidate for tiny canary)' if surv else 'dies on holdout'}\n")
        else:
            print("  TEST insufficient\n")


if __name__ == "__main__":
    main()
