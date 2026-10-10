#!/usr/bin/env python3
"""Train the keystone left-tail model on ALL available data and persist it.

Run this offline before starting the signal bridge.  Output:
  _model.pkl   — the trained HistGradientBoostingClassifier
  _params.json — p_cut threshold, feature list, training stats
"""
from __future__ import annotations
import math, json, pickle, os
from urllib.parse import urlparse
import clickhouse_connect
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

WETH = "0x4200000000000000000000000000000000000006".lower()
LN1P = math.log(1.0001)
W = 600
H = 14400
MIN_PRE = 5
STOP = math.log(0.5)
FRIC_THR = -0.05
DATE_LO, DATE_HI = "2025-10-01", "2026-06-01"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

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
  argMaxIf(tick, bt, sec> {W} AND sec<= {W+H}) last_tick_fwd
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address
GROUP BY pool, orient
HAVING ns_pre >= {MIN_PRE}
"""

COLS = ["pool","orient","t0","ns_pre","buyers_pre","senders_pre","first_tick",
        "last_tick_pre","min_tick_pre","max_tick_pre","liq_first","liq_last_pre",
        "liq_min_pre","ns_h1","ns_h2","last_tick_h1","ns_fwd","min_tick_fwd",
        "max_tick_fwd","last_tick_fwd"]

FEATURE_NAMES = ["ns_pre","buyers_pre","senders_pre","swaps_per_buyer",
    "buyer_sender_ratio","early_ret","pre_vol","pre_pullback","pre_runup_from_lo",
    "liq_log","liq_trend","liq_drawdown","heat_accel","mom_h1","mom_h2"]


def run_sql():
    ch_url = os.environ.get("CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_HTTP_URL") or "http://clickhouse:8123"
    parsed = urlparse(ch_url)
    client = clickhouse_connect.get_client(
        host=parsed.hostname or "clickhouse",
        port=parsed.port or (8443 if parsed.scheme == "https" else 8123),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "eatlq"),
        interface="http",
        secure=parsed.scheme == "https",
    )
    result = client.query(SQL)
    rows = [list(row) for row in result.result_rows]
    df = pd.DataFrame(rows, columns=COLS)
    for c in COLS:
        if c not in ("pool", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["t0"] = pd.to_datetime(df["t0"])
    return df


def build_features(df):
    o = df.orient.values
    lp = lambda t: t * LN1P * o
    lp_first, lp_last = lp(df.first_tick), lp(df.last_tick_pre)
    lp_lo = np.minimum(lp(df.min_tick_pre), lp(df.max_tick_pre))
    lp_hi = np.maximum(lp(df.min_tick_pre), lp(df.max_tick_pre))
    f = pd.DataFrame(index=df.index)
    f["ns_pre"], f["buyers_pre"], f["senders_pre"] = df.ns_pre, df.buyers_pre, df.senders_pre
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
    f["mom_h1"], f["mom_h2"] = lp_h1 - lp_first, lp_last - lp_h1
    has = df.ns_fwd > 0
    fwd_lo = np.where(has, np.minimum(lp(df.min_tick_fwd), lp(df.max_tick_fwd)), -np.inf)
    fatal = (df.ns_fwd <= 2).values | ((fwd_lo - lp_last) <= STOP)
    return f.replace([np.inf, -np.inf], np.nan).fillna(0.0), fatal.astype(int)


TRAIN_QUANTILE = 0.6


def time_ordered_split(df, train_quantile: float = TRAIN_QUANTILE):
    """Split rows by pool-birth time (`t0`) into train / holdout.

    Time-ordered (NOT a random/shuffle split): train = earliest `train_quantile`
    of pools by t0, holdout = the rest. A shuffled split on time-series pool data
    leaks future regime info into the threshold and inflates apparent skill.
    Returns boolean masks (train_mask, holdout_mask) aligned to df.index.
    """
    t0 = pd.to_datetime(df["t0"])
    cut = t0.quantile(train_quantile)
    train_mask = (t0 <= cut).values
    holdout_mask = ~train_mask
    return train_mask, holdout_mask, cut


def main():
    print("Loading training data from ClickHouse...")
    df = run_sql()
    X, y = build_features(df)
    print(f"  pools={len(df)}  fatal_rate={y.mean():.3f}")

    # Time-ordered holdout: fit + threshold on TRAIN only, report on holdout.
    train_mask, holdout_mask, cut = time_ordered_split(df, TRAIN_QUANTILE)
    print(
        f"  time-ordered split @ t0<={cut}: train={int(train_mask.sum())} "
        f"holdout={int(holdout_mask.sum())}"
    )

    clf = HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0
    )
    clf.fit(X[train_mask], y[train_mask])

    # p_cut is learned on TRAIN predictions only (no in-sample full-history leak).
    p_train = clf.predict_proba(X[train_mask])[:, 1]
    p_cut = float(np.quantile(p_train, 0.6))

    # Report holdout metrics at the train-derived threshold (no shuffle split).
    holdout_metrics = {}
    if holdout_mask.sum() > 0:
        p_hold = clf.predict_proba(X[holdout_mask])[:, 1]
        y_hold = y[holdout_mask]
        selected = p_hold <= p_cut
        n_sel = int(selected.sum())
        holdout_metrics = {
            "holdout_n": int(holdout_mask.sum()),
            "holdout_fatal_rate": float(y_hold.mean()),
            "holdout_selected_n": n_sel,
            "holdout_selected_fatal_rate": float(y_hold[selected].mean()) if n_sel else None,
        }
        print(
            f"  holdout: n={holdout_metrics['holdout_n']} "
            f"base_fatal={holdout_metrics['holdout_fatal_rate']:.3f} "
            f"selected_n={n_sel} "
            f"selected_fatal={holdout_metrics['holdout_selected_fatal_rate']}"
        )

    model_path = os.path.join(OUT_DIR, "_model.pkl")
    params_path = os.path.join(OUT_DIR, "_params.json")

    with open(model_path, "wb") as f:
        pickle.dump(clf, f)

    params = {
        "p_cut": float(p_cut),
        "W": W, "H": H, "MIN_PRE": MIN_PRE,
        "FRIC_THR": FRIC_THR,
        "features": FEATURE_NAMES,
        "train_n": int(train_mask.sum()),
        "total_n": int(len(df)),
        "train_quantile": TRAIN_QUANTILE,
        "split": "time_ordered_t0",
        "fatal_rate": float(y[train_mask].mean()),
        "train_date_range": [DATE_LO, DATE_HI],
        **holdout_metrics,
    }
    json.dump(params, open(params_path, "w"), indent=2)
    print(f"  model -> {model_path}")
    print(f"  params -> {params_path} (p_cut={p_cut:.4f})")


if __name__ == "__main__":
    main()
