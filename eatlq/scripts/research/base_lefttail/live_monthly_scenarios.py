#!/usr/bin/env python3
"""Live monthly $ scenarios: $50 stake, max_concurrent=3.

Compares:
  A) Current live (F blacklist + TRAIL0.35 + 4h cap)
  B) +30min max-hold (TRAIL0.30/30min — trail_frontier winner for T<=40%)
  C) Relaxed gates (no F blacklist — 'give up rug blocking' for more volume)
  D) Rational live-adjusted fatal estimates (layers on top of backtest)

Offline, RPC-free. Reuses live_config_rebacktest gates + keystone tick path.
"""
from __future__ import annotations
import json, math, os, sys
from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K
import live_config_rebacktest as L

OUT = "/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624/live_monthly_scenarios.json"
STRESS = "/opt/EATLQ/mvp/audits/stress167_20260624/_stress167_cache.json"

STAKE = 50.0
MAX_CONC = 3
START_CAPITAL = 500.0   # ~wallet at go-live; max deployed = 3×$50 = $150
LN1P = K.LN1P
BLOCK_S = 2.0


def lp_at(ps, tb, orient):
    if not ps:
        return 0.0
    ch = None
    for _, lt, lb in ps:
        if lb <= tb:
            ch = (lt, lb)
        else:
            break
    if ch is None:
        ch = (ps[0][1], ps[0][2])
    return ch[0] * LN1P * orient


def exit_block_param(ps, entry_lp, orient, eb, trail, maxhold_blk):
    if not ps:
        return eb
    peak = entry_lp
    thr = math.log(1 - trail)
    for _, lt, lb in ps:
        lp = lt * LN1P * orient
        peak = max(peak, lp)
        if maxhold_blk is not None and (lb - eb) >= maxhold_blk:
            return lb
        if (lp - entry_lp) <= K.HARD_STOP:
            return lb
        if (lp - peak) <= thr:
            return lb
    return ps[-1][2]


def ret_rescale(base_ret, ps, orient, exb0, new_exb):
    """Tick-path ratio from planned exit exb0 to new exit (causal, no look-ahead)."""
    if new_exb == exb0:
        return base_ret
    lp0 = lp_at(ps, exb0, orient)
    lp1 = lp_at(ps, new_exb, orient)
    return (1.0 + base_ret) * math.exp(lp1 - lp0) - 1.0


def portfolio_sim(trades, start, stake, max_concurrent):
    """Fixed stake, concurrency cap, profits compound into equity (realistic small account)."""
    open_pos = []
    taken = []
    skipped = 0
    for t in trades:
        open_pos = [(xt, i) for xt, i in open_pos if xt > t["t0"]]
        if len(open_pos) >= max_concurrent:
            skipped += 1
            continue
        taken.append(t)
        open_pos.append((t["exit_t"], len(taken) - 1))

    if not taken:
        return {"n": 0}

    events = sorted([(t["exit_t"], stake * t["ret"]) for t in taken], key=lambda x: x[0])
    eq = start
    peak = start
    mdd_pct = 0.0
    mdd_usd = 0.0
    for _, pnl in events:
        eq += pnl
        peak = max(peak, eq)
        mdd_pct = min(mdd_pct, eq / peak - 1.0)
        mdd_usd = min(mdd_usd, eq - peak)

    rets = np.array([t["ret"] for t in taken])
    span = max((max(t["exit_t"] for t in taken) - min(t["t0"] for t in taken)).days, 1)
    monthly_pct = (eq / start) ** (30.0 / span) - 1.0 if eq > 0 else -1.0
    monthly_usd = (eq - start) / span * 30.0

    # peak concurrent deployed
    pts = []
    for t in taken:
        pts.append((t["t0"], 1))
        pts.append((t["exit_t"], -1))
    pts.sort(key=lambda x: (x[0], -x[1]))
    cur = peak_c = 0
    for _, d in pts:
        cur += d
        peak_c = max(peak_c, cur)

    fatals = rets <= -0.5
    return {
        "n": len(taken), "skipped_concurrency": skipped, "span_days": span,
        "start": start, "end": eq, "pnl_total": eq - start,
        "monthly_pct": monthly_pct, "monthly_usd": monthly_usd,
        "fatal_rate": float(fatals.mean()), "fatal_n": int(fatals.sum()),
        "win_rate": float((rets > 0).mean()), "mean_ret": float(rets.mean()),
        "median_ret": float(np.median(rets)), "mdd_pct": mdd_pct, "mdd_usd": mdd_usd,
        "peak_concurrent": peak_c, "peak_deployed_usd": peak_c * stake,
        "trades_per_month": len(taken) / span * 30.0,
    }


def build_trades(df, path, priced, gate_mask, ret_col="ret", trail=0.35, maxhold_min=None):
    mh_blk = None if maxhold_min is None else int(maxhold_min * 60 / 2)
    rows = []
    for r in df[gate_mask & df.tradeable].itertuples(index=False):
        if getattr(r, ret_col) is None or (isinstance(getattr(r, ret_col), float) and math.isnan(getattr(r, ret_col))):
            continue
        base = float(getattr(r, ret_col))
        ps = path.get(r.pool, [])
        if not ps:
            continue
        pr = priced.get(r.pool) or {}
        if pr.get("exb") is None:
            continue
        exb0 = int(pr["exb"])
        orient = int(r.orient)
        entry_lp = r.last_tick_pre * LN1P * orient
        new_exb = exit_block_param(ps, entry_lp, orient, int(r.entry_block), trail, mh_blk)
        ret = ret_rescale(base, ps, orient, exb0, new_exb) if maxhold_min is not None or trail != 0.35 else base
        hold_s = max((new_exb - int(r.entry_block)) * BLOCK_S, BLOCK_S)
        rows.append({
            "pool": r.pool, "label": r.label,
            "t0": pd.Timestamp(r.t0), "exit_t": pd.Timestamp(r.t0) + timedelta(seconds=hold_s),
            "ret": ret, "exb": new_exb, "exb0": exb0,
        })
    rows.sort(key=lambda x: x["t0"])
    return rows


def apply_delay_stress(trades, path, stress, delay=300):
    """For rug-labeled trades, replace ret with real-gas at exit+delay if past exb0."""
    out = []
    for t in trades:
        if t["label"] != "delayed_rug":
            out.append(dict(t))
            continue
        ps = path.get(t["pool"], [])
        # need orient — reload from minimal
        eff = t["exb"] + delay
        if eff <= t["exb0"]:
            lp0 = lp_at(ps, t["exb0"], 1)  # orient handled below
            out.append(dict(t))
            continue
        over = eff - t["exb0"]
        cand = min((60, 300, 600), key=lambda v: abs(v - over))
        rec = stress.get(f"{t['pool']}:exit_p{cand}")
        if rec and rec.get("status") == "ok" and rec.get("real_net") is not None:
            out.append({**t, "ret": float(rec["real_net"])})
        else:
            out.append(dict(t))
    return out


def rational_live_fatal(backtest_fatal, n_trades, scenario):
    """Layered estimate — not a backtest, explicit assumptions."""
    # Layer 1: backtest point estimate
    base = backtest_fatal
    notes = [f"backtest={base:.1%}"]

    if scenario == "live_full":
        # F blacklist in-sample: 0 rug traded in backtest; residual 2 fatals are clean crashes
        # New deployer: ~7-10 PARADAENGI-type / 8mo window → ~1-1.5/mo; if 15 trades/mo → ~7-10% of NEW unknown deployer trades fatal
        # Conservative: add 1-3pp for new-deployer first-shot + honeypot gap
        new_dep_pp = 0.02  # +2pp if ~1 fatal per 50 trades from unknown deployer/honeypot
        exec_pp = 0.00     # live_full blocks all known rug; execution delay only matters IF rug gets in
        reactive_pp = 0.01 # unmodeled reactive rug — directional only
        est = base + new_dep_pp + exec_pp + reactive_pp
        notes += ["+2pp new-deployer/honeypot (fastrug audit ~7-10/window uncaught by F)",
                  "+1pp reactive-rug (unmodeled, directional)",
                  "execution-delay N/A: 0 rug in backtest pass set"]
        lo, hi = max(base, 0.01), est + 0.03
    elif scenario == "no_f_blacklist":
        # 219 rug traded; execution delay @+300 historically ~49-67% on rug WINNERS
        # Full rug population fatal higher; backtest says 1.8% overall (6/339) at perfect timing
        exec_pp = 0.03  # +3pp if avg 2min exit delay on occasional rug
        new_dep_pp = 0.01
        reactive_pp = 0.02
        est = base + exec_pp + new_dep_pp + reactive_pp
        notes += ["+3pp execution-delay on rug subset",
                  "+1pp new deployer", "+2pp reactive rug"]
        lo, hi = base + 0.02, est + 0.05
    elif scenario == "maxhold30":
        base_mh = backtest_fatal
        exec_pp = 0.01  # 30min cap reduces delay cliff; stress167: fatal@+300 drops ~67%→20% ON RUG WINNERS
        new_dep_pp = 0.02
        est = base_mh + exec_pp + new_dep_pp
        notes += ["30min cap mitigates delay cliff on rug winners",
                  "+2pp new deployer/honeypot still applies"]
        lo, hi = max(base_mh, 0.01), est + 0.025
    else:
        est, lo, hi = base, base, base + 0.05
    return {"point": round(est, 4), "range_lo": round(lo, 4), "range_hi": round(hi, 4),
            "per_50_trades_fatal_n": round(est * 50, 1), "notes": notes}


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE))
    stress = json.load(open(STRESS)) if os.path.exists(STRESS) else {}
    df = L.run_lens(L.load_base_df(), "qv2")
    dep, skel, creators, token_skel = L._load_blacklists()
    m = L.gate_masks(df, dep, skel, creators, token_skel)

    scenarios = {}

    # A: current live
    tr_a = build_trades(df, path, priced, m["live_full"], trail=0.35, maxhold_min=None)
    sim_a = portfolio_sim(tr_a, START_CAPITAL, STAKE, MAX_CONC)
    scenarios["A_live_current"] = {
        "desc": "现状：F黑名单+深池3E+TRAIL0.35+4h上限，$50×并发3",
        "gates": "live_full", "sim": sim_a,
        "rational_fatal": rational_live_fatal(sim_a["fatal_rate"], sim_a["n"], "live_full"),
    }

    # B: +30min max-hold (TRAIL 0.30 per trail_frontier T<=40%)
    tr_b = build_trades(df, path, priced, m["live_full"], trail=0.30, maxhold_min=30)
    sim_b = portfolio_sim(tr_b, START_CAPITAL, STAKE, MAX_CONC)
    scenarios["B_maxhold_30min"] = {
        "desc": "+30min时间硬顶+TRAIL0.30（trail_frontier：真降延迟致命，砍~40%右尾）",
        "gates": "live_full", "sim": sim_b,
        "vs_A": {
            "monthly_usd_delta": sim_b["monthly_usd"] - sim_a["monthly_usd"],
            "monthly_pct_delta_pp": (sim_b["monthly_pct"] - sim_a["monthly_pct"]) * 100,
            "fatal_delta_pp": (sim_b["fatal_rate"] - sim_a["fatal_rate"]) * 100,
            "clean_pnl_retain": sim_b["pnl_total"] / sim_a["pnl_total"] if sim_a["pnl_total"] else None,
        },
        "rational_fatal": rational_live_fatal(sim_b["fatal_rate"], sim_b["n"], "maxhold30"),
    }

    # C: no F blacklist (more volume, accept rug exposure) — "放弃毁灭率控制" via gate relaxation
    tr_c = build_trades(df, path, priced, m["model_depth"], trail=0.35, maxhold_min=None)
    sim_c = portfolio_sim(tr_c, START_CAPITAL, STAKE, MAX_CONC)
    scenarios["C_no_f_blacklist"] = {
        "desc": "放宽F黑名单（仅模型+深池）：多交易量，回测rug可进场",
        "gates": "model_depth (no F blacklist)", "sim": sim_c,
        "rational_fatal": rational_live_fatal(sim_c["fatal_rate"], sim_c["n"], "no_f_blacklist"),
    }

    # D: no fatal control on EXIT — current trailing, no 30min, but keep F blacklist
    # Same as A for gates; stress with execution delay on any rug that slipped through
    tr_d = tr_a  # same trades
    sim_d = dict(sim_a)
    scenarios["D_no_exit_control"] = {
        "desc": "放弃出场侧控毁灭：现状TRAIL0.35无30min硬顶（=现状A），但估执行延迟+新deployer",
        "gates": "live_full", "sim": sim_d,
        "rational_fatal": rational_live_fatal(sim_d["fatal_rate"], sim_d["n"], "live_full"),
        "stress_note": "167 rug赢家 stress167：出场晚10min → 72%变致命；但live_full回测0 rug成交",
    }

    # E: no F blacklist + no 30min — worst case volume max, fatal max
    tr_e = tr_c
    sim_e = dict(sim_c)
    scenarios["E_worst_case"] = {
        "desc": "最差：无F黑名单+无30min硬顶",
        "gates": "model_depth", "sim": sim_e,
        "rational_fatal": rational_live_fatal(sim_e["fatal_rate"], sim_e["n"], "no_f_blacklist"),
    }

    # Also run at START=1000 for sensitivity
    sens = {}
    for name, tr in [("A", tr_a), ("B", tr_b), ("C", tr_c)]:
        sens[name] = portfolio_sim(tr, 1000.0, STAKE, MAX_CONC)

    # Label breakdown for A
    labels_a = defaultdict(int)
    fatals_a = []
    for t in tr_a:
        labels_a[t["label"]] += 1
        if t["ret"] <= -0.5:
            fatals_a.append({"pool": t["pool"], "label": t["label"], "ret": t["ret"]})

    out = {
        "meta": {
            "stake_usd": STAKE, "max_concurrent": MAX_CONC,
            "start_capital_default": START_CAPITAL,
            "lens": "QuoterV2 + friction/gas/haircut (optimistic vs real-gas on rug)",
            "not_modeled": ["reactive_rug", "sell_sim_live_only", "E_window", "MEV", "compounding_at_scale"],
            "data_span_pools": len(df),
            "live_full_pass": int(m["live_full"].sum()),
        },
        "scenarios": scenarios,
        "sensitivity_start_1000": sens,
        "A_label_breakdown": dict(labels_a),
        "A_fatals": fatals_a,
    }
    json.dump(out, open(OUT, "w"), indent=2, default=str)

    print(f"=== Live monthly scenarios: ${STAKE}/trade, concurrent={MAX_CONC}, start=${START_CAPITAL} ===")
    print(f"live_full pass={int(m['live_full'].sum())}  model_depth pass={int(m['model_depth'].sum())}")
    print(f"span={sim_a.get('span_days','?')}d  trades/mo~{sim_a.get('trades_per_month',0):.1f}")
    print()
    hdr = f"{'scenario':28s} {'n':>4} {'$/mo':>8} {'%/mo':>7} {'fatal%':>7} {'rational_fatal':>16} {'MDD$':>8}"
    print(hdr)
    for key in ["A_live_current", "B_maxhold_30min", "C_no_f_blacklist", "D_no_exit_control"]:
        s = scenarios[key]["sim"]
        rf = scenarios[key]["rational_fatal"]
        print(f"{key:28s} {s['n']:4d} ${s['monthly_usd']:7.0f} {s['monthly_pct']*100:+6.1f}% "
              f"{s['fatal_rate']*100:6.1f}% {rf['point']*100:6.1f}%[{rf['range_lo']*100:.0f}-{rf['range_hi']*100:.0f}] "
              f"${s['mdd_usd']:7.0f}")
    print(f"\nA fatal detail: {fatals_a}")
    print(f"A labels: {dict(labels_a)}")
    print(f"\nB vs A: monthly ${scenarios['B_maxhold_30min']['vs_A']['monthly_usd_delta']:+.0f}  "
          f"fatal {scenarios['B_maxhold_30min']['vs_A']['fatal_delta_pp']:+.1f}pp  "
          f"pnl retain {scenarios['B_maxhold_30min']['vs_A']['clean_pnl_retain']:.0%}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
