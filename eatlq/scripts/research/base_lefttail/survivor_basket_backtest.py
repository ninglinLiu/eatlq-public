#!/usr/bin/env python3
"""GREEN-REGIME SURVIVOR-BASKET backtest (READ-ONLY).

Answers the open question the RH report could not: on GREEN-regime days, does a
rug-filtered survivor BASKET turn net-positive? Uses the right-tail-appropriate
metrics (basket net value + ruin probability + tail capture), NOT trimmed/median.

Honesty rules (inherited): rug-before-our-exit = -100%; keep all enterable pools
in the denominator; fork-replay sellability truth (644 labeled pools).

Regime (causal): at a pool's entry t0, regime = median realized raw-4h-return of
all pools whose 4h window CLOSED within the trailing 24h (known at entry, no
look-ahead). GREEN if > 0.
"""
from __future__ import annotations
import glob, json, math, os, sys
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))
GAS_FRAC = 0.30 / 100.0
BLOCK_S = 2.0
HOLD_S = 14400  # 4h realized horizon for the regime tape

labels = {}
for f in glob.glob(os.path.join(HERE, "_uni_shards/_uni_s*.json")):
    for r in json.load(open(f))["pools"]:
        labels[r["pool"]] = r
priced = json.load(open(K.PCACHE))
entry_weth = json.load(open(os.path.join(HERE, "_entry_weth_644.json")))


def honest_return(pool, orient, entry_lp, path_steps, entry_block, head, delay_buckets=0):
    """Rug-before-exit -> -100%; else causal trailing exit on the tick path.

    delay_buckets>0 models execution/detection latency: entry slips that many
    10-min buckets into the forward path (worse entry price, less rug protection).
    """
    lab = labels.get(pool)
    steps = sorted(path_steps)
    if delay_buckets > 0:
        idx = next((i for i, (b, _l, _lb) in enumerate(steps) if b >= delay_buckets), None)
        if idx is None:
            return -1.0  # pool died before we could enter -> treat as total loss
        steps = steps[idx:]
        entry_lp = steps[0][1] * K.LN1P * orient
        entry_block = steps[0][2]
    exb, _why = K.exit_block(steps, entry_lp, orient, entry_block, head)
    if lab:
        L = lab.get("label")
        if L in ("dead", "instant_hp"):
            return -1.0
        if L == "delayed_rug":
            fb = lab.get("flip_block")
            if fb is not None and fb <= exb:
                return -1.0
    if not steps:
        return 0.0
    exit_lp = entry_lp
    for bkt, lt, lb in steps:
        if lb <= exb:
            exit_lp = lt * K.LN1P * orient
    return math.exp(exit_lp - entry_lp) - 1.0 - GAS_FRAC


def main():
    df, path = K.load()
    df = df.copy()
    LN1P = K.LN1P
    # raw realized 4h return for EVERY pool (the market "tape", for regime)
    df["raw4h"] = np.exp((df.last_tick_fwd - df.last_tick_pre) * LN1P * df.orient) - 1.0
    df.loc[df.ns_fwd <= 0, "raw4h"] = np.nan
    df["t0"] = pd.to_datetime(df["t0"])
    df = df.sort_values("t0").reset_index(drop=True)

    # ---- causal regime: median raw4h of pools whose 4h window closed in trailing 24h
    tape = df.dropna(subset=["raw4h"]).copy()
    tape["close_t"] = tape["t0"] + pd.to_timedelta(HOLD_S, unit="s")
    tape = tape.sort_values("close_t")
    ct = tape["close_t"].values.astype("datetime64[s]")
    rv = tape["raw4h"].values

    def regime_at(entry_t):
        entry64 = np.datetime64(entry_t, "s")
        lo = entry64 - np.timedelta64(24, "h")
        m = (ct <= entry64) & (ct >= lo)
        if m.sum() < 5:
            return np.nan
        return float(np.median(rv[m]))

    df["regime"] = df["t0"].map(regime_at)

    # ---- honest per-pool return on the 644 fork-labeled set
    head = int(df.entry_block.max()) + 7200
    lab_df = df[df.pool.isin(labels)].copy()
    rets, depths = [], []
    for r in lab_df.itertuples(index=False):
        entry_lp = r.last_tick_pre * LN1P * r.orient
        rets.append(honest_return(r.pool, r.orient, entry_lp, path.get(r.pool, []),
                                  int(r.entry_block), head))
        depths.append((entry_weth.get(r.pool, 0) or 0) / 1e18)
    lab_df["ret"] = rets
    lab_df["depth"] = depths
    lab_df["is_rug"] = lab_df.pool.map(lambda p: labels[p]["label"] == "delayed_rug")

    print(f"# fork-labeled pools={len(lab_df)}  span={lab_df.t0.min().date()}~{lab_df.t0.max().date()}")
    green_frac = (df.regime > 0).mean()
    print(f"# regime GREEN day-fraction (causal, trailing 24h): {green_frac:.1%}  "
          f"(pools with defined regime: {df.regime.notna().sum()}/{len(df)})")

    def basket_metrics(sub, tag, n_draw=20, stake=50.0, trials=20000, seed=0):
        r = sub.ret.dropna().values
        if len(r) < n_draw:
            print(f"  {tag:34s} n={len(r):3d}  (too few for basket sim)")
            return
        rng = np.random.default_rng(seed)
        # per-pool right tail
        wr = (r > 0).mean(); rug = (r <= -0.99).mean()
        p2 = (r >= 1).mean(); p5 = (r >= 4).mean(); p10 = (r >= 9).mean()
        # basket bootstrap: draw n_draw pools, equal $stake, net multiple
        idx = rng.integers(0, len(r), size=(trials, n_draw))
        basket = r[idx]                      # trials x n_draw returns
        net = basket.mean(axis=1)            # equal-weight net return of the basket
        p_profit = (net > 0).mean()
        p_ruin = (net <= -0.5).mean()        # basket loses >50%
        print(f"  {tag:34s} n={len(r):3d} mean/trade={r.mean():+6.2f} WR={wr:.0%} rug={rug:.0%} "
              f"| tail >2x={p2:.0%} >5x={p5:.0%} >10x={p10:.0%} "
              f"|| BASKET(20x${int(stake)}): P(profit)={p_profit:.0%} P(ruin>50%)={p_ruin:.0%} "
              f"mean_net={net.mean():+.2f} med_net={np.median(net):+.2f}")

    print("\n### A) 全体 fork-labeled(不分regime,不加过滤)——基线")
    basket_metrics(lab_df, "ALL pools")

    print("\n### B) 按 regime 切分(因果),不加其它过滤")
    basket_metrics(lab_df[lab_df.regime > 0], "GREEN-regime entry")
    basket_metrics(lab_df[lab_df.regime <= 0], "RED-regime entry")
    basket_metrics(lab_df[lab_df.regime.isna()], "regime-unknown")

    print("\n### C) 绿灯 + 存活者过滤(深池>=5 ETH)")
    g = lab_df[lab_df.regime > 0]
    basket_metrics(g[g.depth >= 5], "GREEN + depth>=5E")
    basket_metrics(g[g.depth >= 10], "GREEN + depth>=10E")

    print("\n### D) 绿灯 + rug过滤(fork标签非rug = 完美rug过滤上界)")
    basket_metrics(g[~g.is_rug], "GREEN + not-rug (oracle)")
    basket_metrics(g[(g.depth >= 5) & (~g.is_rug)], "GREEN + depth>=5E + not-rug")

    print("\n### E) 对照:红灯 + 同样过滤(应仍然差)")
    rd = lab_df[lab_df.regime <= 0]
    basket_metrics(rd[(rd.depth >= 5) & (~rd.is_rug)], "RED + depth>=5E + not-rug")

    # ---- F) 延迟压力测试:realistic +10min execution/detection lag ----
    print("\n### F) 延迟压力测试(+10min 执行/检测延迟)——最大残余风险")
    for tag, sub in [("GREEN + depth>=5E", g[g.depth >= 5]),
                     ("GREEN + depth>=5E + not-rug", g[(g.depth >= 5) & (~g.is_rug)])]:
        rl = []
        for r in sub.itertuples(index=False):
            entry_lp = r.last_tick_pre * LN1P * r.orient
            rl.append(honest_return(r.pool, r.orient, entry_lp, path.get(r.pool, []),
                                    int(r.entry_block), head, delay_buckets=1))
        rl = np.array(rl)
        rng = np.random.default_rng(1)
        if len(rl) >= 20:
            idx = rng.integers(0, len(rl), size=(20000, 20))
            net = rl[idx].mean(axis=1)
            print(f"  {tag+' [+10min]':40s} n={len(rl):3d} mean/trade={rl.mean():+6.2f} "
                  f"WR={(rl>0).mean():.0%} rug={(rl<=-0.99).mean():.0%} "
                  f"|| BASKET P(profit)={(net>0).mean():.0%} P(ruin>50%)={(net<=-0.5).mean():.0%} mean_net={net.mean():+.2f}")


if __name__ == "__main__":
    main()
