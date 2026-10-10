#!/usr/bin/env python3
"""
P2 — small-position right-tail execution simulation (real slippage, no sample dropping).

For every pool in a tier (active_min >= thr), enter a fixed tiny position at
decision point (birth+30min) and simulate a runner with partial take-profits,
death handling, and honeypot (can't-sell) handling. Every entry produces a PnL;
nothing is dropped.

Execution model (honest):
  ENTRY  : buy $size of alt with WETH using entry-bar sqrtP+liquidity (V3 impact + LP fee + gas).
  RUNNER : when MID multiple first crosses a target, sell that fraction of the ORIGINAL
           position using THAT bar's sqrtP+liquidity (real slippage at the pump moment).
  DEATH  : trading gap > gap-min OR end-of-72h -> liquidate remainder at last valid bar.
  HONEYPOT: pool never has any successful sell (wsell==0 forever) -> entire position lost.
  Can't-sell at liquidation (liquidity ~0) -> remainder lost (-100% of remainder).

Outputs per tier x position-size, split by OOS birth time: EV, PF, win rate, -100% rate,
right-tail profit concentration, realized-PnL max drawdown, median hold hours.

Reuses the forward-path fetch from righttail_label_substrate.py.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from righttail_label_substrate import fetch  # noqa: E402

Q96 = 2 ** 96
MULT_CAP = 1000.0


def sgn(t, weth_t1):
    return t if weth_t1 else -t


def mult(signed_tick, entry_signed):
    d = max(-70000, min(70000, signed_tick - entry_signed))
    return min(MULT_CAP, math.pow(1.0001, d))


def v3_buy(weth_in, sp_x96, liq, weth_t1, fee_meta):
    if liq <= 0 or sp_x96 <= 0 or weth_in <= 0:
        return 0.0
    amt = weth_in * (1.0 - fee_meta / 10000.0)
    sp = sp_x96 / Q96
    if weth_t1:
        spn = sp + amt / liq
        return liq * (1.0 / sp - 1.0 / spn)
    spn = (liq * sp) / (liq + amt * sp)
    return liq * (sp - spn)


def v3_sell(alt_in, sp_x96, liq, weth_t1, fee_meta):
    if liq <= 0 or sp_x96 <= 0 or alt_in <= 0:
        return 0.0
    amt = alt_in * (1.0 - fee_meta / 10000.0)
    sp = sp_x96 / Q96
    if weth_t1:
        spn = (liq * sp) / (liq + amt * sp)
        return liq * (sp - spn)
    spn = sp + amt / liq
    return liq * (1.0 / sp - 1.0 / spn)


def simulate_pool(bl, birth, weth_t1, fee, dp, size_wei, gas_wei, gap_sec,
                  targets, moonbag_frac, trail, strict=False):
    """Return (ret_on_size, exit_kind, hold_sec, peak_mult, capture_ratio) or None.

    Exit: partial TP at `targets`; a `moonbag_frac` tranche rides with a trailing
    stop (sell when price retraces `trail` from peak, only armed after 2x).
    """
    pre = [b for b in bl if (b["m"] < dp if strict else b["m"] <= dp)]
    if not pre:
        return None
    eb = pre[-1]
    try:
        sp_in = float(eb["sp"]); l_in = float(eb["liq"])
    except (ValueError, TypeError):
        return None
    alt0 = v3_buy(size_wei, sp_in, l_in, weth_t1, fee)
    if alt0 <= 0:
        return None

    fwd = [b for b in bl if b["m"] > dp]
    entry_signed = sgn(eb["last_tick"], weth_t1)
    pool_ever_sellable = any(b["wsell"] > 0 for b in fwd)

    if not pool_ever_sellable:
        tl = max(-1.05, (-size_wei - gas_wei) / size_wei)
        return tl, "honeypot_total_loss", 24 * 3600, 1.0, 0.0, tl

    # mid-value of held tokens at entry (for the no-exit-slippage ceiling)
    spr0 = sp_in / Q96
    base0 = alt0 * (spr0 * spr0) if weth_t1 else alt0 / (spr0 * spr0)

    proceeds = 0.0
    ideal_proceeds = 0.0
    fee_frac = 1.0 - fee / 10000.0
    remaining = alt0
    moonbag = alt0 * moonbag_frac      # rides with trailing stop
    tp_pool = alt0 - moonbag           # available for fixed TP targets
    tp_sold = 0.0
    last_valid = None
    exit_kind = "horizon"
    hold_end = dp
    ti = 0
    prev_m = dp
    peak = 1.0
    moon_open = moonbag > 0

    for b in fwd:
        if b["m"] - prev_m > gap_sec:
            exit_kind = "death_gap"
            break
        prev_m = b["m"]
        last_valid = b
        cur = mult(sgn(b["last_tick"], weth_t1), entry_signed)
        peak = max(peak, cur)
        # fixed take-profits from the tp_pool
        while ti < len(targets) and cur >= targets[ti][0] and (tp_pool - tp_sold) > 0:
            sell_alt = min(alt0 * targets[ti][1], tp_pool - tp_sold)
            w = v3_sell(sell_alt, float(b["sp"]), float(b["liq"]), weth_t1, fee)
            if w > 0:
                proceeds += w - gas_wei
                ideal_proceeds += (sell_alt / alt0) * base0 * cur * fee_frac
                remaining -= sell_alt; tp_sold += sell_alt
                hold_end = b["m"]
            ti += 1
        # moonbag trailing stop (armed only after a 2x to avoid noise)
        if moon_open and peak >= 2.0 and cur <= peak * (1.0 - trail):
            w = v3_sell(moonbag, float(b["sp"]), float(b["liq"]), weth_t1, fee)
            if w > 0:
                proceeds += w - gas_wei
                ideal_proceeds += (moonbag / alt0) * base0 * cur * fee_frac
                remaining -= moonbag; moon_open = False
                hold_end = b["m"]
                exit_kind = "moonbag_trail"

    # liquidate any remainder (moonbag never stopped, or leftover tp) at last bar
    if remaining > 1e-30:
        lb = last_valid if last_valid is not None else (fwd[-1] if fwd else None)
        if lb is not None:
            cur_l = mult(sgn(lb["last_tick"], weth_t1), entry_signed)
            w = v3_sell(remaining, float(lb["sp"]), float(lb["liq"]), weth_t1, fee)
            if w > 0:
                proceeds += w - gas_wei
                ideal_proceeds += (remaining / alt0) * base0 * cur_l * fee_frac
                hold_end = lb["m"]; remaining = 0.0
        if remaining > 1e-30:
            exit_kind = "death_no_exit"

    net = proceeds - size_wei - gas_wei
    ret = max(-1.05, net / size_wei)
    ret_ideal = max(-1.05, (ideal_proceeds - size_wei - gas_wei) / size_wei)
    capture = round((ret + 1.0) / peak, 4) if peak > 0 else 0.0
    return ret, exit_kind, max(60, hold_end - dp), round(peak, 2), capture, ret_ideal


def portfolio_stats(recs):
    """recs: list of (ret, kind, hold_sec, peak, capture)."""
    if not recs:
        return {"n": 0}
    rets = [r[0] for r in recs]; holds = [r[2] for r in recs]
    ideals = [r[5] for r in recs]
    captures = [r[4] for r in recs if r[3] >= 5.0]  # capture on real movers (peak>=5x)
    n = len(rets)
    pos = sum(r for r in rets if r > 0)
    neg = abs(sum(r for r in rets if r < 0))
    gross_wins = sorted([r for r in rets if r > 0], reverse=True)
    total_win = sum(gross_wins) or 1e-9
    eq = 0.0; peak = 0.0; mdd = 0.0
    for r in rets:
        eq += r; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    return {
        "n": n,
        "EV_per_trade": round(sum(rets) / n, 4),
        "EV_per_trade_IDEAL_no_exit_slip": round(sum(ideals) / n, 4),
        "win_rate": round(sum(1 for r in rets if r > 0) / n, 4),
        "pf": round(pos / neg, 4) if neg > 0 else None,
        "total_loss_-100%_rate": round(sum(1 for r in rets if r <= -0.99) / n, 4),
        "realized_ge2x_rate": round(sum(1 for r in rets if r >= 1.0) / n, 4),
        "realized_ge5x_rate": round(sum(1 for r in rets if r >= 4.0) / n, 4),
        "realized_ge10x_rate": round(sum(1 for r in rets if r >= 9.0) / n, 4),
        "max_realized_mult": round(max(rets) + 1, 2),
        "top1_win_share": round(gross_wins[0] / total_win, 4) if gross_wins else 0,
        "top5_win_share": round(sum(gross_wins[:5]) / total_win, 4) if gross_wins else 0,
        "median_capture_of_paperpeak_movers": round(median(captures), 4) if captures else None,
        "realized_pnl_maxdd_units": round(mdd, 4),
        "median_hold_hours": round(median(holds) / 3600, 2),
        "cum_pnl_units": round(sum(rets), 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--born-before", default="2026-05-22")
    ap.add_argument("--obs-min", type=int, default=30)
    ap.add_argument("--gap-min", type=int, default=90)
    ap.add_argument("--gas-usd", type=float, default=0.05)
    ap.add_argument("--eth-usd", type=float, default=1665.0)
    ap.add_argument("--trail", type=float, default=0.5, help="moonbag trailing stop retrace frac")
    ap.add_argument("--strict-entry", action="store_true", help="use only fully-completed minutes before dp (no boundary leak)")
    ap.add_argument("--tiers", default="3,6,15")
    ap.add_argument("--sizes-usd", default="3,5,10")
    args = ap.parse_args()

    print(f"[p2] fetch forward paths ...", flush=True)
    pools, meta = fetch(args.days, args.born_before, 72)
    print(f"[p2] pools={len(pools):,}", flush=True)

    obs = args.obs_min * 60
    gap_sec = args.gap_min * 60
    targets = [(2.0, 0.5), (5.0, 0.25)]   # 25% left as moonbag
    moonbag_frac = 0.25
    trail = args.trail

    # precompute active_min and decision-point per pool
    prep = {}
    for p, bl in pools.items():
        bl.sort(key=lambda x: x["m"])
        birth = meta[p]["birth"]; dp = birth + obs
        pre = [b for b in bl if (b["m"] < dp if args.strict_entry else b["m"] <= dp)]
        prep[p] = {"bl": bl, "birth": birth, "dp": dp,
                   "weth_t1": bool(meta[p]["weth_t1"]), "fee": meta[p]["fee_bps"],
                   "active_min": len(pre)}

    tiers = [int(x) for x in args.tiers.split(",")]
    sizes = [float(x) for x in args.sizes_usd.split(",")]
    gas_wei = (args.gas_usd / args.eth_usd) * 1e18

    out = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "params": vars(args),
           "exit_model": f"TP 2x:50% 5x:25%; moonbag 25% trailing -{trail:.0%}; gap_min death; honeypot=total loss",
           "results": {}}

    for thr in tiers:
        members = sorted([p for p, d in prep.items() if d["active_min"] >= thr],
                         key=lambda p: prep[p]["birth"])
        for size_usd in sizes:
            size_wei = (size_usd / args.eth_usd) * 1e18
            recs, kinds = [], defaultdict(int)
            for p in members:
                d = prep[p]
                r = simulate_pool(d["bl"], d["birth"], d["weth_t1"], d["fee"],
                                  d["dp"], size_wei, gas_wei, gap_sec, targets, moonbag_frac, trail,
                                  strict=args.strict_entry)
                if r is None:
                    continue
                recs.append((d["birth"], r)); kinds[r[1]] += 1
            recs.sort(key=lambda x: x[0])
            rr = [x[1] for x in recs]
            nn = len(rr)
            key = f"tier_active>={thr}__size_${size_usd:g}"
            out["results"][key] = {
                "ALL": portfolio_stats(rr),
                "recent_oos_newest25": portfolio_stats(rr[nn * 3 // 4:]),
                "exit_kinds": dict(kinds),
            }

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
