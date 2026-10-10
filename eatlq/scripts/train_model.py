#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
from xgboost import XGBClassifier


REALTIME_FEATURES = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train Phase 3 signal model v1")
    parser.add_argument("--input", type=Path, default=Path("labeled_dataset.csv"))
    parser.add_argument("--model-output", type=Path, default=Path("models/signal_model_v1.json"))
    parser.add_argument(
        "--metrics-output", type=Path, default=Path("models/signal_model_v1_metrics.json")
    )
    parser.add_argument("--accuracy-gate", type=float, default=0.55)
    parser.add_argument(
        "--tag", type=str, default=None,
        help="Optional tag appended to model/metrics filenames (e.g. 'base' -> signal_model_base_v1).",
    )
    parser.add_argument(
        "--features", type=str, default=None,
        help="Comma-separated feature column names. Use 'realtime' for the 4 runtime features.",
    )
    parser.add_argument(
        "--label", type=str, default=None,
        help="Label column name (default: next_day_active_flag).",
    )
    return parser


def build_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    numeric_cols = [
        "trade_count",
        "v2_trade_share",
        "v3_trade_share",
        "distinct_venues",
        "distinct_recipients",
        "venue_concentration_ratio",
        "token_concentration_ratio",
    ]
    optional_numeric = ["cross_protocol_flag", "multi_venue_flag"]

    for col in numeric_cols + optional_numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    feature_cols = [col for col in numeric_cols if col in df.columns]
    feature_cols.extend([col for col in optional_numeric if col in df.columns])

    feature_df = df[feature_cols].copy()

    categorical_cols = [c for c in ["protocol_preference", "activity_band"] if c in df.columns]
    if categorical_cols:
        cats = pd.get_dummies(df[categorical_cols].fillna("unknown"), prefix=categorical_cols)
        feature_df = pd.concat([feature_df, cats], axis=1)

    return feature_df


def main() -> int:
    args = build_parser().parse_args()

    if args.tag:
        if args.model_output == Path("models/signal_model_v1.json"):
            args.model_output = Path(f"models/signal_model_{args.tag}_v1.json")
        if args.metrics_output == Path("models/signal_model_v1_metrics.json"):
            args.metrics_output = Path(f"models/signal_model_{args.tag}_v1_metrics.json")

    if not args.input.exists():
        print(f"ERROR: input dataset not found: {args.input}")
        return 1

    df = pd.read_csv(args.input)

    label_col = args.label or "next_day_active_flag"
    realtime_mode = args.features == "realtime"

    if realtime_mode:
        feature_cols = list(REALTIME_FEATURES)
        time_col = "window_start"
        required = {label_col}
    else:
        time_col = "activity_date"
        required = {time_col, label_col}

    missing = required - set(df.columns)
    if missing:
        print(f"ERROR: missing required columns: {sorted(missing)}")
        return 1

    df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    df = df.dropna(subset=[time_col]).sort_values(time_col, ascending=True)
    df[label_col] = pd.to_numeric(df[label_col], errors="coerce").fillna(0).astype(int)

    if len(df) < 20:
        print("ERROR: dataset too small (<20 rows) for time-split training.")
        return 1

    if realtime_mode:
        for col in feature_cols:
            if col not in df.columns:
                df[col] = 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        X = df[feature_cols].copy()
    elif args.features:
        feature_cols = [c.strip() for c in args.features.split(",") if c.strip()]
        for col in feature_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        X = df[feature_cols].copy()
    else:
        X = build_feature_matrix(df)

    y = df[label_col]

    use_presplit = False
    if "dataset_split" in df.columns:
        train_mask = df["dataset_split"] == "train"
        holdout_mask = df["dataset_split"] == "test"
        if train_mask.sum() >= 10 and holdout_mask.sum() >= 10:
            y_holdout_check = y[holdout_mask]
            if len(y_holdout_check.unique()) > 1:
                use_presplit = True

    if use_presplit:
        print("using pre-computed dataset_split column for train/holdout")
        X_train, X_holdout = X[train_mask], X[holdout_mask]
        y_train, y_holdout = y[train_mask], y[holdout_mask]
    else:
        print("falling back to 80/20 time split")
        split_idx = max(int(len(df) * 0.8), 1)
        if split_idx >= len(df):
            split_idx = len(df) - 1
        X_train, X_holdout = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_holdout = y.iloc[:split_idx], y.iloc[split_idx:]

    print("training xgboost model (cpu)...")
    model = XGBClassifier(
        max_depth=4,
        n_estimators=200,
        learning_rate=0.05,
        subsample=0.8,
        objective="binary:logistic",
        eval_metric="logloss",
        device="cpu",
        tree_method="hist",
        random_state=42,
        verbosity=1,
    )
    model.fit(X_train, y_train)
    print("training complete.")

    holdout_proba = model.predict_proba(X_holdout)[:, 1]
    holdout_pred = (holdout_proba >= 0.5).astype(int)

    metrics = {
        "rows_total": int(len(df)),
        "rows_train": int(len(X_train)),
        "rows_holdout": int(len(X_holdout)),
        "accuracy": float(accuracy_score(y_holdout, holdout_pred)),
        "precision": float(precision_score(y_holdout, holdout_pred, zero_division=0)),
        "recall": float(recall_score(y_holdout, holdout_pred, zero_division=0)),
        "f1": float(f1_score(y_holdout, holdout_pred, zero_division=0)),
    }
    try:
        metrics["auc_roc"] = float(roc_auc_score(y_holdout, holdout_proba))
    except ValueError:
        metrics["auc_roc"] = None

    verdict = "PASS" if metrics["accuracy"] > args.accuracy_gate else "FAIL"
    print(json.dumps({"verdict": verdict, "metrics": metrics}, ensure_ascii=True))

    if verdict == "PASS":
        args.model_output.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_output.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(args.model_output)
        metrics["feature_columns"] = list(X.columns)
        args.metrics_output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        try:
            from build_model_manifest import build_manifest, write_manifest

            manifest = build_manifest(
                args.model_output,
                feature_order=list(X.columns),
            )
            write_manifest(args.model_output, manifest)
            print(f"saved_manifest={args.model_output.with_name(args.model_output.stem + '.manifest.json')}")
        except Exception as exc:
            print(f"WARN: manifest not written: {exc}")
        print(f"saved_model={args.model_output}")
        print(f"saved_metrics={args.metrics_output}")
        return 0

    print("gate_failed: model artifacts not saved")
    # LASZLO exit convention: 2 = gate failed (data was OK, model wasn't).
    return 2


if __name__ == "__main__":
    sys.exit(main())
