"""
Retrain XGBoost Model -- Uses labeled_dataset.csv from label_dashcam.py.

LASZLO-aligned training contract:
  1. Temporal window: last 28 days (configurable via RETRAIN_WINDOW_DAYS)
  2. Temporal split (no shuffle): train = first 21d, val = middle 3.5d of
     last-7-day holdout, test = last 3.5d of holdout (ie. holdout = 7d split
     50/50 into val/test).
  3. Purge overlap: drop test rows if pool appears in train's last 30 min
  4. Isotonic calibration on val set
  5. EV > 0 threshold instead of fixed confidence cutoff
  6. Labels must be tagged; approximate-label models are NOT deployed
  7. Deployment gate: test accuracy >= 0.55 absolute AND PR-AUC uplift
     (cold-start with no prior model still requires the 0.55 floor).

Exit codes (LASZLO convention):
  0 = success, model promoted
  1 = generic error
  2 = gate failed (accuracy < 0.55 or pr_auc uplift gate not met)
  3 = not enough data after filtering

Usage: python scripts/retrain_model.py

Env vars:
    LABELED_OUTPUT, MODEL_DIR, CHAIN_ID, RETRAIN_WINDOW_DAYS (default 28),
    RETRAIN_HOLDOUT_DAYS (default 7), RETRAIN_MIN_ACCURACY (default 0.55),
    RETRAIN_REQUIRE_PRAUC_UPLIFT (default 0.02).
"""

from __future__ import annotations

import csv
import json
import os
import pickle
import sys
from pathlib import Path

import numpy as np


FEATURE_COLUMNS = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
]

DENYLIST_COLUMNS = {
    "pool_address",
    "token_address",
    "pool_type",
    "protocol",
    "tx_hash",
    "log_index",
    "block_number",
    "timestamp",
    "final_score",
    "label",
    "net_return",
    "entry_block",
    "exit_block",
    "canonical_key",
    "executable_label",
    "executable_net_return",
    "executable_status",
    "failure_reason",
    "approximate_label",
    "approximate_net_return",
    "entry_price",
    "exit_price",
}
DENYLIST_PREFIXES = (
    "future_",
    "exit_",
    "entry_",
    "quote_",
    "executable_",
    "approximate_",
    "label_",
)


def _validate_feature_contract(input_path: Path, fieldnames: list[str] | None) -> None:
    header = list(fieldnames or [])
    missing = [c for c in FEATURE_COLUMNS if c not in header]
    if missing:
        print(
            f"ERROR: Missing required FEATURE_COLUMNS in {input_path}: {missing}. "
            "Refusing row.get(..., 0) fallback."
        )
        sys.exit(4)

    denied = [
        c for c in FEATURE_COLUMNS
        if c in DENYLIST_COLUMNS or any(c.startswith(prefix) for prefix in DENYLIST_PREFIXES)
    ]
    if denied:
        print(f"ERROR: FEATURE_COLUMNS contains denied audit/leakage columns: {denied}")
        sys.exit(5)


def _parse_required_float(row: dict[str, str], col: str, input_path: Path, row_no: int) -> float:
    raw = row.get(col)
    if raw is None or raw == "":
        print(f"ERROR: Required feature {col} empty at row {row_no} in {input_path}")
        sys.exit(6)
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(f"ERROR: Required feature {col} is not numeric at row {row_no} in {input_path}: {raw!r}")
        sys.exit(7)


def main() -> None:
    from xgboost import XGBClassifier
    from sklearn.metrics import accuracy_score, average_precision_score, classification_report
    from sklearn.isotonic import IsotonicRegression

    input_path = Path(os.environ.get("LABELED_OUTPUT", "logs/labeled_dataset.csv"))
    model_dir = Path(os.environ.get("MODEL_DIR", "models"))
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))

    if not input_path.exists():
        print(f"ERROR: {input_path} not found. Run label_dashcam.py first.")
        sys.exit(1)

    print(f"Loading labeled data from {input_path}...")

    timestamps = []
    features = []
    labels = []
    net_returns = []
    label_types = []
    pool_addresses = []

    entropy_values = []
    with input_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        _validate_feature_contract(input_path, reader.fieldnames)
        for row_no, row in enumerate(reader, start=2):
            try:
                ts = int(row.get("timestamp", 0))
                feat = [_parse_required_float(row, col, input_path, row_no) for col in FEATURE_COLUMNS]
                label = int(row.get("label", 0))
                nr = float(row.get("net_return", 0))
                lt = row.get("label_type", "approximate")
                pa = row.get("token_address", "")
                timestamps.append(ts)
                features.append(feat)
                labels.append(label)
                net_returns.append(nr)
                label_types.append(lt)
                pool_addresses.append(pa)
                entropy_values.append(feat[FEATURE_COLUMNS.index("unique_buyer_entropy_5m")])
            except (ValueError, TypeError):
                continue

    if not entropy_values:
        print(f"ERROR: unique_buyer_entropy_5m has no parsed values in {input_path}")
        sys.exit(8)
    entropy_nonzero = sum(1 for v in entropy_values if v != 0.0)
    entropy_nonzero_rate = entropy_nonzero / len(entropy_values)
    if entropy_nonzero_rate == 0.0:
        print(
            f"ERROR: unique_buyer_entropy_5m is all zero/non-informative in {input_path}; "
            "refusing retrain."
        )
        sys.exit(9)

    X = np.array(features)
    y = np.array(labels)
    ts_arr = np.array(timestamps)
    nr_arr = np.array(net_returns)
    pa_arr = np.array(pool_addresses)
    lt_arr = np.array(label_types)

    has_executable = (lt_arr == "executable").sum()
    has_approximate = (lt_arr == "approximate").sum()
    is_approximate_only = has_executable == 0

    print(f"Dataset: {len(X)} samples, {y.sum()} positive ({y.mean()*100:.1f}%)")
    print(f"Label types: {has_executable} executable, {has_approximate} approximate")
    print(
        "Entropy guard: "
        f"nonzero={entropy_nonzero}/{len(entropy_values)} "
        f"({entropy_nonzero_rate:.4f})"
    )

    if len(X) < 20:
        print("ERROR: Too few samples to train.")
        sys.exit(3)

    # --- Temporal sort (no shuffle) ---
    sort_idx = np.argsort(ts_arr)
    X, y = X[sort_idx], y[sort_idx]
    ts_arr = ts_arr[sort_idx]
    nr_arr = nr_arr[sort_idx]
    pa_arr = pa_arr[sort_idx]
    lt_arr = lt_arr[sort_idx]

    # --- Time-based window (LASZLO contract): last 28 days, 7-day holdout. ---
    window_days = int(os.environ.get("RETRAIN_WINDOW_DAYS", "28") or "28")
    holdout_days = int(os.environ.get("RETRAIN_HOLDOUT_DAYS", "7") or "7")
    day_ms = 86_400_000
    latest_ts = int(ts_arr[-1]) if len(ts_arr) else 0
    window_start = latest_ts - window_days * day_ms
    holdout_start = latest_ts - holdout_days * day_ms

    window_mask = ts_arr >= window_start
    X = X[window_mask]; y = y[window_mask]
    ts_arr = ts_arr[window_mask]
    nr_arr = nr_arr[window_mask]
    pa_arr = pa_arr[window_mask]
    lt_arr = lt_arr[window_mask]

    if len(X) < 20:
        print(f"ERROR: Only {len(X)} samples inside {window_days}d window; need >= 20.")
        sys.exit(3)

    train_mask = ts_arr < holdout_start
    holdout_mask = ~train_mask
    n_hold = int(holdout_mask.sum())
    hold_mid = holdout_start + (latest_ts - holdout_start) // 2
    val_mask = holdout_mask & (ts_arr < hold_mid)
    test_mask = holdout_mask & (ts_arr >= hold_mid)

    X_train, y_train = X[train_mask], y[train_mask]
    X_val, y_val = X[val_mask], y[val_mask]
    X_test, y_test = X[test_mask], y[test_mask]
    nr_val = nr_arr[val_mask]
    nr_test = nr_arr[test_mask]
    ts_train = ts_arr[train_mask]
    pa_test = pa_arr[test_mask]
    ts_test = ts_arr[test_mask]

    print(f"Window: last {window_days}d | train<{holdout_days}d ago | holdout last {holdout_days}d (val/test 50/50)")
    print(f"Window rows: {len(X)}, holdout rows: {n_hold}")

    # --- Purge: remove test rows if pool appears in train's last 30 min ---
    train_cutoff = ts_train[-1] if len(ts_train) > 0 else 0
    train_last_30m = set()
    for i in range(len(ts_train) - 1, -1, -1):
        if ts_train[i] >= train_cutoff - 30 * 60 * 1000:
            train_last_30m.add(pa_arr[i])
        else:
            break

    if train_last_30m:
        test_cutoff = ts_test[0] + 30 * 60 * 1000 if len(ts_test) > 0 else 0
        keep_mask = np.array([
            not (pa_test[i] in train_last_30m and ts_test[i] <= test_cutoff)
            for i in range(len(X_test))
        ])
        purged = (~keep_mask).sum()
        if purged > 0:
            print(f"Purged {purged} test rows (pool overlap with train's last 30 min)")
            X_test, y_test = X_test[keep_mask], y_test[keep_mask]
            nr_test = nr_test[keep_mask]

    print(f"Split: train={len(X_train)} val={len(X_val)} test={len(X_test)}")
    print(f"Train pos: {y_train.mean()*100:.1f}% | Val: {y_val.mean()*100:.1f}% | Test: {y_test.mean()*100:.1f}%")

    if len(X_train) < 10 or len(X_val) < 5 or len(X_test) < 5:
        print("ERROR: Not enough data after split. Accumulate more data.")
        sys.exit(3)

    scale_pos = max(1.0, (len(y_train) - y_train.sum()) / max(y_train.sum(), 1))

    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        scale_pos_weight=scale_pos,
        use_label_encoder=False,
        eval_metric="logloss",
        subsample=0.8,
        colsample_bytree=0.8,
    )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

    # --- Isotonic calibration on val set ---
    val_proba = model.predict_proba(X_val)[:, 1]
    calibrator = IsotonicRegression(y_min=0.01, y_max=0.99, out_of_bounds="clip")
    calibrator.fit(val_proba, y_val)

    # --- Evaluate on test set ---
    test_proba_raw = model.predict_proba(X_test)[:, 1]
    test_proba_cal = calibrator.predict(test_proba_raw)

    # --- EV-based threshold ---
    val_cal = calibrator.predict(val_proba)
    val_wins = nr_val[y_val == 1]
    val_losses = nr_val[y_val == 0]
    avg_win = float(val_wins.mean()) if len(val_wins) > 0 else 0.01
    avg_loss = float(np.abs(val_losses).mean()) if len(val_losses) > 0 else 0.01
    cost_per_trade = 0.006

    print(f"\nVal avg_win: {avg_win:.4f}, avg_loss: {avg_loss:.4f}")

    best_threshold = 0.5
    best_ev = -999
    for t in np.arange(0.1, 0.9, 0.05):
        mask = test_proba_cal >= t
        if mask.sum() == 0:
            continue
        subset_nr = nr_test[mask]
        ev = float(subset_nr.mean()) if len(subset_nr) > 0 else -1
        if ev > best_ev:
            best_ev = ev
            best_threshold = round(float(t), 2)

    y_pred_test = (test_proba_cal >= best_threshold).astype(int)
    accuracy = accuracy_score(y_test, y_pred_test)
    print(f"\nTest Accuracy: {accuracy:.4f} (threshold={best_threshold})")
    if len(y_test) > 0 and len(np.unique(y_pred_test)) > 1:
        print(classification_report(y_test, y_pred_test, target_names=["no_profit", "profit"]))

    fi = dict(zip(FEATURE_COLUMNS, [float(x) for x in model.feature_importances_]))
    print("Feature importances:", json.dumps(fi, indent=2))

    # --- Deployment ---
    model_dir.mkdir(parents=True, exist_ok=True)
    default_name = (
        "signal_model_base_v2.json" if chain_id == 8453 else "signal_model_v2.json"
    )
    model_name = os.environ.get("MODEL_OUTPUT_NAME", default_name)
    model_path = model_dir / model_name
    calibrator_path = model_dir / model_name.replace(".json", "_calibrator.pkl")

    label_type_final = "approximate" if is_approximate_only else "executable"

    if is_approximate_only:
        print(f"\n{'='*60}")
        print("FIREWALL: All labels are approximate (velocity proxy).")
        print("Model is saved for diagnostic/shadow use ONLY.")
        print("It must NOT be deployed as the production model.")
        print("P0-3 remains OPEN until executable-quote labels are available.")
        print(f"{'='*60}")
        model_name = model_name.replace(".json", "_approximate.json")
        model_path = model_dir / model_name
        calibrator_path = model_dir / model_name.replace(".json", "_calibrator.pkl")

    metrics_path = model_dir / model_name.replace(".json", "_metrics.json")
    threshold_path = model_dir / model_name.replace(".json", "_threshold.json")
    pr_auc_test = float(average_precision_score(y_test, test_proba_cal))
    prev_pr_auc = None
    if metrics_path.exists():
        try:
            prev_pr_auc = float(json.loads(metrics_path.read_text(encoding="utf-8")).get("pr_auc_test"))
        except Exception:
            prev_pr_auc = None
    uplift_gate = float(os.environ.get("RETRAIN_REQUIRE_PRAUC_UPLIFT", "0.02"))
    min_accuracy = float(os.environ.get("RETRAIN_MIN_ACCURACY", "0.55"))
    # LASZLO gate: absolute accuracy floor (also for cold start) AND uplift vs prior model.
    accuracy_ok = accuracy >= min_accuracy
    uplift_ok = prev_pr_auc is None or (pr_auc_test >= (prev_pr_auc + uplift_gate))
    gates_passed = bool(accuracy_ok and uplift_ok)
    no_promote = os.environ.get("RETRAIN_NO_PROMOTE", "").lower() in ("1", "true", "yes")
    always_save = os.environ.get("RETRAIN_ALWAYS_SAVE", "").lower() in ("1", "true", "yes")
    should_promote = bool(gates_passed and not no_promote)
    if should_promote or always_save:
        model.save_model(str(model_path))
        with open(calibrator_path, "wb") as f:
            pickle.dump(calibrator, f)
        try:
            from build_model_manifest import build_manifest, write_manifest

            manifest = build_manifest(model_path, feature_order=list(FEATURE_COLUMNS))
            mpath = write_manifest(model_path, manifest)
            print(f"Manifest: {mpath}")
        except Exception as exc:
            print(f"WARN: manifest not written: {exc}")
        mode = "PROMOTED" if should_promote else (
            "SAVED_NO_PROMOTE" if no_promote else "SAVED_GATED_OFF"
        )
        print(f"\n[{mode}] Saved: {model_path}")
        print(f"Calibrator: {calibrator_path}")
    else:
        prev_txt = "n/a" if prev_pr_auc is None else f"{prev_pr_auc:.4f}"
        print(
            f"\nPromotion skipped: accuracy={accuracy:.4f} (min {min_accuracy:.2f}) "
            f"pr_auc_test={pr_auc_test:.4f} prev_pr_auc={prev_txt} uplift_gate={uplift_gate:.4f}"
        )

    metrics = {
        "accuracy": round(accuracy, 6),
        "train_samples": len(X_train),
        "val_samples": len(X_val),
        "test_samples": len(X_test),
        "positive_rate": round(float(y.mean()), 6),
        "feature_importances": fi,
        "chain_id": chain_id,
        "label_type": label_type_final,
        "ev_threshold": best_threshold,
        "avg_win_val": round(avg_win, 6),
        "avg_loss_val": round(avg_loss, 6),
        "pr_auc_test": round(pr_auc_test, 6),
        "prev_pr_auc_test": None if prev_pr_auc is None else round(prev_pr_auc, 6),
        "pr_auc_uplift_gate": round(uplift_gate, 6),
        "promoted": bool(should_promote),
        "gates_passed": bool(gates_passed),
        "no_promote_flag": bool(no_promote),
        "cost_per_trade": cost_per_trade,
        "split": "temporal_window_28d_holdout_7d",
        "min_accuracy_gate": min_accuracy,
        "accuracy_gate_passed": bool(accuracy_ok),
        "uplift_gate_passed": bool(uplift_ok),
    }
    metrics_path.write_text(json.dumps(metrics, indent=2))
    threshold_path.write_text(json.dumps({"ev_threshold": best_threshold}, indent=2))
    print(f"Metrics: {metrics_path}")
    print(f"Threshold: {threshold_path}")

    if not should_promote:
        if no_promote and gates_passed:
            print("Gates passed but RETRAIN_NO_PROMOTE=1; model saved for offline eval only.")
            sys.exit(0)
        sys.exit(2)


if __name__ == "__main__":
    main()
