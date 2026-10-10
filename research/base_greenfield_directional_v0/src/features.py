#!/usr/bin/env python3
"""Fold-safe feature matrix — bans KEYSTONE/F10 scores."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.common import OUT, assert_no_banned_features, utcnow  # noqa: E402

LABELS = OUT / "labels" / "labels_full.parquet"
FEAT_DIR = OUT / "features"

PRICE_PATH = [
    "early_ret", "xs_early_ret_rank", "pre_vol", "pre_pullback", "pre_runup_from_lo",
    "range_location", "mom_h1", "mom_h2", "accel", "age_s",
]
FLOW = ["net_flow", "trade_count_imbalance", "ns_pre", "buys_10m", "sells_10m"]
PARTICIPATION = ["buyers_pre", "senders_pre", "buyer_sender_ratio"]
LIQUIDITY = ["liq_log", "liq_trend", "liq_drawdown", "entry_liq"]
MARKET = [
    "base_breadth_1h", "active_pools_day", "return_dispersion_1h", "opportunity_supply",
]

ALL_RAW = PRICE_PATH + FLOW + PARTICIPATION + LIQUIDITY + MARKET
PRICE_ONLY = [
    "early_ret", "xs_early_ret_rank", "pre_vol", "pre_pullback", "pre_runup_from_lo",
    "range_location", "mom_h1", "mom_h2", "accel", "age_s",
]
SELLABILITY_ONLY = ["liq_log", "liq_trend", "liq_drawdown", "entry_liq", "sells_10m", "buys_10m"]


def fold_fit_residuals(train: pd.DataFrame, test: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    tr, te = train.copy(), test.copy()

    def resid(ycol, xcols, name):
        for c in xcols + [ycol]:
            if c not in tr.columns:
                tr[c] = 0.0
            if c not in te.columns:
                te[c] = 0.0
        Xtr = tr[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        ytr = tr[ycol].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        if len(tr) < 40:
            tr[name] = 0.0
            te[name] = 0.0
            return
        lr = LinearRegression()
        lr.fit(Xtr, ytr)
        tr[name] = ytr - lr.predict(Xtr)
        Xte = te[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        te[name] = te[ycol].fillna(0.0).values - lr.predict(Xte)

    resid("early_ret", ["liq_log", "pre_vol", "ns_pre", "base_breadth_1h"], "market_adj_ret")
    resid("net_flow", ["liq_log", "pre_vol", "base_breadth_1h"], "flow_surprise")
    for d in (tr, te):
        d["path_curvature"] = d["mom_h2"] - d["mom_h1"]
        d["vol_depth"] = d["pre_vol"] / (d["liq_log"].abs() + 1e-6)
        d["part_concentration"] = 1.0 / d["buyers_pre"].clip(lower=1)
        d["flow_persistence"] = d["trade_count_imbalance"] * np.sign(d["net_flow"])
    return tr, te


FEATURE_COLS = ALL_RAW + [
    "market_adj_ret", "flow_surprise", "path_curvature", "vol_depth",
    "part_concentration", "flow_persistence",
]


def build_feature_table(df: pd.DataFrame) -> pd.DataFrame:
    assert_no_banned_features(list(df.columns))
    # residualize within DEV using expanding time folds later in models;
    # here produce raw + globally prior-safe placeholders (= raw copies)
    out = df.copy()
    out["market_adj_ret"] = out["early_ret"]
    out["flow_surprise"] = out["net_flow"]
    out["path_curvature"] = out["accel"]
    out["vol_depth"] = out["pre_vol"] / (out["liq_log"].abs() + 1e-6)
    out["part_concentration"] = 1.0 / out["buyers_pre"].clip(lower=1)
    out["flow_persistence"] = out["trade_count_imbalance"] * np.sign(out["net_flow"])
    keep = [
        "signal_id", "pool", "alt", "decision_time", "label_end_time", "split_role",
        "age_bucket", "age_s", "atomic_net_return", "xs_return_pctile", "barrier_event",
        "atomic_sell_failure", "mae_before_mfe", "expected_shortfall", "time_to_sellable",
        "decision_block", "fee",
    ] + FEATURE_COLS
    keep = list(dict.fromkeys([c for c in keep if c in out.columns]))
    feat = out[keep].copy()
    assert_no_banned_features(FEATURE_COLS)
    return feat


def main():
    FEAT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(LABELS)
    feat = build_feature_table(df)
    path = FEAT_DIR / "feature_matrix.parquet"
    feat.to_parquet(path, index=False)
    meta = {
        "created_utc": utcnow(),
        "n_rows": int(len(feat)),
        "feature_cols": FEATURE_COLS,
        "price_only": PRICE_ONLY,
        "sellability_only": SELLABILITY_ONLY,
        "path": str(path),
        "banned_check": "passed",
    }
    (FEAT_DIR / "feature_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2), flush=True)


if __name__ == "__main__":
    main()
