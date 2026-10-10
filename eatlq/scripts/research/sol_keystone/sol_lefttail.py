#!/usr/bin/env python3
"""Task 3 — left-tail predictor (zero RPC, local ML)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from common import (
    OUT,
    TRAIN_DAYS,
    TEST_DAYS,
    build_universe_rows,
    feature_matrix,
    load_json,
    write_json,
)


def main():
    raw = load_json(OUT / "_sol_realcost_labels.json")
    labels = {x["mint"]: x for x in raw["labels"]}

    rows = [r for r in build_universe_rows() if r["mint"] in labels and r["has_earlypath"]]
    X_rows, y, meta = [], [], []
    for r in rows:
        X_rows.append(feature_matrix(r))
        y.append(1 if labels[r["mint"]]["fatal"] else 0)
        meta.append({"mint": r["mint"], "split": r["split"], "day": r["day"]})

    X = pd.DataFrame(X_rows).fillna(0)
    y = np.array(y)
    tr = np.array([m["day"] in TRAIN_DAYS for m in meta])
    te = np.array([m["day"] in TEST_DAYS for m in meta])

    clf = HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0,
    )
    clf.fit(X[tr], y[tr])
    p_tr = clf.predict_proba(X[tr])[:, 1]
    p_te = clf.predict_proba(X[te])[:, 1] if te.sum() else np.array([])

    auc_tr = roc_auc_score(y[tr], p_tr) if tr.sum() and len(np.unique(y[tr])) > 1 else float("nan")
    auc_te = roc_auc_score(y[te], p_te) if te.sum() and len(np.unique(y[te])) > 1 else float("nan")

    cut = float(np.quantile(p_tr, 0.60))
    kept_tr = p_tr <= cut
    kept_te = p_te <= cut if len(p_te) else np.array([])

    probs = np.zeros(len(meta))
    probs[tr] = p_tr
    if te.sum():
        probs[te] = p_te

    out = {
        "keystone_base_auc": 0.86,
        "n_total": int(len(rows)),
        "n_train": int(tr.sum()),
        "n_test": int(te.sum()),
        "fatal_base_rate": {
            "all": float(y.mean()),
            "train": float(y[tr].mean()),
            "test": float(y[te].mean()),
        },
        "auc": {"train": float(auc_tr), "test": float(auc_te)},
        "prune_cut_p60": cut,
        "survivor_fraction_after_prune": {
            "train": float(kept_tr.mean()),
            "test": float(kept_te.mean()) if len(kept_te) else None,
        },
        "fatal_rate_after_prune": {
            "train": float(y[tr][kept_tr].mean()) if kept_tr.any() else None,
            "test": float(y[te][kept_te].mean()) if len(kept_te) and kept_te.any() else None,
        },
        "feature_columns": list(X.columns),
        "predictions": [
            {
                "mint": m["mint"],
                "split": m["split"],
                "p_fatal": float(probs[i]),
                "fatal": int(y[i]),
                "keep_pruned": bool(probs[i] <= cut),
            }
            for i, m in enumerate(meta)
        ],
    }
    write_json("_sol_lefttail_model.json", out)
    print(f"lefttail n={len(rows)} AUC train={auc_tr:.3f} test={auc_te:.3f}")


if __name__ == "__main__":
    main()
