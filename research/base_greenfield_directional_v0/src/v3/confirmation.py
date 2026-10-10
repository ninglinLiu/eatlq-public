"""One-shot INTERNAL_HOLDOUT and UNTOUCHED confirmation.

After these run, features/models/thresholds must not be retuned.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

from src.v3.dual_head_model import (
    OPP_TARGETS,
    RISK_TARGETS,
    _X,
    _fit_predict_col,
    compute_utility,
)
from src.v3.features import FEATURE_COLS, HYPOTHESIS_FEATURE_MAP
from src.v3.oof_eval import decide_route_verdict, eval_policy

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out_v3"


def _train_predict_split(
    train: pd.DataFrame,
    test: pd.DataFrame,
    cols: list[str],
    freeze: dict,
) -> pd.DataFrame:
    amount = int(freeze["execution"]["amount_in_wei"]) / 1e18
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    top_k = int(freeze["oof"]["top_k_per_decision_time"])
    pred = test[["signal_id", "decision_time"]].copy()
    for name, ycol, kind in RISK_TARGETS + OPP_TARGETS:
        pred[name] = _fit_predict_col(train, test, cols, ycol, kind)
    pred["entry_slip_proxy"] = test["entry_slip_proxy"].to_numpy() if "entry_slip_proxy" in test.columns else 0.05
    pred["expected_wallet_utility"] = compute_utility(pred, gas_weth=gas, amount_in_weth=amount)
    # threshold frozen from train utilities distribution
    train_pred = train[["signal_id", "decision_time"]].copy()
    for name, ycol, kind in RISK_TARGETS + OPP_TARGETS:
        train_pred[name] = _fit_predict_col(train, train, cols, ycol, kind)
    train_pred["entry_slip_proxy"] = train["entry_slip_proxy"].to_numpy() if "entry_slip_proxy" in train.columns else 0.05
    train_pred["expected_wallet_utility"] = compute_utility(train_pred, gas_weth=gas, amount_in_weth=amount)
    thr = float(np.quantile(train_pred["expected_wallet_utility"], select_q))
    selected = np.zeros(len(pred), dtype=bool)
    tmp = pred.reset_index(drop=True)
    for _, g in tmp.groupby("decision_time"):
        cand = g[g["expected_wallet_utility"] >= thr]
        if len(cand) == 0:
            continue
        take = cand.nlargest(min(top_k, len(cand)), "expected_wallet_utility").index
        selected[take.to_numpy()] = True
    pred["policy_selected"] = selected
    for c in [
        "wallet_pnl_weth",
        "pool",
        "deployer",
        "age_bucket",
        "fatal_event",
        "y_right_tail_25",
        "label_end_time",
        "buy_status",
        "mfe",
        "formal_bucket",
    ]:
        if c in test.columns:
            pred[c] = test[c].to_numpy()
    pred["utility_threshold_frozen"] = thr
    return pred


def run_confirmation(
    feat: pd.DataFrame,
    freeze: dict,
    best_hypothesis: str,
    baseline_metrics_for_compare: dict | None = None,
) -> dict[str, Any]:
    """
    Train on LABEL_DESIGN+DEV (+ optionally not holdout), score INTERNAL_HOLDOUT once,
    then if eligible score UNTOUCHED once.
    """
    cols = HYPOTHESIS_FEATURE_MAP.get(best_hypothesis, FEATURE_COLS)
    cols = [c for c in cols if c in feat.columns]
    formal = feat[feat["formal_bucket"]].copy() if "formal_bucket" in feat.columns else feat.copy()

    train_roles = {"LABEL_DESIGN", "DEV"}
    train = formal[formal["split_role"].isin(train_roles)].copy()
    holdout = formal[formal["split_role"] == "INTERNAL_HOLDOUT"].copy()
    untouched = formal[formal["split_role"] == "UNTOUCHED"].copy()

    result: dict[str, Any] = {
        "best_hypothesis": best_hypothesis,
        "feature_cols": cols,
        "holdout_executed": False,
        "untouched_executed": False,
        "holdout": None,
        "untouched": None,
        "retune_after_confirmation": False,
        "note": "one-shot confirmation; thresholds frozen from train only",
    }

    if len(train) < 80 or len(holdout) < 10:
        result["holdout_skip_reason"] = "insufficient_train_or_holdout_rows"
        result["final_confirmation_verdict"] = "ORTHOGONAL_EDGE_NOT_CONFIRMED"
        return result

    hold_pred = _train_predict_split(train, holdout, cols, freeze)
    hold_metrics = eval_policy(hold_pred, "policy_selected", freeze, f"{best_hypothesis}_INTERNAL_HOLDOUT")
    # compare against empty baselines dict → decide_route_verdict still works
    hold_verdict = decide_route_verdict(hold_metrics, baseline_metrics_for_compare or {})
    hold_metrics["verdict"] = hold_verdict
    result["holdout_executed"] = True
    result["holdout"] = {
        "n": int(len(holdout)),
        "n_selected": int(hold_pred["policy_selected"].sum()),
        "utility_threshold_frozen": float(hold_pred["utility_threshold_frozen"].iloc[0]),
        "metrics": hold_metrics,
        "verdict": hold_verdict,
    }
    hold_pred.to_parquet(OUT / "confirmation" / "internal_holdout_preds.parquet", index=False)

    # Eligibility for untouched: holdout not lottery/exec-risk dominated and pnl>=0 haircut
    eligible = hold_verdict in {
        "PROSPECTIVE_CONFIRMATION_REQUIRED",
        "ORTHOGONAL_EDGE_CONFIRMED",
    } or (
        hold_verdict == "ORTHOGONAL_EDGE_NOT_CONFIRMED"
        and float(hold_metrics["conservative_tail_haircut_pnl_weth"]) > 0
        and float(hold_metrics["total_wallet_pnl_weth"]) > 0
    )
    # Stricter: only proceed if holdout wallet pnl > 0 and not lottery/exec risk
    eligible = (
        float(hold_metrics["total_wallet_pnl_weth"]) > 0
        and float(hold_metrics["conservative_tail_haircut_pnl_weth"]) > 0
        and hold_verdict
        not in {
            "LOTTERY_DEPENDENT_REJECT",
            "EXECUTION_RISK_DOMINATES",
            "INSUFFICIENT_INDEPENDENT_RIGHT_TAIL_EVENTS",
        }
    )

    if not eligible or len(untouched) < 10:
        result["untouched_skip_reason"] = "holdout_not_eligible_or_insufficient_rows"
        result["final_confirmation_verdict"] = hold_verdict if hold_verdict != "PROSPECTIVE_CONFIRMATION_REQUIRED" else "ORTHOGONAL_EDGE_NOT_CONFIRMED"
        return result

    # Train includes holdout labels only for final untouched fit? Protocol: freeze before untouched;
    # train on LABEL_DESIGN+DEV only (do not fit on holdout after seeing it for selection — already used holdout for gate).
    # For scoring untouched, refit on LABEL_DESIGN+DEV with same frozen threshold rule.
    unt_pred = _train_predict_split(train, untouched, cols, freeze)
    # force same threshold as holdout freeze
    thr = float(result["holdout"]["utility_threshold_frozen"])
    selected = np.zeros(len(unt_pred), dtype=bool)
    tmp = unt_pred.reset_index(drop=True)
    top_k = int(freeze["oof"]["top_k_per_decision_time"])
    for _, g in tmp.groupby("decision_time"):
        cand = g[g["expected_wallet_utility"] >= thr]
        if len(cand) == 0:
            continue
        take = cand.nlargest(min(top_k, len(cand)), "expected_wallet_utility").index
        selected[take.to_numpy()] = True
    unt_pred["policy_selected"] = selected
    unt_pred["utility_threshold_frozen"] = thr
    unt_metrics = eval_policy(unt_pred, "policy_selected", freeze, f"{best_hypothesis}_UNTOUCHED")
    unt_verdict = decide_route_verdict(unt_metrics, baseline_metrics_for_compare or {})
    if unt_verdict == "PROSPECTIVE_CONFIRMATION_REQUIRED" and float(unt_metrics["total_wallet_pnl_weth"]) > 0:
        unt_verdict = "ORTHOGONAL_EDGE_CONFIRMED"
    unt_metrics["verdict"] = unt_verdict
    result["untouched_executed"] = True
    result["untouched"] = {
        "n": int(len(untouched)),
        "n_selected": int(unt_pred["policy_selected"].sum()),
        "utility_threshold_frozen": thr,
        "metrics": unt_metrics,
        "verdict": unt_verdict,
    }
    unt_pred.to_parquet(OUT / "confirmation" / "untouched_preds.parquet", index=False)
    result["final_confirmation_verdict"] = unt_verdict
    return result
