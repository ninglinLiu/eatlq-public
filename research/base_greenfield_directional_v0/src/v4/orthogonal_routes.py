"""V4 orthogonal selection policies — structure/sample/exit/capacity, not V3 retunes."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.v3.dual_head_model import (
    OPP_TARGETS,
    RISK_TARGETS,
    _fit_predict_col,
    compute_utility,
    walk_forward_folds,
)
from src.v3.features import FEATURE_COLS

VETO_FEATS = [
    "liq_drawdown",
    "depth_proxy",
    "buy_ok_proxy",
    "infra_lag_s",
    "executable_depth_weth",
    "flow_persist_score",
    "early_ret",
    "pre_vol",
]
EXIT_FEATS = ["mom_h1", "mom_h2", "accel", "pre_vol", "range_location", "flow_persist_score", "vol_depth"]
AVOID_FEATS = [
    "early_ret",
    "pre_vol",
    "liq_drawdown",
    "flow_persist_score",
    "buys_10m",
    "sells_10m",
    "part_concentration",
    "mom_h2",
]
BUCKET_FEATS = [
    "age_s",
    "flow_persist_score",
    "liq_trend",
    "liq_drawdown",
    "early_ret",
    "mom_h2",
    "part_concentration",
    "deployer_prior_fatal_rate",
]
CLEAN_FEATS = [
    "executable_depth_weth",
    "entry_slip_proxy",
    "depth_proxy",
    "buy_ok_proxy",
    "flow_persist_score",
    "liq_log",
    "early_ret",
    "pre_vol",
    "age_s",
]


def _week_key(ts: int) -> str:
    t = pd.Timestamp(int(ts), unit="s", tz="UTC")
    iso = t.isocalendar()
    return f"{iso.year}-W{int(iso.week):02d}"


def _select_topk_utility(pred: pd.DataFrame, thr: float, top_k: int) -> np.ndarray:
    selected = np.zeros(len(pred), dtype=bool)
    tmp = pred.reset_index(drop=True)
    for _, g in tmp.groupby("decision_time"):
        cand = g[g["expected_wallet_utility"] >= thr]
        if len(cand) == 0:
            continue
        take = cand.nlargest(min(top_k, len(cand)), "expected_wallet_utility").index
        selected[take.to_numpy()] = True
    return selected


def _attach_eval_cols(pred: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    for c in [
        "wallet_pnl_weth",
        "pool",
        "deployer",
        "age_bucket",
        "fatal_event",
        "y_right_tail_25",
        "label_end_time",
        "buy_status",
        "current_sell_status",
        "mfe",
        "formal_bucket",
        "split_role",
    ]:
        if c in test.columns:
            pred[c] = test[c].to_numpy()
    return pred


def _base_dual_pred(train: pd.DataFrame, test: pd.DataFrame, cols: list[str], freeze: dict) -> pd.DataFrame:
    amount = int(freeze["execution"]["amount_in_wei"]) / 1e18
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    pred = test[["signal_id", "decision_time"]].copy()
    for name, ycol, kind in RISK_TARGETS + OPP_TARGETS:
        pred[name] = _fit_predict_col(train, test, cols, ycol, kind)
    pred["entry_slip_proxy"] = test["entry_slip_proxy"].to_numpy() if "entry_slip_proxy" in test.columns else 0.05
    pred["expected_wallet_utility"] = compute_utility(pred, gas_weth=gas, amount_in_weth=amount)
    return pred


def oof_hard_fatal_veto(df: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    cols = [c for c in VETO_FEATS if c in df.columns]
    formal = df[(df["split_role"] == "DEV") & (df["formal_bucket"])].copy()
    folds = walk_forward_folds(formal, n_folds=int(freeze["oof"]["n_folds"]), embargo_s=int(freeze["oof"]["embargo_seconds"]))
    fut_max = float(freeze["route_defaults"]["hard_veto_future_unsellable_max"])
    liq_max = float(freeze["route_defaults"]["hard_veto_liq_collapse_max"])
    top_k = int(freeze["oof"]["top_k_per_decision_time"])
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    parts = []
    for train, test, meta in folds:
        pred = _base_dual_pred(train, test, cols, freeze)
        train_pred = _base_dual_pred(train, train, cols, freeze)
        thr = float(np.quantile(train_pred["expected_wallet_utility"], select_q))
        veto = (pred["p_future_unsellable"] > fut_max) | (pred["p_liquidity_collapse"] > liq_max)
        pred["expected_wallet_utility"] = np.where(veto, -1e9, pred["expected_wallet_utility"])
        pred["policy_selected"] = _select_topk_utility(pred, thr, top_k) & (~veto.to_numpy())
        pred["fold"] = meta["fold"]
        parts.append(_attach_eval_cols(pred, test))
    oof = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return oof, {"hypothesis_id": "H_HARD_FATAL_VETO", "n_oof": len(oof), "n_selected": int(oof["policy_selected"].sum()) if len(oof) else 0, "feature_cols": cols}


def oof_atomic_audited_only(df: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    cols = [c for c in CLEAN_FEATS if c in df.columns]
    clean = df[
        (df["split_role"] == "DEV")
        & (df["formal_bucket"])
        & (df["buy_status"] == "SUCCESS")
        & (df["current_sell_status"] == "SUCCESS")
    ].copy()
    folds = walk_forward_folds(clean, n_folds=int(freeze["oof"]["n_folds"]), embargo_s=int(freeze["oof"]["embargo_seconds"]))
    top_k = 1
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    parts = []
    for train, test, meta in folds:
        if len(train) < 40 or len(test) < 10:
            continue
        pred = _base_dual_pred(train, test, cols, freeze)
        train_pred = _base_dual_pred(train, train, cols, freeze)
        thr = float(np.quantile(train_pred["expected_wallet_utility"], select_q))
        pred["policy_selected"] = _select_topk_utility(pred, thr, top_k)
        pred["fold"] = meta["fold"]
        parts.append(_attach_eval_cols(pred, test))
    oof = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return oof, {
        "hypothesis_id": "H_ATOMIC_AUDITED_ONLY",
        "n_oof": len(oof),
        "n_selected": int(oof["policy_selected"].sum()) if len(oof) else 0,
        "feature_cols": cols,
        "n_clean_dev": int(len(clean)),
    }


def oof_bucket_specialist(df: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    cols = [c for c in BUCKET_FEATS if c in df.columns]
    top_k = int(freeze["oof"]["top_k_per_decision_time"])
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    parts = []
    bucket_meta = {}
    for bucket in freeze["formal_buckets"]:
        sub = df[(df["split_role"] == "DEV") & (df["age_bucket"] == bucket)].copy()
        folds = walk_forward_folds(sub, n_folds=min(4, int(freeze["oof"]["n_folds"])), embargo_s=int(freeze["oof"]["embargo_seconds"]))
        n_sel_b = 0
        for train, test, meta in folds:
            if len(train) < 40 or len(test) < 8:
                continue
            pred = _base_dual_pred(train, test, cols, freeze)
            train_pred = _base_dual_pred(train, train, cols, freeze)
            thr = float(np.quantile(train_pred["expected_wallet_utility"], select_q))
            pred["policy_selected"] = _select_topk_utility(pred, thr, top_k)
            pred["fold"] = int(meta["fold"]) + 100 * hash(bucket) % 97
            pred["specialist_bucket"] = bucket
            parts.append(_attach_eval_cols(pred, test))
            n_sel_b += int(pred["policy_selected"].sum())
        bucket_meta[bucket] = {"n_dev": int(len(sub)), "n_selected_oof": n_sel_b}
    oof = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return oof, {"hypothesis_id": "H_BUCKET_SPECIALIST", "n_oof": len(oof), "n_selected": int(oof["policy_selected"].sum()) if len(oof) else 0, "feature_cols": cols, "per_bucket": bucket_meta}


def oof_exit_barrier_short(df: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    cols = [c for c in EXIT_FEATS if c in df.columns]
    formal = df[(df["split_role"] == "DEV") & (df["formal_bucket"]) & (df["age_bucket"].isin(["1-6h", "6-24h", "1-3d"]))].copy()
    folds = walk_forward_folds(formal, n_folds=int(freeze["oof"]["n_folds"]), embargo_s=int(freeze["oof"]["embargo_seconds"]))
    parts = []
    for train, test, meta in folds:
        pred = test[["signal_id", "decision_time"]].copy()
        pred["p_tp_before_sl"] = _fit_predict_col(train, test, cols, "y_tp_before_sl", "clf")
        pred["time_to_right_tail"] = _fit_predict_col(train, test, cols, "time_to_right_tail", "reg")
        pred["p_right_tail_25"] = _fit_predict_col(train, test, cols, "y_right_tail_25", "clf")
        # score: want high TP-before-SL, high RT prob, low time-to-tail
        ttrt = pred["time_to_right_tail"].replace([np.inf, -np.inf], np.nan).fillna(21600)
        pred["expected_wallet_utility"] = (
            1.2 * pred["p_tp_before_sl"] + 0.8 * pred["p_right_tail_25"] - 0.3 * (ttrt / 21600.0)
        )
        train_score = (
            1.2 * _fit_predict_col(train, train, cols, "y_tp_before_sl", "clf")
            + 0.8 * _fit_predict_col(train, train, cols, "y_right_tail_25", "clf")
            - 0.3
            * (
                pd.Series(_fit_predict_col(train, train, cols, "time_to_right_tail", "reg"))
                .replace([np.inf, -np.inf], np.nan)
                .fillna(21600)
                / 21600.0
            )
        )
        thr = float(np.quantile(train_score, 0.75))
        pred["policy_selected"] = _select_topk_utility(pred, thr, top_k=1)
        pred["fold"] = meta["fold"]
        parts.append(_attach_eval_cols(pred, test))
    oof = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return oof, {"hypothesis_id": "H_EXIT_BARRIER_SHORT", "n_oof": len(oof), "n_selected": int(oof["policy_selected"].sum()) if len(oof) else 0, "feature_cols": cols}


def oof_sparse_capacity_cap(df: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    cols = [c for c in FEATURE_COLS if c in df.columns]
    formal = df[(df["split_role"] == "DEV") & (df["formal_bucket"])].copy()
    folds = walk_forward_folds(formal, n_folds=int(freeze["oof"]["n_folds"]), embargo_s=int(freeze["oof"]["embargo_seconds"]))
    q = float(freeze["route_defaults"]["sparse_select_quantile"])
    dep_cap = int(freeze["route_defaults"]["deployer_max_per_week"])
    parts = []
    for train, test, meta in folds:
        pred = _base_dual_pred(train, test, cols, freeze)
        train_pred = _base_dual_pred(train, train, cols, freeze)
        thr = float(np.quantile(train_pred["expected_wallet_utility"], q))
        # at most 1 per decision_time among above thr
        selected = np.zeros(len(pred), dtype=bool)
        tmp = pred.reset_index(drop=True)
        tmp["deployer"] = test["deployer"].to_numpy() if "deployer" in test.columns else ""
        tmp["week"] = [_week_key(int(t)) for t in tmp["decision_time"]]
        dep_week_count: dict[tuple[str, str], int] = {}
        for t, g in tmp.groupby("decision_time"):
            cand = g[g["expected_wallet_utility"] >= thr].sort_values("expected_wallet_utility", ascending=False)
            for idx, row in cand.iterrows():
                key = (str(row["deployer"]), str(row["week"]))
                if dep_week_count.get(key, 0) >= dep_cap:
                    continue
                selected[idx] = True
                dep_week_count[key] = dep_week_count.get(key, 0) + 1
                break  # one per decision_time
        pred["policy_selected"] = selected
        pred["fold"] = meta["fold"]
        parts.append(_attach_eval_cols(pred, test))
    oof = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return oof, {"hypothesis_id": "H_SPARSE_CAPACITY_CAP", "n_oof": len(oof), "n_selected": int(oof["policy_selected"].sum()) if len(oof) else 0, "feature_cols": cols}


def oof_negctrl_avoid_transfer(df: pd.DataFrame, freeze: dict) -> tuple[pd.DataFrame, dict]:
    cols = [c for c in AVOID_FEATS if c in df.columns]
    # avoid model trained on negative control only (any split except untouched/holdout for purity use DEV+LD negctrl)
    neg = df[(df["age_bucket"] == freeze["negative_control_bucket"]) & (df["split_role"].isin(["LABEL_DESIGN", "DEV"]))].copy()
    formal = df[(df["split_role"] == "DEV") & (df["formal_bucket"])].copy()
    folds = walk_forward_folds(formal, n_folds=int(freeze["oof"]["n_folds"]), embargo_s=int(freeze["oof"]["embargo_seconds"]))
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    top_k = int(freeze["oof"]["top_k_per_decision_time"])
    parts = []
    for train, test, meta in folds:
        # PIT: only negctrl rows with decision_time <= train end
        train_end = int(train["decision_time"].max())
        neg_tr = neg[neg["decision_time"] <= train_end]
        if len(neg_tr) < 40:
            continue
        p_avoid = _fit_predict_col(neg_tr, test, cols, "fatal_event", "clf")
        pred = _base_dual_pred(train, test, cols, freeze)
        pred["p_avoid_fatal"] = p_avoid
        # veto high avoid score
        veto = pred["p_avoid_fatal"] >= 0.40
        pred["expected_wallet_utility"] = np.where(veto, -1e9, pred["expected_wallet_utility"])
        train_pred = _base_dual_pred(train, train, cols, freeze)
        thr = float(np.quantile(train_pred["expected_wallet_utility"], select_q))
        pred["policy_selected"] = _select_topk_utility(pred, thr, top_k) & (~veto.to_numpy())
        pred["fold"] = meta["fold"]
        parts.append(_attach_eval_cols(pred, test))
    oof = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return oof, {
        "hypothesis_id": "H_NEGCTRL_AVOID_TRANSFER",
        "n_oof": len(oof),
        "n_selected": int(oof["policy_selected"].sum()) if len(oof) else 0,
        "feature_cols": cols,
        "n_negctrl_train_pool": int(len(neg)),
    }


ROUTE_FNS = {
    "H_HARD_FATAL_VETO": oof_hard_fatal_veto,
    "H_ATOMIC_AUDITED_ONLY": oof_atomic_audited_only,
    "H_BUCKET_SPECIALIST": oof_bucket_specialist,
    "H_EXIT_BARRIER_SHORT": oof_exit_barrier_short,
    "H_SPARSE_CAPACITY_CAP": oof_sparse_capacity_cap,
    "H_NEGCTRL_AVOID_TRANSFER": oof_negctrl_avoid_transfer,
}


def fit_v4_routes(df: pd.DataFrame, freeze: dict) -> dict[str, tuple[pd.DataFrame, dict[str, Any]]]:
    out = {}
    for hid, fn in ROUTE_FNS.items():
        out[hid] = fn(df, freeze)
    return out
