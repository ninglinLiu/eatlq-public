#!/usr/bin/env python3
"""STAGE 0 (zero-RPC, decisive): the latency-decay curve for smart-money following.

Thesis under test: "the smart-money edge is real but eaten by our 60s follow delay;
same-block capture recovers most of it." That is an ASSUMPTION until we measure
EV as a function of entry timing. This script measures the recoverable CEILING from
already-cached data, so we know whether geyser+Jito infra is even justified.

Reuses the exact smart-wallet discovery of sol_smart_analysis.py (train days only),
then for each TEST coin where a smart wallet is an early buyer, compares entry at:
  - SMART_FILL : the smart wallet's own buy price (same-slot, optimistic ceiling)
  - NEXT_FILL  : price of the buy immediately AFTER the smart wallet (realistic
                 same-block: you're ordered behind them, no competition modeled)
  - GRID_60    : price at creation+60s   (~ today's poller follow delay)
  - GRID_180   : price at creation+180s
Exit fixed at a grid horizon. Cost = round-trip. Reports EV, win%, and the
right-tail contribution (top-decile share) needed to size Kelly.

CRITICAL CAVEAT (printed): SMART_FILL/NEXT_FILL are an UPPER BOUND. They assume you
fill at/right-behind the smart wallet with NO market impact and NO copybot
competition. The real same-block haircut can only be measured by a live canary.
This stage only tells us if the ceiling is high enough to bother.
"""
from __future__ import annotations
import json, statistics as st

SB_FILES = [
    "mvp/diagnostics/sol/smart_buyers.jsonl",
    "mvp/diagnostics/sol/smart_buyers_extra.jsonl",
    "mvp/diagnostics/sol/smart_buyers_x2.jsonl",
    "mvp/diagnostics/sol/smart_buyers_x3.jsonl",
    "mvp/diagnostics/sol/smart_buyers_collected.jsonl",
]
EP = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}
WIN_THR, MIN_TTATH, EARLY_T, MIN_APP, LIFT = 20000.0, 120.0, 180.0, 3, 1.5
COST = 0.03
EXIT_KEY = "3600"


def is_winner(r):
    return (r.get("ath_mc") or 0) >= WIN_THR and (r.get("time_to_ath_s") or 0) >= MIN_TTATH


def early_buyers(r):
    s = set()
    for e in r.get("early_buys", []):
        if e.get("is_buy") and e.get("t_rel") is not None and e["t_rel"] <= EARLY_T and e.get("sol", 0) >= 0.05:
            s.add(e["buyer"])
    return s


def load():
    rows, seen = [], set()
    for p in SB_FILES:
        try:
            f = open(p if p.startswith("/") else "/opt/EATLQ/" + p)
        except FileNotFoundError:
            continue
        for l in f:
            if not l.strip():
                continue
            r = json.loads(l)
            if r["mint"] in seen:
                continue
            seen.add(r["mint"]); rows.append(r)
    return rows


def discover_smart(train):
    base = sum(1 for r in train if is_winner(r)) / len(train)
    app, wins = {}, {}
    for r in train:
        w = is_winner(r)
        for b in early_buyers(r):
            app[b] = app.get(b, 0) + 1
            if w:
                wins[b] = wins.get(b, 0) + 1
    smart = {b for b, a in app.items() if a >= MIN_APP and wins.get(b, 0) / a >= LIFT * base}
    return smart, base


def smart_fill(r, smart):
    """earliest smart wallet buy -> (price, index in early_buys)."""
    for i, e in enumerate(r.get("early_buys", [])):
        if e.get("is_buy") and e["buyer"] in smart and e.get("t_rel", 1e9) <= EARLY_T and e.get("price"):
            return e["price"], i
    return None, None


def next_fill(r, idx):
    for e in r.get("early_buys", [])[idx + 1:]:
        if e.get("is_buy") and e.get("price"):
            return e["price"]
    return None


def evstats(rets, label):
    if len(rets) < 10:
        print(f"  {label:12s} n={len(rets)} (too few)"); return None
    n = len(rets)
    mean = st.mean(rets)
    win = sum(1 for x in rets if x > 0) / n
    s = sorted(rets, reverse=True)
    top10 = s[:max(1, n // 10)]
    top10_share = sum(x for x in top10 if x > 0) / sum(x for x in rets if x > 0) if any(x > 0 for x in rets) else 0
    gross = st.mean([(1 + x) / (1 - COST) - 1 for x in rets])  # back out cost for gross view
    print(f"  {label:12s} n={n:3d}  EV={100*mean:+6.1f}%  gross={100*gross:+6.1f}%  win={100*win:4.0f}%  "
          f"max={100*s[0]:+5.0f}%  top10%share={100*top10_share:3.0f}%")
    return mean


def main():
    rows = load()
    train = [r for r in rows if r["day"] in TRAIN_DAYS]
    test = [r for r in rows if r["day"] in TEST_DAYS]
    smart, base = discover_smart(train)
    print(f"=== STAGE 0: latency-decay curve (smart-money follow) ===")
    print(f"train={len(train)} test={len(test)} smart_wallets={len(smart)} | exit=grid+{EXIT_KEY}s cost={100*COST:.0f}%rt")
    print(f"!! SMART_FILL/NEXT_FILL are an UPPER BOUND (no impact, no copybot competition) !!\n")

    buckets = {"SMART_FILL": [], "NEXT_FILL": [], "GRID_60": [], "GRID_180": []}
    n_present = 0
    for r in test:
        eb = early_buyers(r)
        if not (eb & smart):
            continue
        px = r["grid"].get(EXIT_KEY)
        if not px:
            continue
        n_present += 1
        pf, idx = smart_fill(r, smart)
        if pf:
            buckets["SMART_FILL"].append((px / pf) * (1 - COST) - 1)
            nf = next_fill(r, idx)
            if nf:
                buckets["NEXT_FILL"].append((px / nf) * (1 - COST) - 1)
        e60, e180 = r["grid"].get("60"), r["grid"].get("180")
        if e60:
            buckets["GRID_60"].append((px / e60) * (1 - COST) - 1)
        if e180:
            buckets["GRID_180"].append((px / e180) * (1 - COST) - 1)

    print(f"smart-present test coins: {n_present}\n")
    print(f"  entry timing  -> EV(net, {100*COST:.0f}% rt) | gross | win% | max | right-tail concentration")
    means = {}
    for k in ["SMART_FILL", "NEXT_FILL", "GRID_60", "GRID_180"]:
        means[k] = evstats(buckets[k], k)
    if means.get("SMART_FILL") is not None and means.get("GRID_60") is not None:
        rec = means["SMART_FILL"] - means["GRID_60"]
        print(f"\n  >>> RECOVERABLE EDGE (same-block ceiling - 60s delay) = {100*rec:+.1f}pp")
        print(f"      if this is large & SMART_FILL EV>0 -> infra worth it; else thesis dead.")

    # === FULL ENTRY x EXIT MATRIX (gross, zero cost) — is there ANY profitable combo? ===
    print(f"\n=== ENTRY x EXIT GROSS-EV MATRIX (cost=0, the most generous test) ===")
    exits = ["60", "180", "600", "1800", "3600", "7200"]
    # (entry fn, approx entry time seconds-from-creation for validity masking)
    entry_defs = {
        "SMART_FILL(0s)": (lambda r, s: (smart_fill(r, s)[0]), 0),
        "NEXT_FILL(~0s)": (lambda r, s: (next_fill(r, smart_fill(r, s)[1]) if smart_fill(r, s)[1] is not None else None), 0),
        "GRID_60": (lambda r, s: r["grid"].get("60"), 60),
        "GRID_180": (lambda r, s: r["grid"].get("180"), 180),
    }
    print(f"  {'entry':16s}" + "".join(f"x{e:>7s}" for e in exits))
    for ename, (efn, et) in entry_defs.items():
        row = []
        for ex in exits:
            if int(ex) <= et:
                row.append(f"{'--':>7s}"); continue
            rr = []
            for r in test:
                if not (early_buyers(r) & smart):
                    continue
                pe = efn(r, smart); pxx = r["grid"].get(ex)
                if pe and pxx:
                    rr.append(pxx / pe - 1)
            row.append(f"{100*st.mean(rr):+7.0f}" if len(rr) >= 10 else f"{'--':>7s}")
        print(f"  {ename:16s}" + " ".join(row))
    print("\n  (rows=entry timing, cols=exit secs-from-creation; gross mean %, cost=0; '--'=exit<=entry)")
    print("  net needs > +3% to beat round-trip cost. NO valid cell clears it.")


if __name__ == "__main__":
    main()
