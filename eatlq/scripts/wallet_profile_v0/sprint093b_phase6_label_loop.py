#!/usr/bin/env python3
"""
Sprint093-B — Independent Phase 6 label loop.

Goal (per user spec): answer the question
  "Do these 5m features have any predictive power for 5-minute-forward
   price movement, regardless of whitepaper signal trigger?"

This is an ORTHOGONAL test to Sprint093-A. It does NOT use the score formula
at all. It just asks: if we labelled every candidate by its actual 5-minute
forward return and trained a simple classifier on the 5m pre-signal feature
vector, would there be any signal at all?

Pipeline:
  1. Candidate stream from sprint092 v3 (base-routing-excluded + Quoter V2).
  2. For each candidate, fetch slot0/tick at signal_ts + 300s and compute
     forward return using `tick_delta_to_return(Δtick, direction)`.
  3. Label = 1 if forward_return_pct > threshold (default 0.0 = simple sign).
  4. Time-based train/holdout split (no random shuffle — preserves causality):
        train = first 70% by signal_ts
        holdout = last 30%
  5. Report holdout accuracy / WR / PF on multiple baselines:
        - majority-class baseline
        - logistic regression on 5m features (5-d input)
        - if xgboost available: XGB with same features
  6. Single-token concentration call-out (AERO etc.) flagged in red.
  7. If holdout n < 30 → verdict = INCONCLUSIVE, no metrics interpreted.

DOES NOT lift FAILED_NO_7D_ALPHA. RESEARCH_ONLY.
DOES NOT merge with Sprint093-A verdict.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))

from sprint093_common import (  # noqa: E402
    attach_5m_forward_label,
    attach_5m_pre_features,
    fetch_candidates,
)

log = logging.getLogger("sprint093b")

OUT_DIR = REPO / "mvp" / "audits" / "wallet_profile_v0" / "sprint093b_phase6_label_loop"
OUT_DIR.mkdir(parents=True, exist_ok=True)

RESEARCH_ONLY = True
MIN_HOLDOUT_FOR_VERDICT = 30  # whitepaper-aligned forward gate n threshold
TRAIN_FRAC = 0.70
WHITEPAPER_HOLDOUT_ACCURACY_GATE = 0.55  # per design.md §3 + whitepaper Phase 6

FEATURE_NAMES = (
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
    "unique_buyer_entropy_5m",
)


def _build_feature_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[float]]:
    X = []
    y = []
    rets = []
    for r in rows:
        f = r["features_5m"]
        X.append([
            float(f.get("price_velocity_5m", 0.0)),
            float(f.get("buy_pressure_ratio", 0.5)),
            float(f.get("token_volatility_5m", 0.0)),
            float(f.get("activity_heat", 0.0)),
            float(f.get("unique_buyer_entropy_5m", 0.0)),
        ])
        y.append(int(r["label_5m"]["label"]))
        rets.append(float(r["label_5m"]["forward_return_pct"]))
    return np.array(X), np.array(y), rets


def _metrics(y_true: np.ndarray, y_pred: np.ndarray, rets: list[float]) -> dict:
    """Acc/WR/PF/n where:
      - acc: classification accuracy
      - WR: % of positively-predicted candidates where forward return > 0
      - PF: sum(positive returns of positives) / |sum(negative returns of positives)|
    """
    n = int(len(y_true))
    if n == 0:
        return {"n": 0}
    accuracy = float((y_true == y_pred).mean())
    pos_idx = np.where(y_pred == 1)[0]
    if pos_idx.size == 0:
        return {
            "n": n, "n_predicted_pos": 0,
            "accuracy": accuracy, "win_rate": None, "pf": None,
            "mean_return_pos": None,
        }
    pos_returns = np.array([rets[i] for i in pos_idx])
    wins = pos_returns[pos_returns > 0].sum()
    losses = abs(pos_returns[pos_returns < 0].sum())
    pf = float(wins / losses) if losses > 0 else None
    wr = float((pos_returns > 0).mean())
    return {
        "n": n,
        "n_predicted_pos": int(pos_idx.size),
        "accuracy": accuracy,
        "win_rate": wr,
        "pf": pf,
        "mean_return_pos": float(pos_returns.mean()),
    }


def _train_predict_logreg(X_tr: np.ndarray, y_tr: np.ndarray, X_te: np.ndarray) -> np.ndarray:
    """Fall back implementation: tiny binary logistic regression using sklearn."""
    from sklearn.linear_model import LogisticRegression
    if len(np.unique(y_tr)) < 2:
        return np.full(len(X_te), int(y_tr[0] if len(y_tr) else 0))
    clf = LogisticRegression(max_iter=500, class_weight="balanced")
    clf.fit(X_tr, y_tr)
    return clf.predict(X_te)


def _train_predict_xgb(X_tr: np.ndarray, y_tr: np.ndarray, X_te: np.ndarray) -> np.ndarray | None:
    try:
        from xgboost import XGBClassifier
        if len(np.unique(y_tr)) < 2:
            return np.full(len(X_te), int(y_tr[0] if len(y_tr) else 0))
        clf = XGBClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.1,
            use_label_encoder=False, eval_metric="logloss",
        )
        clf.fit(X_tr, y_tr)
        return clf.predict(X_te)
    except Exception as exc:
        log.warning("xgb unavailable: %s", exc)
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--max-pool-age-hours", type=int, default=8760)
    p.add_argument("--lowest-thresh", type=int, default=10_000)
    p.add_argument("--label-threshold-pct", type=float, default=0.0,
                   help="forward return % above which label=1 (default: 0 = up/down sign)")
    p.add_argument("--no-quoter", action="store_true")
    args = p.parse_args()

    # 1 — discovery
    cands = fetch_candidates(
        days=args.days,
        max_pool_age_hours=args.max_pool_age_hours,
        lowest_thresh=args.lowest_thresh,
        apply_quoter_v2=not args.no_quoter,
    )
    log.info("candidates (post-quoter): %d", len(cands))

    # 2 — attach 5m pre features
    enriched: list[dict] = []
    for c in cands:
        try:
            attach_5m_pre_features(c)
            enriched.append(c)
        except Exception as exc:
            log.warning("pre-feature fail %s: %s", c.get("pool", "?")[:10], exc)
    log.info("enriched: %d", len(enriched))

    # 3 — attach 5m forward labels
    labelled: list[dict] = []
    skipped_no_forward = 0
    for c in enriched:
        attach_5m_forward_label(c, threshold_pct=args.label_threshold_pct)
        lab = c["label_5m"]
        if lab["label"] is None:
            skipped_no_forward += 1
            continue
        labelled.append(c)
    log.info("labelled: %d (skipped no-forward: %d)", len(labelled), skipped_no_forward)

    # 4 — token concentration callout (per user spec: AERO etc. flagged red)
    target_counter: Counter = Counter(r["target_address"] for r in labelled)
    top_tokens = target_counter.most_common(5)
    total = len(labelled)
    concentration_flag = False
    top_token_addr = top_tokens[0][0] if top_tokens else ""
    top_token_share = (top_tokens[0][1] / total) if total else 0.0
    if total > 0 and top_token_share >= 0.50:
        concentration_flag = True
        log.warning("[CONCENTRATION] %s holds %.0f%% of labelled set (%d/%d)",
                    top_token_addr, top_token_share * 100, top_tokens[0][1], total)

    # 5 — time-based split
    labelled_sorted = sorted(labelled, key=lambda r: int(r["signal_ts"]))
    split = int(len(labelled_sorted) * TRAIN_FRAC)
    train_rows = labelled_sorted[:split]
    holdout_rows = labelled_sorted[split:]
    log.info("split: train=%d holdout=%d (train_frac=%.2f)",
             len(train_rows), len(holdout_rows), TRAIN_FRAC)

    holdout_n = len(holdout_rows)
    inconclusive = holdout_n < MIN_HOLDOUT_FOR_VERDICT

    # 6 — metrics
    results: dict[str, Any] = {
        "majority_class_baseline": {},
        "logistic_regression": {},
        "xgboost": {},
    }
    if labelled_sorted:
        X_tr, y_tr, rets_tr = _build_feature_matrix(train_rows)
        X_te, y_te, rets_te = _build_feature_matrix(holdout_rows)

        # baseline: predict majority class from train
        if y_tr.size:
            major = int(round(y_tr.mean()))
        else:
            major = 0
        y_pred_baseline = np.full(len(y_te), major)
        results["majority_class_baseline"] = _metrics(y_te, y_pred_baseline, rets_te)
        results["majority_class_baseline"]["predicted_class"] = major

        # logreg
        if X_tr.size and X_te.size:
            y_pred_lr = _train_predict_logreg(X_tr, y_tr, X_te)
            results["logistic_regression"] = _metrics(y_te, y_pred_lr, rets_te)

        # xgb
        y_pred_xgb = _train_predict_xgb(X_tr, y_tr, X_te) if X_tr.size and X_te.size else None
        if y_pred_xgb is not None:
            results["xgboost"] = _metrics(y_te, y_pred_xgb, rets_te)
        else:
            results["xgboost"] = {"unavailable": True}

    # 7 — return distribution for transparency
    all_returns = [r["label_5m"]["forward_return_pct"] for r in labelled]
    return_dist = {}
    if all_returns:
        arr = np.array(all_returns)
        return_dist = {
            "n": int(arr.size),
            "label_threshold_pct": args.label_threshold_pct,
            "frac_positive_label": float((arr > args.label_threshold_pct).mean()),
            "mean_return_pct": float(arr.mean()),
            "median_return_pct": float(np.median(arr)),
            "p10": float(np.quantile(arr, 0.10)),
            "p90": float(np.quantile(arr, 0.90)),
            "max": float(arr.max()),
            "min": float(arr.min()),
        }

    # 8 — write per-candidate CSV
    out_csv = OUT_DIR / "labelled_candidates.csv"
    if labelled:
        with out_csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "signal_ts", "signal_time", "pool", "target_address", "sender",
                "notional_usd", "fee_bps", "pool_age_sec",
                "entry_tick", "forward_tick", "delta_tick", "forward_return_pct", "label",
                "n_swaps_in_5m_window",
                *FEATURE_NAMES,
            ])
            for r in labelled:
                lab = r["label_5m"]
                f5 = r["features_5m"]
                w.writerow([
                    r["signal_ts"], r.get("signal_time", ""),
                    r["pool"], r.get("target_address", ""), r.get("sender", ""),
                    r["notional_usd"], r.get("fee_bps"), r.get("pool_age_sec"),
                    r.get("tick"), lab.get("forward_tick"), lab.get("delta_tick"),
                    lab.get("forward_return_pct"), lab.get("label"),
                    lab.get("n_swaps_in_window"),
                    *(f5.get(name, 0.0) for name in FEATURE_NAMES),
                ])
    log.info("labelled CSV: %s (%d rows)", out_csv, len(labelled))

    # 9 — verdict
    if inconclusive:
        verdict = "INCONCLUSIVE_N_TOO_SMALL"
        verdict_reason = (
            f"holdout n={holdout_n} < {MIN_HOLDOUT_FOR_VERDICT} → cannot meaningfully evaluate "
            f"holdout accuracy/PF/WR. No alpha claim either way."
        )
    else:
        # Compare to whitepaper gate (55% holdout accuracy)
        best_acc = max(
            results.get("logistic_regression", {}).get("accuracy", 0.0) or 0.0,
            results.get("xgboost", {}).get("accuracy", 0.0) or 0.0,
        )
        if best_acc >= WHITEPAPER_HOLDOUT_ACCURACY_GATE:
            verdict = "PASS_PHASE6_HOLDOUT_GATE"
            verdict_reason = f"best holdout acc {best_acc:.3f} ≥ {WHITEPAPER_HOLDOUT_ACCURACY_GATE}"
        else:
            verdict = "FAIL_PHASE6_HOLDOUT_GATE"
            verdict_reason = f"best holdout acc {best_acc:.3f} < {WHITEPAPER_HOLDOUT_ACCURACY_GATE}"

    summary = {
        "sprint": "093B",
        "name": "phase6_label_loop_independent",
        "research_only": RESEARCH_ONLY,
        "guard_lifted": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": vars(args),
        "candidates_post_quoter": len(enriched),
        "labelled": len(labelled),
        "skipped_no_forward": skipped_no_forward,
        "train_n": len(train_rows),
        "holdout_n": holdout_n,
        "train_frac": TRAIN_FRAC,
        "min_holdout_for_verdict": MIN_HOLDOUT_FOR_VERDICT,
        "whitepaper_holdout_accuracy_gate": WHITEPAPER_HOLDOUT_ACCURACY_GATE,
        "top_tokens": [{"address": addr, "n": n, "share": n / max(total, 1)}
                       for addr, n in top_tokens],
        "concentration_flag": concentration_flag,
        "top_token_share": top_token_share,
        "return_distribution": return_dist,
        "results": results,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "notes": [
            "Time-based split (no random shuffle) preserves causality",
            "Holdout accuracy gated against whitepaper Phase 6 spec (≥55%)",
            "If holdout n < 30 → INCONCLUSIVE, no metric interpreted",
            "Single-token concentration flagged when top target ≥ 50%",
            "FAILED_NO_7D_ALPHA NOT lifted. RESEARCH_ONLY.",
        ],
    }
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info("summary: %s", summary_path)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
