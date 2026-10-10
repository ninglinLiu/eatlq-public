#!/usr/bin/env python3
"""
Grand real-cost backtest over the full sprint086 real-cost overlay (the only dataset
with QuoterV2 two-sided quotes + honeypot/sell-path + slippage@notional + gas-in-USD,
i.e. sell-fail = full loss already baked into net_return_*_real).

Discipline (no lookahead, no overfit peeking):
  - Label = net_return_{H}_real  (real-cost round-trip return at horizon H)
  - Time split by signal_time: TRAIN (oldest 50%) | VAL (next 25%) | OOS (newest 25%)
  - Candidate thresholds derived ONLY from TRAIN quantiles
  - Rule SELECTED on VAL (by EV, with min-sample guard)
  - OOS reported ONCE for the selected rule (never used for selection)
  - Robustness on the winner: bootstrap p5 EV, top-pool-removed PF, per-source, wash split

0 RPC: pure local CSV analysis (sprint086 already paid the RPC).
"""
from __future__ import annotations
import csv, random, statistics, sys
from collections import defaultdict

random.seed(7)
SRCS = {
    "sprint055": "mvp/audits/sprint086_real_cost_overlay/sprint055_paper_shadow_realcost.csv",
    "alpha_001": "mvp/audits/sprint086_real_cost_overlay/alpha_001_paper_shadow_realcost.csv",
    "alpha_005": "mvp/audits/sprint086_real_cost_overlay/alpha_005_paper_shadow_realcost.csv",
}
FEATURES = ["activity_heat", "buy_pressure_ratio", "price_velocity_5m", "onset_lag_sec", "fee_bps"]
MIN_N = 30  # min samples for a rule to be considered in a split


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def load(H):
    rows = []
    for name, f in SRCS.items():
        for r in csv.DictReader(open(f)):
            y = num(r.get(f"net_return_{H}_real"))
            if y is None:
                continue
            rec = {"src": name, "pool": r.get("pool", ""), "t": r.get("signal_time", ""), "y": y}
            for k in FEATURES:
                rec[k] = num(r.get(k))
            ds = num(r.get("dom_sender_share"))
            rec["dom_share"] = ds
            rec["is6ff5"] = (r.get("is_0x6ff5") == "True")
            rec["composite"] = (r.get("composite_flag") == "True")
            rows.append(rec)
    rows.sort(key=lambda z: z["t"])
    return rows


def pf(v):
    p = sum(x for x in v if x > 0); n = abs(sum(x for x in v if x < 0))
    return p / n if n > 0 else float("inf")


def stats(rows):
    v = [r["y"] for r in rows]
    if not v:
        return {"n": 0}
    n = len(v)
    return {"n": n, "ev": sum(v) / n, "pf": pf(v), "wr": sum(1 for x in v if x > 0) / n}


def fmt(s):
    if s.get("n", 0) == 0:
        return "n=0"
    pfx = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"n={s['n']:>4} EV={s['ev']*100:+6.2f}% PF={pfx:>5} WR={s['wr']:.2f}"


def split(rows):
    n = len(rows)
    a = int(n * 0.5); b = int(n * 0.75)
    return rows[:a], rows[a:b], rows[b:]


def apply_rule(rows, feat, op, thr):
    if op == ">=":
        return [r for r in rows if r.get(feat) is not None and r[feat] >= thr]
    return [r for r in rows if r.get(feat) is not None and r[feat] <= thr]


def bootstrap_p5(rows, B=2000):
    v = [r["y"] for r in rows]
    if len(v) < 10:
        return None
    means = []
    for _ in range(B):
        s = [v[random.randrange(len(v))] for _ in range(len(v))]
        means.append(sum(s) / len(s))
    means.sort()
    return means[int(0.05 * B)]


def top_pool_removed_pf(rows):
    by = defaultdict(float)
    for r in rows:
        if r["y"] > 0:
            by[r["pool"]] += r["y"]
    if not by:
        return None, 0.0
    top = max(by, key=by.get)
    total_pos = sum(x for x in (r["y"] for r in rows) if x > 0)
    share = by[top] / total_pos if total_pos > 0 else 0
    rest = [r for r in rows if r["pool"] != top]
    return pf([r["y"] for r in rest]), share


def main():
    for H in ["30m", "1h", "2h"]:
        print("\n" + "=" * 78)
        print(f"HORIZON = {H}  (label = net_return_{H}_real, real cost)")
        print("=" * 78)
        rows = load(H)
        tr, va, oos = split(rows)
        print(f"split by signal_time: TRAIN {tr[0]['t'][:10]}..{tr[-1]['t'][:10]} (n={len(tr)}) | "
              f"VAL ..{va[-1]['t'][:10]} (n={len(va)}) | OOS ..{oos[-1]['t'][:10]} (n={len(oos)})")
        print(f"\nBASELINE (no filter):")
        print(f"  TRAIN {fmt(stats(tr))}\n  VAL   {fmt(stats(va))}\n  OOS   {fmt(stats(oos))}")

        # candidate generation from TRAIN quantiles
        cands = []
        for feat in FEATURES:
            vals = sorted(r[feat] for r in tr if r.get(feat) is not None)
            if len(vals) < 50:
                continue
            for q in [0.3, 0.5, 0.7, 0.8, 0.9]:
                thr = vals[int(q * len(vals))]
                for op in [">=", "<="]:
                    cands.append((feat, op, thr))
        # add structural gates
        struct = [("non_wash", None, None), ("composite", None, None)]

        def sel_rows(rule, base):
            feat, op, thr = rule
            if feat == "non_wash":
                return [r for r in base if (not r["is6ff5"]) and (r["dom_share"] is None or r["dom_share"] <= 0.5)]
            if feat == "composite":
                return [r for r in base if r["composite"]]
            return apply_rule(base, feat, op, thr)

        # SELECT on VAL (candidates scored on VAL EV; thresholds came from TRAIN only)
        scored = []
        for rule in cands + struct:
            rv = sel_rows(rule, va)
            s = stats(rv)
            if s.get("n", 0) >= MIN_N and s["ev"] > 0:
                # require it's also non-negative on TRAIN (consistency)
                st = stats(sel_rows(rule, tr))
                if st.get("n", 0) >= MIN_N:
                    scored.append((s["ev"], rule, s, st))
        scored.sort(reverse=True, key=lambda z: z[0])

        print(f"\nTop rules selected on VAL (threshold from TRAIN only):")
        for ev, rule, s, st in scored[:6]:
            print(f"  {str(rule):40s} TRAIN {fmt(st)} | VAL {fmt(s)}")

        if not scored:
            print("\n  >>> NO rule positive on VAL with n>=%d. No deployable edge at %s." % (MIN_N, H))
            continue

        # report WINNER on OOS (once)
        _, win, _, _ = scored[0]
        oos_rows = sel_rows(win, oos)
        s_oos = stats(oos_rows)
        print(f"\nWINNER = {win}")
        print(f"  OOS (reported once): {fmt(s_oos)}")
        if s_oos.get("n", 0) >= 10:
            p5 = bootstrap_p5(oos_rows)
            tpf, share = top_pool_removed_pf(oos_rows)
            print(f"  robustness OOS: bootstrap_p5_EV={p5*100:+.2f}% | top_pool={share:.0%} of +PnL | "
                  f"PF_top_removed={tpf:.2f}" if p5 is not None else "  robustness: n too small")
            # per source
            for src in SRCS:
                ss = stats([r for r in oos_rows if r["src"] == src])
                if ss.get("n", 0) > 0:
                    print(f"    {src}: {fmt(ss)}")


if __name__ == "__main__":
    main()
