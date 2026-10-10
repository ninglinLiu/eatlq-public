#!/usr/bin/env python3
"""WIDE multivariate winner-vs-loser discriminant search (the proper version).

Critique answered: prior work tested COARSE UNIVARIATE flags one at a time. This
builds a RICH feature set per coin from the early-buy tape and searches HUNDREDS
of thresholded predicates + their 2-way conjunctions for a rule that separates
REALIZED winners from losers.

ANTI-P-HACK PROTOCOL (the only guardrail, and it is non-negotiable):
  - search EVERYTHING on TRAIN (06-11..13).
  - rank, LOCK the top rules.
  - judge ONCE on an untouched TEST holdout (06-14..15) the search never saw.
  - report how many combos were tried (multiple-testing context). A rule that is
    real survives the holdout; a rule that needed cherry-picking does not.

Outcome = REALIZED sellable return (grid entry T+180s, exit T+1800s, 3% cost).
We report BOTH win-rate lift AND realized EV (EV is what pays you), plus
PF_top1_removed + bootstrap p5 so a single-pool lottery can't masquerade as edge.
"""
from __future__ import annotations
import glob
import json
import random
import statistics as st
import datetime as dt
from collections import defaultdict

SB_GLOB = "/opt/EATLQ/mvp/diagnostics/sol/smart_buyers*.jsonl"
W = 30.0                  # feature window seconds (knowable before T+60 entry)
ENTRY, EXIT, COST = "180", "1800", 0.03
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}
MIN_SEL_TRAIN = 30
MIN_SEL_TEST = 20


def day_of(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%m-%d")


def grid_price(grid, key):
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
        pe, px = grid_price(grid, ENTRY), grid_price(grid, EXIT)
        if not pe or not px:
            continue
        eb = [e for e in d["early_buys"] if (e.get("t_rel") or 9e9) <= W]
        if not eb:
            continue
        d["_day"] = day_of(d["created_ms"])
        d["_ret"] = (px / pe) * (1 - COST) - 1
        d["_win"] = 1 if d["_ret"] > 0 else 0
        d["_f"] = feats(eb)
        out.append(d)
    return out


def feats(eb):
    buy = defaultdict(float)
    sell = defaultdict(float)
    nbuy = nsell = 0
    sizes = []
    first5_w, late_w = set(), set()
    for e in eb:
        w = e.get("buyer")
        s = float(e.get("sol") or 0.0)
        if e.get("is_buy"):
            buy[w] += s
            nbuy += 1
            sizes.append(s)
            (first5_w if (e.get("t_rel") or 0) <= 5 else late_w).add(w)
        else:
            sell[w] += s
            nsell += 1
    wallets = set(buy) | set(sell)
    net = {w: buy[w] - sell[w] for w in wallets}
    tot_buy = sum(buy.values())
    tot_sell = sum(sell.values())
    top_w = max(net, key=net.get) if net else None
    top_net = net.get(top_w, 0.0)
    buys_sorted = sorted(buy.values(), reverse=True)
    top3 = sum(buys_sorted[:3])
    prices = [e["price"] for e in eb if e.get("price")]
    ramp = (prices[-1] / prices[0] - 1) if len(prices) >= 2 and prices[0] else 0.0
    return {
        "n_wallets": len(wallets),
        "n_buys": nbuy,
        "n_sells": nsell,
        "buy_sell_ratio": nbuy / (nsell + 1),
        "tot_buy": tot_buy,
        "net_sol": tot_buy - tot_sell,
        "sell_buy_sol": tot_sell / (tot_buy + 1e-9),
        "top_net": top_net,
        "top_net_share": top_net / (tot_buy + 1e-9),
        "top3_share": top3 / (tot_buy + 1e-9),
        "top_dumped": 1.0 if (top_w and sell.get(top_w, 0) > 0) else 0.0,
        "max_buy": max(sizes) if sizes else 0.0,
        "mean_buy": (sum(sizes) / len(sizes)) if sizes else 0.0,
        "n_small": sum(1 for s in sizes if s < 0.1),
        "breadth_accel": len(late_w) - len(first5_w),
        "early_ramp": ramp,
        "n_dumpers": sum(1 for w in wallets if sell.get(w, 0) > buy.get(w, 0)),
    }


FEATURE_KEYS = ["n_wallets", "n_buys", "n_sells", "buy_sell_ratio", "tot_buy",
                "net_sol", "sell_buy_sol", "top_net", "top_net_share", "top3_share",
                "top_dumped", "max_buy", "mean_buy", "n_small", "breadth_accel",
                "early_ramp", "n_dumpers"]


def build_predicates(train):
    """for each feature, candidate thresholds at quartiles, both directions."""
    preds = []
    for k in FEATURE_KEYS:
        vals = sorted(r["_f"][k] for r in train)
        if not vals:
            continue
        qs = {q: vals[int(q * len(vals))] for q in (0.25, 0.5, 0.75)}
        cuts = sorted(set(qs.values()))
        for c in cuts:
            preds.append((f"{k}>={c:.3g}", (lambda r, k=k, c=c: r["_f"][k] >= c)))
            preds.append((f"{k}<={c:.3g}", (lambda r, k=k, c=c: r["_f"][k] <= c)))
    return preds


def lift(rows, pred):
    wins = [r["_win"] for r in rows]
    flags = [1 if pred(r) else 0 for r in rows]
    n = len(rows); nsel = sum(flags)
    base = sum(wins) / n if n else 0
    if nsel == 0 or base == 0:
        return None
    nws = sum(w for w, f in zip(wins, flags) if f)
    sel = [r["_ret"] for r, f in zip(rows, flags) if f]
    return dict(nsel=nsel, nwin=nws, lift=(nws / nsel) / base, ev=sum(sel) / nsel,
                med=st.median(sel))


def boot_p5(rets, n=1500, seed=2):
    if len(rets) < 5:
        return -9.99
    evs = sorted(st.mean([random.choice(rets) for _ in rets]) for _ in range(n))
    return evs[int(0.05 * n)]


def pf_top1_removed(rets):
    if len(rets) < 3:
        return 0.0
    srt = sorted(rets, reverse=True)
    g = sum(x for x in srt[1:] if x > 0); l = -sum(x for x in srt if x < 0)
    return g / l if l > 0 else 0.0


def main():
    rows = load()
    train = [r for r in rows if r["_day"] in TRAIN_DAYS]
    test = [r for r in rows if r["_day"] in TEST_DAYS]
    print("#### WIDE multivariate winner discriminant search (TRAIN->lock->untouched TEST) ####")
    print(f"coins={len(rows)} train={len(train)} test={len(test)} | "
          f"realized win base: train {100*sum(r['_win'] for r in train)/len(train):.1f}% "
          f"test {100*sum(r['_win'] for r in test)/len(test):.1f}%\n")

    preds = build_predicates(train)
    # univariate on TRAIN
    uni = []
    for name, fn in preds:
        L = lift(train, fn)
        if L and L["nsel"] >= MIN_SEL_TRAIN and L["lift"] >= 1.1:
            uni.append((name, fn, L))
    uni.sort(key=lambda x: x[2]["lift"], reverse=True)
    print(f"predicates tried: {len(preds)} | univariate passing TRAIN(lift>=1.1,n>={MIN_SEL_TRAIN}): {len(uni)}")

    # 2-way conjunctions among the better univariate predicates
    pool = uni[:40]
    combos = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            n1, f1, _ = pool[i]; n2, f2, _ = pool[j]
            fn = (lambda r, a=f1, b=f2: a(r) and b(r))
            L = lift(train, fn)
            if L and L["nsel"] >= 25:
                combos.append((f"({n1}) AND ({n2})", fn, L))
    n_combos = len(combos)
    allrules = [(n, f, L, "uni") for n, f, L in uni] + [(n, f, L, "pair") for n, f, L in combos]
    allrules.sort(key=lambda x: x[2]["lift"], reverse=True)
    print(f"2-way conjunctions evaluated: {n_combos} | total rules searched: {len(preds)+n_combos}")
    print("(multiple-testing: with this many TRAIN comparisons, some lift>1.5 is EXPECTED by chance -> TEST is the judge)\n")

    print("=== TOP TRAIN RULES, then LOCKED and judged ONCE on untouched TEST ===")
    print(f"{'rule':<58} {'TRAIN lift/EV/n':<22} {'TEST lift/EV/n':<22} {'TEST p5 / PFtop1rm':<18} verdict")
    survivors = []
    for name, fn, Ltr, kind in allrules[:20]:
        Lte = lift(test, fn)
        if not Lte:
            continue
        sel_te = [r["_ret"] for r in test if fn(r)]
        p5 = boot_p5(sel_te)
        pft = pf_top1_removed(sel_te)
        surv = (Lte["lift"] >= 1.3 and Lte["ev"] > 0 and p5 > 0 and Lte["nsel"] >= MIN_SEL_TEST and pft >= 1.3)
        if surv:
            survivors.append(name)
        print(f"{name[:57]:<58} "
              f"{Ltr['lift']:.2f}/{100*Ltr['ev']:+.0f}%/{Ltr['nsel']:<6} "
              f"{Lte['lift']:.2f}/{100*Lte['ev']:+.0f}%/{Lte['nsel']:<6} "
              f"{100*p5:+.0f}%/{pft:.2f}      {'SURVIVES' if surv else 'fail'}")

    print(f"\n>>> survivors on untouched holdout (lift>=1.3 & EV>0 & p5>0 & PF_top1rm>=1.3 & n>=20): "
          f"{len(survivors)}")
    if survivors:
        print("    " + "\n    ".join(survivors))
        print("    -> these are CANDIDATES for a tiny forward canary (NOT scale-on-backtest).")
    else:
        print("    none. The wide search found TRAIN rules, but none separated winners on money")
        print("    in the untouched holdout -> consistent with overfit, not a stable edge.")


if __name__ == "__main__":
    main()
