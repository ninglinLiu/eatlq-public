#!/usr/bin/env python3
"""Map the 'exit-earlier sweet spot' to ACTIONABLE trailing params (offline, RPC-free).

Re-walks the realized forward tick path with alternative exit rules and prices the
resulting exit block via tick ratio anchored on the real-gas base (rug) / QV2 base (clean).

Rules swept:
  TRAIL in {0.35(cur), 0.25, 0.20, 0.15, 0.10}   (exit when this far below running peak)
  plus optional MAX_HOLD cap in minutes {none, 60, 30, 20, 10}

Also recomputes the rug fatal-under-execution-delay for each rule, to show which
trailing setting immunizes the +60/+300-block delay cliff while keeping clean upside.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa

SIZE_USD = 50.0
LN1P = K.LN1P
HARD_STOP = K.HARD_STOP
STRESS = "/opt/EATLQ/mvp/audits/stress167_20260624/_stress167_cache.json"
OUT_DIR = "/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624"
TRAILS = [0.35, 0.25, 0.20, 0.15, 0.10]
MAXHOLD_MIN = [None, 60, 30, 20]
DELAYS = [60, 300]


def exit_block_param(path_steps, entry_lp, orient, entry_block, trail, max_hold_blocks):
    """Causal trailing exit with configurable TRAIL and optional max-hold cap.
    Mirrors keystone_live.exit_block but parameterized. Returns (block, lp_at_exit, reason)."""
    if not path_steps:
        return entry_block, entry_lp, "silent"
    peak = entry_lp
    trail_thr = math.log(1 - trail)
    for bkt, ltick, lblock in path_steps:
        lp = ltick * LN1P * orient
        peak = max(peak, lp)
        if max_hold_blocks is not None and (lblock - entry_block) >= max_hold_blocks:
            return lblock, lp, "maxhold"
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, lp, "stop"
        if (lp - peak) <= trail_thr:
            return lblock, lp, "trail"
    last = path_steps[-1]
    return last[2], last[1] * LN1P * orient, "hold_end"


def lp_at(path_steps, target_block, orient):
    chosen = None
    for bkt, ltick, lblock in path_steps:
        if lblock <= target_block:
            chosen = (ltick, lblock)
        else:
            break
    if chosen is None:
        chosen = (path_steps[0][1], path_steps[0][2])
    return chosen[0] * LN1P * orient, chosen[1]


def build():
    df = L.run_lens(L.load_base_df(), "qv2")
    dep, skel, creators, token_skel = L._load_blacklists()
    m = L.gate_masks(df, dep, skel, creators, token_skel)
    rug = df[m["model_depth"] & ~m["live_full"] & df.gate_fric & df.tradeable
             & (df.label == "delayed_rug") & (df.ret > 0)].copy()
    clean = df[m["live_full"] & df.tradeable & (df.label == "clean") & (df.ret > 0)].copy()
    return rug, clean


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE))
    labels = L._load_labels()
    stress = json.load(open(STRESS))
    rug, clean = build()

    rug_base = {}
    for _, r in rug.iterrows():
        rec = stress.get(f"{r.pool}:base50")
        if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
            rug_base[r.pool] = float(rec["real_net"])
    clean_base = {r.pool: float(r.ret) for _, r in clean.iterrows()}

    def ctx_rows(frame):
        rows = []
        for _, r in frame.iterrows():
            sh = labels[r.pool]
            exb0 = int(priced[r.pool]["exb"])
            flip = int(sh["flip_block"]) if sh.get("flip_block") else 10**12
            entry_lp = r.last_tick_pre * LN1P * int(r.orient)
            rows.append((r.pool, int(r.orient), int(r.entry_block), exb0, flip, entry_lp))
        return rows

    rug_rows = ctx_rows(rug)
    clean_rows = ctx_rows(clean)

    def eval_rule(rows, base_map, trail, maxhold_min, with_delay=None):
        maxhold_blk = None if maxhold_min is None else int(maxhold_min * 60 / 2)
        nets, margins, holds, fatals = [], [], [], 0
        for pool, orient, eb, exb0, flip, entry_lp in rows:
            ps = path.get(pool, [])
            base = base_map.get(pool)
            if not ps or base is None:
                continue
            # anchor lp at original exb0 (where base_net was measured)
            lp_exit0, _ = lp_at(ps, exb0, orient)
            b_new, lp_new, _why = exit_block_param(ps, entry_lp, orient, eb, trail, maxhold_blk)
            eff = b_new + (with_delay or 0)
            if with_delay and eff > exb0:
                over = eff - exb0
                # nearest available real-gas delay sample
                cand = min((60, 300, 600), key=lambda x: abs(x - over))
                rec = stress.get(f"{pool}:exit_p{cand}")
                if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
                    net = float(rec["real_net"])
                else:
                    net = -1.0 if eff >= flip else base
            else:
                lp_t, _ = lp_at(ps, eff, orient)
                net = (1.0 + base) * math.exp(lp_t - lp_exit0) - 1.0
            nets.append(net)
            margins.append(flip - b_new)
            holds.append((b_new - eb) * 2 / 60)
            if net <= -0.5:
                fatals += 1
        a = np.array(nets)
        return {
            "n": len(a),
            "total_pnl": round(float(a.sum() * SIZE_USD), 0),
            "median_ret": round(float(np.median(a)), 4),
            "wr": round(float((a > 0).mean()), 4),
            "fatal": fatals,
            "fatal_rate": round(fatals / len(a), 4) if len(a) else None,
            "max_ret": round(float(a.max()), 3),
            "median_hold_min": round(float(np.median(holds)), 1),
            "median_margin_blk": int(np.median(margins)),
        }

    report = {"meta": {"trails": TRAILS, "maxhold_min": MAXHOLD_MIN, "delays": DELAYS,
                       "rug_n": len(rug_rows), "clean_n": len(clean_rows)}, "sweep": []}

    print("=== TRAILING PARAM SWEEP ===")
    print(f"{'TRAIL':>6}{'maxHold':>8} | RUG: {'totPnL':>8}{'medHold':>8}{'medMargin':>10}{'fatal@0':>8}{'fatal@+60':>10}{'fatal@+300':>11} | CLEAN: {'totPnL':>8}{'maxWin':>8}{'medRet':>8}")
    for trail in TRAILS:
        for mh in MAXHOLD_MIN:
            r0 = eval_rule(rug_rows, rug_base, trail, mh, None)
            r60 = eval_rule(rug_rows, rug_base, trail, mh, 60)
            r300 = eval_rule(rug_rows, rug_base, trail, mh, 300)
            c0 = eval_rule(clean_rows, clean_base, trail, mh, None)
            row = {"trail": trail, "maxhold_min": mh,
                   "rug": r0, "rug_delay60": r60, "rug_delay300": r300, "clean": c0}
            report["sweep"].append(row)
            print(f"{trail:>6}{str(mh):>8} | "
                  f"{r0['total_pnl']:>13.0f}{r0['median_hold_min']:>8}{r0['median_margin_blk']:>10}"
                  f"{r0['fatal_rate']*100:>7.0f}%{r60['fatal_rate']*100:>9.0f}%{r300['fatal_rate']*100:>10.0f}%"
                  f" | {c0['total_pnl']:>13.0f}{c0['max_ret']*100:>7.0f}%{c0['median_ret']*100:>7.0f}%")

    json.dump(report, open(os.path.join(OUT_DIR, "trailing_param_sweep.json"), "w"), indent=2)
    print(f"\nwrote {OUT_DIR}/trailing_param_sweep.json")


if __name__ == "__main__":
    main()
