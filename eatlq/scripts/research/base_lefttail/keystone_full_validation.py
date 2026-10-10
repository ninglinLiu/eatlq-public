#!/usr/bin/env python3
"""Full KEYSTONE backtest validation — all requested metrics with real costs.

Strategy (frozen, no look-ahead):
  1. Universe: Base V3 WETH pools, >=5 swaps in first 600s pre-entry window
  2. Depth gate: live QuoterV2 round-trip friction >= FRIC_THR at entry block
  3. Left-tail ML: HistGradientBoosting on TRAIN only; keep p <= p_cut (TRAIN q60)
  4. Exit: trailing 35% + hard -50% + ~4h hold (real QuoterV2 sell at exit block)
  5. Costs: QuoterV2 slippage+LP fee (in net) + gas per round-trip

Reports: max drawdown, Sharpe, win rate, profit/loss ratio, PF (portfolio + monthly),
         winner avg/median, walk-forward pooled, smart-money contrast note.
"""
from __future__ import annotations
import json, math, sys
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import load, build  # noqa: E402

FRIC_THR = -0.05
NOTIONAL_USD = 100.0
GAS_USD_RT = 0.15          # round-trip gas on Base (conservative)
PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
POSITION_FRAC = 0.10       # 10% of equity per trade (convex sizing probe)
START_EQUITY = 1000.0
MAX_CONCURRENT = 1


def fit(Xtr, ytr):
    c = HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0,
    )
    c.fit(Xtr, ytr)
    return c


def net_after_gas(raw_net: float | None) -> float | None:
    if raw_net is None:
        return None
    gas_pct = GAS_USD_RT / NOTIONAL_USD
    return raw_net - gas_pct


def portfolio_pf(rets: np.ndarray) -> float:
    wins = rets[rets > 0].sum()
    losses = abs(rets[rets < 0].sum())
    return wins / losses if losses > 0 else float("inf")


def profit_loss_ratio(rets: np.ndarray) -> float:
    w = rets[rets > 0]
    l = rets[rets < 0]
    if len(w) == 0 or len(l) == 0:
        return float("nan")
    return w.mean() / abs(l.mean())


def equity_curve(rets: np.ndarray, start: float = START_EQUITY, frac: float = POSITION_FRAC):
    eq = [start]
    for r in rets:
        stake = eq[-1] * frac
        eq.append(eq[-1] + stake * r)
    return np.array(eq)


def max_drawdown(eq: np.ndarray) -> tuple[float, float]:
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    i = int(np.argmin(dd))
    return float(dd.min()), float(-dd.min())  # negative fraction, positive pct


def sharpe(rets: np.ndarray, trades_per_year: float) -> float:
    if len(rets) < 2 or rets.std() == 0:
        return float("nan")
    return float(rets.mean() / rets.std() * math.sqrt(trades_per_year))


def describe_trades(tag: str, rets: np.ndarray, dates: pd.Series | None = None):
    rets = np.array([x for x in rets if x is not None and not np.isnan(x)])
    if len(rets) == 0:
        print(f"\n[{tag}] n=0")
        return {}
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = portfolio_pf(rets)
    plr = profit_loss_ratio(rets)
    eq = equity_curve(rets)
    mdd_frac, mdd_pct = max_drawdown(eq)
    span_days = 1
    if dates is not None and len(dates) > 1:
        span_days = max((dates.max() - dates.min()).days, 1)
    tpy = len(rets) / span_days * 365
    sh = sharpe(rets, tpy)

    monthly_pf = []
    if dates is not None:
        df = pd.DataFrame({"r": rets, "t": dates.values})
        df["mon"] = pd.to_datetime(df["t"]).dt.to_period("M")
        for _, g in df.groupby("mon"):
            if len(g) >= 3:
                monthly_pf.append(portfolio_pf(g["r"].values))

    print(f"\n{'='*72}")
    print(f"  {tag}")
    print(f"{'='*72}")
    print(f"  样本笔数 n              : {len(rets)}")
    print(f"  时间跨度                : {span_days} 天 (~{len(rets)/span_days*30:.1f} 笔/月)")
    print(f"  单笔胜率 WR             : {(rets>0).mean():.1%}")
    print(f"  单笔均值 EV             : {rets.mean():+.2%}")
    print(f"  单笔中位数              : {np.median(rets):+.2%}")
    print(f"  盈利单均值 (winners)    : {wins.mean():+.2%}" if len(wins) else "  盈利单均值: n/a")
    print(f"  盈利单中位数 (winners)  : {np.median(wins):+.2%}" if len(wins) else "  盈利单中位数: n/a")
    print(f"  亏损单均值 (losers)     : {losses.mean():+.2%}" if len(losses) else "  亏损单均值: n/a")
    print(f"  盈亏比 (avg win/|avg loss|): {plr:.2f}")
    print(f"  组合 PF (gross win/loss): {pf:.2f}")
    if monthly_pf:
        print(f"  月度 PF 均值            : {np.mean(monthly_pf):.2f}")
        print(f"  月度 PF 中位数          : {np.median(monthly_pf):.2f}  (n={len(monthly_pf)} 月)")
    print(f"  致命率 (ret<=-50%)      : {(rets<=-0.5).mean():.1%}")
    print(f"  最好/最差单笔           : {rets.max():+.1%} / {rets.min():+.1%}")
    print(f"  top1 占盈利总额         : {(rets.max()/wins.sum() if wins.sum()>0 else 0):.1%}")
    print(f"--- 组合模拟 (${START_EQUITY:.0f} 起始, {POSITION_FRAC:.0%}/笔, {MAX_CONCURRENT}并发) ---")
    print(f"  终值 equity             : ${eq[-1]:,.0f}  ({(eq[-1]/eq[0]-1):+.1%} 总回报)")
    print(f"  最大回撤 MDD            : {mdd_pct:.1%}")
    print(f"  Sharpe (年化, per-trade): {sh:.2f}")
    return {
        "n": len(rets), "wr": float((rets > 0).mean()), "mean": float(rets.mean()),
        "med": float(np.median(rets)), "pf": pf, "plr": plr, "mdd": mdd_pct, "sharpe": sh,
        "win_mean": float(wins.mean()) if len(wins) else None,
        "win_med": float(np.median(wins)) if len(wins) else None,
    }


def main():
    df, _ = load()
    X, y = build(df)
    base = json.load(open(PCACHE))
    df = df.copy()
    df["fric"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    df["net_raw"] = df.pool.map(lambda x: base.get(x, {}).get("net"))
    df["net"] = df.net_raw.map(net_after_gas)
    df["why"] = df.pool.map(lambda x: base.get(x, {}).get("why"))

    cut = df.t0.quantile(0.6)
    tr, te = df.t0 <= cut, df.t0 > cut
    clf = fit(X[tr.values], y[tr.values])
    df["p"] = clf.predict_proba(X)[:, 1]

    ok = df[df.why == "ok"].copy()
    tr_ok = ok.t0 <= cut
    te_ok = ~tr_ok
    trd = ok[tr_ok]
    ted = ok[te_ok]
    gtr = trd[trd.fric >= FRIC_THR]
    gte = ted[ted.fric >= FRIC_THR]
    p_cut = gtr.p.quantile(0.6)

    print("#### KEYSTONE 全新回测验证 ####")
    print(f"定价: QuoterV2 ${NOTIONAL_USD:.0f} 往返 + gas ${GAS_USD_RT:.2f}/RT")
    print(f"出场: TRAIL=35% hardstop=-50% hold~4h | 深度门 fric>={FRIC_THR:+.0%}")
    print(f"模型: 左尾预测器 TRAIN-only | p_cut={p_cut:.3f} (TRAIN q60)")
    print(f"池子: 全样本={len(df)} TRAIN={tr.sum()} HOLDOUT TEST={te.sum()}")
    print(f"      priced_ok={len(ok)} holdout_priced={len(ted)}")

    # --- Baselines on holdout ---
    ted_n = ted[ted.net.notna()]
    gte_n = gte[gte.net.notna()]
    kept = gte_n[gte_n.p <= p_cut]
    describe_trades("HOLDOUT 全宇宙 (priced ok, 无过滤)", ted_n.net.values, ted_n.t0)
    describe_trades("HOLDOUT 仅深度门 (fric>=-5%)", gte_n.net.values, gte_n.t0)
    describe_trades("HOLDOUT ★ KEYSTONE (深度门+左尾模型)", kept.net.values, kept.t0)

    # --- Walk-forward pooled (same as harden_walkforward) ---
    df["mon"] = df.t0.dt.strftime("%Y-%m")
    TEST_MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    wf_rets, wf_dates = [], []
    print(f"\n{'='*72}")
    print("  WALK-FORWARD 逐月 (模型+阈值仅用该月之前数据)")
    print(f"{'='*72}")
    for tm in TEST_MONTHS:
        trm = df.mon < tm
        if trm.sum() < 150:
            continue
        c = fit(X[trm.values], y[trm.values])
        df.loc[:, "p_wf"] = c.predict_proba(X)[:, 1]
        gtr_wf = df[trm & df.fric.notna() & (df.fric >= FRIC_THR)]
        pc = gtr_wf.p_wf.quantile(0.6)
        tem = (df.mon == tm) & df.fric.notna() & (df.fric >= FRIC_THR)
        k = df[tem & (df.p_wf <= pc)]
        r = k.net.dropna().values
        if len(r):
            wf_rets.extend(r)
            wf_dates.extend(k.t0.tolist())
            print(f"  {tm}: n={len(r):3d} mean={r.mean():+.1%} med={np.median(r):+.1%} "
                  f"WR={(r>0).mean():.0%} fatal={(r<=-0.5).mean():.0%} PF={portfolio_pf(r):.2f}")
    if wf_rets:
        describe_trades("WALK-FORWARD 汇总 (最真实 OOS)", np.array(wf_rets), pd.Series(wf_dates))

    # --- TRAIN sanity (in-sample, labeled) ---
    kept_tr = gtr[gtr.p <= p_cut]
    kept_tr_n = kept_tr[kept_tr.net.notna()]
    describe_trades("TRAIN 同策略 (样本内, 仅对照)", kept_tr_n.net.values, kept_tr_n.t0)

    print(f"\n{'='*72}")
    print("  成本分解说明")
    print(f"{'='*72}")
    fr = gte_n.fric.dropna().values
    print(f"  进场摩擦 (QuoterV2 同块买卖): mean={fr.mean():+.2%} med={np.median(fr):+.2%}")
    print(f"  4h 净收益 (含出场滑点): gate-only mean={gte_n.net.mean():+.2%}")
    print(f"  Gas 额外扣除: {GAS_USD_RT/NOTIONAL_USD:.2%}/笔 (未含 MEV/抢跑/延迟)")


if __name__ == "__main__":
    main()
