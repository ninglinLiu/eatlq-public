#!/usr/bin/env python3
"""KEYSTONE — left-tail (death/collapse) predictor on Base V3, pre-entry only.

Convex thesis foundation: if AI can flag the FATAL subset (rug/death/collapse)
BEFORE entry, significantly better than random, the convex (lottery) strategy
becomes viable. This builds that predictor and judges it with CONVEX-APPROPRIATE
criteria -- NOT the factor-strategy gates (no PF_top1_removed, no bootstrap_p5>=1,
no top1_share cap). The decisive extra check: pruning the predicted-fatal must
REMOVE the left tail WITHOUT also killing the right-tail winners.

Universe : Base V3 WETH pools, first swap t0 in window, >=MIN_PRE swaps in [0,W].
           NO pool dropped for being a death -- deaths are the label.
Entry    : t0 + W  (only [0,W] observable used as features; no look-ahead).
Outcome  : forward (W, W+H]; FATAL = min forward price <= -50% from entry OR
           activity death (<=2 fwd swaps => no exit => -100%).
Split    : time-based TRAIN (early) -> holdout TEST (recent). AUC on TEST.
"""
from __future__ import annotations
import subprocess, math, sys
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

WETH = "0x4200000000000000000000000000000000000006".lower()
LN1P = math.log(1.0001)
W = int(sys.argv[1]) if len(sys.argv) > 1 else 600        # pre-entry obs window (s)
H = int(sys.argv[2]) if len(sys.argv) > 2 else 14400      # forward horizon (s)
MIN_PRE = 5
DATE_LO, DATE_HI = "2025-10-01", "2026-06-01"
STOP = math.log(0.5)                                       # -50% convex stop
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]

SQL = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0a
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0
  FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL
  GROUP BY pool_address
  HAVING t0 >= '{DATE_LO} 00:00:00' AND t0 <= '{DATE_HI} 23:59:59'
),
sw AS (
  SELECT s.pool_address pool, s.tick tick, s.block_time bt, s.recipient rcp, s.sender snd,
         toFloat64(s.liquidity) liq, dateDiff('second', fs.t0, s.block_time) sec, fs.t0 t0
  FROM eatlq.uniswap_v3_swaps_decoded s
  INNER JOIN fs ON s.pool_address=fs.pool_address
  WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
    AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W+H}
)
SELECT pool, if(c.t0a='{WETH}', -1, 1) orient, any(t0) t0,
  countIf(sec<= {W}) ns_pre,
  uniqExactIf(rcp, sec<= {W}) buyers_pre,
  uniqExactIf(snd, sec<= {W}) senders_pre,
  argMinIf(tick, bt, sec<= {W}) first_tick,
  argMaxIf(tick, bt, sec<= {W}) last_tick_pre,
  minIf(tick, sec<= {W}) min_tick_pre,
  maxIf(tick, sec<= {W}) max_tick_pre,
  argMinIf(liq, bt, sec<= {W}) liq_first,
  argMaxIf(liq, bt, sec<= {W}) liq_last_pre,
  minIf(liq, sec<= {W}) liq_min_pre,
  countIf(sec<= {W//2}) ns_h1,
  countIf(sec> {W//2} AND sec<= {W}) ns_h2,
  argMaxIf(tick, bt, sec<= {W//2}) last_tick_h1,
  countIf(sec> {W} AND sec<= {W+H}) ns_fwd,
  minIf(tick, sec> {W} AND sec<= {W+H}) min_tick_fwd,
  maxIf(tick, sec> {W} AND sec<= {W+H}) max_tick_fwd,
  argMaxIf(tick, bt, sec> {W} AND sec<= {W+H}) last_tick_fwd,
  argMaxIf(liq, bt, sec> {W} AND sec<= {W+H}) liq_last_fwd
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address
GROUP BY pool, orient
HAVING ns_pre >= {MIN_PRE}
FORMAT TabSeparated
"""

COLS = ["pool","orient","t0","ns_pre","buyers_pre","senders_pre","first_tick",
        "last_tick_pre","min_tick_pre","max_tick_pre","liq_first","liq_last_pre",
        "liq_min_pre","ns_h1","ns_h2","last_tick_h1","ns_fwd","min_tick_fwd",
        "max_tick_fwd","last_tick_fwd","liq_last_fwd"]


def run_sql():
    r = subprocess.run(CH + [SQL], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print("SQL ERROR:\n", r.stderr[:2000]); sys.exit(1)
    rows = [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]
    df = pd.DataFrame(rows, columns=COLS)
    for c in COLS:
        if c not in ("pool", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["t0"] = pd.to_datetime(df["t0"])
    return df


def build(df):
    o = df["orient"].values
    lp = lambda t: t * LN1P * o
    lp_first, lp_last = lp(df.first_tick), lp(df.last_tick_pre)
    lp_a, lp_b = lp(df.min_tick_pre), lp(df.max_tick_pre)
    lp_lo, lp_hi = np.minimum(lp_a, lp_b), np.maximum(lp_a, lp_b)
    entry_lp = lp_last
    f = pd.DataFrame(index=df.index)
    f["ns_pre"] = df.ns_pre
    f["buyers_pre"] = df.buyers_pre
    f["senders_pre"] = df.senders_pre
    f["swaps_per_buyer"] = df.ns_pre / df.buyers_pre.clip(lower=1)
    f["buyer_sender_ratio"] = df.buyers_pre / df.senders_pre.clip(lower=1)
    f["early_ret"] = lp_last - lp_first
    f["pre_vol"] = lp_hi - lp_lo
    f["pre_pullback"] = lp_hi - lp_last
    f["pre_runup_from_lo"] = lp_last - lp_lo
    f["liq_log"] = np.log1p(df.liq_last_pre.clip(lower=0))
    f["liq_trend"] = df.liq_last_pre / df.liq_first.clip(lower=1)
    f["liq_drawdown"] = df.liq_min_pre / df.liq_first.clip(lower=1)
    f["heat_accel"] = df.ns_h2 / df.ns_h1.clip(lower=1)
    lp_h1 = lp(df.last_tick_h1)
    f["mom_h1"] = lp_h1 - lp_first
    f["mom_h2"] = lp_last - lp_h1
    # ---- outcome (forward) ----
    has_fwd = df.ns_fwd > 0
    fa, fb = lp(df.min_tick_fwd), lp(df.max_tick_fwd)
    fwd_lo = np.where(has_fwd, np.minimum(fa, fb), -np.inf)
    fwd_last = np.where(has_fwd, lp(df.last_tick_fwd), -np.inf)
    fwd_min_ret = fwd_lo - entry_lp
    fwd_ret = fwd_last - entry_lp
    death = (df.ns_fwd <= 2).values
    fatal = death | (fwd_min_ret <= STOP)
    # realized convex P&L proxy: -100% if death, -50% if stopped, else exp(fwd_ret)-1
    realized = np.where(death, -1.0,
                np.where(fwd_min_ret <= STOP, -0.5, np.exp(np.clip(fwd_ret, -5, 5)) - 1))
    out = pd.DataFrame({"t0": df.t0, "fatal": fatal.astype(int),
                        "fwd_ret": np.where(has_fwd, fwd_ret, np.nan),
                        "realized": realized, "death": death.astype(int)})
    return f.replace([np.inf, -np.inf], np.nan).fillna(0.0), out


def main():
    print(f"#### KEYSTONE left-tail predictor | W={W}s entry, H={H}s horizon ####")
    df = run_sql()
    X, Y = build(df)
    cut = df.t0.quantile(0.6)
    tr = df.t0 <= cut
    te = ~tr
    print(f"pools={len(df)}  TRAIN={tr.sum()} (<= {cut.date()})  TEST={te.sum()}")
    print(f"FATAL base rate: ALL={Y.fatal.mean():.3f}  TRAIN={Y.fatal[tr].mean():.3f}  TEST={Y.fatal[te].mean():.3f}")
    print(f"  (of which death/no-exit: ALL={Y.death.mean():.3f})")
    clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4,
                                         learning_rate=0.05, l2_regularization=1.0,
                                         min_samples_leaf=20, random_state=0)
    clf.fit(X[tr], Y.fatal[tr])
    p_te = clf.predict_proba(X[te])[:, 1]
    auc = roc_auc_score(Y.fatal[te], p_te)
    print(f"\n>>> HOLDOUT AUC = {auc:.3f}   (keystone bar = 0.65)")
    # convex check: prune predicted-fatal, keep winners?
    yte = Y[te].reset_index(drop=True); pte = p_te
    print("\n--- CONVEX CHECK: prune top predicted-fatal, measure kept set ---")
    print("  (left-tail pruning is only good if it removes deaths but KEEPS right-tail winners)")
    win_thr = np.nanquantile(yte.fwd_ret, 0.90)   # top-decile winners (by fwd log-ret)
    n_win_all = (yte.fwd_ret >= win_thr).sum()

    def desc(k, tag):
        r = k.realized.values
        wr = (r > 0).mean()
        med = np.median(r)
        top = r[r > 0].sum()
        topshare = (np.sort(r)[-3:].sum() / max(r[r > 0].sum(), 1e-9)) if (r > 0).any() else 0
        win_kept = (k.fwd_ret >= win_thr).sum()
        print(f"  {tag:16s} n={len(k):4d} fatal={k.fatal.mean():.3f} death={k.death.mean():.3f} "
              f"mean={r.mean():+.3f} med={med:+.3f} winrate={wr:.2f} "
              f"top3share={topshare:.2f} winners_kept={win_kept}/{n_win_all}")
    desc(yte, "ALL")
    for cut_q in (0.30, 0.50, 0.70):
        keep = pte <= np.quantile(pte, cut_q)   # keep the safest cut_q fraction
        desc(yte[keep], f"keep safest {int(cut_q*100)}%")
    # feature importance via permutation-free: use the model's internal? print top by simple corr
    print("\n--- feature signal (point-biserial corr with FATAL, TRAIN) ---")
    cors = [(c, np.corrcoef(X[tr][c], Y.fatal[tr])[0, 1]) for c in X.columns]
    for c, v in sorted(cors, key=lambda x: -abs(x[1]))[:8]:
        print(f"  {c:20s} {v:+.3f}")


if __name__ == "__main__":
    main()
