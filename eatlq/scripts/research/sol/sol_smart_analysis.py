#!/usr/bin/env python3
"""Direction B analysis — smart-money following, OOS, real-cost.

Pipeline:
  1) TRAIN (older days): score wallets by how often they are EARLY buyers
     (is_buy, t_rel<=EARLY_T) of winners vs all coins. Smart wallet = enough
     appearances AND winner-precision lift over base.
  2) TEST (newer days, OOS): for each coin, is any TRAIN-smart wallet an early
     buyer? Measure (a) selectability lift P(winner|smart)/base, and (b) realized
     FOLLOW EV entering at grid[entry] (>= poller speed, AFTER the smart buy) and
     exiting at grid[exit], with round-trip cost.

SAMPLING / REWEIGHTING (read carefully — affects EV interpretation):
  - The ORIGINAL smart_buyers.jsonl is case-control: ALL ath>=8k winners (p=1) +
    a random control of the rest (p<1). On THAT file the test set is oversampled,
    so use --reweight to restore the real base rate (inverse sampling prob).
  - The POWER-UP smart_buyers_extra.jsonl collected ALL test-day coins (day-mode),
    so when combined the TEST set (06-14/06-15) is the FULL population, NOT
    oversampled. In that case RAW EV is already honest and --reweight would be
    WRONG (it would over-weight test-day losers). Default is therefore RAW.
  - We always also compare smart-present vs smart-absent within the same test set;
    the EDGE is robust to either interpretation.
Gates: edge>0, EV>0, PF_top1>=1.3, bootstrap p5>0, n>=30.
"""
from __future__ import annotations
import argparse
import json
import random

SB = "/opt/EATLQ/mvp/diagnostics/sol/smart_buyers.jsonl"
EP = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}


def is_winner(r, thr, min_ttath):
    return (r.get("ath_mc") or 0) >= thr and (r.get("time_to_ath_s") or 0) >= min_ttath


def early_buyers(r, early_t):
    s = set()
    for e in r.get("early_buys", []):
        if e.get("is_buy") and e.get("t_rel") is not None and e["t_rel"] <= early_t and e.get("sol", 0) >= 0.05:
            s.add(e["buyer"])
    return s


def gates(rets, weights=None, n_boot=3000):
    n = len(rets)
    if n < 10:
        return None
    if weights is None:
        weights = [1.0] * n
    W = sum(weights)
    mean = sum(r * w for r, w in zip(rets, weights)) / W
    gains = sum(r * w for r, w in zip(rets, weights) if r > 0)
    losses = -sum(r * w for r, w in zip(rets, weights) if r < 0)
    pf = gains / losses if losses else 99
    srt = sorted(zip(rets, weights), key=lambda x: -x[0])
    pf1 = (sum(r * w for r, w in srt[1:] if r > 0)) / losses if losses else 99
    rng = random.Random(1)
    idx = list(range(n))
    means = []
    for _ in range(n_boot):
        s = [rng.choice(idx) for _ in range(n)]
        ww = sum(weights[i] for i in s)
        means.append(sum(rets[i] * weights[i] for i in s) / ww if ww else 0)
    means.sort()
    p5 = means[int(0.05 * len(means))]
    return dict(n=n, mean=mean, win=sum(1 for r in rets if r > 0) / n, pf=pf, pf1=pf1, p5=p5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--win-thr", type=float, default=20000)
    ap.add_argument("--min-ttath", type=float, default=120)
    ap.add_argument("--early-t", type=float, default=180)
    ap.add_argument("--min-app", type=int, default=3)
    ap.add_argument("--lift", type=float, default=1.5, help="min winner-precision lift over base for smart wallet")
    ap.add_argument("--entry", type=str, default="180")
    ap.add_argument("--exit", type=str, default="3600")
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--min-smart", type=int, default=1, help="min #smart wallets present to take a coin")
    ap.add_argument("--sb", type=str, default=SB, help="smart_buyers jsonl(s), comma-separated")
    ap.add_argument("--reweight", action="store_true",
                    help="ONLY for case-control test sets (original file alone). For the "
                         "powered full-population test (combined w/ extra) leave OFF: raw is honest.")
    args = ap.parse_args()

    rows = []
    seen = set()
    for path in args.sb.split(","):
        for l in open(path.strip()):
            r = json.loads(l)
            if r["mint"] in seen:
                continue
            seen.add(r["mint"]); rows.append(r)
    train = [r for r in rows if r["day"] in TRAIN_DAYS]
    test = [r for r in rows if r["day"] in TEST_DAYS]

    # real population base rate (winner share) from full sample, for reweighting
    full = [json.loads(l) for l in open(EP)]
    real_base = sum(1 for r in full if is_winner(r, args.win_thr, args.min_ttath)) / len(full)

    # 1) score wallets on TRAIN
    base_tr = sum(1 for r in train if is_winner(r, args.win_thr, args.min_ttath)) / len(train)
    app = {}      # wallet -> total train coins it early-bought
    wins = {}     # wallet -> winner train coins
    for r in train:
        w = is_winner(r, args.win_thr, args.min_ttath)
        for b in early_buyers(r, args.early_t):
            app[b] = app.get(b, 0) + 1
            if w:
                wins[b] = wins.get(b, 0) + 1
    smart = set()
    for b, a in app.items():
        if a >= args.min_app:
            prec = wins.get(b, 0) / a
            if prec >= args.lift * base_tr:
                smart.add(b)
    print(f"=== Direction B: smart-money following ===")
    print(f"  winner def: ath>={int(args.win_thr)} & ttath>={int(args.min_ttath)}s | real base={100*real_base:.1f}% | train base(sample)={100*base_tr:.1f}%")
    print(f"  train coins={len(train)} test coins={len(test)} | early_t={args.early_t}s min_app={args.min_app} lift={args.lift}")
    print(f"  smart wallets discovered: {len(smart)}")
    if not smart:
        print("  (none) -> no signal at these thresholds"); return

    # 2) TEST: selectability + follow EV
    pres, absent = [], []
    for r in test:
        eb = early_buyers(r, args.early_t)
        n_smart = len(eb & smart)
        (pres if n_smart >= args.min_smart else absent).append(r)
    bt = sum(1 for r in test if is_winner(r, args.win_thr, args.min_ttath)) / len(test)
    wp = (sum(1 for r in pres if is_winner(r, args.win_thr, args.min_ttath)) / len(pres)) if pres else 0
    print(f"\n  [TEST selectability] base winner%={100*bt:.1f}%")
    print(f"    smart-present: n={len(pres)} winner%={100*wp:.1f}%  lift={wp/bt if bt else 0:.2f}x")
    print(f"    smart-absent : n={len(absent)} winner%={100*(sum(1 for r in absent if is_winner(r,args.win_thr,args.min_ttath))/len(absent)) if absent else 0:.1f}%")

    def follow_ret(r):
        pe = r["grid"].get(args.entry); px = r["grid"].get(args.exit)
        if not pe or not px:
            return None
        return (px / pe) * (1 - args.cost) - 1

    # inverse-sampling-probability weights (case-control correction). The universe
    # took ALL ath>=SAMP_THR coins (p=1) and a random CONTROL of the rest. This
    # corrects the oversampling WITHOUT destroying genuine selectability.
    SAMP_THR = 8000.0
    n_full_win = sum(1 for r in full if (r.get("ath_mc") or 0) >= SAMP_THR)
    n_full_los = len(full) - n_full_win
    n_samp_los = sum(1 for r in rows if (r.get("ath_mc") or 0) < SAMP_THR)
    p_los = n_samp_los / n_full_los if n_full_los else 1.0
    w_los = 1.0 / p_los if p_los else 1.0

    def evset(coins, label, reweight=False):
        rr, ww = [], []
        for r in coins:
            ret = follow_ret(r)
            if ret is None:
                continue
            rr.append(ret)
            ww.append((1.0 if (r.get("ath_mc") or 0) >= SAMP_THR else w_los) if reweight else 1.0)
        g = gates(rr, ww)
        if g:
            print(f"    {label:22s} n={g['n']:3d} mean={100*g['mean']:+6.1f}% win={100*g['win']:4.0f}% PF={g['pf']:.2f} PF_top1={g['pf1']:.2f} p5={100*g['p5']:+.1f}%")
        return g

    mode = "REWEIGHTED (case-control)" if args.reweight else "RAW (full-population test)"
    print(f"\n  [TEST follow EV] entry grid+{args.entry}s -> exit grid+{args.exit}s, cost {100*args.cost:.0f}% rt | EV mode: {mode}")
    gp = evset(pres, "smart-present(raw)")
    ga = evset(absent, "smart-absent(raw)")
    g_eval = gp
    if args.reweight:
        g_eval = evset(pres, "smart-present(reweight)", reweight=True)

    if g_eval and gp and ga:
        edge = gp["mean"] - ga["mean"]
        passed = (g_eval["mean"] > 0 and g_eval["pf1"] >= 1.3 and g_eval["p5"] > 0 and g_eval["n"] >= 30 and edge > 0)
        print(f"  raw edge (present-absent) = {100*edge:+.1f}pp")
        print(f"\n  >>> GATE: {'PASS' if passed else 'FAIL'} (EV>0 & PF_top1>=1.3 & p5>0 & n>=30 & edge>0)")


if __name__ == "__main__":
    main()
