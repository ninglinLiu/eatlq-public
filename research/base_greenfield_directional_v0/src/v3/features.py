"""Orthogonal on-chain feature matrix for V3 Rescue. No LatchPi. No old-line scores."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import assert_no_banned_features, utcnow  # noqa: E402

OUT = ROOT / "out_v3"

FEATURE_COLS = [
    # flow / participation
    "flow_persist_score",
    "active_minutes_pre_feat",
    "n_swaps_pre",
    "ns_pre",
    "trade_count_imbalance",
    "buys_10m",
    "sells_10m",
    "net_flow",
    "buyers_pre",
    "sellers_pre",
    "senders_pre",
    "buyer_sender_ratio",
    "buyer_growth",
    "seller_growth",
    "part_concentration",
    # liquidity regime
    "liq_log",
    "liq_trend",
    "liq_drawdown",
    "liq_accel",
    "depth_proxy",
    "entry_slip_proxy",
    "buy_ok_proxy",
    # path state
    "early_ret",
    "pre_vol",
    "pre_pullback",
    "pre_runup_from_lo",
    "range_location",
    "mom_h1",
    "mom_h2",
    "accel",
    "vol_depth",
    "xs_early_ret_rank",
    # deployer PIT
    "deployer_prior_pools",
    "deployer_prior_fatal_rate",
    "deployer_prior_right_tail_rate",
    "deployer_age_hours",
    # tradability / infra observables at decision (not future)
    "infra_lag_s",
    "executable_depth_weth",
    "age_s",
    "base_breadth_1h",
    "opportunity_supply",
]

PRICE_ONLY = [
    "early_ret",
    "pre_vol",
    "pre_pullback",
    "pre_runup_from_lo",
    "range_location",
    "mom_h1",
    "mom_h2",
    "accel",
    "xs_early_ret_rank",
    "age_s",
]

HYPOTHESIS_FEATURE_MAP = {
    "H_FLOW_PERSIST": [
        "flow_persist_score",
        "active_minutes_pre_feat",
        "n_swaps_pre",
        "trade_count_imbalance",
        "buys_10m",
        "sells_10m",
        "net_flow",
        "age_s",
    ],
    "H_LIQ_REGIME": [
        "liq_log",
        "liq_trend",
        "liq_drawdown",
        "liq_accel",
        "depth_proxy",
        "vol_depth",
        "age_s",
    ],
    "H_PARTICIPANT": [
        "buyers_pre",
        "sellers_pre",
        "senders_pre",
        "part_concentration",
        "buyer_growth",
        "seller_growth",
        "buyer_sender_ratio",
        "age_s",
    ],
    "H_DEPLOYER_PIT": [
        "deployer_prior_pools",
        "deployer_prior_fatal_rate",
        "deployer_prior_right_tail_rate",
        "deployer_age_hours",
        "age_s",
    ],
    "H_PATH_STATE": PRICE_ONLY
    + [
        "vol_depth",
        "flow_persist_score",
        "depth_proxy",
    ],
    "H_TRADEABLE_RISK_SPLIT": [
        "buy_ok_proxy",
        "depth_proxy",
        "entry_slip_proxy",
        "liq_drawdown",
        "infra_lag_s",
        "flow_persist_score",
        "executable_depth_weth",
        "age_s",
    ],
}

ID_COLS = [
    "signal_id",
    "opportunity_id",
    "pool",
    "deployer",
    "decision_time",
    "label_end_time",
    "split_role",
    "age_bucket",
    "age_s",
    "formal_bucket",
    "is_negative_control",
    "wallet_pnl_weth",
    "pnl_bucket",
    "fatal_event",
    "resolved_trade",
    "y_buy_fail",
    "y_current_unsellable",
    "y_future_unsellable",
    "y_liquidity_collapse",
    "y_infrastructure_unknown",
    "y_recovery_fraction",
    "y_right_tail_25",
    "y_right_tail_50",
    "y_right_tail_100",
    "y_tp_before_sl",
    "y_mae_before_mfe",
    "y_conditional_return",
    "mfe",
    "mae",
    "terminal_ret",
    "time_to_right_tail",
    "buy_status",
    "current_sell_status",
    "future_sell_status",
]


def build_features(labels: pd.DataFrame | None = None) -> pd.DataFrame:
    if labels is None:
        labels = pd.read_parquet(OUT / "labels" / "dual_labels.parquet")
    d = labels.copy()
    for c in FEATURE_COLS:
        if c not in d.columns:
            d[c] = 0.0
    assert_no_banned_features(FEATURE_COLS)
    keep = list(dict.fromkeys([c for c in ID_COLS if c in d.columns] + FEATURE_COLS))
    feat = d.loc[:, keep].copy()
    assert_no_banned_features(list(feat.columns))
    path = OUT / "features" / "feature_matrix.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    feat.to_parquet(path, index=False)
    meta = {
        "created_utc": utcnow(),
        "n": int(len(feat)),
        "n_features": len(FEATURE_COLS),
        "feature_cols": FEATURE_COLS,
        "latchpi_used": False,
        "old_line_inputs_used": False,
    }
    (OUT / "features" / "FEATURE_META.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"[v3.features] n={len(feat)} cols={len(FEATURE_COLS)}", flush=True)
    return feat


def main() -> None:
    build_features()


if __name__ == "__main__":
    main()
