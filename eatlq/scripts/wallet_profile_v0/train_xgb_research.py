#!/usr/bin/env python3
"""
XGBoost research smoke — isolated from production model path.

RESEARCH_ONLY: true
NO_ALPHA_CLAIM: true
MODEL_PROMOTION_ALLOWED: false
TRAINING_ALLOWED: false
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "mvp" / "audits" / "wallet_profile_v0"
LABELED = AUDIT / "labels" / "dashcam_labeled_research.csv"
OUT = AUDIT / "xgb_research"

log = logging.getLogger("train_xgb_research")

FEATURE_COLS = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
    "is_smart_money",
    "is_sniper",
    "is_diamond_hand",
    "is_paper_hand",
    "is_whale",
    "wallet_tag_count",
]

FRONTMATTER = {
    "RESEARCH_ONLY": True,
    "NO_ALPHA_CLAIM": True,
    "MODEL_PROMOTION_ALLOWED": False,
    "TRAINING_ALLOWED": False,
}


def _float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return default
        if isinstance(v, bool):
            return float(v)
        return float(v)
    except (TypeError, ValueError):
        return default


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def row_features(row: dict) -> list[float]:
    """Map dashcam columns to whitepaper feature names."""
    activity = _float(row.get("activity_val") or row.get("activity_heat"))
    if activity <= 1.0:
        activity *= 100.0
    return [
        _float(row.get("momentum_val") or row.get("price_velocity_5m")),
        _float(row.get("whale_val") or row.get("buy_pressure_ratio")),
        _float(row.get("risk_val") or row.get("token_volatility_5m")),
        activity,
        _float(row.get("unique_buyer_entropy_5m")),
        1.0 if str(row.get("is_smart_money", "")).lower() in ("true", "1") else 0.0,
        1.0 if str(row.get("is_sniper", "")).lower() in ("true", "1") else 0.0,
        1.0 if str(row.get("is_diamond_hand", "")).lower() in ("true", "1") else 0.0,
        1.0 if str(row.get("is_paper_hand", "")).lower() in ("true", "1") else 0.0,
        1.0 if str(row.get("is_whale", "")).lower() in ("true", "1") else 0.0,
        _float(row.get("wallet_tag_count")),
    ]


def build_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xs, ys, ts = [], [], []
    for row in rows:
        label_proxy = 1 if _float(row.get("final_score", 0)) >= 0.52 else 0
        if row.get("triggered_signal", "").lower() in ("true", "1"):
            label_proxy = 1
        xs.append(row_features(row))
        ys.append(label_proxy)
        ts.append(_float(row.get("timestamp", 0)))
    return np.array(xs, dtype=np.float64), np.array(ys, dtype=np.int32), np.array(ts, dtype=np.float64)


def train_and_eval(
    *,
    train_days: int = 28,
    holdout_days: int = 7,
    labeled_path: Path = LABELED,
) -> dict[str, Any]:
    if not labeled_path.exists():
        raise FileNotFoundError(f"Run label_research.py first: {labeled_path}")

    rows = load_rows(labeled_path)
    X, y, ts = build_matrix(rows)
    if len(rows) < 100:
        log.warning("small dataset n=%d", len(rows))

    now_ms = max(ts) if len(ts) else datetime.now(timezone.utc).timestamp() * 1000
    holdout_cut = now_ms - holdout_days * 86400 * 1000
    train_cut = holdout_cut - train_days * 86400 * 1000

    train_mask = (ts >= train_cut) & (ts < holdout_cut)
    hold_mask = ts >= holdout_cut

    # Fallback: if timestamps cluster (all recent), use last 15% as holdout
    if train_mask.sum() < 10:
        order = np.argsort(ts)
        n = len(ts)
        hold_n = max(int(n * 0.15), 10)
        train_n = max(int(n * 0.70), 10)
        hold_idx = set(order[-hold_n:])
        train_idx = set(order[:train_n])
        train_mask = np.array([i in train_idx for i in range(n)])
        hold_mask = np.array([i in hold_idx for i in range(n)])

    X_train, y_train = X[train_mask], y[train_mask]
    X_hold, y_hold = X[hold_mask], y[hold_mask]

    metrics: dict[str, Any] = {"frontmatter": FRONTMATTER, "n_total": len(rows),
                               "n_train": int(train_mask.sum()), "n_holdout": int(hold_mask.sum())}

    try:
        import xgboost as xgb
        from sklearn.metrics import accuracy_score, average_precision_score

        model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            eval_metric="logloss",
            random_state=42,
        )
        if len(X_train) < 10:
            X_train, y_train = X[: int(len(X) * 0.7)], y[: int(len(y) * 0.7)]
            X_hold, y_hold = X[int(len(X) * 0.7):], y[int(len(y) * 0.7):]

        model.fit(X_train, y_train)
        proba = model.predict_proba(X_hold)[:, 1] if len(X_hold) else np.array([])
        pred = (proba >= 0.5).astype(int) if len(proba) else np.array([])

        metrics["accuracy"] = float(accuracy_score(y_hold, pred)) if len(pred) else None
        metrics["pr_auc"] = float(average_precision_score(y_hold, proba)) if len(proba) and y_hold.sum() > 0 else None

        imp = model.feature_importances_
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "feature_importance.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["feature", "importance"])
            for name, val in sorted(zip(FEATURE_COLS, imp), key=lambda x: -x[1]):
                w.writerow([name, float(val)])
        model.save_model(str(OUT / "model.json"))
    except ImportError:
        log.warning("xgboost/sklearn not installed — writing stub metrics")
        metrics["accuracy"] = None
        metrics["pr_auc"] = None
        metrics["stub"] = True
        OUT.mkdir(parents=True, exist_ok=True)

    # Holdout economics proxy: predicted-positive mean final_score as return proxy
    hold_rows = [r for r, m in zip(rows, hold_mask) if m]
    pos_scores = [_float(r.get("final_score", 0)) for r in hold_rows if _float(r.get("final_score", 0)) >= 0.52]
    neg_scores = [_float(r.get("final_score", 0)) for r in hold_rows if _float(r.get("final_score", 0)) < 0.52]
    wins = sum(s for s in pos_scores if s > 0)
    losses = abs(sum(s for s in neg_scores if s < 0))
    pf = wins / losses if losses > 0 else (None if wins == 0 else float("inf"))
    mean_ret = float(np.mean(pos_scores)) if pos_scores else 0.0

    metrics["holdout_pf_proxy"] = pf
    metrics["holdout_mean_return_proxy"] = mean_ret
    metrics["wallet_tagged_holdout"] = sum(
        1 for r, m in zip(rows, hold_mask) if m and _float(r.get("wallet_tag_count", 0)) > 0
    )
    metrics["generated_at"] = datetime.now(timezone.utc).isoformat()

    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    p.add_argument("--labeled", default=str(LABELED))
    args = p.parse_args()
    metrics = train_and_eval(labeled_path=Path(args.labeled))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
