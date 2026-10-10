#!/usr/bin/env python3
"""READ-ONLY recompute for live-config audit (no env / no live changes).

Reproduces:
  A. the trade set used by risk_rebacktest.py (131) and the walk-forward
     pooled set (217), with no look-ahead.
  B. peak simultaneous deployed capital + MDD-vs-peak-at-risk for the
     "$2000 start + $25/trade + 6 concurrent" config.
  C. a concurrency-aware portfolio backtest of the REAL live config:
     fixed $100/trade, max_concurrent=10, start $1000, no compounding,
     frac 0.10 (=> $100 on $1000), no daily-loss fuse, p1 depth gate,
     left-tail model, real gas + MEV haircut.

All economics reuse cached QuoterV2 round-trip nets (entry + real exit block).
"""
from __future__ import annotations
import json, math, os, sys
from collections import Counter, defaultdict
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

HERE = "/opt/EATLQ/scripts/research/base_lefttail"
sys.path.insert(0, HERE)
import keystone_live as K  # noqa: E402

FRIC_THR = -0.05
GAS_USD_RT = 0.15
SEC_PER_BLOCK = 2.0           # Base ~2s blocks
TEST_MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]

priced = json.load(open(os.path.join(HERE, "_priced_live.json")))
dynamic = json.load(open(os.path.join(HERE, "_dynamic_size_realgas.json")))


def fit(X, y, mask):
    c = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                       l2_regularization=1.0, min_samples_leaf=20, random_state=0)
    c.fit(X[mask], y[mask])
    return c


def net_100(pool):
    """Per-trade net at $100 notional, real gas + caller-applied haircut later.
    Prefer size-specific real-gas replay; fall back to priced_live $100 net."""
    rec = dynamic.get(f"{pool}:100")
    if rec and rec.get("real_net") is not None and rec.get("fric") is not None:
        return rec["real_net"], rec.get("entry_block"), rec.get("exit_block"), rec.get("fric")
    pr = priced.get(pool)
    if pr and pr.get("net") is not None and pr.get("why") == "ok":
        return pr["net"], None, pr.get("exb"), pr.get("fric")
    return None, None, None, None


def build_walkforward(haircut: float):
    """Walk-forward expanding monthly. Returns list of trade dicts (no look-ahead)."""
    df, _ = K.load()
    X, y = K.build(df)
    df = df.copy()
    df["fric"] = df.pool.map(lambda x: priced.get(x, {}).get("fric"))
    df["mon"] = df.t0.dt.strftime("%Y-%m")
    df["entry_block"] = pd.to_numeric(df["entry_block"], errors="coerce")

    trades = []
    for tm in TEST_MONTHS:
        trm = (df.mon < tm).values
        if trm.sum() < 150:
            continue
        clf = fit(X, y, trm)
        p = clf.predict_proba(X)[:, 1]
        df["p"] = p
        gate_train = df[trm & df.fric.notna() & (df.fric >= FRIC_THR)]
        p_cut = gate_train.p.quantile(0.6)
        keep = df[(df.mon == tm) & df.fric.notna() & (df.fric >= FRIC_THR) & (df.p <= p_cut)]
        for row in keep.itertuples(index=False):
            base_net, eb, xb, fric = net_100(row.pool)
            if base_net is None:
                continue
            ret = float(base_net) - (GAS_USD_RT / 100.0) - haircut
            entry_block = eb if eb is not None else (int(row.entry_block) if not math.isnan(row.entry_block) else None)
            if entry_block is None or xb is None:
                hold_s = K.HOLD_BLOCKS * SEC_PER_BLOCK
            else:
                hold_s = max(0.0, (int(xb) - int(entry_block)) * SEC_PER_BLOCK)
            entry_t = pd.Timestamp(row.t0)
            trades.append({
                "pool": row.pool, "t0": entry_t, "exit_t": entry_t + pd.Timedelta(seconds=hold_s),
                "ret": ret, "fric": fric, "month": tm,
            })
    trades.sort(key=lambda x: x["t0"])
    return trades


def portfolio_sim(trades, start, stake, max_concurrent):
    """Event-driven, fixed stake, concurrency cap, no compounding.
    Equity is realized (steps at each exit). Returns metrics + per-trade taken list."""
    open_pos = []   # list of (exit_t, taken_index)
    taken = []
    skipped_concurrency = 0
    # process by entry time; release closed positions as time advances
    for t in trades:
        open_pos = [(xt, i) for (xt, i) in open_pos if xt > t["t0"]]
        if len(open_pos) >= max_concurrent:
            skipped_concurrency += 1
            continue
        idx = len(taken)
        taken.append(t)
        open_pos.append((t["exit_t"], idx))

    if not taken:
        return {"n": 0}

    # realized equity curve: order PnL events by exit time
    events = sorted([(t["exit_t"], stake * t["ret"]) for t in taken], key=lambda x: x[0])
    eq = start
    peak = start
    mdd = 0.0
    curve = [(taken[0]["t0"], start)]
    for xt, pnl in events:
        eq += pnl
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
        curve.append((xt, eq))
    end = eq
    mdd_dollars = min(0.0, min(e for _, e in curve) - start) if curve else 0.0
    # also drawdown in dollars from running peak (true MDD $)
    eq2 = start; pk2 = start; mdd_dol = 0.0
    for xt, pnl in events:
        eq2 += pnl
        pk2 = max(pk2, eq2)
        mdd_dol = min(mdd_dol, eq2 - pk2)

    # peak simultaneous deployed capital (point process over open intervals)
    points = []
    for t in taken:
        points.append((t["t0"], +1))
        points.append((t["exit_t"], -1))
    points.sort(key=lambda x: (x[0], -x[1]))  # opens before closes at same instant
    cur = 0; peak_open = 0
    for _, d in points:
        cur += d
        peak_open = max(peak_open, cur)
    peak_deployed = peak_open * stake

    # daily realized PnL (by exit day)
    by_day = defaultdict(float)
    fatals_by_day = defaultdict(list)
    loss_sum_by_day = defaultdict(float)
    for t in taken:
        d = t["exit_t"].date()
        pnl = stake * t["ret"]
        by_day[d] += pnl
        if pnl < 0:
            loss_sum_by_day[d] += pnl
        if t["ret"] <= -0.5:
            fatals_by_day[d].append((t["pool"], t["ret"], pnl))

    worst_net_day = min(by_day.items(), key=lambda kv: kv[1]) if by_day else (None, 0.0)
    worst_loss_day = min(loss_sum_by_day.items(), key=lambda kv: kv[1]) if loss_sum_by_day else (None, 0.0)
    days_ge2_fatal = {str(d): v for d, v in fatals_by_day.items() if len(v) >= 2}
    days_ge3_fatal = {str(d): v for d, v in fatals_by_day.items() if len(v) >= 3}

    rets = np.array([t["ret"] for t in taken])
    span_days = max((max(t["exit_t"] for t in taken) - min(t["t0"] for t in taken)).days, 1)
    monthly = (end / start) ** (30.0 / span_days) - 1.0 if end > 0 else -1.0

    return {
        "n": len(taken), "skipped_concurrency": skipped_concurrency,
        "start": start, "stake": stake, "max_concurrent": max_concurrent,
        "end": end, "total_return": end / start - 1.0, "monthly_return": monthly,
        "mdd_pct_of_equity_peak": mdd, "mdd_dollars": mdd_dol,
        "mdd_pct_of_start": (min(e for _, e in curve) - start) / start,
        "peak_concurrent": peak_open, "peak_deployed_usd": peak_deployed,
        "mdd_pct_of_peak_deployed": (mdd_dol / peak_deployed) if peak_deployed else None,
        "win_rate": float((rets > 0).mean()), "fatal_rate": float((rets <= -0.5).mean()),
        "mean_ret": float(rets.mean()), "median_ret": float(np.median(rets)),
        "worst_net_day": (str(worst_net_day[0]), worst_net_day[1]),
        "worst_loss_sum_day": (str(worst_loss_day[0]), worst_loss_day[1]),
        "days_ge2_fatal": days_ge2_fatal, "days_ge3_fatal": days_ge3_fatal,
        "span_days": span_days,
    }


def main():
    HAIRCUT = 0.02
    trades = build_walkforward(HAIRCUT)
    print(f"#### RECOMPUTE (read-only) — MEV haircut={HAIRCUT:.0%}, real gas ${GAS_USD_RT}/RT ####")
    print(f"walk-forward kept trades (priced) n={len(trades)}")
    rets = np.array([t['ret'] for t in trades])
    print(f"  trade-level: WR={(rets>0).mean():.1%} fatal(<=-50%)={(rets<=-0.5).mean():.1%} "
          f"mean={rets.mean():+.1%} med={np.median(rets):+.1%} best={rets.max():+.1%} worst={rets.min():+.1%}")
    print(f"  date span: {min(t['t0'] for t in trades).date()} .. {max(t['t0'] for t in trades).date()}")

    print("\n=== B. $2000 start / $25 stake / max_concurrent=6 ===")
    b = portfolio_sim(trades, start=2000.0, stake=25.0, max_concurrent=6)
    for k in ("n","skipped_concurrency","end","total_return","monthly_return",
              "mdd_pct_of_equity_peak","mdd_dollars","peak_concurrent","peak_deployed_usd",
              "mdd_pct_of_peak_deployed","worst_net_day","worst_loss_sum_day"):
        print(f"  {k:28s}: {b[k]}")
    print(f"  days >=2 simultaneous -50%: {list(b['days_ge2_fatal'].keys())}")
    print(f"  days >=3 simultaneous -50%: {list(b['days_ge3_fatal'].keys())}")

    print("\n=== C. LIVE config: $1000 start / $100 stake / max_concurrent=10 / no compound ===")
    c = portfolio_sim(trades, start=1000.0, stake=100.0, max_concurrent=10)
    for k in ("n","skipped_concurrency","end","total_return","monthly_return",
              "mdd_pct_of_equity_peak","mdd_dollars","mdd_pct_of_start","peak_concurrent",
              "peak_deployed_usd","win_rate","fatal_rate","mean_ret","median_ret",
              "worst_net_day","worst_loss_sum_day","span_days"):
        print(f"  {k:28s}: {c[k]}")
    print(f"  days >=2 simultaneous -50%: {json.dumps(b_dates(c['days_ge2_fatal']))}")
    print(f"  days >=3 simultaneous -50%: {json.dumps(b_dates(c['days_ge3_fatal']))}")

    # haircut sensitivity for C headline
    print("\n=== C sensitivity (MEV haircut) ===")
    for hc in (0.0, 0.02, 0.05):
        tr = build_walkforward(hc)
        cc = portfolio_sim(tr, start=1000.0, stake=100.0, max_concurrent=10)
        print(f"  haircut={hc:.0%}: total_ret={cc['total_return']:+.1%} monthly={cc['monthly_return']:+.1%} "
              f"MDD%(eqpeak)={cc['mdd_pct_of_equity_peak']:.1%} MDD$={cc['mdd_dollars']:+.0f} "
              f"worst_day$={cc['worst_net_day'][1]:+.0f} on {cc['worst_net_day'][0]}")

    out = {"haircut": HAIRCUT, "n_trades": len(trades), "B": b, "C": c}
    json.dump(out, open(os.path.join(HERE, "_recompute_live_config.json"), "w"), indent=2, default=str)
    print(f"\nwrote {HERE}/_recompute_live_config.json")


def b_dates(d):
    return {k: [(p, round(r, 3), round(pnl, 2)) for (p, r, pnl) in v] for k, v in d.items()}


if __name__ == "__main__":
    main()
