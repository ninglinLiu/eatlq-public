#!/usr/bin/env python3
"""Experiment 1 + 3 (offline, RPC-free): exit lead-time sweep + E-window sweep.

Lens disclosure:
  - RUG base PnL @ planned_exb = REAL-GAS (stress167 _stress167_cache base50).
  - CLEAN base PnL @ planned_exb = QuoterV2 single-shot (_priced_live net, adj). OPTIMISTIC.
  - Earlier-exit DELTA = realized forward tick-path price ratio (pre-flip, alive pool).
        net(exb - L) = (1 + base_net) * exp(lp[exb-L] - lp[exb]) - 1
  - Delay-past-exb fatal = REAL-GAS (stress167 exit_p60/300/600), reused.

All earlier exits are strictly pre-flip (flip > exb for the 167), so:
  fatal% = 0 and hit-rug = 0 for every lead L>0 (deterministic).
The payload is the PnL-retention curve + the lead-vs-delay immunization grid.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa

SIZE_USD = 50.0
LEADS = [0, 30, 60, 120, 300, 600]          # blocks earlier than planned_exb
DELAYS = [0, 60, 300, 600]                    # blocks later (execution latency)
STRESS = "/opt/EATLQ/mvp/audits/stress167_20260624/_stress167_cache.json"
OUT_DIR = "/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624"
LN1P = K.LN1P


def lp_at(path_steps, target_block):
    """Realized log-price at the largest path block <= target_block.
    Returns (lp, used_block) or (None, None) if target precedes first step."""
    chosen = None
    for bkt, ltick, lblock in path_steps:
        if lblock <= target_block:
            chosen = (ltick, lblock)
        else:
            break
    if chosen is None:
        return None, None
    return chosen[0] * LN1P, chosen[1]  # orient applied by caller


def build_frame():
    df = L.run_lens(L.load_base_df(), "qv2")
    dep, skel, creators, token_skel = L._load_blacklists()
    m = L.gate_masks(df, dep, skel, creators, token_skel)
    rug = df[m["model_depth"] & ~m["live_full"] & df.gate_fric & df.tradeable
             & (df.label == "delayed_rug") & (df.ret > 0)].copy()
    clean = df[m["live_full"] & df.tradeable & (df.label == "clean") & (df.ret > 0)].copy()
    return df, rug, clean


def lead_curve(rows, base_net_map, path, priced, labels, lens_name):
    """Return per-lead aggregate. rows: iterable of (pool, orient, exb, flip)."""
    out = []
    detail = {L_: [] for L_ in LEADS}
    for pool, orient, exb, flip in rows:
        ps = path.get(pool, [])
        if not ps:
            continue
        base_net = base_net_map.get(pool)
        if base_net is None:
            continue
        lp_exit, _ = lp_at(ps, exb)
        if lp_exit is None:
            continue
        lp_exit *= orient
        for L_ in LEADS:
            target = exb - L_
            lp_t, used = lp_at(ps, target)
            if lp_t is None:
                # earlier than first observed -> approximate entry (no run-up yet): net ~ -friction
                # use first path step
                ltick0, lb0 = ps[0][1], ps[0][2]
                lp_t = ltick0 * LN1P
                used = lb0
            lp_t *= orient
            ratio = math.exp(lp_t - lp_exit)
            net = (1.0 + base_net) * ratio - 1.0
            detail[L_].append((pool, net, flip - (exb - L_)))
    for L_ in LEADS:
        d = detail[L_]
        nets = np.array([x[1] for x in d], dtype=float)
        margins = np.array([x[2] for x in d], dtype=float)
        base_total = sum(x[1] for x in detail[0]) * SIZE_USD
        tot = float(nets.sum() * SIZE_USD)
        out.append({
            "lead_blocks": L_,
            "lead_min": round(L_ * 2 / 60, 1),
            "n": len(d),
            "total_pnl_usd": round(tot, 1),
            "median_ret": round(float(np.median(nets)), 4),
            "wr": round(float((nets > 0).mean()), 4),
            "fatal_rate": round(float((nets <= -0.5).mean()), 4),  # =0, pre-flip
            "hit_rug": int((margins <= 0).sum()),                  # =0, pre-flip
            "pnl_retained_pct": round(tot / base_total * 100, 1) if base_total else None,
            "median_margin_blocks": int(np.median(margins)),
        })
    return out, detail


def immunization_grid(rug_rows, base_net_map, path, priced, stress):
    """For each (lead L, delay D): net exit = exb - L + D.
    If D<=L (net exit <= exb): pre-flip -> tick PnL. If D>L: reuse real-gas exit_p{D-L}.
    fatal if crosses flip."""
    grid = {}
    for L_ in LEADS:
        for D in DELAYS:
            net_total = 0.0
            fatal = 0
            n = 0
            for pool, orient, exb, flip in rug_rows:
                ps = path.get(pool, [])
                base_net = base_net_map.get(pool)
                if not ps or base_net is None:
                    continue
                lp_exit, _ = lp_at(ps, exb)
                if lp_exit is None:
                    continue
                lp_exit *= orient
                eff = exb - L_ + D
                n += 1
                if eff <= exb:
                    # pre-exb, pre-flip -> tick PnL
                    lp_t, _ = lp_at(ps, eff)
                    if lp_t is None:
                        lp_t = ps[0][1] * LN1P
                    lp_t *= orient
                    net = (1.0 + base_net) * math.exp(lp_t - lp_exit) - 1.0
                else:
                    # delay beyond exb: reuse real-gas exit_p{eff-exb}
                    over = eff - exb
                    key = f"{pool}:exit_p{over}"
                    rec = stress.get(key)
                    if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
                        net = float(rec["real_net"])
                    else:
                        # fallback: crossed flip?
                        net = -1.0 if eff >= flip else base_net
                if net <= -0.5:
                    fatal += 1
                net_total += net
            grid[f"L{L_}_D{D}"] = {
                "lead": L_, "delay": D, "n": n,
                "fatal": fatal, "fatal_rate": round(fatal / n, 4) if n else None,
                "total_pnl_usd": round(net_total * SIZE_USD, 1),
            }
    return grid


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE))
    labels = L._load_labels()
    stress = json.load(open(STRESS))
    df, rug, clean = build_frame()

    # base net maps
    rug_base = {}  # real-gas from stress167 base50
    for _, r in rug.iterrows():
        rec = stress.get(f"{r.pool}:base50")
        if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
            rug_base[r.pool] = float(rec["real_net"])
    clean_base = {r.pool: float(r.ret) for _, r in clean.iterrows()}  # QV2 adj

    def rows_of(frame):
        out = []
        for _, r in frame.iterrows():
            sh = labels[r.pool]
            exb = int(priced[r.pool]["exb"])
            flip = int(sh["flip_block"]) if sh.get("flip_block") else 10**12
            out.append((r.pool, int(r.orient), exb, flip))
        return out

    rug_rows = rows_of(rug)
    clean_rows = rows_of(clean)

    rug_curve, _ = lead_curve(rug_rows, rug_base, path, priced, labels, "real-gas-anchored")
    clean_curve, clean_detail = lead_curve(clean_rows, clean_base, path, priced, labels, "qv2-optimistic")
    grid = immunization_grid(rug_rows, rug_base, path, priced, stress)

    # clean right-tail specific: max winner ret at each lead
    clean_bigmax = {}
    for L_ in LEADS:
        nets = [x[1] for x in clean_detail[L_]]
        clean_bigmax[L_] = round(max(nets), 3) if nets else None

    result = {
        "meta": {
            "size_usd": SIZE_USD, "leads": LEADS, "delays": DELAYS,
            "rug_n": len(rug_rows), "clean_n": len(clean_rows),
            "rug_base_lens": "real-gas (stress167 base50)",
            "clean_base_lens": "QuoterV2 single-shot adj (optimistic upper bound)",
            "earlier_exit_delta_lens": "realized forward tick-path price ratio",
        },
        "rug_lead_curve": rug_curve,
        "clean_lead_curve": clean_curve,
        "clean_max_winner_by_lead": clean_bigmax,
        "immunization_grid": grid,
    }
    json.dump(result, open(os.path.join(OUT_DIR, "exit_lead_scan.json"), "w"), indent=2)

    # console
    print("=== RUG lead curve (real-gas anchored) ===")
    print(f"{'lead':>6}{'min':>6}{'totPnL':>10}{'medRet':>9}{'WR':>6}{'fatal':>7}{'hitRug':>7}{'retain%':>9}{'medMargin':>10}")
    for r in rug_curve:
        print(f"{r['lead_blocks']:>6}{r['lead_min']:>6}{r['total_pnl_usd']:>10}{r['median_ret']*100:>8.1f}%{r['wr']*100:>5.0f}%{r['fatal_rate']*100:>6.1f}%{r['hit_rug']:>7}{r['pnl_retained_pct']:>8.1f}%{r['median_margin_blocks']:>10}")
    print("\n=== CLEAN lead curve (QV2 optimistic) ===")
    print(f"{'lead':>6}{'min':>6}{'totPnL':>10}{'medRet':>9}{'WR':>6}{'retain%':>9}{'maxWin':>9}")
    for r in clean_curve:
        print(f"{r['lead_blocks']:>6}{r['lead_min']:>6}{r['total_pnl_usd']:>10}{r['median_ret']*100:>8.1f}%{r['wr']*100:>5.0f}%{r['pnl_retained_pct']:>8.1f}%{clean_bigmax[r['lead_blocks']]*100:>8.0f}%")
    print("\n=== IMMUNIZATION GRID (rug fatal% | totPnL): rows=lead, cols=delay ===")
    hdr = "lead\\delay " + "".join(f"{d:>14}" for d in DELAYS)
    print(hdr)
    for L_ in LEADS:
        cells = []
        for d in DELAYS:
            g = grid[f"L{L_}_D{d}"]
            cells.append(f"{g['fatal_rate']*100:>5.0f}%/${g['total_pnl_usd']:>6.0f}")
        print(f"{L_:>9} " + "".join(f"{c:>14}" for c in cells))
    print(f"\nwrote {OUT_DIR}/exit_lead_scan.json")


if __name__ == "__main__":
    main()
