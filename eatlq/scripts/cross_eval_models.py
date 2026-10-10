"""Cross-evaluate v1 (heuristic-trained) vs v2 (executable-trained)
on the SAME target: the executable-quote P&L label (real money).

Also evaluates trivial baselines so we know the noise floor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
)
from xgboost import XGBClassifier


FEATURES = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]


def load_model(path: Path) -> XGBClassifier:
    m = XGBClassifier()
    m.load_model(path)
    return m


def eval_on(model: XGBClassifier, X: pd.DataFrame, y: pd.Series) -> dict:
    proba = model.predict_proba(X)[:, 1]
    pred = (proba >= 0.5).astype(int)
    out = {
        "accuracy": float(accuracy_score(y, pred)),
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "f1": float(f1_score(y, pred, zero_division=0)),
    }
    try:
        out["auc_roc"] = float(roc_auc_score(y, proba))
    except ValueError:
        out["auc_roc"] = None
    return out


def main() -> int:
    exec_csv = Path(sys.argv[1] if len(sys.argv) > 1 else "models/realtime_dataset_v2.csv")
    v1_model = Path("models/signal_model_realtime_v1.json")
    v2_model = Path("models/signal_model_realtime_v2.json")

    df = pd.read_csv(exec_csv)
    df["window_start"] = pd.to_datetime(df["window_start"], errors="coerce")
    df = df.dropna(subset=["window_start"]).sort_values("window_start")
    for c in FEATURES:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["label"] = pd.to_numeric(df["label"], errors="coerce").fillna(0).astype(int)
    df["mv_label"] = pd.to_numeric(df["mv_label"], errors="coerce").fillna(0).astype(int)

    split = max(int(len(df) * 0.8), 1)
    test = df.iloc[split:].reset_index(drop=True)
    X_test = test[FEATURES]
    y_exec = test["label"]
    y_mv = test["mv_label"]

    print(f"test rows: {len(test)}")
    print(f"test pos rate (executable): {y_exec.mean():.3f}")
    print(f"test pos rate (mv heuristic): {y_mv.mean():.3f}")
    print(f"mv_vs_exec agree: {(y_exec == y_mv).mean():.3f}")
    print()

    report = {
        "test_size": int(len(test)),
        "pos_rate_executable": float(y_exec.mean()),
        "pos_rate_mv": float(y_mv.mean()),
        "baselines": {},
        "v1_on_executable": None,
        "v2_on_executable": None,
        "v1_on_mv": None,
        "v2_on_mv": None,
    }

    # trivial baselines vs executable label
    majority = 1 - int(y_exec.mean() >= 0.5)  # predict majority class
    y_maj = np.full(len(y_exec), majority)
    y_rand = np.random.RandomState(42).randint(0, 2, size=len(y_exec))
    report["baselines"]["majority_acc"] = float(accuracy_score(y_exec, y_maj))
    report["baselines"]["random_acc"] = float(accuracy_score(y_exec, y_rand))
    report["baselines"]["mv_label_as_pred_acc"] = float(accuracy_score(y_exec, y_mv))
    # use buy_pressure > 0.5 heuristic directly (author's label logic approximated)
    y_bp = (X_test["buy_pressure_ratio"] > 0.5).astype(int)
    report["baselines"]["buy_pressure_heuristic_acc"] = float(accuracy_score(y_exec, y_bp))

    print("=== baselines (target = executable P&L) ===")
    for k, v in report["baselines"].items():
        print(f"  {k}: {v:.4f}")
    print()

    # v1 vs exec label
    if v1_model.exists():
        m1 = load_model(v1_model)
        report["v1_on_executable"] = eval_on(m1, X_test, y_exec)
        report["v1_on_mv"] = eval_on(m1, X_test, y_mv)
        print("=== v1 (heuristic-trained) evaluated on executable target ===")
        print(json.dumps(report["v1_on_executable"], indent=2))
        print("=== v1 evaluated on its native (mv) target ===")
        print(json.dumps(report["v1_on_mv"], indent=2))
    else:
        print(f"skip v1: {v1_model} missing")
    print()

    # v2 vs exec label
    if v2_model.exists():
        m2 = load_model(v2_model)
        report["v2_on_executable"] = eval_on(m2, X_test, y_exec)
        report["v2_on_mv"] = eval_on(m2, X_test, y_mv)
        print("=== v2 (executable-trained) evaluated on executable target ===")
        print(json.dumps(report["v2_on_executable"], indent=2))
        print("=== v2 evaluated on mv target ===")
        print(json.dumps(report["v2_on_mv"], indent=2))
    else:
        print(f"skip v2: {v2_model} missing")

    Path("models/cross_eval_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print("\nsaved: models/cross_eval_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
