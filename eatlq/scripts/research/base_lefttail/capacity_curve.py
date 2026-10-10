#!/usr/bin/env python3
"""READ-ONLY capacity / absolute-profit saturation curve. Uses real QuoterV2
multi-size quotes (_capacity_size.json) so slippage rises with $ deployed.
Config gate: depth>=3 ETH + MIN_PRE5 + p_cut0.27 (prior conservative optimum).
No env/strategy/sizing change.
"""
from __future__ import annotations
import heapq
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import gate_sweep as G  # noqa

GAS = 0.15
HAIRCUT = 0.02
FRIC_THR = -0.05
SIZES = [100, 300, 500, 1000, 2000, 3000, 5000, 10000]
BLOCK_S = 2.0


def build_pools():
    df = G.load_all()
    priced = json.load(open(K.PCACHE))
    cap = json.load(open(os.path.join(HERE, "_capacity_size.json")))
    cfg = (df.ns_pre >= 5) & (df.p <= 0.27) & (df.entry_weth >= 3) & df.tradeable
    pools = []
    for r in df[cfg].itertuples(index=False):
        exb = (priced.get(r.pool) or {}).get("exb")
        if not exb:
            continue
        hold = max((int(exb) - int(r.entry_block)) * BLOCK_S, BLOCK_S)
        sizemap = {}
        for s in SIZES:
            rec = cap.get(f"{r.pool}:{s}")
            if rec and rec.get("net") is not None and rec.get("fric") is not None:
                ret = float(rec["net"]) - GAS / s - HAIRCUT
                sizemap[s] = {"fric": float(rec["fric"]), "ret": ret}
        if 100 not in sizemap:
            continue
        pools.append({"pool": r.pool, "t0": r.t0, "hold": hold, "depth": float(r.entry_weth),
                      "label": r.label, "sizes": sizemap})
    pools.sort(key=lambda x: x["t0"])
    return pools


def part1(pools):
    print("\n## PART 1: single-pool capacity (net% vs size; slippage saturation)")
    targets = [(3.5, "shallow~3-4E"), (6.0, "mid~6E"), (12.0, "deep~12E"), (20.0, "deep~20E")]
    print(f"{'pool(depth)':22s} " + " ".join(f"{s:>7d}" for s in SIZES))
    for tgt, lab in targets:
        cand = [p for p in pools if p["label"] == "clean" and 100 in p["sizes"] and p["sizes"][100]["ret"] > 0]
        if not cand:
            continue
        p = min(cand, key=lambda x: abs(x["depth"] - tgt))
        nets = []
        for s in SIZES:
            v = p["sizes"].get(s)
            nets.append(f"{v['ret']*100:+6.1f}" if v else "   n/a")
        print(f"{lab:10s} {p['depth']:5.1f}E      " + " ".join(f"{x:>7s}" for x in nets))
        fr = []
        for s in SIZES:
            v = p["sizes"].get(s)
            fr.append(f"{v['fric']*100:+6.1f}" if v else "   n/a")
        print(f"{'  (slip% rt)':22s} " + " ".join(f"{x:>7s}" for x in fr))


def choose_stake(pool, avail, rule):
    """avail = B - currently deployed (fixed working capital, no compounding)."""
    if rule == "fixed100":
        return (100, pool["sizes"][100]["ret"]) if (100 in pool["sizes"] and avail >= 100) else None
    pcap = {"dynamic": 10 ** 9, "dyn_cap1000": 1000, "dyn_cap2000": 2000}[rule]
    best = None
    for s in SIZES:
        v = pool["sizes"].get(s)
        if not v or v["fric"] < FRIC_THR:  # depth/friction caps per-trade size
            continue
        if s > avail or s > pcap:
            continue
        best = (s, v["ret"])
    return best


def simulate(pools, B, rule):
    # Fixed working capital B. deployed = sum of active stakes at COST (<= B).
    # Profits are realized to a separate account and NOT recycled into sizing or
    # concurrency (no compounding). Bet size is set by rule, never by equity.
    active = []  # heap (release_time, stake, ret)
    realized = 0.0
    peak_realized = 0.0
    mdd_usd = 0.0
    peak_conc = 0
    peak_dep = 0.0
    n = 0
    fatal = 0
    max_stake = 0.0
    dep_integral = 0.0
    t_span_s = 0.0
    last_t = None
    deployed = 0.0
    skipped = 0

    def release_until(t):
        nonlocal realized, peak_realized, mdd_usd, deployed, dep_integral, t_span_s, last_t
        while active and active[0][0] <= t:
            rt, stake, ret = heapq.heappop(active)
            seg = (rt - last_t).total_seconds() if last_t else 0
            dep_integral += deployed * max(seg, 0); t_span_s += max(seg, 0); last_t = rt
            deployed -= stake
            realized += stake * ret
            peak_realized = max(peak_realized, realized)
            mdd_usd = min(mdd_usd, realized - peak_realized)
        seg = (t - last_t).total_seconds() if last_t else 0
        dep_integral += deployed * max(seg, 0); t_span_s += max(seg, 0); last_t = t

    for p in pools:
        if last_t is None:
            last_t = p["t0"]
        release_until(p["t0"])
        ch = choose_stake(p, B - deployed, rule)
        if not ch:
            skipped += 1
            continue
        stake, ret = ch
        deployed += stake
        heapq.heappush(active, (p["t0"] + __import__("datetime").timedelta(seconds=p["hold"]), stake, ret))
        n += 1
        max_stake = max(max_stake, stake)
        peak_conc = max(peak_conc, len(active))
        peak_dep = max(peak_dep, deployed)
        if ret <= -0.5:
            fatal += 1
    while active:
        rt, stake, ret = heapq.heappop(active)
        seg = (rt - last_t).total_seconds() if last_t else 0
        dep_integral += deployed * max(seg, 0); t_span_s += max(seg, 0); last_t = rt
        deployed -= stake
        realized += stake * ret
        peak_realized = max(peak_realized, realized)
        mdd_usd = min(mdd_usd, realized - peak_realized)

    span_days = max((pools[-1]["t0"] - pools[0]["t0"]).days, 1)
    monthly_usd = realized / span_days * 30.0
    monthly_pct = monthly_usd / B
    mean_dep = dep_integral / t_span_s if t_span_s else 0.0
    idle = 1 - mean_dep / B
    return {
        "B": B, "rule": rule, "n": n, "skipped": skipped,
        "monthly_pct": monthly_pct, "monthly_usd": monthly_usd,
        "peak_conc": peak_conc, "peak_dep": peak_dep, "mean_dep": mean_dep,
        "idle_pct": idle, "mdd_usd": mdd_usd, "fatal_rate": fatal / n if n else 0,
        "max_stake": max_stake,
    }


def main():
    pools = build_pools()
    print(f"# config pools with multi-size quotes: {len(pools)} "
          f"| span {(pools[-1]['t0']-pools[0]['t0']).days}d "
          f"| depth med {np.median([p['depth'] for p in pools]):.1f}E")
    part1(pools)

    print("\n## PART 2: portfolio capacity vs bankroll B")
    print(f"{'B':>7s} {'rule':10s} {'n':>4s} {'monthly%':>9s} {'monthly$':>9s} "
          f"{'pkConc':>6s} {'pkDep$':>8s} {'idle%':>6s} {'MDD$':>8s} {'fatal':>6s} {'maxStake':>8s}")
    rows = []
    for B in [1000, 2000, 3000, 5000, 10000, 20000]:
        for rule in ["fixed100", "dyn_cap1000", "dyn_cap2000", "dynamic"]:
            r = simulate(pools, B, rule)
            rows.append(r)
            print(f"{B:7d} {rule:10s} {r['n']:4d} {r['monthly_pct']:+8.1%} "
                  f"${r['monthly_usd']:8,.0f} {r['peak_conc']:6d} ${r['peak_dep']:7,.0f} "
                  f"{r['idle_pct']:5.0%} ${r['mdd_usd']:7,.0f} {r['fatal_rate']:5.1%} ${r['max_stake']:7,.0f}")
    json.dump(rows, open(os.path.join(HERE, "_capacity_curve.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
