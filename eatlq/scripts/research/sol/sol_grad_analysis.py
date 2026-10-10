#!/usr/bin/env python3
"""Direction A analysis — post-graduation mid-term EV + ex-ante selectability.

Entry at grad+ENTRY_MIN (speed-insensitive), exit at entry+HOLD_MIN, on PumpSwap
mid prices from GT OHLCV, with round-trip cost. Plus selectability of clean
ex-ante features (graduation speed, sniper%, social) and a TP/SL variant. Gates:
EV>0, PF, PF_top1_removed>=1.3, bootstrap p5>0, and split-half OOS by grad time.
"""
from __future__ import annotations
import argparse
import json
import random
import statistics as st

GP = "/opt/EATLQ/mvp/diagnostics/sol/grad_paths.jsonl"


def P(path, m):
    return path.get(str(int(m)))


def ret_fixed(path, te, tx, cost):
    pe, px = P(path, te), P(path, tx)
    if not pe or not px:
        return None
    return (px / pe) * (1 - cost) - 1


def ret_tpsl(path, te, tp, sl, hold, cost):
    pe = P(path, te)
    if not pe:
        return None
    last = pe
    for m in range(int(te) + 1, int(te + hold) + 1):
        p = P(path, m)
        if p is None:
            continue
        last = p
        if p / pe - 1 >= tp:
            return (1 + tp) * (1 - cost) - 1
        if p / pe - 1 <= -sl:
            return (1 - sl) * (1 - cost) - 1
    return (last / pe) * (1 - cost) - 1


def gates(rets, n_boot=3000):
    n = len(rets)
    if n < 20:
        return None
    gains = sum(x for x in rets if x > 0)
    losses = -sum(x for x in rets if x < 0)
    pf = gains / losses if losses else 99
    srt = sorted(rets, reverse=True)
    pf1 = (sum(x for x in srt[1:] if x > 0)) / losses if losses else 99
    rng = random.Random(1)
    means = sorted(sum(rng.choice(rets) for _ in range(n)) / n for _ in range(n_boot))
    p5 = means[int(0.05 * len(means))]
    mean = sum(rets) / n
    return dict(n=n, mean=mean, win=sum(1 for x in rets if x > 0) / n, pf=pf, pf1=pf1, p5=p5,
                pass_=(mean > 0 and pf1 >= 1.3 and p5 > 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.015, help="PumpSwap round-trip cost")
    args = ap.parse_args()
    rows = [json.loads(l) for l in open(GP)]
    # need enough forward minutes
    rows = [r for r in rows if r.get("path") and r.get("last_min", 0) >= 30]
    print(f"=== Direction A: post-graduation mid-term (N={len(rows)}, cost={100*args.cost:.1f}%) ===\n")

    print("--- fixed entry(min)->exit(min) EV ---")
    print("  %-16s %4s %7s %6s %6s %7s %7s %s" % ("cfg", "n", "mean", "win", "PF", "PF_top1", "p5", ""))
    configs = [(5, 35), (5, 65), (10, 40), (10, 70), (10, 130), (15, 75), (15, 135), (30, 90), (30, 150)]
    for te, tx in configs:
        rets = [r for r in (ret_fixed(x["path"], te, tx, args.cost) for x in rows) if r is not None]
        g = gates(rets)
        if g:
            print("  e%-3d->x%-8d %4d %+6.1f%% %5.1f%% %5.2f %6.2f %+6.1f%% %s" % (
                te, tx, g["n"], 100*g["mean"], 100*g["win"], g["pf"], g["pf1"], 100*g["p5"],
                "PASS" if g["pass_"] else ""))

    print("\n--- TP/SL from grad+10min ---")
    for tp in [0.3, 0.5, 1.0]:
        for sl in [0.2, 0.3]:
            for hold in [60, 120]:
                rets = [r for r in (ret_tpsl(x["path"], 10, tp, sl, hold, args.cost) for x in rows) if r is not None]
                g = gates(rets)
                if g and (g["pass_"] or g["mean"] > 0):
                    print("  tp%d sl%d h%d  n=%d mean=%+.1f%% win=%.0f%% PF=%.2f PF_top1=%.2f p5=%+.1f%% %s" % (
                        int(tp*100), int(sl*100), hold, g["n"], 100*g["mean"], 100*g["win"], g["pf"], g["pf1"],
                        100*g["p5"], "PASS" if g["pass_"] else ""))

    # ex-ante selectability on the best fixed cfg (use e10->x70)
    te, tx = 10, 70
    base = [ret_fixed(x["path"], te, tx, args.cost) for x in rows]
    pairs = [(x, r) for x, r in zip(rows, base) if r is not None]
    allr = [r for _, r in pairs]
    print(f"\n--- ex-ante feature conditioning on e{te}->x{tx} (base mean {100*sum(allr)/len(allr):+.1f}%) ---")
    feats = {
        "fast_grad<2h": lambda x: x.get("grad_speed_h") is not None and x["grad_speed_h"] < 2,
        "fast_grad<0.5h": lambda x: x.get("grad_speed_h") is not None and x["grad_speed_h"] < 0.5,
        "slow_grad>12h": lambda x: x.get("grad_speed_h") is not None and x["grad_speed_h"] > 12,
        "hasTwitter": lambda x: x.get("hasTwitter"),
        "hasWebsite": lambda x: x.get("hasWebsite"),
        "lowSniper<5": lambda x: (x.get("sniperCount") or 0) < 5,
        "highVol10m": lambda x: (x.get("vol10m") or 0) > 5000,
        "rising_first10m": lambda x: P(x["path"], 10) and P(x["path"], 0) and P(x["path"], 10) > P(x["path"], 0),
    }
    for name, fn in feats.items():
        sub = [r for x, r in pairs if fn(x)]
        if len(sub) >= 10:
            g = gates(sub, n_boot=2000)
            print("  %-16s n=%3d mean=%+6.1f%% win=%4.0f%% PF=%.2f p5=%+.1f%%" % (
                name, len(sub), 100*sum(sub)/len(sub), 100*sum(1 for x in sub if x>0)/len(sub),
                g["pf"] if g else 0, 100*g["p5"] if g else 0))

    # split-half OOS by grad time on e10->x70
    pairs2 = sorted(pairs, key=lambda pr: pr[0]["grad_ms"])
    half = len(pairs2) // 2
    for nm, seg in [("TRAIN(old half)", pairs2[:half]), ("TEST(recent half)", pairs2[half:])]:
        rr = [r for _, r in seg]
        g = gates(rr, n_boot=2000)
        if g:
            print("  %-18s n=%d mean=%+.1f%% win=%.0f%% PF=%.2f p5=%+.1f%%" % (
                nm, g["n"], 100*g["mean"], 100*g["win"], g["pf"], 100*g["p5"]))


if __name__ == "__main__":
    main()
