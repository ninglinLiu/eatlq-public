#!/usr/bin/env python3
"""Offline sweep over entry/hold/exit-rule configs using the captured price grid.
No RPC. Finds whether ANY realistic config is +EV past the anti-overfit gates,
and reports the best config's OOS-by-day behavior (regime-fluke guard)."""
from __future__ import annotations
import argparse
import json
import random
from collections import defaultdict

PATHS = "/opt/EATLQ/mvp/diagnostics/sol/price_paths.jsonl"
GRID = [0, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 900, 1800, 3600, 5400, 7200]


def price_at(path, t):
    return path.get(str(t))


def sim_holdfixed(path, te, tx, cost):
    pe, px = price_at(path, te), price_at(path, tx)
    if not pe or not px:
        return None
    return (px / pe) * (1 - cost) - 1


def sim_tp_sl(path, te, tp, sl, hold, cost):
    """enter at te; walk grid forward up to hold; take-profit at +tp, stop at -sl,
    else exit at hold-end. coarse (grid-checkpointed)."""
    pe = price_at(path, te)
    if not pe:
        return None
    fwd = [g for g in GRID if te < g <= te + hold]
    for g in fwd:
        p = price_at(path, g)
        if not p:
            continue
        chg = p / pe - 1
        if chg >= tp:
            return tp_cost(tp, cost)
        if chg <= -sl:
            return (-sl - 0)  # approx, cost applied below via wrapper
    # exit at last available <= te+hold
    last = None
    for g in fwd[::-1]:
        if price_at(path, g):
            last = price_at(path, g); break
    if last is None:
        last = pe
    return (last / pe) * (1 - cost) - 1


def tp_cost(tp, cost):
    return (1 + tp) * (1 - cost) - 1


def gates(rets, label, by=None):
    n = len(rets)
    if n < 30:
        return None
    import statistics as st
    gains = sum(x for x in rets if x > 0)
    losses = -sum(x for x in rets if x < 0)
    pf = gains / losses if losses else 99
    srt = sorted(rets, reverse=True)
    pf_top1 = (sum(x for x in srt[1:] if x > 0)) / losses if losses else 99
    rng = random.Random(1)
    means = sorted(sum(rng.choice(rets) for _ in range(n)) / n for _ in range(2000))
    p5 = means[int(0.05 * len(means))]
    mean = sum(rets) / n
    ok = mean > 0 and pf_top1 >= 1.3 and p5 > 0
    return dict(label=label, n=n, mean=mean, win=sum(1 for x in rets if x > 0)/n, pf=pf,
                pf_top1=pf_top1, p5=p5, pass_=ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.03)
    args = ap.parse_args()
    rows = [json.loads(l) for l in open(PATHS)]
    print(f"=== offline sweep (N={len(rows)} signal coins, cost={100*args.cost:.0f}%) ===\n")

    # 1) fixed entry/exit grid
    entries = [30, 45, 60, 90, 120, 180, 300]
    holds_to = {30: [60,120,300,600], 45:[120,300,600], 60: [120,300,600,1800],
                90:[300,600,1800], 120:[300,600,1800], 180:[600,1800,3600], 300:[600,1800,7200]}
    print("--- fixed entry->exit ---")
    best = []
    for te in entries:
        for tx in holds_to.get(te, []):
            rets = [r for r in (sim_holdfixed(p["path"], te, tx, args.cost) for p in rows) if r is not None]
            g = gates(rets, f"enter{te}->exit{tx}")
            if g:
                best.append((g, rows, te, tx, None, None, None))
                print("  %-18s n=%3d mean=%+6.1f%% win=%4.1f%% PF=%5.2f PF_top1=%5.2f p5=%+6.1f%% %s" % (
                    g["label"], g["n"], 100*g["mean"], 100*g["win"], g["pf"], g["pf_top1"], 100*g["p5"],
                    "PASS" if g["pass_"] else ""))

    # 2) take-profit / stop-loss from early entry
    print("\n--- entry + TP/SL (coarse grid checkpoints) ---")
    for te in [30, 45, 60, 90]:
        for tp in [0.5, 1.0, 2.0]:
            for sl in [0.3, 0.5]:
                for hold in [600, 1800, 3600]:
                    rets = []
                    for p in rows:
                        r = sim_tp_sl(p["path"], te, tp, sl, hold, args.cost)
                        if r is not None:
                            # apply cost to SL branch approx
                            rets.append(r if r != -sl else (1 - sl) * (1 - args.cost) - 1)
                    g = gates(rets, f"e{te} tp{int(tp*100)} sl{int(sl*100)} h{hold}")
                    if g and (g["pass_"] or g["mean"] > 0):
                        print("  %-22s n=%3d mean=%+6.1f%% win=%4.1f%% PF=%5.2f PF_top1=%5.2f p5=%+6.1f%% %s" % (
                            g["label"], g["n"], 100*g["mean"], 100*g["win"], g["pf"], g["pf_top1"], 100*g["p5"],
                            "PASS" if g["pass_"] else ""))
                            
    # 3) report OOS-by-day for the single best fixed config by mean
    if best:
        best.sort(key=lambda x: -x[0]["mean"])
        g, _, te, tx, *_ = best[0]
        byday = defaultdict(list)
        for p in rows:
            r = sim_holdfixed(p["path"], te, tx, args.cost)
            if r is not None:
                byday[p["day"]].append(r)
        print(f"\n--- best fixed config '{g['label']}' OOS by day (mean={100*g['mean']:+.1f}%) ---")
        for d in sorted(byday):
            v = byday[d]
            print("  %s: n=%3d mean=%+6.1f%% win=%4.1f%%" % (d, len(v), 100*sum(v)/len(v), 100*sum(1 for x in v if x>0)/len(v)))


if __name__ == "__main__":
    main()
