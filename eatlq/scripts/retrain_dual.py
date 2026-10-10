"""Run-003B dual-model retrain: baseline (4 features) vs enriched (4 + new).

Reads an enriched labeled dataset (output of enrich_features.py), trains two
XGBoost models on the *same* temporal split, then runs the full Run-003B
evaluation contract on both.

The "promote" decision is deferred to eval_run003b_gate.py — this script
only writes models and JSON metrics.

Usage:
  python scripts/retrain_dual.py \\
      --input  logs/labeled_dataset_run003a_v2_only_enriched.csv \\
      --outdir models/run003b \\
      --tag    run003a_smoke
"""
from __future__ import annotations
import argparse
import json
import pickle
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import accuracy_score, average_precision_score

BASELINE_FEATURES = [
    "price_velocity_5m",
    "buy_pressure_ratio",
    "token_volatility_5m",
    "activity_heat",
]

ENRICHED_ADDITIONS = [
    "pool_age_min",
    "is_new_pool_5m",
    "is_new_pool_15m",
    "is_new_pool_60m",
    "sm_buy_count_5m",
    "sm_sell_count_5m",
    "sm_unique_buyers_5m",
    "sm_net_flow_5m",
    "sm_buy_pressure_ratio_5m",
    "sm_first_seen_in_pool",
    "sm_wallet_tier_max",
    "sm_wallet_tier_mean",
    "unique_buyers_5m",
    "buyer_gini_5m",
    "top1_buyer_share_5m",
    "top3_buyer_share_5m",
    "top5_buyer_share_5m",
    "new_buyer_ratio_5m",
    "repeat_buyer_ratio_5m",
    "unique_buyer_entropy_5m",
]

ENRICHED_FEATURES = BASELINE_FEATURES + ENRICHED_ADDITIONS


def build_splits(df: pd.DataFrame, holdout_days: int = 2):
    df = df.sort_values("timestamp").reset_index(drop=True)
    latest_ms = int(df["timestamp"].max())
    cutoff_ms = latest_ms - holdout_days * 86_400_000
    train_val = df[df["timestamp"] < cutoff_ms].copy()
    holdout = df[df["timestamp"] >= cutoff_ms].copy()
    mid = len(train_val) * 7 // 10
    train = train_val.iloc[:mid].copy()
    val = train_val.iloc[mid:].copy()
    mid_h = len(holdout) // 2
    val_h = holdout.iloc[:mid_h].copy()
    test = holdout.iloc[mid_h:].copy()
    return train, val, val_h, test


def train_one(feat_cols: List[str], train: pd.DataFrame, val: pd.DataFrame):
    X_tr = train[feat_cols].fillna(0).to_numpy()
    y_tr = train["label"].astype(int).to_numpy()
    X_va = val[feat_cols].fillna(0).to_numpy()
    y_va = val["label"].astype(int).to_numpy()

    pos_weight = (y_tr == 0).sum() / max(1, (y_tr == 1).sum())
    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        scale_pos_weight=pos_weight,
        n_jobs=4,
        eval_metric="logloss",
        tree_method="hist",
        random_state=17,
    )
    model.fit(X_tr, y_tr, eval_set=[(X_va, y_va)], verbose=False)

    raw_val = model.predict_proba(X_va)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(raw_val, y_va)
    return model, calibrator


def score(model, calibrator, X: np.ndarray) -> np.ndarray:
    raw = model.predict_proba(X)[:, 1]
    return np.asarray(calibrator.transform(raw))


def pick_ev_threshold(proba: np.ndarray, y: np.ndarray, nr: np.ndarray,
                      cost: float = 0.006) -> float:
    best_thr = 0.5
    best_ev = -1e9
    for thr in np.arange(0.15, 0.80, 0.05):
        taken = proba >= thr
        if taken.sum() < 5:
            continue
        ev = float(nr[taken].mean()) - cost
        if ev > best_ev:
            best_ev = ev
            best_thr = float(thr)
    return round(best_thr, 2)


def evaluate(tag: str, proba: np.ndarray, y: np.ndarray, nr: np.ndarray,
             pools: np.ndarray, threshold: float, cost_per_trade: float = 0.006) -> dict:
    y_pred = (proba >= threshold).astype(int)
    acc = float(accuracy_score(y, y_pred))
    pr_auc = float(average_precision_score(y, proba))

    order = np.argsort(-proba)
    k10 = max(1, len(proba) // 10)
    top = order[:k10]

    top_nr = nr[top]
    top_pool = pools[top]

    def avg_median_win(vals):
        if len(vals) == 0:
            return float("nan"), float("nan"), float("nan")
        return float(vals.mean()), float(np.median(vals)), float((vals > 0).mean())

    tdec_avg, tdec_med, tdec_win = avg_median_win(top_nr)

    # remove top-K biggest winners (by actual net_return) and re-check
    def remove_top_k_avg(k: int):
        if len(top_nr) <= k:
            return float("nan"), 0
        sorted_idx = np.argsort(-top_nr)
        kept = np.array([top[sorted_idx][i] for i in range(k, len(sorted_idx))])
        return float(nr[kept].mean()), int(len(kept))

    tdec_ex5, ex5_n = remove_top_k_avg(5)
    tdec_ex10, ex10_n = remove_top_k_avg(10)

    # profit concentration: what share of the SUM of positive net in top-decile
    # came from the top 5 winners?
    pos_vals = top_nr[top_nr > 0]
    sum_pos = float(pos_vals.sum()) if len(pos_vals) else 0.0
    top5_sorted = np.sort(top_nr)[-5:] if len(top_nr) >= 5 else top_nr
    top5_pos_sum = float(top5_sorted[top5_sorted > 0].sum()) if len(top5_sorted) else 0.0
    profit_concentration_top5_pct = (
        round(100.0 * top5_pos_sum / sum_pos, 2) if sum_pos > 0 else None
    )

    # pool-grouped bootstrap CI on top-decile avg_net
    # resample unique pools in the top decile with replacement to get a
    # pool-level robustness confidence interval. Using 2000 resamples.
    unique_top_pools = np.unique(top_pool)
    rng = np.random.default_rng(17)
    boot = []
    for _ in range(2000):
        sample = rng.choice(unique_top_pools, size=len(unique_top_pools), replace=True)
        mask = np.isin(top_pool, sample)
        if mask.sum() == 0:
            continue
        boot.append(float(top_nr[mask].mean()))
    boot_arr = np.array(boot) if boot else np.array([float("nan")])
    ci_low = float(np.percentile(boot_arr, 2.5))
    ci_high = float(np.percentile(boot_arr, 97.5))
    ci_median = float(np.percentile(boot_arr, 50))

    # confidence buckets
    buckets = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)]
    bucket_rows = []
    for lo, hi in buckets:
        mask = (proba >= lo) & (proba < hi)
        n = int(mask.sum())
        if n == 0:
            bucket_rows.append({"bucket": f"[{lo:.1f},{hi:.2f})",
                                "n": 0, "pos_rate": None, "avg_net": None})
            continue
        bucket_rows.append({
            "bucket": f"[{lo:.1f},{hi:.2f})",
            "n": n,
            "pos_rate": round(float(y[mask].mean()), 4),
            "avg_net": round(float(nr[mask].mean()), 4),
        })

    # "confidence monotonicity" — does avg_net trend up as bucket ↑?
    bucket_avgs = [b["avg_net"] for b in bucket_rows if b["avg_net"] is not None]
    mono_ok = bool(all(bucket_avgs[i] <= bucket_avgs[i + 1] + 0.02
                       for i in range(len(bucket_avgs) - 1))) if len(bucket_avgs) >= 2 else False

    # trades-at-threshold summary
    taken = proba >= threshold
    traded_n = int(taken.sum())
    traded_avg = float(nr[taken].mean()) if traded_n else float("nan")

    return {
        "tag": tag,
        "threshold": threshold,
        "n_test": int(len(y)),
        "accuracy": round(acc, 4),
        "pr_auc": round(pr_auc, 4),
        "positive_rate": round(float(y.mean()), 4),
        "traded_n_at_threshold": traded_n,
        "avg_net_at_threshold": round(traded_avg, 4) if traded_n else None,
        "top_decile_k": int(k10),
        "top_decile_unique_pools": int(len(unique_top_pools)),
        "top_decile_avg_net": round(tdec_avg, 4),
        "top_decile_median_net": round(tdec_med, 4),
        "top_decile_win_rate": round(tdec_win, 4),
        "top_decile_remove_top5_avg_net": round(tdec_ex5, 4),
        "top_decile_remove_top5_n": ex5_n,
        "top_decile_remove_top10_avg_net": round(tdec_ex10, 4),
        "top_decile_remove_top10_n": ex10_n,
        "profit_concentration_top5_pct": profit_concentration_top5_pct,
        "pool_bootstrap_ci95_low": round(ci_low, 4),
        "pool_bootstrap_ci95_high": round(ci_high, 4),
        "pool_bootstrap_ci_median": round(ci_median, 4),
        "confidence_buckets": bucket_rows,
        "confidence_monotonic": mono_ok,
    }


def promotion_gate(metric: dict, ci_must_be_positive: bool = True) -> dict:
    """Apply Run-003B strict promotion gate.

    Pass ONLY if ALL of:
      1. top-decile avg_net > 0
      2. top-decile remove_top5 avg_net > 0
      3. top-decile remove_top10 avg_net > -0.05  (allow small regress)
      4. confidence_monotonic == True
      5. pool_bootstrap_ci95_low > 0  (optional but strongly recommended)
    """
    reasons = []
    passed = True
    if metric["top_decile_avg_net"] <= 0:
        passed = False
        reasons.append(f"top_decile_avg_net={metric['top_decile_avg_net']:.4f} <= 0")
    if metric["top_decile_remove_top5_avg_net"] is None or \
       metric["top_decile_remove_top5_avg_net"] <= 0:
        passed = False
        reasons.append(
            f"top_decile_remove_top5_avg_net="
            f"{metric['top_decile_remove_top5_avg_net']} <= 0"
        )
    if metric["top_decile_remove_top10_avg_net"] is None or \
       metric["top_decile_remove_top10_avg_net"] <= -0.05:
        passed = False
        reasons.append(
            f"top_decile_remove_top10_avg_net="
            f"{metric['top_decile_remove_top10_avg_net']} <= -0.05"
        )
    if not metric["confidence_monotonic"]:
        passed = False
        reasons.append("confidence buckets not monotonic (higher conf must → higher avg_net)")
    if ci_must_be_positive and metric["pool_bootstrap_ci95_low"] <= 0:
        passed = False
        reasons.append(
            f"pool_bootstrap_ci95_low={metric['pool_bootstrap_ci95_low']:.4f} <= 0 "
            "(top-decile alpha not robust to pool-level resampling)"
        )
    return {"passed": passed, "reasons": reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", default="models/run003b")
    ap.add_argument("--tag", default="dual")
    ap.add_argument("--holdout-days", type=int, default=2)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(args.input)
    df = df.dropna(subset=["timestamp", "label", "net_return"]).copy()
    df["timestamp"] = df["timestamp"].astype("int64")
    df["label"] = df["label"].astype(int)
    df["net_return"] = df["net_return"].astype(float)

    # Fill missing feature columns (baseline + enriched) as 0.
    for c in ENRICHED_FEATURES:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    train, val, val_h, test = build_splits(df, holdout_days=args.holdout_days)
    print(f"Split: train={len(train)}  val={len(val)}  val_h={len(val_h)}  test={len(test)}")
    print(f"Positive rate  train={train['label'].mean():.3f}  "
          f"test={test['label'].mean():.3f}")

    test_pools = test["token_address"].astype(str).str.lower().to_numpy()
    y_test = test["label"].astype(int).to_numpy()
    nr_test = test["net_return"].astype(float).to_numpy()

    results = {}
    for variant, feat_cols in [("baseline", BASELINE_FEATURES),
                                ("enriched", ENRICHED_FEATURES)]:
        print(f"\n=== training {variant} ({len(feat_cols)} features) ===")
        model, calibrator = train_one(feat_cols, train, val)

        # threshold picked on val_h (held-out validation)
        X_vh = val_h[feat_cols].fillna(0).to_numpy()
        proba_vh = score(model, calibrator, X_vh)
        thr = pick_ev_threshold(proba_vh, val_h["label"].to_numpy(),
                                 val_h["net_return"].to_numpy())
        print(f"  ev_threshold picked on val_h = {thr}")

        X_te = test[feat_cols].fillna(0).to_numpy()
        proba_te = score(model, calibrator, X_te)
        metrics = evaluate(variant, proba_te, y_test, nr_test, test_pools, thr)
        metrics["features"] = feat_cols
        metrics["feature_importances"] = dict(
            zip(feat_cols, [float(x) for x in model.feature_importances_])
        )
        gate = promotion_gate(metrics)
        metrics["promotion_gate"] = gate
        print(f"  {variant} accuracy={metrics['accuracy']:.4f}  "
              f"pr_auc={metrics['pr_auc']:.4f}")
        print(f"  {variant} top-decile avg={metrics['top_decile_avg_net']:.4f}  "
              f"ex5={metrics['top_decile_remove_top5_avg_net']:.4f}  "
              f"ex10={metrics['top_decile_remove_top10_avg_net']:.4f}")
        print(f"  {variant} CI95=[{metrics['pool_bootstrap_ci95_low']:.4f}, "
              f"{metrics['pool_bootstrap_ci95_high']:.4f}]")
        print(f"  {variant} gate_passed={gate['passed']}  reasons={gate['reasons']}")

        mp = outdir / f"signal_model_{variant}_{args.tag}.json"
        cp = outdir / f"signal_model_{variant}_{args.tag}_calibrator.pkl"
        mep = outdir / f"signal_model_{variant}_{args.tag}_metrics.json"
        model.save_model(str(mp))
        with cp.open("wb") as f:
            pickle.dump(calibrator, f)
        mep.write_text(json.dumps(metrics, indent=2))
        print(f"  saved {mp}")
        results[variant] = metrics

    summary_path = outdir / f"run003b_compare_{args.tag}.json"
    summary = {
        "input": args.input,
        "tag": args.tag,
        "baseline": results["baseline"],
        "enriched": results["enriched"],
    }
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\ncomparison summary: {summary_path}")


if __name__ == "__main__":
    main()
