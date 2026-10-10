#!/usr/bin/env python3
"""
Build a whitepaper-faithful Phase 1 v1_reconstruction.

The original `whale_sniper_v1.json` is NOT present in this repo. The whitepaper
Phase 1 spec is explicit (`原作者白皮书 2 外宣版.txt:184`):

    Phase 1: Synthetic Bootstrap
    Features: win_rate_30d, avg_roi, price_velocity_5m, buy_pressure_ratio, is_whale
    Limitation: win_rate_30d fixed at 0.5, avg_roi fixed at 0.0
                → "半随机猜测" (semi-random guess)
    Output: whale_sniper_v1.json — initial scoring capability for the fusion layer

This script reproduces that artifact:
  - Generates synthetic labelled data matching the 5-feature spec.
  - Trains XGBoost with parameters consistent with the EATLQ baseline.
  - Saves to models/research_only/whale_sniper_v1_reconstruction.json.
  - Documents the synthetic label rule so the reconstruction is auditable.

Label rule (synthetic ground truth for bootstrap):
  positive iff (price_velocity_5m > 0.005) AND (buy_pressure_ratio > 0.55)
        AND ((is_whale = 1) OR (win_rate_30d > 0.55))

This is the kind of bootstrap label the original author would have used to
seed the model before real Phase 2 labels were available. Stored verbatim
in the metrics file for transparency.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "models" / "research_only"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FEATURES = ["win_rate_30d", "avg_roi", "price_velocity_5m", "buy_pressure_ratio", "is_whale"]


def synthetic_label(row: dict) -> int:
    pv_ok  = row["price_velocity_5m"] > 0.005
    bpr_ok = row["buy_pressure_ratio"] > 0.55
    whale_ok = (row["is_whale"] == 1) or (row["win_rate_30d"] > 0.55)
    return int(pv_ok and bpr_ok and whale_ok)


def generate_synthetic(n: int, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    # Distributions chosen to span realistic Base ranges per sprint092 5m features
    win_rate = rng.beta(2, 2, n) * 0.4 + 0.30      # 0.30-0.70
    avg_roi  = rng.normal(0.0, 0.05, n)            # near zero, slight noise
    price_v  = rng.normal(0.0, 0.015, n)           # 5m return distribution
    buy_pr   = rng.beta(2, 2, n)                   # 0-1, mode ~0.5
    is_whale = rng.binomial(1, 0.15, n).astype(float)  # ~15% whale events
    X = np.column_stack([win_rate, avg_roi, price_v, buy_pr, is_whale])
    y = np.array([
        synthetic_label({"win_rate_30d": w, "avg_roi": r, "price_velocity_5m": v,
                         "buy_pressure_ratio": b, "is_whale": iw})
        for w, r, v, b, iw in X
    ])
    return X, y


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=20_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", type=str, default=str(OUT_DIR / "whale_sniper_v1_reconstruction.json"))
    args = p.parse_args()

    from xgboost import XGBClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, average_precision_score

    X, y = generate_synthetic(args.n, args.seed)
    print(f"synthetic dataset: n={args.n}, positive_rate={y.mean():.3f}")

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=args.seed,
                                              stratify=y if y.sum() > 1 else None)
    pos_rate = max(y_tr.mean(), 1e-6)
    spw = max(1.0, (1 - pos_rate) / pos_rate)
    clf = XGBClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.1,
        scale_pos_weight=spw,
        use_label_encoder=False, eval_metric="logloss",
    )
    clf.fit(X_tr, y_tr)
    proba = clf.predict_proba(X_te)[:, 1]
    pred = (proba >= 0.5).astype(int)
    metrics = {
        "rows_total": int(args.n),
        "rows_train": int(len(y_tr)),
        "rows_holdout": int(len(y_te)),
        "positive_rate_train": float(y_tr.mean()),
        "positive_rate_holdout": float(y_te.mean()),
        "accuracy": float(accuracy_score(y_te, pred)),
        "average_precision": float(average_precision_score(y_te, proba)),
        "scale_pos_weight": float(spw),
        "feature_names": FEATURES,
        "label_rule_source": "scripts/wallet_profile_v0/sprint093_build_v1_reconstruction.py::synthetic_label",
        "label_rule_description": "label = 1 iff price_velocity_5m > 0.005 AND buy_pressure_ratio > 0.55 AND (is_whale = 1 OR win_rate_30d > 0.55)",
        "whitepaper_phase1_disclosure": (
            "Original whale_sniper_v1.json artifact is NOT present in this repo. "
            "This is a synthetic-bootstrap reconstruction following whitepaper Phase 1 spec "
            "(5 features: win_rate_30d, avg_roi, price_velocity_5m, buy_pressure_ratio, is_whale; "
            "Phase 1 used fixed defaults win_rate_30d=0.5 and avg_roi=0.0 → semi-random guess)."
        ),
    }

    out_path = Path(args.out)
    clf.save_model(out_path)
    metrics_path = out_path.with_name(out_path.stem + "_metrics.json")
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(f"saved: {out_path}")
    print(f"metrics: {metrics_path}")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
