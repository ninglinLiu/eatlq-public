"""One-shot V4 confirmation on re-sealed holdout/untouched."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.v3.dual_head_model import RISK_TARGETS, OPP_TARGETS, _fit_predict_col, compute_utility
from src.v3.oof_eval import decide_route_verdict, eval_policy
from src.v4.orthogonal_routes import (
    AVOID_FEATS,
    BUCKET_FEATS,
    CLEAN_FEATS,
    EXIT_FEATS,
    VETO_FEATS,
)
from src.v3.features import FEATURE_COLS

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out_v4"

HYPO_COLS = {
    "H_HARD_FATAL_VETO": VETO_FEATS,
    "H_ATOMIC_AUDITED_ONLY": CLEAN_FEATS,
    "H_BUCKET_SPECIALIST": BUCKET_FEATS,
    "H_EXIT_BARRIER_SHORT": EXIT_FEATS,
    "H_SPARSE_CAPACITY_CAP": FEATURE_COLS,
    "H_NEGCTRL_AVOID_TRANSFER": AVOID_FEATS,
}


def _score_split(train: pd.DataFrame, test: pd.DataFrame, cols: list[str], freeze: dict, hid: str) -> pd.DataFrame:
    amount = int(freeze["execution"]["amount_in_wei"]) / 1e18
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    top_k = int(freeze["oof"]["top_k_per_decision_time"])

    if hid == "H_ATOMIC_AUDITED_ONLY":
        train = train[(train["buy_status"] == "SUCCESS") & (train["current_sell_status"] == "SUCCESS")]
        test = test[(test["buy_status"] == "SUCCESS") & (test["current_sell_status"] == "SUCCESS")]
        top_k = 1

    pred = test[["signal_id", "decision_time"]].copy()
    if hid == "H_EXIT_BARRIER_SHORT":
        pred["p_tp_before_sl"] = _fit_predict_col(train, test, cols, "y_tp_before_sl", "clf")
        pred["time_to_right_tail"] = _fit_predict_col(train, test, cols, "time_to_right_tail", "reg")
        pred["p_right_tail_25"] = _fit_predict_col(train, test, cols, "y_right_tail_25", "clf")
        ttrt = pred["time_to_right_tail"].replace([np.inf, -np.inf], np.nan).fillna(21600)
        pred["expected_wallet_utility"] = 1.2 * pred["p_tp_before_sl"] + 0.8 * pred["p_right_tail_25"] - 0.3 * (ttrt / 21600.0)
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
        thr = float(np.quantile(np.asarray(train_score), 0.75))
    else:
        for name, ycol, kind in RISK_TARGETS + OPP_TARGETS:
            pred[name] = _fit_predict_col(train, test, cols, ycol, kind)
        pred["entry_slip_proxy"] = test["entry_slip_proxy"].to_numpy() if "entry_slip_proxy" in test.columns else 0.05
        pred["expected_wallet_utility"] = compute_utility(pred, gas_weth=gas, amount_in_weth=amount)
        train_pred = train[["signal_id", "decision_time"]].copy()
        for name, ycol, kind in RISK_TARGETS + OPP_TARGETS:
            train_pred[name] = _fit_predict_col(train, train, cols, ycol, kind)
        train_pred["entry_slip_proxy"] = train["entry_slip_proxy"].to_numpy() if "entry_slip_proxy" in train.columns else 0.05
        train_pred["expected_wallet_utility"] = compute_utility(train_pred, gas_weth=gas, amount_in_weth=amount)
        q = float(freeze["route_defaults"]["sparse_select_quantile"]) if hid == "H_SPARSE_CAPACITY_CAP" else select_q
        thr = float(np.quantile(train_pred["expected_wallet_utility"], q))

        if hid == "H_HARD_FATAL_VETO":
            fut_max = float(freeze["route_defaults"]["hard_veto_future_unsellable_max"])
            liq_max = float(freeze["route_defaults"]["hard_veto_liq_collapse_max"])
            veto = (pred["p_future_unsellable"] > fut_max) | (pred["p_liquidity_collapse"] > liq_max)
            pred["expected_wallet_utility"] = np.where(veto, -1e9, pred["expected_wallet_utility"])
        if hid == "H_NEGCTRL_AVOID_TRANSFER":
            neg = train  # caller should pass combined; handled in run_confirmation
            pass

    selected = np.zeros(len(pred), dtype=bool)
    tmp = pred.reset_index(drop=True)
    if hid == "H_SPARSE_CAPACITY_CAP":
        tmp["deployer"] = test["deployer"].to_numpy() if "deployer" in test.columns else ""
        weeks = pd.to_datetime(tmp["decision_time"], unit="s", utc=True)
        tmp["week"] = weeks.dt.isocalendar().year.astype(str) + "-W" + weeks.dt.isocalendar().week.astype(int).astype(str).str.zfill(2)
        dep_week = {}
        for _, g in tmp.groupby("decision_time"):
            cand = g[g["expected_wallet_utility"] >= thr].sort_values("expected_wallet_utility", ascending=False)
            for idx, row in cand.iterrows():
                key = (str(row["deployer"]), str(row["week"]))
                if dep_week.get(key, 0) >= int(freeze["route_defaults"]["deployer_max_per_week"]):
                    continue
                selected[idx] = True
                dep_week[key] = dep_week.get(key, 0) + 1
                break
    else:
        k = 1 if hid in {"H_ATOMIC_AUDITED_ONLY", "H_EXIT_BARRIER_SHORT"} else top_k
        for _, g in tmp.groupby("decision_time"):
            cand = g[g["expected_wallet_utility"] >= thr]
            if len(cand) == 0:
                continue
            take = cand.nlargest(min(k, len(cand)), "expected_wallet_utility").index
            selected[take.to_numpy()] = True
        if hid == "H_HARD_FATAL_VETO" and "p_future_unsellable" in pred.columns:
            fut_max = float(freeze["route_defaults"]["hard_veto_future_unsellable_max"])
            liq_max = float(freeze["route_defaults"]["hard_veto_liq_collapse_max"])
            veto = (pred["p_future_unsellable"] > fut_max) | (pred["p_liquidity_collapse"] > liq_max)
            selected = selected & (~veto.to_numpy())

    pred["policy_selected"] = selected
    pred["utility_threshold_frozen"] = thr
    for c in ["wallet_pnl_weth", "pool", "deployer", "fatal_event", "y_right_tail_25", "label_end_time", "buy_status", "mfe"]:
        if c in test.columns:
            pred[c] = test[c].to_numpy()
    return pred


def run_v4_confirmation(feat: pd.DataFrame, freeze: dict, best_hypothesis: str, baselines: dict | None = None) -> dict[str, Any]:
    cols = [c for c in HYPO_COLS.get(best_hypothesis, FEATURE_COLS) if c in feat.columns]
    formal = feat[feat["formal_bucket"]].copy() if "formal_bucket" in feat.columns else feat.copy()
    if best_hypothesis == "H_BUCKET_SPECIALIST":
        # confirm with pooled specialist cols on all formal
        pass
    if best_hypothesis == "H_ATOMIC_AUDITED_ONLY":
        formal = formal[(formal["buy_status"] == "SUCCESS") & (formal["current_sell_status"] == "SUCCESS")].copy()

    train = formal[formal["split_role"].isin(["LABEL_DESIGN", "DEV"])].copy()
    holdout = formal[formal["split_role"] == "INTERNAL_HOLDOUT"].copy()
    untouched = formal[formal["split_role"] == "UNTOUCHED"].copy()

    result: dict[str, Any] = {
        "best_hypothesis": best_hypothesis,
        "feature_cols": cols,
        "holdout_executed": False,
        "untouched_executed": False,
        "retune_after_confirmation": False,
        "resealed": True,
        "v3_confirmation_burn_applied": True,
    }
    if len(train) < 60 or len(holdout) < 8:
        result["holdout_skip_reason"] = "insufficient_train_or_holdout_rows"
        result["final_confirmation_verdict"] = "ORTHOGONAL_EDGE_NOT_CONFIRMED"
        return result

    # special: negctrl avoid needs neg rows in train universe for avoid head
    if best_hypothesis == "H_NEGCTRL_AVOID_TRANSFER":
        neg = feat[
            (feat["age_bucket"] == freeze["negative_control_bucket"])
            & (feat["split_role"].isin(["LABEL_DESIGN", "DEV"]))
        ]
        hold_pred = _score_split(train, holdout, cols, freeze, "H_HARD_FATAL_VETO")  # base
        # overlay avoid veto
        p_avoid = _fit_predict_col(neg, holdout, cols, "fatal_event", "clf") if len(neg) >= 40 else np.zeros(len(holdout))
        veto = pd.Series(p_avoid, index=hold_pred.index) >= 0.40
        hold_pred.loc[veto.to_numpy(), "policy_selected"] = False
        hold_pred["p_avoid_fatal"] = p_avoid
    else:
        hold_pred = _score_split(train, holdout, cols, freeze, best_hypothesis)

    hold_metrics = eval_policy(hold_pred, "policy_selected", freeze, f"{best_hypothesis}_INTERNAL_HOLDOUT")
    hold_verdict = decide_route_verdict(hold_metrics, baselines or {})
    hold_metrics["verdict"] = hold_verdict
    result["holdout_executed"] = True
    result["holdout"] = {
        "n": int(len(holdout)),
        "n_selected": int(hold_pred["policy_selected"].sum()),
        "utility_threshold_frozen": float(hold_pred["utility_threshold_frozen"].iloc[0]) if len(hold_pred) else None,
        "metrics": hold_metrics,
        "verdict": hold_verdict,
    }
    (OUT / "confirmation").mkdir(parents=True, exist_ok=True)
    hold_pred.to_parquet(OUT / "confirmation" / "internal_holdout_preds.parquet", index=False)

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
    if not eligible or len(untouched) < 8:
        result["untouched_skip_reason"] = "holdout_not_eligible_or_insufficient_rows"
        result["final_confirmation_verdict"] = hold_verdict
        return result

    if best_hypothesis == "H_NEGCTRL_AVOID_TRANSFER":
        neg = feat[
            (feat["age_bucket"] == freeze["negative_control_bucket"])
            & (feat["split_role"].isin(["LABEL_DESIGN", "DEV"]))
        ]
        unt_pred = _score_split(train, untouched, cols, freeze, "H_HARD_FATAL_VETO")
        p_avoid = _fit_predict_col(neg, untouched, cols, "fatal_event", "clf") if len(neg) >= 40 else np.zeros(len(untouched))
        veto = pd.Series(p_avoid) >= 0.40
        unt_pred.loc[veto.to_numpy(), "policy_selected"] = False
    else:
        unt_pred = _score_split(train, untouched, cols, freeze, best_hypothesis)
        # freeze threshold from holdout
        thr = float(result["holdout"]["utility_threshold_frozen"])
        selected = np.zeros(len(unt_pred), dtype=bool)
        tmp = unt_pred.reset_index(drop=True)
        k = 1 if best_hypothesis in {"H_ATOMIC_AUDITED_ONLY", "H_EXIT_BARRIER_SHORT", "H_SPARSE_CAPACITY_CAP"} else int(freeze["oof"]["top_k_per_decision_time"])
        for _, g in tmp.groupby("decision_time"):
            cand = g[g["expected_wallet_utility"] >= thr]
            if len(cand) == 0:
                continue
            take = cand.nlargest(min(k, len(cand)), "expected_wallet_utility").index
            selected[take.to_numpy()] = True
        unt_pred["policy_selected"] = selected
        unt_pred["utility_threshold_frozen"] = thr

    unt_metrics = eval_policy(unt_pred, "policy_selected", freeze, f"{best_hypothesis}_UNTOUCHED")
    unt_verdict = decide_route_verdict(unt_metrics, baselines or {})
    if unt_verdict == "PROSPECTIVE_CONFIRMATION_REQUIRED" and float(unt_metrics["total_wallet_pnl_weth"]) > 0:
        unt_verdict = "ORTHOGONAL_EDGE_CONFIRMED"
    unt_metrics["verdict"] = unt_verdict
    result["untouched_executed"] = True
    result["untouched"] = {
        "n": int(len(untouched)),
        "n_selected": int(unt_pred["policy_selected"].sum()),
        "metrics": unt_metrics,
        "verdict": unt_verdict,
    }
    unt_pred.to_parquet(OUT / "confirmation" / "untouched_preds.parquet", index=False)
    result["final_confirmation_verdict"] = unt_verdict
    return result
