"""Apples-to-apples comparison: v1 (toxic-trained) vs detox_v2 models
on the same executable P&L ground truth.

Method:
  1. Load realtime_dataset_v2.csv     -> (pool, window, v1_features, exec_label)
  2. Load realtime_dataset_detox.csv  -> (pool, window, clean_features)
  3. INNER JOIN on (pool_address, window_start)
  4. Take the same time-ordered tail 20% as hold-out (matches train_model split)
  5. Score both models on the hold-out:
     - v1 model on v1 features
     - detox_v2 model on clean features
  6. Report accuracy / precision / recall / F1 / AUC-ROC against executable labels.
  7. Also compute selective metrics at confidence thresholds (for fired-signal semantics).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from xgboost import XGBClassifier

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

FEATURES = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]


def load_exec_labels() -> pd.DataFrame:
    p = MODELS / "realtime_dataset_v2.csv"
    df = pd.read_csv(p, parse_dates=["window_start"])
    df["pool_address"] = df["pool_address"].str.lower()
    df = df[df["label_type"] == "executable"].copy()
    df = df.rename(columns={
        "price_velocity_5m": "price_velocity_5m_v1",
        "buy_pressure_ratio": "buy_pressure_ratio_v1",
        "token_volatility_5m": "token_volatility_5m_v1",
        "activity_heat": "activity_heat_v1",
        "unique_buyer_entropy_5m": "unique_buyer_entropy_5m_v1",
    })
    return df[[
        "pool_address", "window_start",
        "price_velocity_5m_v1", "buy_pressure_ratio_v1",
        "token_volatility_5m_v1", "activity_heat_v1",
        "unique_buyer_entropy_5m_v1",
        "label", "net_return",
    ]].rename(columns={"label": "exec_label"})


def load_clean_features() -> pd.DataFrame:
    p = MODELS / "realtime_dataset_detox.csv"
    df = pd.read_csv(p, parse_dates=["window_start"])
    df["pool_address"] = df["pool_address"].str.lower()
    # Match train_model.py behaviour: fill any missing feature column with 0.
    for col in FEATURES:
        if col not in df.columns:
            df[col] = 0.0
    return df[["pool_address", "window_start"] + FEATURES]


def eval_model(model: XGBClassifier, X: pd.DataFrame, y: np.ndarray, y_prob: np.ndarray | None = None) -> dict:
    if y_prob is None:
        y_prob = model.predict_proba(X)[:, 1]
    y_pred = (y_prob >= 0.5).astype(int)
    out = {
        "n": int(len(y)),
        "pos_rate": float(np.mean(y)),
        "accuracy": float(accuracy_score(y, y_pred)),
        "precision": float(precision_score(y, y_pred, zero_division=0)),
        "recall": float(recall_score(y, y_pred, zero_division=0)),
        "f1": float(f1_score(y, y_pred, zero_division=0)),
    }
    try:
        out["auc_roc"] = float(roc_auc_score(y, y_prob))
    except ValueError:
        out["auc_roc"] = None
    return out


def precision_at_k(y: np.ndarray, y_prob: np.ndarray, k: float) -> dict:
    """Simulate 'top-k%' signal firing: pick samples with highest model score."""
    thresh = np.quantile(y_prob, 1 - k)
    fired = y_prob >= thresh
    if fired.sum() == 0:
        return {"k": k, "n_fired": 0, "precision": None}
    return {
        "k": k,
        "n_fired": int(fired.sum()),
        "fire_rate": float(fired.mean()),
        "precision": float(y[fired].mean()),  # share of fired that were +P&L
    }


def main() -> None:
    exec_df = load_exec_labels()
    clean_df = load_clean_features()
    print(f"exec labels: {len(exec_df)} rows, {exec_df['pool_address'].nunique()} pools")
    print(f"clean features: {len(clean_df)} rows, {clean_df['pool_address'].nunique()} pools")

    merged = exec_df.merge(clean_df, on=["pool_address", "window_start"], how="inner")
    merged = merged.sort_values("window_start").reset_index(drop=True)
    print(f"joined (exec ∩ clean): {len(merged)} rows, {merged['pool_address'].nunique()} pools")

    if len(merged) == 0:
        print("ERROR: empty join")
        return

    split_idx = int(len(merged) * 0.8)
    test = merged.iloc[split_idx:].reset_index(drop=True)
    print(f"test holdout (tail 20%): {len(test)} rows\n")

    v1_model = XGBClassifier()
    v1_model.load_model(str(MODELS / "signal_model_realtime_v1.json"))
    detox_model = XGBClassifier()
    detox_model.load_model(str(MODELS / "signal_model_realtime_detox_v2.json"))

    # v1 model eats v1 features (with toxic);
    # detox_v2 eats clean features.
    X_v1 = test[["price_velocity_5m_v1", "buy_pressure_ratio_v1",
                 "token_volatility_5m_v1", "activity_heat_v1",
                 "unique_buyer_entropy_5m_v1"]].rename(columns={
        "price_velocity_5m_v1": "price_velocity_5m",
        "buy_pressure_ratio_v1": "buy_pressure_ratio",
        "token_volatility_5m_v1": "token_volatility_5m",
        "activity_heat_v1": "activity_heat",
        "unique_buyer_entropy_5m_v1": "unique_buyer_entropy_5m",
    })
    X_clean = test[FEATURES]
    y_exec = test["exec_label"].astype(int).values

    p_v1 = v1_model.predict_proba(X_v1)[:, 1]
    p_detox = detox_model.predict_proba(X_clean)[:, 1]

    result = {
        "holdout_size": int(len(test)),
        "executable_pos_rate": float(y_exec.mean()),
        "mean_net_return": float(test["net_return"].mean()),
        "v1_on_v1_features": eval_model(v1_model, X_v1, y_exec, y_prob=p_v1),
        "detox_v2_on_clean_features": eval_model(detox_model, X_clean, y_exec, y_prob=p_detox),
        "precision_at_top_k": {
            "v1":     {str(k): precision_at_k(y_exec, p_v1, k)    for k in [0.05, 0.10, 0.20, 0.30]},
            "detox":  {str(k): precision_at_k(y_exec, p_detox, k) for k in [0.05, 0.10, 0.20, 0.30]},
        },
        "baseline_majority": float(max(y_exec.mean(), 1 - y_exec.mean())),
    }
    # Feature distribution shift (v1 vs clean) among positive exec samples
    pos_mask = y_exec == 1
    if pos_mask.any():
        result["feature_shift_for_winners"] = {
            feat: {
                "v1_mean": float(X_v1[feat][pos_mask].mean()),
                "clean_mean": float(X_clean[feat][pos_mask].mean()),
                "delta": float(X_clean[feat][pos_mask].mean() - X_v1[feat][pos_mask].mean()),
            }
            for feat in FEATURES
        }

    print(json.dumps(result, indent=2))
    out = MODELS / "detox_cross_eval_report.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"\nsaved_report={out}")


if __name__ == "__main__":
    main()
