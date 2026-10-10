#!/usr/bin/env python3
"""Re-backtest CURRENT aggressive live config (2026-06-25 go-live), dual lens, RPC-free.

Live config mirrored:
  - F LP-pull blacklist OFF (245 deployer + 33 skeleton removed)
  - PARADAENGI 2 honeypot deployer seeds KEPT
  - TRAIL=0.25, no 30min max-hold, 4h time-exit path only
  - MIN_PRE>=5, p_fatal<=0.27, depth>=3 ETH, friction>=-5%
  - $50 fixed stake (0.0302 ETH ref), max_concurrent=3, $500 wallet, no compounding

Lenses (same gates, different PnL ground truth):
  A) QuoterV2 — _priced_live.json net + tick-path exit rescale to TRAIL=0.25
  B) Fork real-gas — _realgas_replay.json real_net + same exit rescale

Not replayed (live-only): sell-sim, external-risk, E-window 1800s, MEV, reactive rug.
Zero archive RPC — reuses _priced_live.json, _realgas_replay.json, _fwdpath.tsv.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/aggressive_live_rebacktest_20260625"

# ── current aggressive live (2026-06-25) ──
MIN_PRE = 5
P_CUT = 0.27
MIN_DEPTH_ETH = 3.0
FRIC_THR = L.FRIC_THR
TRAIL = 0.25
MAXHOLD_MIN = None
SIZE_ETH = 0.0302
ETH_USD_REF = 1658.75
SIZE_USD = SIZE_ETH * ETH_USD_REF
GAS_USD_RT = L.GAS_USD_RT
HAIRCUT = L.HAIRCUT
WALLET = 500.0
STAKE = 50.0
MAX_CONC = 3
MAX_DEPLOYED = STAKE * MAX_CONC
LN1P = K.LN1P
BLOCK_S = 2.0

PARADAENGI_DEPLOYERS = L.PARADAENGI_DEPLOYERS

# walk-forward OOS months (train t0 < month; first month when train n >= MIN_TRAIN_WF)
MIN_TRAIN_WF = 100
TEST_MONTHS_WF = [
    "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
]
AS_OF_DATE = "2026-06-25"  # partial June fill target (day 25 of month 6 in WF window)


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
        return float(base_ret)
    lp0 = lp_at(ps, exb0, orient)
    lp1 = lp_at(ps, new_exb, orient)
    return (1.0 + base_ret) * math.exp(lp1 - lp0) - 1.0


def aggressive_gate(df, creators: dict) -> pd.Series:
    """Model + depth + friction; F rug list OFF; only 2 PARADAENGI deployers blocked."""
    model = (df.ns_pre >= MIN_PRE) & (df.p <= P_CUT)
    depth = df.entry_weth >= MIN_DEPTH_ETH
    para = df.token.map(
        lambda t: (creators.get(t) or {}).get("creator", "").lower() in PARADAENGI_DEPLOYERS
    )
    return model & depth & ~para & df.gate_fric


def build_trades(df, path, priced, gate_mask, trail=TRAIL, maxhold_min=MAXHOLD_MIN):
    mh = None if maxhold_min is None else int(maxhold_min * 60 / 2)
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
        ret = ret_rescale(float(base), ps, orient, exb0, exb)
        rows.append({
            "pool": r.pool,
            "label": r.label,
            "mechanism": getattr(r, "mechanism", None),
            "t0": pd.Timestamp(r.t0),
            "ret": ret,
            "raw_base": float(base),
            "exb0": exb0,
            "exb": exb,
            "eb": eb,
            "depth_eth": float(r.entry_weth),
            "p_fatal": float(r.p),
        })
    rows.sort(key=lambda x: x["t0"])
    return rows


def portfolio_sim(trades):
    open_pos = []
    taken = []
    skipped_conc = skipped_cap = 0
    for t in trades:
        open_pos = [(xt, st) for xt, st in open_pos if xt > t["t0"]]
        deployed = sum(st for _, st in open_pos)
        if len(open_pos) >= MAX_CONC:
            skipped_conc += 1
            continue
        if deployed + STAKE > MAX_DEPLOYED:
            skipped_cap += 1
            continue
        pnl = STAKE * t["ret"]
        taken.append({**t, "stake": STAKE, "pnl_usd": pnl})
        hold_s = max((t["exb"] - t["eb"]) * BLOCK_S, BLOCK_S)
        open_pos.append((t["t0"] + timedelta(seconds=hold_s), STAKE))

    if not taken:
        return {"n": 0, "skipped_concurrency": skipped_conc, "skipped_deploy_cap": skipped_cap}

    pnls = np.array([x["pnl_usd"] for x in taken])
    rets = np.array([x["ret"] for x in taken])
    span = max((max(x["t0"] for x in taken) - min(x["t0"] for x in taken)).days, 1)
    fatals = rets <= -0.5
    labels = Counter(x["label"] for x in taken)
    dr = sum(1 for x in taken if x["label"] == "delayed_rug")
    clean = sum(1 for x in taken if x["label"] == "clean")

    return {
        "n": len(taken),
        "skipped_concurrency": skipped_conc,
        "skipped_deploy_cap": skipped_cap,
        "span_days": span,
        "trades_per_month": len(taken) / span * 30,
        "total_pnl_usd": float(pnls.sum()),
        "monthly_usd": float(pnls.sum()) / span * 30,
        "monthly_pct_wallet": float(pnls.sum()) / span * 30 / WALLET,
        "fatal_rate": float(fatals.mean()),
        "fatal_n": int(fatals.sum()),
        "mean_ret": float(rets.mean()),
        "median_ret": float(np.median(rets)),
        "win_rate": float((rets > 0).mean()),
        "worst_trade_usd": float(pnls.min()),
        "best_trade_usd": float(pnls.max()),
        "labels": dict(labels),
        "delayed_rug_n": dr,
        "clean_n": clean,
    }


def pool_metrics(trades):
    if not trades:
        return {}
    rets = np.array([t["ret"] for t in trades])
    return {
        "n": len(trades),
        "fatal_rate": float((rets <= -0.5).mean()),
        "fatal_n": int((rets <= -0.5).sum()),
        "mean_ret": float(rets.mean()),
        "total_pnl_at_50": float(rets.sum() * STAKE),
    }


def qv2_vs_fork(tr_q, tr_f):
    qmap = {t["pool"]: t for t in tr_q}
    fmap = {t["pool"]: t for t in tr_f}
    common = sorted(set(qmap) & set(fmap))
    rows = []
    for p in common:
        q, f = qmap[p], fmap[p]
        rows.append({
            "pool": p,
            "label": q["label"],
            "qv2_ret": q["ret"],
            "fork_ret": f["ret"],
            "delta_fork_minus_qv2": f["ret"] - q["ret"],
            "qv2_fatal": q["ret"] <= -0.5,
            "fork_fatal": f["ret"] <= -0.5,
        })
    if not rows:
        return {"common_n": 0}
    d = pd.DataFrame(rows)
    return {
        "common_n": len(rows),
        "qv2_fatal_n": int(d.qv2_fatal.sum()),
        "fork_fatal_n": int(d.fork_fatal.sum()),
        "mean_delta": float(d.delta_fork_minus_qv2.mean()),
        "median_delta": float(d.delta_fork_minus_qv2.median()),
        "pct_fork_worse_5pp": float((d.delta_fork_minus_qv2 < -0.05).mean()),
        "pct_same_2pp": float((d.delta_fork_minus_qv2.abs() < 0.02).mean()),
        "qv2_ok_fork_bad": d[(d.qv2_ret > -0.5) & (d.fork_ret <= -0.5)][
            ["pool", "label", "qv2_ret", "fork_ret"]
        ].to_dict("records"),
        "qv2_bad_fork_ok": d[(d.qv2_ret <= -0.5) & (d.fork_ret > -0.5)][
            ["pool", "label", "qv2_ret", "fork_ret"]
        ].to_dict("records"),
    }


def _month_elapsed_days(month: str, as_of: pd.Timestamp) -> tuple[int, int]:
    """Return (elapsed calendar days month-start..min(as_of, month-end), month length)."""
    ym, yd = month.split("-")
    start = pd.Timestamp(f"{ym}-{yd}-01")
    end = start + pd.offsets.MonthEnd(0)
    elapsed = (min(as_of, end) - start).days + 1
    return int(elapsed), int(end.day)


def _days_observed(df: pd.DataFrame, mask: pd.Series) -> int:
    ho = df[mask]
    return int(ho.t0.dt.date.nunique()) if len(ho) else 0


def walkforward_monthly(df, path, priced, creators, lens: str, as_of: str = AS_OF_DATE):
    """Expanding-window OOS by calendar month; dual actual + filled-to-as_of columns."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    as_of_ts = pd.Timestamp(as_of)
    X, y = K.build(df)
    rows = []
    prev_filled_daily = None

    for tm in TEST_MONTHS_WF:
        trm = (df.t0.dt.strftime("%Y-%m") < tm).values
        if trm.sum() < MIN_TRAIN_WF:
            continue

        clf = HistGradientBoostingClassifier(
            max_iter=300, max_depth=4, learning_rate=0.05,
            l2_regularization=1.0, min_samples_leaf=20, random_state=0,
        )
        clf.fit(X[trm], y[trm])
        dfc = df.copy()
        dfc["p"] = clf.predict_proba(X)[:, 1]
        gate = aggressive_gate(dfc, creators)
        month_mask = (dfc.t0.dt.strftime("%Y-%m") == tm) & gate & dfc.tradeable
        d_obs = _days_observed(dfc, month_mask)
        elapsed, cal_days = _month_elapsed_days(tm, as_of_ts)

        tr = build_trades(dfc, path, priced, month_mask)
        sim = portfolio_sim(tr) if tr else {"n": 0, "total_pnl_usd": 0.0, "fatal_rate": 0.0}

        pnl = float(sim.get("total_pnl_usd", 0.0))
        span = max(int(sim.get("span_days", 0)), 1)
        # Scale observed PnL to elapsed-to-as_of when sampling covers fewer days.
        if sim.get("n", 0) and d_obs > 0 and d_obs < elapsed:
            pnl_filled = pnl * (elapsed / d_obs)
            fill_method = "linear_days_observed"
        elif sim.get("n", 0):
            pnl_filled = pnl
            fill_method = "actual"
        elif prev_filled_daily is not None and tm.endswith("-06"):
            pnl_filled = prev_filled_daily * elapsed
            fill_method = "projected_from_prior_month_daily"
        else:
            pnl_filled = 0.0
            fill_method = "no_data"

        monthly_raw = float(sim.get("monthly_usd", 0.0))
        monthly_filled = pnl_filled / max(elapsed, 1) * 30

        if sim.get("n", 0) and elapsed:
            prev_filled_daily = pnl_filled / elapsed

        ho = dfc[month_mask]
        day_range = None
        if len(ho):
            day_range = f"{ho.t0.min().date()}..{ho.t0.max().date()}"

        rows.append({
            "month": tm,
            "lens": lens,
            "candidates": int(month_mask.sum()),
            "days_observed": d_obs,
            "elapsed_to_asof": elapsed,
            "month_calendar_days": cal_days,
            "day_range": day_range,
            "n": int(sim.get("n", 0)),
            "total_pnl_usd": pnl,
            "monthly_usd_raw": monthly_raw,
            "total_pnl_usd_filled": float(pnl_filled),
            "monthly_usd_filled_30d": float(monthly_filled),
            "fill_method": fill_method,
            "fatal_rate": float(sim.get("fatal_rate", 0.0)),
            "fatal_n": int(sim.get("fatal_n", 0)),
            "delayed_rug_n": int(sim.get("delayed_rug_n", 0)),
            "clean_n": int(sim.get("clean_n", 0)),
            "skipped_concurrency": int(sim.get("skipped_concurrency", 0)),
        })
    return rows


def calendar_month_insample(trades, df, gate_mask, as_of: str = AS_OF_DATE):
    """In-sample trade list bucketed by t0 month (reference; not OOS)."""
    as_of_ts = pd.Timestamp(as_of)
    months = sorted({t["t0"].strftime("%Y-%m") for t in trades})
    rows = []
    for tm in months:
        sub = [t for t in trades if t["t0"].strftime("%Y-%m") == tm]
        sim = portfolio_sim(sub)
        month_mask = (df.t0.dt.strftime("%Y-%m") == tm) & gate_mask & df.tradeable
        d_obs = _days_observed(df, month_mask)
        elapsed, cal_days = _month_elapsed_days(tm, as_of_ts)
        pnl = float(sim.get("total_pnl_usd", 0.0))
        if d_obs > 0 and d_obs < elapsed:
            pnl_filled = pnl * (elapsed / d_obs)
            fill_method = "linear_days_observed"
        else:
            pnl_filled = pnl
            fill_method = "actual"
        rows.append({
            "month": tm,
            "candidates": int(month_mask.sum()),
            "days_observed": d_obs,
            "elapsed_to_asof": elapsed,
            "n": int(sim.get("n", 0)),
            "total_pnl_usd": pnl,
            "total_pnl_usd_filled": float(pnl_filled),
            "monthly_usd_filled_30d": float(pnl_filled / max(elapsed, 1) * 30),
            "fill_method": fill_method,
            "fatal_n": int(sim.get("fatal_n", 0)),
            "delayed_rug_n": int(sim.get("delayed_rug_n", 0)),
        })
    return rows


def gate_breakdown(df, creators, token_skel):
    dep, skel, _, _ = L._load_blacklists()
    m = L.gate_masks(df, dep, skel, creators, token_skel)
    agg = aggressive_gate(df, creators)
    return {
        "labeled_universe": len(df),
        "model_pre_pfatal": int(((df.ns_pre >= MIN_PRE) & (df.p <= P_CUT)).sum()),
        "plus_depth_ge3": int(m["model_depth"].sum()),
        "old_live_full_with_F": int(m["live_full"].sum()),
        "aggressive_live_no_F": int(agg.sum()),
        "blocked_paradaengi_only": int((m["model_depth"] & ~agg).sum()),
        "blocked_depth": int(m["blocked_depth"].sum()),
        "blocked_fric": int((m["model_depth"] & ~df.gate_fric).sum()),
        "extra_vs_old_F": int(agg.sum()) - int(m["live_full"].sum()),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE))
    df_base = L.load_base_df()
    dep, skel, creators, token_skel = L._load_blacklists()

    df_qv2 = L.run_lens(df_base, "qv2")
    df_fork = L.run_lens(df_base, "fork")
    gate_q = aggressive_gate(df_qv2, creators)
    gate_f = aggressive_gate(df_fork, creators)

    tr_q = build_trades(df_qv2, path, priced, gate_q)
    tr_f = build_trades(df_fork, path, priced, gate_f)

    sim_q = portfolio_sim(tr_q)
    sim_f = portfolio_sim(tr_f)
    cmp = qv2_vs_fork(tr_q, tr_f)

    # reference: old config (F on + TRAIL 0.35) for contrast
    m_old = L.gate_masks(df_qv2, dep, skel, creators, token_skel)
    tr_old = build_trades(df_qv2, path, priced, m_old["live_full"], trail=0.35)
    sim_old = portfolio_sim(tr_old)

    wf_q = walkforward_monthly(df_qv2, path, priced, creators, "qv2")
    wf_f = walkforward_monthly(df_fork, path, priced, creators, "fork")
    cal_q = calendar_month_insample(tr_q, df_qv2, gate_q)

    report = {
        "meta": {
            "ts": pd.Timestamp.now("UTC").isoformat(),
            "config": {
                "F_lp_pull_blacklist": "OFF (245 deployer + 33 skeleton)",
                "paradaengi_honeypot_deployers": "ON (2 EOAs)",
                "TRAIL": TRAIL,
                "maxhold_min": MAXHOLD_MIN,
                "MIN_PRE": MIN_PRE,
                "P_CUT": P_CUT,
                "MIN_DEPTH_ETH": MIN_DEPTH_ETH,
                "FRIC_THR": FRIC_THR,
                "SIZE_ETH": SIZE_ETH,
                "SIZE_USD_ref": SIZE_USD,
                "WALLET_USD": WALLET,
                "STAKE_USD": STAKE,
                "MAX_CONCURRENT": MAX_CONC,
                "MAX_DEPLOYED_USD": MAX_DEPLOYED,
                "compounding": False,
                "GAS_USD_RT": GAS_USD_RT,
                "HAIRCUT": HAIRCUT,
            },
            "rpc_calls": 0,
            "caches": [K.PCACHE, os.path.join(HERE, "_realgas_replay.json")],
            "not_replayed": [
                "sell_sim", "external_risk", "E_window_1800s", "MEV",
                "reactive_rug", "execution_delay",
            ],
        },
        "gate_breakdown": gate_breakdown(df_qv2, creators, token_skel),
        "lens_qv2": {"portfolio": sim_q, "pool_level": pool_metrics(tr_q)},
        "lens_fork": {"portfolio": sim_f, "pool_level": pool_metrics(tr_f)},
        "qv2_vs_fork": cmp,
        "reference_old_live_F_on_TRAIL035_qv2": sim_old,
        "fatals_qv2": [
            {k: t[k] for k in ("pool", "label", "ret", "depth_eth", "p_fatal")}
            | {"pnl_usd": t["ret"] * STAKE}
            for t in tr_q if t["ret"] <= -0.5
        ],
        "fatals_fork": [
            {k: t[k] for k in ("pool", "label", "ret", "depth_eth", "p_fatal")}
            | {"pnl_usd": t["ret"] * STAKE}
            for t in tr_f if t["ret"] <= -0.5
        ],
        "walkforward_monthly": {
            "as_of_date": AS_OF_DATE,
            "test_months": TEST_MONTHS_WF,
            "min_train_pools": MIN_TRAIN_WF,
            "fill_note": (
                "partial months: total_pnl_usd_filled = pnl * (elapsed_to_asof / days_observed); "
                "monthly_usd_filled_30d = filled / elapsed * 30. "
                "2026-06 OOS n=0 (no gated pools in labeled set) → projected from prior month daily rate."
            ),
            "qv2": wf_q,
            "fork": wf_f,
            "qv2_vs_fork_identical": wf_q == wf_f,
        },
        "calendar_month_insample_qv2": cal_q,
    }
    out_json = os.path.join(OUT_DIR, "aggressive_live_rebacktest.json")
    wf_json = os.path.join(OUT_DIR, "walkforward_monthly.json")
    json.dump(report, open(out_json, "w"), indent=2, default=str)
    json.dump(report["walkforward_monthly"], open(wf_json, "w"), indent=2, default=str)

    print("=== AGGRESSIVE LIVE REBACKTEST (RPC-free) ===")
    gb = gate_breakdown(df_qv2, creators, token_skel)
    print(f"gate pass: aggressive={gb['aggressive_live_no_F']}  "
          f"old_F_on={gb['old_live_full_with_F']}")
    for tag, sim in [("QV2", sim_q), ("FORK", sim_f), ("OLD_F+TRAIL0.35(QV2)", sim_old)]:
        if sim.get("n", 0) == 0:
            print(f"  {tag}: no trades")
            continue
        print(f"  {tag:22s} n={sim['n']:3d}  ${sim['monthly_usd']:7.0f}/mo  "
              f"fatal={sim['fatal_rate']:.1%}({sim['fatal_n']})  "
              f"dr={sim.get('delayed_rug_n',0)} clean={sim.get('clean_n',0)}  "
              f"skip_conc={sim.get('skipped_concurrency',0)}")
    print(f"  QV2 vs FORK common={cmp.get('common_n',0)}  "
          f"fatal qv2/fork={cmp.get('qv2_fatal_n','?')}/{cmp.get('fork_fatal_n','?')}  "
          f"meanΔ={cmp.get('mean_delta',0):+.3f}")
    print("\n=== walk-forward OOS (fixed p<=0.27, F off, TRAIL 0.25) ===")
    print(f"  as_of={AS_OF_DATE}  fill: partial days → elapsed; Jun n=0 → prior-month daily proj")
    for r in wf_q:
        print(f"  {r['month']}: n={r['n']:3d}  pnl=${r['total_pnl_usd']:7.0f}  "
              f"filled=${r['total_pnl_usd_filled']:7.0f}  "
              f"mo_filled=${r['monthly_usd_filled_30d']:6.0f}  "
              f"d_obs={r['days_observed']}/{r['elapsed_to_asof']}  "
              f"fatal={r['fatal_n']}  [{r['fill_method']}]")
    print(f"wrote {out_json}\n      {wf_json}")
    return report


if __name__ == "__main__":
    main()
