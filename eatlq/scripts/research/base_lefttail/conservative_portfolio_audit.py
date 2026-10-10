#!/usr/bin/env python3
"""Conservative portfolio audit: $500 wallet, no-compound USD, T=40/60%, QV2 vs fork,
walk-forward sizing modes (fixed / safe-dynamic / unsafe-dynamic). Offline, RPC-free."""
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

OUT = "/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624/conservative_portfolio_audit.json"
OUT_MD = "/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624/conservative_portfolio_audit_cn.md"

WALLET = 500.0
STAKE_CAP = 50.0
MAX_CONC = 3
MAX_DEPLOYED = STAKE_CAP * MAX_CONC  # $150
PORTFOLIO_FRAC = 0.35  # live env MAX_PORTFOLIO_FRACTION
BLOCK_S = 2.0
GAS = 0.15
HAIRCUT = 0.02
FRIC = -0.05
LN1P = K.LN1P
DYN = os.path.join(HERE, "_dynamic_size_realgas.json")
TEST_MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


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
    if new_exb == exb0:
        return base_ret
    return (1.0 + base_ret) * math.exp(lp_at(ps, new_exb, orient) - lp_at(ps, exb0, orient)) - 1.0


def adj_ret(raw):
    if raw is None:
        return None
    return float(raw) - GAS / STAKE_CAP - HAIRCUT  # gas normalized at $50 ref


DYN_CACHE = json.load(open(DYN)) if os.path.exists(DYN) else {}


def pick_dynamic_size(pool, depth_eth, ladder, max_usd, pool_share=0.05):
    """Causal: largest size in ladder passing friction + depth cap."""
    depth_usd = depth_eth * 1658.75
    cap = min(max_usd, pool_share * depth_usd) if depth_usd else max_usd
    dyn = DYN_CACHE
    for s in sorted(ladder, reverse=True):
        if s > cap:
            continue
        rec = dyn.get(f"{pool}:{int(s)}")
        if not rec or rec.get("fric") is None or rec.get("real_net") is None:
            continue
        if rec["fric"] < FRIC:
            continue
        ret = float(rec["real_net"]) - GAS / s - HAIRCUT
        return float(s), ret
    return None, None


def portfolio_sim(trades, wallet, stake_mode, max_conc=3, compound=False, max_deployed=None):
    """stake_mode: 'fixed50' | 'soft10pct' | 'per_trade_size' (each trade has .stake)
    compound=False: profits withdrawn; equity for sizing stays at wallet (except soft10pct uses wallet only)
    compound=True: equity grows with PnL for soft10pct sizing
    """
    if max_deployed is None:
        max_deployed = min(MAX_DEPLOYED, wallet * PORTFOLIO_FRAC)

    open_pos = []
    taken = []
    skipped_conc = skipped_cap = 0
    equity = wallet  # for compound mode
    realized = 0.0  # withdrawn profits tracker

    for t in trades:
        open_pos = [(xt, st) for xt, st in open_pos if xt > t["t0"]]
        deployed = sum(st for _, st in open_pos)
        if len(open_pos) >= max_conc:
            skipped_conc += 1
            continue
        if stake_mode == "fixed50":
            stake = STAKE_CAP
        elif stake_mode == "soft10pct":
            base = equity if compound else wallet
            stake = min(STAKE_CAP, base * 0.10)
            stake = max(stake, 25.0)  # floor $25
        elif stake_mode == "per_trade_size":
            stake = t.get("stake", STAKE_CAP)
        else:
            stake = STAKE_CAP
        if deployed + stake > max_deployed:
            skipped_cap += 1
            continue
        pnl = stake * t["ret"]
        if compound:
            equity += pnl
        else:
            realized += pnl
        taken.append({**t, "stake": stake, "pnl": pnl})
        hold_s = max((t.get("exb", t.get("exb0", 0)) - t.get("eb", 0)) * BLOCK_S, BLOCK_S) if t.get("eb") else 3600
        open_pos.append((t["t0"] + timedelta(seconds=hold_s), stake))

    if not taken:
        return {"n": 0}

    pnls = np.array([x["pnl"] for x in taken])
    stakes = np.array([x["stake"] for x in taken])
    rets = np.array([x["ret"] for x in taken])
    span = max((max(x["t0"] for x in taken) - min(x["t0"] for x in taken)).days, 1)
    total_pnl = float(pnls.sum())
    monthly_usd = total_pnl / span * 30.0
    monthly_pct_wallet = monthly_usd / wallet
    fatals = rets <= -0.5

    # peak deployed
    pts = []
    for x in taken:
        hold_s = max((x.get("exb", 0) - x.get("eb", 0)) * BLOCK_S, BLOCK_S) if x.get("eb") else 3600
        pts.append((x["t0"], x["stake"]))
        pts.append((x["t0"] + timedelta(seconds=hold_s), -x["stake"]))
    pts.sort(key=lambda z: (z[0], -abs(z[1])))
    cur = pk = 0.0
    for _, d in pts:
        cur += d
        pk = max(pk, cur)

    return {
        "n": len(taken), "skipped_concurrency": skipped_conc, "skipped_deploy_cap": skipped_cap,
        "span_days": span, "trades_per_month": len(taken) / span * 30,
        "total_pnl_usd": total_pnl, "monthly_usd": monthly_usd,
        "monthly_pct_on_wallet": monthly_pct_wallet,
        "fatal_rate": float(fatals.mean()), "fatal_n": int(fatals.sum()),
        "mean_ret": float(rets.mean()), "median_ret": float(np.median(rets)),
        "mean_stake": float(stakes.mean()), "max_stake": float(stakes.max()),
        "peak_deployed_usd": pk, "worst_single_pnl": float(pnls.min()),
        "compound": compound, "stake_mode": stake_mode,
    }


def build_trade_list(df, path, priced, gate_mask=None, lens="qv2", trail=0.35, maxhold_min=None):
    mh = None if maxhold_min is None else int(maxhold_min * 60 / 2)
    if gate_mask is None:
        gate_mask = pd.Series(True, index=df.index)
    rows = []
    for r in df[gate_mask & df.tradeable].itertuples(index=False):
        base = r.ret
        if base is None or (isinstance(base, float) and math.isnan(base)):
            continue
        ps = path.get(r.pool, [])
        pr = priced.get(r.pool) or {}
        if not ps or pr.get("exb") is None:
            continue
        exb0 = int(pr["exb"])
        orient = int(r.orient)
        eb = int(r.entry_block)
        entry_lp = r.last_tick_pre * LN1P * orient
        exb = exit_block_param(ps, entry_lp, orient, eb, trail, mh)
        ret = ret_rescale(float(base), ps, orient, exb0, exb) if (maxhold_min or trail != 0.35) else float(base)
        rows.append({
            "pool": r.pool, "label": r.label, "t0": pd.Timestamp(r.t0),
            "ret": ret, "exb0": exb0, "exb": exb, "eb": eb, "depth": float(r.entry_weth),
            "mon": pd.Timestamp(r.t0).strftime("%Y-%m"),
        })
    rows.sort(key=lambda x: x["t0"])
    return rows


def _unused_walkforward():
    pass  # walk-forward inline in main()


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE))
    df_base = L.load_base_df()
    dep, skel, creators, token_skel = L._load_blacklists()

    # dual lens
    df_qv2 = L.run_lens(df_base, "qv2")
    df_fork = L.run_lens(df_base, "fork")
    m = L.gate_masks(df_qv2, dep, skel, creators, token_skel)

    scenarios = {}

    # --- conservative $500 no compound ---
    for tag, df, lens in [("qv2", df_qv2, "QuoterV2"), ("fork", df_fork, "fork_realgas")]:
        for exit_tag, trail, mh in [("status_quo", 0.35, None), ("maxhold30", 0.30, 30)]:
            tr = build_trade_list(df, path, priced, m["live_full"], trail=trail, maxhold_min=mh)
            key = f"{tag}_{exit_tag}"
            scenarios[key] = {
                "lens": lens, "exit": exit_tag,
                "fixed50_no_compound": portfolio_sim(tr, WALLET, "fixed50", compound=False),
                "soft10pct_no_compound": portfolio_sim(tr, WALLET, "soft10pct", compound=False),
                "soft10pct_compound": portfolio_sim(tr, WALLET, "soft10pct", compound=True),
            }

    # dynamic sizing on fork (more honest)
    tr_fork = build_trade_list(df_fork, path, priced, m["live_full"])
    dyn_trades_safe, dyn_trades_unsafe = [], []
    for t in tr_fork:
        s, r = pick_dynamic_size(t["pool"], t["depth"], [50, 30, 25], max_usd=50)
        if s:
            dyn_trades_safe.append({**t, "stake": s, "ret": r})
        else:
            # fallback: fork ret at $50 (most live_full pools lack pool:50 cache)
            dyn_trades_safe.append({**t, "stake": STAKE_CAP, "ret": t["ret"]})
        s2, r2 = pick_dynamic_size(t["pool"], t["depth"], [1000, 300, 100, 50], max_usd=1000)
        if s2:
            dyn_trades_unsafe.append({**t, "stake": s2, "ret": r2})

    def _sim_or_empty(tr, **kw):
        r = portfolio_sim(tr, WALLET, "per_trade_size", compound=False, **kw)
        return r if r.get("n") else {"n": 0, "monthly_usd": 0, "note": "no trades"}

    scenarios["dynamic_safe_cap50"] = {
        "desc": "动态仓：优先 real-gas 多档报价；无缓存则退回 fork@$50；单笔≤$50+组合≤$150",
        "sim": portfolio_sim(dyn_trades_safe, WALLET, "per_trade_size", compound=False),
    }
    scenarios["dynamic_unsafe_cap1000"] = {
        "desc": "动态仓（高风险）：阶梯[1000,300,100,50]+5%池深+单笔≤$1000+无组合上限",
        "sim": _sim_or_empty(dyn_trades_unsafe, max_deployed=10000),
    }

    # QV2 vs fork comparison on same gates
    tr_q = build_trade_list(df_qv2, path, priced, m["live_full"])
    tr_f = build_trade_list(df_fork, path, priced, m["live_full"])
    qv2_fork_cmp = []
    pools_q = {t["pool"]: t["ret"] for t in tr_q}
    pools_f = {t["pool"]: t["ret"] for t in tr_f}
    common = set(pools_q) & set(pools_f)
    for p in common:
        qv2_fork_cmp.append({"pool": p, "qv2": pools_q[p], "fork": pools_f[p], "delta": pools_f[p] - pools_q[p]})
    deltas = np.array([x["delta"] for x in qv2_fork_cmp]) if qv2_fork_cmp else np.array([0.0])
    fatal_q = sum(1 for p in common if pools_q[p] <= -0.5)
    fatal_f = sum(1 for p in common if pools_f[p] <= -0.5)

    # T=40/60 from trail_frontier
    tf = json.load(open("/opt/EATLQ/mvp/audits/exit_logic_lookahead_20260624/trail_frontier.json"))
    t_rec = {40: tf["rule_R_by_T"].get(30, tf["rule_R_by_T"].get(40)),
             60: tf["rule_R_by_T"].get(60)}

    # walk-forward (fixed $50, live_full, fork lens)
    wf = []
    from sklearn.ensemble import HistGradientBoostingClassifier
    df = df_fork.copy()
    X, y = K.build(df)
    for tm in TEST_MONTHS:
        trm = (df.t0.dt.strftime("%Y-%m") < tm).values
        if trm.sum() < 100:
            continue
        clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
            l2_regularization=1.0, min_samples_leaf=20, random_state=0)
        clf.fit(X[trm], y[trm])
        df["p"] = clf.predict_proba(X)[:, 1]
        pc = df[trm & df.tradeable & df.gate_fric].p.quantile(0.6)
        mask = (df.t0.dt.strftime("%Y-%m") == tm) & m["live_full"] & df.tradeable & df.gate_fric & (df.p <= pc)
        ho = df[mask]
        tr = build_trade_list(ho, path, priced)
        if not tr:
            wf.append({"month": tm, "n": 0})
            continue
        s = portfolio_sim(tr, WALLET, "fixed50", compound=False)
        wf.append({"month": tm, **{k: s[k] for k in ("n", "total_pnl_usd", "fatal_rate", "monthly_usd")}})

    out = {
        "meta": {"wallet": WALLET, "stake_cap": STAKE_CAP, "max_concurrent": MAX_CONC,
                 "max_deployed": MAX_DEPLOYED, "portfolio_frac": PORTFOLIO_FRAC},
        "scenarios": scenarios,
        "qv2_vs_fork": {
            "common_pools": len(common),
            "fatal_qv2": fatal_q, "fatal_fork": fatal_f,
            "mean_delta_fork_minus_qv2": float(np.mean(deltas)),
            "median_delta": float(np.median(deltas)),
            "pct_fork_worse": float((deltas < -0.05).mean()),
            "pct_same": float((np.abs(deltas) < 0.02).mean()),
        },
        "trail_frontier_T": t_rec,
        "walkforward_fork_fixed50": wf,
    }
    json.dump(out, open(OUT, "w"), indent=2, default=str)

    # print summary
    print("=== $500 wallet, NO compound, absolute USD ===")
    for k in ["qv2_status_quo", "fork_status_quo", "qv2_maxhold30", "fork_maxhold30"]:
        s = scenarios[k]["fixed50_no_compound"]
        print(f"{k:22s} ${s['monthly_usd']:7.0f}/mo  fatal={s['fatal_rate']:.1%}  n={s['n']}  peak_dep=${s['peak_deployed_usd']:.0f}")
    print("\n=== compound comparison (fork status_quo) ===")
    for mode in ["fixed50_no_compound", "soft10pct_no_compound", "soft10pct_compound"]:
        s = scenarios["fork_status_quo"][mode]
        print(f"  {mode:25s} ${s['monthly_usd']:7.0f}/mo")
    print("\n=== dynamic sizing ===")
    for k in ["dynamic_safe_cap50", "dynamic_unsafe_cap1000"]:
        s = scenarios[k]["sim"]
        if s.get("n", 0) == 0:
            print(f"  {k}: no trades")
            continue
        print(f"  {k}: ${s['monthly_usd']:.0f}/mo mean_stake=${s['mean_stake']:.0f} max_stake=${s['max_stake']:.0f} peak_dep=${s['peak_deployed_usd']:.0f} fatal={s['fatal_rate']:.1%}")
    print("\n=== QV2 vs fork (live_full) ===")
    print(out["qv2_vs_fork"])
    print("\n=== walk-forward (fork, fixed $50) ===")
    for r in wf:
        print(f"  {r['month']}: n={r.get('n',0)} pnl=${r.get('total_pnl_usd',0):.0f} fatal={r.get('fatal_rate',0):.1%}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
