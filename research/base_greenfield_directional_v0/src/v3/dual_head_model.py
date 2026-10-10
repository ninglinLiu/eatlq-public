"""Interpretable Risk + Opportunity heads and utility combination."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

from src.v3.features import FEATURE_COLS, HYPOTHESIS_FEATURE_MAP

HGB_CLF = dict(max_depth=3, max_iter=60, learning_rate=0.06, random_state=0)
HGB_REG = dict(max_depth=3, max_iter=60, learning_rate=0.06, random_state=0)

RISK_TARGETS = [
    ("p_buy_fail", "y_buy_fail", "clf"),
    ("p_current_unsellable", "y_current_unsellable", "clf"),
    ("p_future_unsellable", "y_future_unsellable", "clf"),
    ("p_liquidity_collapse", "y_liquidity_collapse", "clf"),
    ("p_infrastructure_unknown", "y_infrastructure_unknown", "clf"),
    ("expected_recovery_fraction", "y_recovery_fraction", "reg"),
]

OPP_TARGETS = [
    ("p_right_tail_25", "y_right_tail_25", "clf"),
    ("p_right_tail_50", "y_right_tail_50", "clf"),
    ("p_right_tail_100", "y_right_tail_100", "clf"),
    ("conditional_q50_return", "y_conditional_return", "reg"),
    ("expected_mfe", "mfe", "reg"),
    ("p_tp_before_sl", "y_tp_before_sl", "clf"),
    ("time_to_right_tail", "time_to_right_tail", "reg"),
]


def _X(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    return df.reindex(columns=cols).replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)


def _fit_predict_col(
    train: pd.DataFrame,
    test: pd.DataFrame,
    cols: list[str],
    ycol: str,
    kind: str,
) -> np.ndarray:
    if ycol not in train.columns:
        return np.zeros(len(test))
    y = pd.to_numeric(train[ycol], errors="coerce")
    mask = y.notna()
    if mask.sum() < 40:
        return np.full(len(test), float(y.mean()) if mask.any() else 0.0)
    Xtr = _X(train.loc[mask], cols)
    ytr = y.loc[mask].to_numpy(dtype=float)
    Xte = _X(test, cols)
    if kind == "clf":
        # need both classes ideally
        if len(np.unique(ytr)) < 2:
            return np.full(len(test), float(ytr.mean()))
        m = HistGradientBoostingClassifier(**HGB_CLF)
        m.fit(Xtr, ytr.astype(int))
        return m.predict_proba(Xte)[:, 1]
    m = HistGradientBoostingRegressor(**HGB_REG)
    m.fit(Xtr, ytr)
    return m.predict(Xte)


def _col(pred: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
    if name in pred.columns:
        return pd.to_numeric(pred[name], errors="coerce").fillna(default)
    return pd.Series(default, index=pred.index, dtype=float)


def compute_utility(
    pred: pd.DataFrame,
    *,
    gas_weth: float,
    amount_in_weth: float,
    capital_time_cost: float = 0.0005,
) -> pd.Series:
    """
    expected_wallet_utility =
      resolved_probability × conditional_trade_value
      - fatal_probability × expected_fatal_loss
      - gas - slippage - capital_time_cost
    """
    p_buy_fail = _col(pred, "p_buy_fail")
    p_fut = _col(pred, "p_future_unsellable")
    p_liq = _col(pred, "p_liquidity_collapse")
    p_infra = _col(pred, "p_infrastructure_unknown")
    p_cur = _col(pred, "p_current_unsellable")
    rec = _col(pred, "expected_recovery_fraction", 0.1).clip(0, 1)

    fatal_p = (0.55 * p_fut + 0.25 * p_liq + 0.10 * p_cur + 0.10 * p_infra).clip(0, 1)
    resolve_p = (1.0 - p_buy_fail) * (1.0 - 0.5 * p_infra)
    cond_value = (
        0.45 * _col(pred, "conditional_q50_return")
        + 0.35 * _col(pred, "expected_mfe").clip(upper=2.0)
        + 0.20 * _col(pred, "p_right_tail_25")
    ) * amount_in_weth
    fatal_loss = amount_in_weth * (1.0 - rec)
    slip = 0.5 * amount_in_weth * _col(pred, "entry_slip_proxy", 0.05)
    gas = 2.0 * gas_weth
    util = resolve_p * cond_value - fatal_p * fatal_loss - gas - slip - capital_time_cost
    return util

def walk_forward_folds(dev: pd.DataFrame, n_folds: int = 5, embargo_s: int = 86400) -> list[tuple[pd.DataFrame, pd.DataFrame, dict]]:
    times = np.sort(dev["decision_time"].unique())
    if len(times) < 4:
        return []
    if len(times) < n_folds + 1:
        n_folds = max(2, min(n_folds, len(times) // 2))
    cuts = np.linspace(0, len(times), n_folds + 1).astype(int)
    folds = []
    for i in range(1, n_folds):
        train_end_t = times[cuts[i] - 1]
        test_start_t = times[cuts[i]]
        test_end_t = times[min(cuts[i + 1], len(times)) - 1] if i + 1 < len(cuts) else times[-1]
        train = dev[dev["decision_time"] <= train_end_t - embargo_s]
        test = dev[(dev["decision_time"] >= test_start_t) & (dev["decision_time"] <= test_end_t)]
        if len(train) < 60 or len(test) < 15:
            continue
        folds.append((train, test, {"fold": i, "train_end": int(train_end_t), "test_start": int(test_start_t), "test_end": int(test_end_t)}))
    return folds


def fit_dual_head_oof(
    df: pd.DataFrame,
    *,
    feature_cols: list[str] | None = None,
    n_folds: int = 5,
    embargo_s: int = 86400,
    gas_weth: float = 0.00008,
    amount_in_weth: float = 0.016666666666666666,
    select_q: float = 0.70,
    top_k: int = 2,
    hypothesis_id: str = "H_FULL_DUAL",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cols = feature_cols or FEATURE_COLS
    cols = [c for c in cols if c in df.columns]
    dev = df[df["split_role"] == "DEV"].copy()
    # modeling universe: formal buckets preferred; keep neg control as optional eval later
    formal = dev[dev["formal_bucket"]].copy() if "formal_bucket" in dev.columns else dev
    if len(formal) < 80:
        formal = dev
    folds = walk_forward_folds(formal, n_folds=n_folds, embargo_s=embargo_s)
    oof_parts = []
    fold_stats = []
    for train, test, meta in folds:
        pred = test[["signal_id", "decision_time"]].copy()
        for name, ycol, kind in RISK_TARGETS + OPP_TARGETS:
            pred[name] = _fit_predict_col(train, test, cols, ycol, kind)
        if "entry_slip_proxy" in test.columns:
            pred["entry_slip_proxy"] = test["entry_slip_proxy"].to_numpy()
        else:
            pred["entry_slip_proxy"] = 0.05
        pred["expected_wallet_utility"] = compute_utility(pred, gas_weth=gas_weth, amount_in_weth=amount_in_weth)
        thr = float(np.quantile(pred["expected_wallet_utility"], select_q))
        # top-k within decision_time among those above thr
        selected = np.zeros(len(pred), dtype=bool)
        tmp = pred.reset_index(drop=True)
        for _, g in tmp.groupby("decision_time"):
            cand = g[g["expected_wallet_utility"] >= thr]
            if len(cand) == 0:
                continue
            take = cand.nlargest(min(top_k, len(cand)), "expected_wallet_utility").index
            selected[take.to_numpy()] = True
        pred["policy_selected"] = selected
        pred["fold"] = meta["fold"]
        # attach labels for eval
        for c in [
            "wallet_pnl_weth",
            "pool",
            "deployer",
            "age_bucket",
            "fatal_event",
            "y_right_tail_25",
            "y_future_unsellable",
            "label_end_time",
            "split_role",
            "formal_bucket",
            "mfe",
        ]:
            if c in test.columns:
                pred[c] = test[c].to_numpy()
        oof_parts.append(pred)
        fold_stats.append(
            {
                "fold": meta["fold"],
                "n_test": int(len(test)),
                "n_selected": int(selected.sum()),
                "mean_utility": float(pred["expected_wallet_utility"].mean()),
                "selected_pnl": float(pred.loc[selected, "wallet_pnl_weth"].sum()) if selected.any() and "wallet_pnl_weth" in pred.columns else 0.0,
            }
        )
    oof = pd.concat(oof_parts, ignore_index=True) if oof_parts else pd.DataFrame()
    meta_out = {
        "hypothesis_id": hypothesis_id,
        "n_folds": len(folds),
        "feature_cols": cols,
        "fold_stats": fold_stats,
        "n_oof": int(len(oof)),
        "n_selected": int(oof["policy_selected"].sum()) if len(oof) and "policy_selected" in oof.columns else 0,
    }
    return oof, meta_out


def fit_hypothesis_routes(
    df: pd.DataFrame,
    freeze: dict,
) -> dict[str, tuple[pd.DataFrame, dict]]:
    amount = int(freeze["execution"]["amount_in_wei"]) / 1e18
    gas = float(freeze["execution"]["gas_weth_per_leg"])
    n_folds = int(freeze["oof"]["n_folds"])
    embargo = int(freeze["oof"]["embargo_seconds"])
    select_q = float(freeze["oof"]["utility_select_threshold_quantile"])
    top_k = int(freeze["oof"]["top_k_per_decision_time"])
    out = {}
    # full dual head
    out["H_FULL_DUAL"] = fit_dual_head_oof(
        df,
        feature_cols=FEATURE_COLS,
        n_folds=n_folds,
        embargo_s=embargo,
        gas_weth=gas,
        amount_in_weth=amount,
        select_q=select_q,
        top_k=top_k,
        hypothesis_id="H_FULL_DUAL",
    )
    for hid, cols in HYPOTHESIS_FEATURE_MAP.items():
        out[hid] = fit_dual_head_oof(
            df,
            feature_cols=cols,
            n_folds=n_folds,
            embargo_s=embargo,
            gas_weth=gas,
            amount_in_weth=amount,
            select_q=select_q,
            top_k=top_k,
            hypothesis_id=hid,
        )
    return out
