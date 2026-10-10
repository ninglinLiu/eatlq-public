"""Offline alpha evaluation for Run-003A V2-only model.

Compares:
  (A) signal_model_v2only_run003a.json  (trained on V2-only clean dataset)
  (B) signal_model_base_v2.json         (run-002, mixed V2/V3/aero-labeled dataset)

Both are scored on the SAME held-out V2-only test window, so we get an
apples-to-apples answer to "does the V2-only model have real alpha?"

Metrics reported:
  - accuracy at ev_threshold
  - PR-AUC
  - positive rate in test window
  - avg net_return
  - avg net_return for top-decile by predicted probability
  - avg net_return after excluding the top 5 biggest winners (robustness check)
  - confidence bucket table: p<0.3, 0.3-0.5, 0.5-0.7, 0.7+
"""
from __future__ import annotations
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import accuracy_score, average_precision_score

FEATURE_COLUMNS = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]

DATA = Path("logs/labeled_dataset_run003a_v2_only.csv")
MODEL_A_NAME = "signal_model_v2only_run003a"
MODEL_B_NAME = "signal_model_base_v2"


def load_split(holdout_days: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (train_val, test) split on the clean V2-only dataset.

    Uses the same temporal logic as retrain_model.py so the V2-only model is
    scored on its own held-out test slice, and the run-002 model is scored on
    the exact same rows (fair comparison).
    """
    df = pd.read_csv(DATA)
    df = df.dropna(subset=["timestamp", "label", "net_return"]).copy()
    df["timestamp"] = df["timestamp"].astype("int64")
    df["label"] = df["label"].astype(int)
    df["net_return"] = df["net_return"].astype(float)
    # fill in features (legacy rows may be missing unique_buyer_entropy_5m)
    for c in FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = df[c].astype(float).fillna(0.0)

    df = df.sort_values("timestamp").reset_index(drop=True)
    cutoff_ms = int(df["timestamp"].max()) - holdout_days * 86_400_000
    train_val = df[df["timestamp"] < cutoff_ms].copy()
    holdout = df[df["timestamp"] >= cutoff_ms].copy()
    # we don't need train/val separately here, we only score on holdout
    # split holdout into val/test (second half is "test")
    mid = len(holdout) // 2
    test = holdout.iloc[mid:].copy().reset_index(drop=True)
    return train_val, test


def load_model(name: str):
    model_path = Path(f"models/{name}.json")
    cal_path = Path(f"models/{name}_calibrator.pkl")
    metrics_path = Path(f"models/{name}_metrics.json")
    threshold_path = Path(f"models/{name}_threshold.json")
    model = xgb.XGBClassifier()
    model.load_model(str(model_path))
    with open(cal_path, "rb") as f:
        calibrator = pickle.load(f)
    metrics = json.loads(metrics_path.read_text())
    threshold = json.loads(threshold_path.read_text())["ev_threshold"]
    return model, calibrator, metrics, threshold


def score_model(model, calibrator, X: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(X)[:, 1]
    return np.asarray(calibrator.transform(raw))


def evaluate(tag: str, proba: np.ndarray, y: np.ndarray, net: np.ndarray,
             threshold: float) -> dict:
    y_pred = (proba >= threshold).astype(int)
    acc = accuracy_score(y, y_pred)
    pr_auc = average_precision_score(y, proba)
    pos_rate = float(y.mean())

    # top-decile by predicted probability
    order = np.argsort(-proba)
    k10 = max(1, len(proba) // 10)
    top_idx = order[:k10]
    top_net = float(net[top_idx].mean())
    top_pos = float(y[top_idx].mean())

    # remove top-5 biggest winners (by actual net_return) and re-check
    # decile to show we're not just riding a jackpot
    top_decile_net_sorted = net[top_idx]
    order_in_decile = np.argsort(-top_decile_net_sorted)
    if len(order_in_decile) > 5:
        kept_idx = np.array(
            [i for j, i in enumerate(top_idx[order_in_decile]) if j >= 5]
        )
        top_net_no5 = float(net[kept_idx].mean())
        kept_n = len(kept_idx)
    else:
        top_net_no5 = float("nan")
        kept_n = 0

    # confidence buckets
    buckets = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)]
    bucket_rows = []
    for lo, hi in buckets:
        mask = (proba >= lo) & (proba < hi)
        n = int(mask.sum())
        if n == 0:
            bucket_rows.append({
                "bucket": f"[{lo:.1f},{hi:.2f})",
                "n": 0,
                "pos_rate": float("nan"),
                "avg_net": float("nan"),
            })
            continue
        bucket_rows.append({
            "bucket": f"[{lo:.1f},{hi:.2f})",
            "n": n,
            "pos_rate": round(float(y[mask].mean()), 4),
            "avg_net": round(float(net[mask].mean()), 4),
        })

    # overall avg net if we traded everything scored above threshold
    taken = proba >= threshold
    if taken.any():
        traded_avg = float(net[taken].mean())
        traded_n = int(taken.sum())
    else:
        traded_avg = float("nan")
        traded_n = 0

    return {
        "tag": tag,
        "threshold": threshold,
        "n_test": int(len(y)),
        "accuracy": round(acc, 4),
        "pr_auc": round(pr_auc, 4),
        "positive_rate": round(pos_rate, 4),
        "top_decile_k": k10,
        "top_decile_avg_net": round(top_net, 4),
        "top_decile_pos_rate": round(top_pos, 4),
        "top_decile_avg_net_ex_top5": round(top_net_no5, 4),
        "top_decile_kept_n_after_ex5": kept_n,
        "trades_at_threshold": traded_n,
        "avg_net_at_threshold": round(traded_avg, 4) if traded_avg == traded_avg else None,
        "confidence_buckets": bucket_rows,
    }


def main():
    train_val, test = load_split()
    print(f"Clean V2-only dataset: train+val={len(train_val)}  test={len(test)}")
    print(f"Test window pos_rate: {test['label'].mean():.4f}")
    print(f"Test window avg_net (random trader): {test['net_return'].mean():.4f}")
    print(f"Test window median_net: {test['net_return'].median():.4f}")
    print()

    X = test[FEATURE_COLUMNS].to_numpy()
    y = test["label"].to_numpy()
    net = test["net_return"].to_numpy()

    results = []
    for name, tag in [(MODEL_A_NAME, "V2-only run003a"), (MODEL_B_NAME, "run-002 prod")]:
        model, calibrator, metrics, threshold = load_model(name)
        proba = score_model(model, calibrator, X)
        res = evaluate(tag, proba, y, net, threshold)
        res["model_file"] = name + ".json"
        res["train_samples_in_metrics"] = metrics.get("train_samples")
        res["train_positive_rate"] = metrics.get("positive_rate")
        results.append(res)

    out = {
        "dataset": str(DATA),
        "test_window_rows": int(len(test)),
        "test_window_pos_rate": round(float(test["label"].mean()), 4),
        "test_window_avg_net": round(float(test["net_return"].mean()), 4),
        "test_window_median_net": round(float(test["net_return"].median()), 4),
        "models": results,
    }
    print(json.dumps(out, indent=2))
    Path("mvp/diagnostics").mkdir(parents=True, exist_ok=True)
    Path("mvp/diagnostics/run003a_alpha_eval.json").write_text(json.dumps(out, indent=2))
    print("\nSaved: mvp/diagnostics/run003a_alpha_eval.json")


if __name__ == "__main__":
    main()
