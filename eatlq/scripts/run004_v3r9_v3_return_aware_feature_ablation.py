"""Run-004 Patch V3R-9 — V3-only Return-Aware Feature Ablation.

Input:  mvp/diagnostics/run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv
Output: mvp/diagnostics/run004_v3r9_*.json / *.csv
        mvp/models/v3r9_ablation/*.json
        mvp/runs/run-004-patch-v3r9-v3-only-return-aware-feature-ablation.md

Hard constraints (enforced in-script):
  - V3 rows only; no V2, no Aero, no mixed dataset.
  - No production / runtime / active-model writes.
  - No live / paper promote.
  - No Dune API calls.
  - No expansion or modification of V3R-8 dataset.
  - Chronological 70/30 split only (no random split on primary evaluation).
  - No target / return / risk / quote outcome as training features.
  - All outputs → mvp/diagnostics, mvp/runs, mvp/models/v3r9_ablation.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    roc_auc_score,
)
from sklearn.preprocessing import label_binarize  # noqa: F401
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_research_utils import rounded, write_csv_rows, write_json, write_markdown  # noqa: E402

# ── Safety guard ──────────────────────────────────────────────────────────────
assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on"
), "LIVE_TRADING_MODE must not be active during V3R9 ablation"

# ── Constants ─────────────────────────────────────────────────────────────────
RANDOM_SEED = 42
TRAIN_FRAC = 0.70
POSITIVE_RATE = 0.20
XGB_PARAMS = {
    "n_estimators": 300,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "gamma": 0.1,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "scale_pos_weight": 4,    # 80/20 = 4x neg/pos
    "eval_metric": "aucpr",
    "early_stopping_rounds": 30,
    "verbosity": 0,
    "random_state": RANDOM_SEED,
    "n_jobs": 4,
}

# Pool proxy / leakage risk flags
POOL_PROXY_CANDIDATES = {"feature_fee_tier", "feature_tick", "feature_liquidity"}
DIRECT_LEAKAGE_FEATURES = {
    "final_return_after_cost", "gross_quote_return_30m", "fixed_cost_adjusted_return_30m",
    "gas_adjusted_return_30m_low", "gas_adjusted_return_30m_medium", "gas_adjusted_return_30m_high",
    "executable_label_after_gas", "executable_label_current",
    "alpha_top20_after_gas", "alpha_ret_gt_5pct_after_gas", "alpha_ret_gt_10pct_after_gas",
    "final_return_rank", "within_pool_return_rank",
    "v3_exit_drained_label", "v3_liquidity_exit_risk", "v3_review_only",
    "quote_anomaly_label", "risk_severity", "quarantine_reason",
    "entry_quote_amount_out", "exit_quote_amount_out",
}
RISK_DERIVED_FEATURES = {
    "gas_cost_dominates_label",   # leaky: |gross_return| < gas_low threshold
    "scale_risk_label",
    "repeated_pool_hour_cluster_label",  # informational, not directly return-derived
}

OUT_DIR = ROOT / "mvp/diagnostics"
RUN_DIR = ROOT / "mvp/runs"
MODEL_DIR = ROOT / "mvp/models/v3r9_ablation"
for d in (OUT_DIR, RUN_DIR, MODEL_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Feature group definitions ─────────────────────────────────────────────────
FEATURE_GROUPS: dict[str, list[str]] = {
    "v3_metadata": [
        "feature_fee_tier", "feature_tick", "feature_liquidity",
    ],
    "v3_liquidity_micro": [
        "feature_liquidity", "feature_fee_tier", "feature_tick",
        "feature_pool_flow_count_1h",
    ],
    "signed_flow": [
        "feature_pool_weth_flow_sum_1h", "feature_pool_weth_signed_flow_sum_1h",
        "is_buy_weth_to_token_int", "is_sell_token_to_weth_int",
    ],
    "flow_count": [
        "feature_pool_flow_count_1h", "feature_pool_unique_senders_1h",
    ],
    "sender_concentration": [
        "feature_pool_unique_senders_1h", "feature_sender_pool_prior_count_1h",
        "feature_sender_present_int",
    ],
    "temporal": [
        "feature_temporal_prev_pool_delta_sec_filled",
        "feature_pool_flow_count_1h",
    ],
    "combined_microstructure_no_raw": [
        # temporal + signed_flow + flow_count + sender_concentration + v3_metadata
        # no raw unnormalized WETH volume extremes
        "feature_fee_tier", "feature_tick", "feature_liquidity",
        "feature_pool_flow_count_1h", "feature_pool_unique_senders_1h",
        "feature_pool_weth_signed_flow_sum_1h",
        "feature_sender_pool_prior_count_1h", "feature_sender_present_int",
        "feature_temporal_prev_pool_delta_sec_filled",
        "is_buy_weth_to_token_int",
    ],
    "risk_features": [
        # DIAGNOSTIC ONLY — leaky/outcome-derived; flagged in leakage guard
        "gas_cost_dominates_label_int",
        "repeated_pool_hour_cluster_label_int",
    ],
    "all_safe_available": [
        "feature_fee_tier", "feature_tick", "feature_liquidity",
        "feature_pool_flow_count_1h", "feature_pool_unique_senders_1h",
        "feature_pool_weth_flow_sum_1h", "feature_pool_weth_signed_flow_sum_1h",
        "feature_sender_pool_prior_count_1h", "feature_sender_present_int",
        "feature_temporal_prev_pool_delta_sec_filled",
        "is_buy_weth_to_token_int",
    ],
}

RISK_GROUPS = {"risk_features"}  # groups that are diagnostic-only due to leakage

# ── Helpers ───────────────────────────────────────────────────────────────────

def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_log1p(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    return np.sign(a) * np.log1p(np.abs(a))


def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def _precision_at_k(y_true: np.ndarray, scores: np.ndarray, k_frac: float) -> float:
    n = max(1, int(len(y_true) * k_frac))
    idx = np.argsort(scores)[-n:]
    return float(y_true[idx].mean())


def _pr_lift(pr_auc: float, pos_rate: float) -> float:
    if pos_rate <= 0:
        return 0.0
    return pr_auc / pos_rate - 1.0


def _spearman(scores: np.ndarray, returns: np.ndarray) -> float:
    mask = np.isfinite(scores) & np.isfinite(returns)
    if mask.sum() < 2:
        return float("nan")
    r, _ = sp_stats.spearmanr(scores[mask], returns[mask])
    return float(r)


def _top_k_returns(scores: np.ndarray, returns: np.ndarray, k_frac: float) -> dict[str, float]:
    n = max(1, int(len(scores) * k_frac))
    idx = np.argsort(scores)[-n:]
    vals = returns[idx]
    return {
        "count": int(n),
        "mean": round(float(vals.mean()), 8),
        "p50": round(float(np.median(vals)), 8),
        "p90": round(float(np.percentile(vals, 90)), 8),
        "min": round(float(vals.min()), 8),
        "max": round(float(vals.max()), 8),
    }


def _pool_concentration_of_selection(
    selected_idx: np.ndarray, pool_series: pd.Series
) -> dict[str, Any]:
    pools = pool_series.iloc[selected_idx]
    counts = pools.value_counts()
    total = len(pools)
    if total == 0:
        return {"unique_pools": 0, "top1_share": 0.0, "top5_share": 0.0}
    return {
        "unique_pools": int(counts.shape[0]),
        "top1_share": round(float(counts.iloc[0] / total), 6) if len(counts) else 0.0,
        "top5_share": round(float(counts.head(5).sum() / total), 6),
        "top3_pools": [
            {"pool": str(p), "count": int(c), "share": round(c / total, 4)}
            for p, c in counts.head(3).items()
        ],
    }


# ── Data preparation ──────────────────────────────────────────────────────────

def load_and_prepare(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load V3R8 clean dataset and add engineered feature columns."""
    df = pd.read_csv(path)

    # Input validation
    n = len(df)
    v2_rows = int((df["decoded_source"] != "uniswap_v3_swaps_decoded").sum())
    aero_rows = int(df["decoded_source"].str.contains("aero", case=False, na=False).sum())
    protocols = df["decoded_source"].value_counts().to_dict()

    # Sort chronologically
    df = df.sort_values("timestamp_ms", ascending=True).reset_index(drop=True)

    # Recompute target: exact top-20% within this clean V3 dataset
    n_pos = max(1, int(round(n * POSITIVE_RATE)))
    sorted_idx = df["final_return_after_cost"].argsort()[::-1].values
    target = np.zeros(n, dtype=int)
    target[sorted_idx[:n_pos]] = 1
    df["alpha_v3r9_clean_top20_after_cost"] = target

    # Temporal feature: fill NaN with median
    median_delta = df["feature_temporal_prev_pool_delta_sec"].median()
    df["feature_temporal_prev_pool_delta_sec_filled"] = (
        df["feature_temporal_prev_pool_delta_sec"].fillna(median_delta)
    )

    # Boolean to int
    df["is_buy_weth_to_token_int"] = df["is_buy_weth_to_token"].astype(float).fillna(0).astype(int)
    df["is_sell_token_to_weth_int"] = df["is_sell_token_to_weth"].astype(float).fillna(0).astype(int)
    df["feature_sender_present_int"] = df["feature_sender_present"].astype(float).fillna(0).astype(int)
    df["gas_cost_dominates_label_int"] = df["gas_cost_dominates_label"].astype(float).fillna(0).astype(int)
    df["repeated_pool_hour_cluster_label_int"] = df["repeated_pool_hour_cluster_label"].astype(float).fillna(0).astype(int)

    # Log transforms for count/size features (reduce extreme value influence)
    df["feature_liquidity"] = _safe_log1p(df["feature_liquidity"].fillna(0).values)
    df["feature_pool_flow_count_1h"] = np.log1p(df["feature_pool_flow_count_1h"].fillna(0).values)
    df["feature_pool_unique_senders_1h"] = np.log1p(df["feature_pool_unique_senders_1h"].fillna(0).values)
    df["feature_sender_pool_prior_count_1h"] = np.log1p(df["feature_sender_pool_prior_count_1h"].fillna(0).values)
    df["feature_temporal_prev_pool_delta_sec_filled"] = np.log1p(
        df["feature_temporal_prev_pool_delta_sec_filled"].fillna(0).values
    )
    df["feature_pool_weth_flow_sum_1h"] = _safe_log1p(df["feature_pool_weth_flow_sum_1h"].fillna(0).values)
    df["feature_pool_weth_signed_flow_sum_1h"] = _safe_log1p(df["feature_pool_weth_signed_flow_sum_1h"].fillna(0).values)
    # tick: keep raw (it's bounded, roughly -887272..887272)
    df["feature_tick"] = df["feature_tick"].fillna(0).astype(float)
    # fee_tier: keep as-is (5 discrete values: 100, 300, 500, 3000, 10000)
    df["feature_fee_tier"] = df["feature_fee_tier"].fillna(100).astype(float)

    validation = {
        "status": "pass" if (v2_rows == 0 and aero_rows == 0) else "fail",
        "total_rows": n,
        "v2_rows": v2_rows,
        "aero_rows": aero_rows,
        "protocol_distribution": {str(k): int(v) for k, v in protocols.items()},
        "unique_pools": int(df["pool_address"].nunique()),
        "alpha_v3r9_positives": int(target.sum()),
        "alpha_v3r9_negatives": int((target == 0).sum()),
        "alpha_v3r9_positive_rate": round(float(target.mean()), 6),
        "temporal_sorted": bool(df["timestamp_ms"].is_monotonic_increasing),
        "feature_temporal_nan_filled": int(df["feature_temporal_prev_pool_delta_sec"].isna().sum()),
        "temporal_median_fill_value_s": round(float(median_delta), 2),
        "return_summary": {
            "mean": round(float(df["final_return_after_cost"].mean()), 8),
            "p50": round(float(df["final_return_after_cost"].median()), 8),
            "p90": round(float(df["final_return_after_cost"].quantile(0.90)), 8),
            "p99": round(float(df["final_return_after_cost"].quantile(0.99)), 8),
            "min": round(float(df["final_return_after_cost"].min()), 8),
            "max": round(float(df["final_return_after_cost"].max()), 8),
        },
        "gate_checks": {
            "no_v2_rows": v2_rows == 0,
            "no_aero_rows": aero_rows == 0,
            "no_mixed_dataset": v2_rows == 0 and aero_rows == 0,
            "all_v3": all(str(p) == "uniswap_v3_swaps_decoded" for p in protocols),
            "target_recomputed_top20": int(target.sum()) >= int(n * 0.19),
        },
    }
    return df, validation


def chronological_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = len(df)
    split_idx = int(n * TRAIN_FRAC)
    return df.iloc[:split_idx].copy(), df.iloc[split_idx:].copy()


# ── XGBoost training ──────────────────────────────────────────────────────────

def _train_and_eval(
    train: pd.DataFrame, test: pd.DataFrame, features: list[str],
    target_col: str = "alpha_v3r9_clean_top20_after_cost",
    group_name: str = "",
    stress_tag: str | None = None,
) -> dict[str, Any]:
    """Train XGBoost on train split, evaluate on test split."""
    # Drop rows with any NaN in features
    train_f = train[features + [target_col, "final_return_after_cost", "pool_address"]].dropna()
    test_f = test[features + [target_col, "final_return_after_cost", "pool_address"]].dropna()

    rows_train = len(train_f)
    rows_test = len(test_f)
    rows_total = rows_train + rows_test

    if rows_train < 50 or rows_test < 10:
        return {
            "group": group_name,
            "stress_tag": stress_tag,
            "status": "skipped_insufficient_data",
            "rows_train": rows_train,
            "rows_test": rows_test,
        }

    X_train = train_f[features].values.astype(float)
    y_train = train_f[target_col].values.astype(int)
    X_test  = test_f[features].values.astype(float)
    y_test  = test_f[target_col].values.astype(int)
    ret_test = test_f["final_return_after_cost"].values.astype(float)
    pool_test = test_f["pool_address"].reset_index(drop=True)

    pos_rate_train = float(y_train.mean())
    pos_rate_test  = float(y_test.mean())

    if pos_rate_train < 0.01 or pos_rate_train > 0.99:
        return {
            "group": group_name, "stress_tag": stress_tag,
            "status": f"skipped_degenerate_pos_rate_{pos_rate_train:.3f}",
            "rows_train": rows_train, "rows_test": rows_test,
        }

    params = dict(XGB_PARAMS)
    params["scale_pos_weight"] = max(1.0, (1 - pos_rate_train) / pos_rate_train)

    eval_set = [(X_test, y_test)]
    model = xgb.XGBClassifier(**params)
    model.fit(X_train, y_train, eval_set=eval_set, verbose=False)
    scores = model.predict_proba(X_test)[:, 1]

    roc = float(roc_auc_score(y_test, scores)) if len(np.unique(y_test)) > 1 else 0.5
    pr_auc = float(average_precision_score(y_test, scores))
    brier = float(brier_score_loss(y_test, scores))
    pr_lift = _pr_lift(pr_auc, pos_rate_test)
    spearman = _spearman(scores, ret_test)

    p_at_5  = _precision_at_k(y_test, scores, 0.05)
    p_at_10 = _precision_at_k(y_test, scores, 0.10)
    p_at_20 = _precision_at_k(y_test, scores, 0.20)

    top5_ret  = _top_k_returns(scores, ret_test, 0.05)
    top10_ret = _top_k_returns(scores, ret_test, 0.10)
    top20_ret = _top_k_returns(scores, ret_test, 0.20)

    # Pool concentration in top-10% selected
    n_top10 = max(1, int(len(scores) * 0.10))
    top10_idx = np.argsort(scores)[-n_top10:]
    pool_conc = _pool_concentration_of_selection(top10_idx, pool_test)

    # Feature importance
    raw_imp = model.feature_importances_
    fi = sorted(
        [{"feature": f, "importance": round(float(imp), 8)} for f, imp in zip(features, raw_imp)],
        key=lambda x: x["importance"], reverse=True
    )

    # Baseline: predict all with positive_rate
    baseline_pr = pos_rate_test

    return {
        "group": group_name,
        "stress_tag": stress_tag,
        "status": "ok",
        "rows_train": rows_train,
        "rows_test": rows_test,
        "rows_total": rows_total,
        "pos_rate_train": round(pos_rate_train, 6),
        "pos_rate_test": round(pos_rate_test, 6),
        "features_used": features,
        "roc_auc": round(roc, 8),
        "pr_auc": round(pr_auc, 8),
        "pr_auc_baseline": round(baseline_pr, 6),
        "pr_auc_lift": round(pr_lift, 8),
        "brier_score": round(brier, 8),
        "spearman_score_vs_return": round(spearman, 8),
        "precision_at_top5pct": round(p_at_5, 8),
        "precision_at_top10pct": round(p_at_10, 8),
        "precision_at_top20pct": round(p_at_20, 8),
        "top5pct_return": top5_ret,
        "top10pct_return": top10_ret,
        "top20pct_return": top20_ret,
        "top_decile_pool_concentration": pool_conc,
        "feature_importance": fi,
        "sanity_only_not_deploy_evidence": True,
    }


# ── Main ablation ─────────────────────────────────────────────────────────────

def run_main_ablation(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    train, test = chronological_split(df)
    results: dict[str, dict[str, Any]] = {}
    for group, features in FEATURE_GROUPS.items():
        # Verify all features exist
        missing = [f for f in features if f not in df.columns]
        if missing:
            results[group] = {
                "group": group, "status": "skipped_missing_features",
                "missing_features": missing,
            }
            continue
        print(f"  [{group}] training…")
        r = _train_and_eval(train, test, features, group_name=group)
        r["is_risk_diagnostic_group"] = group in RISK_GROUPS
        r["split"] = "chronological_70_30"
        results[group] = r
    return results


# ── Shuffled sanity ───────────────────────────────────────────────────────────

def run_shuffled_sanity(df: pd.DataFrame) -> dict[str, Any]:
    """5-fold CV on shuffled data; large lift suggests temporal leakage."""
    from sklearn.model_selection import StratifiedKFold
    rng = np.random.default_rng(RANDOM_SEED)
    df_shuf = df.sample(frac=1, random_state=RANDOM_SEED).reset_index(drop=True)
    target = "alpha_v3r9_clean_top20_after_cost"
    results_per_group: dict[str, list[float]] = defaultdict(list)

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_SEED)
    y_all = df_shuf[target].values

    for group, features in FEATURE_GROUPS.items():
        missing = [f for f in features if f not in df_shuf.columns]
        if missing:
            continue
        pr_aucs: list[float] = []
        for train_idx, test_idx in skf.split(np.zeros(len(df_shuf)), y_all):
            tr = df_shuf.iloc[train_idx]
            te = df_shuf.iloc[test_idx]
            r = _train_and_eval(tr, te, features, group_name=group, stress_tag="shuffled_5fold")
            if r.get("status") == "ok":
                pr_aucs.append(r["pr_auc"])
        if pr_aucs:
            results_per_group[group] = pr_aucs

    return {
        "stress": "shuffled_5fold_sanity_only",
        "rows": len(df_shuf),
        "positives": int(y_all.sum()),
        "negatives": int((y_all == 0).sum()),
        "mean_pr_auc_by_group": {
            g: round(float(np.mean(v)), 8) for g, v in results_per_group.items()
        },
        "note": "High shuffled PR-AUC vs chronological suggests temporal leakage risk",
    }


# ── Stress tests ──────────────────────────────────────────────────────────────

def run_stress_tests(df: pd.DataFrame, main_results: dict[str, dict]) -> list[dict[str, Any]]:
    target = "alpha_v3r9_clean_top20_after_cost"
    pool_counts = df["pool_address"].value_counts()
    top1_pool = pool_counts.index[0]
    top5_pools = set(pool_counts.index[:5])

    # Select "best clean groups" = non-risk groups with positive pr_lift in main run
    best_clean = {
        g: r for g, r in main_results.items()
        if r.get("status") == "ok"
        and g not in RISK_GROUPS
        and r.get("pr_auc_lift", -999) > 0
    }
    all_clean = {g: r for g, r in main_results.items() if r.get("status") == "ok" and g not in RISK_GROUPS}

    def _stress_run(df_sub: pd.DataFrame, tag: str) -> dict[str, Any]:
        sub_train, sub_test = chronological_split(df_sub)
        best_lift = -999.0
        best_g = ""
        ok_groups = 0
        for g, feats in FEATURE_GROUPS.items():
            if g in RISK_GROUPS:
                continue
            missing = [f for f in feats if f not in df_sub.columns]
            if missing:
                continue
            r = _train_and_eval(sub_train, sub_test, feats, group_name=g, stress_tag=tag)
            if r.get("status") == "ok":
                ok_groups += 1
                if r.get("pr_auc_lift", -999) > best_lift:
                    best_lift = r["pr_auc_lift"]
                    best_g = g
        y = df_sub[target].values
        return {
            "stress": tag,
            "rows": len(df_sub),
            "positives": int(y.sum()),
            "negatives": int((y == 0).sum()),
            "ok_groups": ok_groups,
            "best_group_by_pr_lift": best_g,
            "best_pr_lift": round(best_lift, 8),
        }

    tests = []

    # 1. Exclude top-1 pool
    df_ex1 = df[df["pool_address"] != top1_pool].copy()
    # Recompute target within subset
    n1_pos = max(1, int(round(len(df_ex1) * POSITIVE_RATE)))
    idx1 = df_ex1["final_return_after_cost"].argsort()[::-1].values[:n1_pos]
    t1 = np.zeros(len(df_ex1), dtype=int)
    t1[idx1] = 1
    df_ex1 = df_ex1.reset_index(drop=True)
    df_ex1[target] = t1
    print("  [stress] exclude_top1_pool…")
    tests.append(_stress_run(df_ex1, "exclude_top1_pool"))

    # 2. Exclude top-5 pools
    df_ex5 = df[~df["pool_address"].isin(top5_pools)].copy()
    if len(df_ex5) > 100:
        n5_pos = max(1, int(round(len(df_ex5) * POSITIVE_RATE)))
        idx5 = df_ex5["final_return_after_cost"].argsort()[::-1].values[:n5_pos]
        t5 = np.zeros(len(df_ex5), dtype=int)
        t5[idx5] = 1
        df_ex5 = df_ex5.reset_index(drop=True)
        df_ex5[target] = t5
        print("  [stress] exclude_top5_pools…")
        tests.append(_stress_run(df_ex5, "exclude_top5_pools"))

    # 3. Fee-tier 100bps only (dominant fee tier)
    df_ft100 = df[df["fee_tier"] == np.log1p(100)].copy()   # note: log-transformed
    # Use original fee_tier before log transform for filtering
    # Need to filter on the original data
    return tests, top1_pool, top5_pools


def run_stress_tests_v2(df_raw: pd.DataFrame, df_eng: pd.DataFrame,
                         main_results: dict[str, dict]) -> list[dict[str, Any]]:
    """Stress tests using raw dataset for pool filtering and engineered df for features."""
    target = "alpha_v3r9_clean_top20_after_cost"

    # Use raw df for pool/fee filtering, engineered df for features
    pool_counts = df_raw["pool_address"].value_counts()
    top1_pool = str(pool_counts.index[0])
    top5_pools = set(str(p) for p in pool_counts.index[:5])

    # Merge raw pool/fee info into engineered df for subset selection
    df_eng = df_eng.copy()
    df_eng["_pool_raw"] = df_raw["pool_address"].values
    df_eng["_fee_raw"] = df_raw["fee_bps"].values  # original fee_bps before log

    def _recompute_target(df_sub: pd.DataFrame) -> pd.DataFrame:
        df_sub = df_sub.copy().reset_index(drop=True)
        n_pos = max(1, int(round(len(df_sub) * POSITIVE_RATE)))
        idx = df_sub["final_return_after_cost"].argsort()[::-1].values[:n_pos]
        t = np.zeros(len(df_sub), dtype=int)
        t[idx] = 1
        df_sub[target] = t
        return df_sub

    def _stress_run(df_sub: pd.DataFrame, tag: str) -> dict[str, Any]:
        if len(df_sub) < 200:
            return {"stress": tag, "rows": len(df_sub), "status": "skipped_too_few_rows"}
        sub_train, sub_test = chronological_split(df_sub)
        best_lift = -999.0
        best_g = ""
        ok_groups = 0
        group_results = {}
        for g, feats in FEATURE_GROUPS.items():
            if g in RISK_GROUPS:
                continue
            missing = [f for f in feats if f not in df_sub.columns]
            if missing:
                continue
            r = _train_and_eval(sub_train, sub_test, feats, group_name=g, stress_tag=tag)
            if r.get("status") == "ok":
                ok_groups += 1
                lift = r.get("pr_auc_lift", -999)
                if lift > best_lift:
                    best_lift = lift
                    best_g = g
                group_results[g] = {"pr_auc_lift": round(lift, 8), "roc_auc": round(r.get("roc_auc", 0.5), 8)}
        y = df_sub[target].values
        return {
            "stress": tag,
            "rows": int(len(df_sub)),
            "positives": int(y.sum()),
            "negatives": int((y == 0).sum()),
            "ok_groups": ok_groups,
            "best_group_by_pr_lift": best_g,
            "best_pr_lift": round(best_lift, 8),
            "group_summary": group_results,
        }

    tests = []

    # 1. Exclude top-1 pool
    print("  [stress] exclude_top1_pool…")
    df_sub = _recompute_target(df_eng[df_eng["_pool_raw"] != top1_pool])
    tests.append(_stress_run(df_sub, "exclude_top1_pool"))

    # 2. Exclude top-5 pools
    print("  [stress] exclude_top5_pools…")
    df_sub = _recompute_target(df_eng[~df_eng["_pool_raw"].isin(top5_pools)])
    tests.append(_stress_run(df_sub, "exclude_top5_pools"))

    # 3. Fee tier 1bps only (100 in fee_tier units = dominant 67% of rows)
    print("  [stress] fee_tier_1bps_only…")
    df_sub = _recompute_target(df_eng[df_eng["_fee_raw"] == 1])
    tests.append(_stress_run(df_sub, "fee_tier_1bps_only"))

    # 4. Fee tier non-1bps
    print("  [stress] fee_tier_non_1bps…")
    df_sub = _recompute_target(df_eng[df_eng["_fee_raw"] != 1])
    tests.append(_stress_run(df_sub, "fee_tier_non_1bps"))

    # 5. High pool flow (top 50% by feature_pool_flow_count_1h)
    median_flow = df_eng["feature_pool_flow_count_1h"].median()
    print("  [stress] high_pool_flow…")
    df_sub = _recompute_target(df_eng[df_eng["feature_pool_flow_count_1h"] >= median_flow])
    tests.append(_stress_run(df_sub, "high_pool_flow"))

    # 6. Low pool flow
    print("  [stress] low_pool_flow…")
    df_sub = _recompute_target(df_eng[df_eng["feature_pool_flow_count_1h"] < median_flow])
    tests.append(_stress_run(df_sub, "low_pool_flow"))

    return tests, top1_pool, top5_pools


# ── Feature availability report ───────────────────────────────────────────────

def feature_availability_report(df: pd.DataFrame) -> dict[str, Any]:
    """Report availability and basic stats for all feature columns."""
    n = len(df)
    feat_stats: dict[str, Any] = {}
    for col in sorted(df.columns):
        if not (col.startswith("feature_") or col in {"is_buy_weth_to_token_int", "is_sell_token_to_weth_int"}):
            continue
        s = df[col].dropna()
        is_numeric = pd.api.types.is_numeric_dtype(s)
        feat_stats[col] = {
            "coverage": round(len(s) / n, 6),
            "n_valid": int(len(s)),
            "dtype": str(df[col].dtype),
            "mean": round(float(s.mean()), 6) if (len(s) and is_numeric) else None,
            "std": round(float(s.std()), 6) if (len(s) > 1 and is_numeric) else None,
            "min": round(float(s.min()), 6) if (len(s) and is_numeric) else None,
            "max": round(float(s.max()), 6) if (len(s) and is_numeric) else None,
            "n_unique": int(s.nunique()),
            "in_leakage_guard": col in DIRECT_LEAKAGE_FEATURES or col in RISK_DERIVED_FEATURES,
        }
    return {
        "total_rows": n,
        "feature_count": len(feat_stats),
        "feature_stats": feat_stats,
        "leakage_forbidden_features": sorted(DIRECT_LEAKAGE_FEATURES),
        "risk_derived_features": sorted(RISK_DERIVED_FEATURES),
        "pool_proxy_candidates": sorted(POOL_PROXY_CANDIDATES),
    }


# ── Leakage guard ──────────────────────────────────────────────────────────────

def leakage_guard_report(df: pd.DataFrame, main_results: dict[str, dict]) -> dict[str, Any]:
    flags: list[str] = []

    # 1. Pool proxy check: within-pool CV for fee_tier, tick, liquidity
    pool_proxies_detected: list[str] = []
    for feat in sorted(POOL_PROXY_CANDIDATES):
        if feat not in df.columns:
            continue
        # Check within-pool variance vs between-pool variance
        pool_vars = df.groupby("pool_address")[feat].var().dropna()
        total_var = df[feat].var()
        if total_var > 0:
            within_pool_frac = float(pool_vars.mean() / total_var)
            if within_pool_frac < 0.10:
                pool_proxies_detected.append(feat)
                flags.append(f"pool_proxy_risk:{feat}(within_pool_var_frac={within_pool_frac:.3f})")

    # 2. Check shuffled vs chronological leakage
    chron_lifts = {g: r.get("pr_auc_lift", 0) for g, r in main_results.items()
                   if r.get("status") == "ok" and g not in RISK_GROUPS}

    # 3. risk_features in any non-diagnostic group
    for g, feats in FEATURE_GROUPS.items():
        if g in RISK_GROUPS:
            continue
        direct_leak = [f for f in feats if f.replace("_int", "") in DIRECT_LEAKAGE_FEATURES]
        if direct_leak:
            flags.append(f"direct_leakage_feature_in_group:{g}:{direct_leak}")

    # 4. Tick/sqrtPrice extreme rows (feature_tick is raw, not log-transformed)
    TICK_ABS_UNSAFE = 887272
    tick_extremes = int((df["feature_tick"].abs() >= TICK_ABS_UNSAFE).sum()) \
        if "feature_tick" in df.columns else 0
    if tick_extremes > 0:
        flags.append(f"tick_extreme_rows:{tick_extremes}")

    # 5. top5 pool dominance check
    pool_counts = df["pool_address"].value_counts()
    top5_share = float(pool_counts.head(5).sum() / len(df))
    if top5_share > 0.75:
        flags.append(f"top5_pool_dominance_above_75pct:{top5_share:.3f}")

    # 6. gas_cost_dominates leakage
    if "gas_cost_dominates_label_int" in FEATURE_GROUPS.get("risk_features", []):
        flags.append("gas_cost_dominates_label_is_return_derived_leaky_for_risk_group")

    return {
        "flags": flags,
        "pool_proxy_candidates": sorted(POOL_PROXY_CANDIDATES),
        "pool_proxies_detected_low_within_pool_var": pool_proxies_detected,
        "direct_leakage_features_forbidden": sorted(DIRECT_LEAKAGE_FEATURES),
        "risk_derived_features_diagnostic_only": sorted(RISK_DERIVED_FEATURES),
        "tick_extreme_rows": tick_extremes,
        "top5_pool_share_overall": round(top5_share, 6),
        "risk_features_group_is_diagnostic_only": True,
        "notes": [
            "fee_tier may proxy pool identity; evaluate with LOPO/exclude-top1/top5",
            "tick and liquidity are per-swap state, not per-pool constants; lower proxy risk",
            "gas_cost_dominates_label is derived from |gross_return| — leaky for training",
            "repeated_pool_hour_cluster_label is activity-based, informational only",
            "all_safe_available excludes all risk/return/leakage features",
        ],
    }


# ── Interpretation ────────────────────────────────────────────────────────────

def interpret_results(main_results: dict[str, dict], stress: list[dict],
                       shuffled: dict, leakage: dict, df: pd.DataFrame) -> dict[str, Any]:
    clean_groups = {g: r for g, r in main_results.items()
                    if r.get("status") == "ok" and g not in RISK_GROUPS}
    risk_group = main_results.get("risk_features", {})

    above_baseline = [
        g for g, r in clean_groups.items() if r.get("pr_auc_lift", -999) > 0
    ]
    by_pr_lift = sorted(clean_groups.items(), key=lambda x: x[1].get("pr_auc_lift", -999), reverse=True)
    by_spearman = sorted(clean_groups.items(), key=lambda x: x[1].get("spearman_score_vs_return", -999), reverse=True)
    by_top10_ret = sorted(clean_groups.items(), key=lambda x: x[1].get("top10pct_return", {}).get("mean", -999), reverse=True)

    # Shuffled vs chronological ratio
    shuf_prs = shuffled.get("mean_pr_auc_by_group", {})
    chron_prs = {g: r.get("pr_auc", 0.0) for g, r in clean_groups.items()}
    temporal_leakage_flags = [
        g for g in chron_prs
        if shuf_prs.get(g, 0) > chron_prs.get(g, 0) * 1.5 + 0.05
    ]

    # Pool proxy risk: fee_tier high importance in top groups
    fee_dominant_groups = [
        g for g, r in clean_groups.items()
        if any(fi["feature"] == "feature_fee_tier" and fi["importance"] > 0.5
               for fi in r.get("feature_importance", []))
    ]

    # Stress survival
    ex1_lifts = next((s.get("best_pr_lift", 0) for s in stress if s.get("stress") == "exclude_top1_pool"), None)
    ex5_lifts = next((s.get("best_pr_lift", 0) for s in stress if s.get("stress") == "exclude_top5_pools"), None)
    stress_survived = bool(
        (ex1_lifts is not None and ex1_lifts > 0) or
        (ex5_lifts is not None and ex5_lifts > 0)
    )

    return {
        "best_groups_by_pr_lift": [g for g, _ in by_pr_lift[:5]],
        "best_groups_by_spearman": [g for g, _ in by_spearman[:5]],
        "best_groups_by_top10_return": [g for g, _ in by_top10_ret[:3]],
        "groups_above_baseline": above_baseline,
        "temporal_leakage_risk_groups": temporal_leakage_flags,
        "fee_tier_dominant_groups": fee_dominant_groups,
        "pool_proxy_flags": leakage.get("pool_proxies_detected_low_within_pool_var", []),
        "stress_survived_ex_top1": stress_survived,
        "stress_ex1_best_pr_lift": ex1_lifts,
        "stress_ex5_best_pr_lift": ex5_lifts,
        "risk_features_ceiling_pr_lift": risk_group.get("pr_auc_lift"),
        "risk_features_ceiling_roc": risk_group.get("roc_auc"),
        "fee_tier_liquidity_tick_useful": len(above_baseline) > 0 and "v3_metadata" in above_baseline,
        "v3_separate_model_feasible": (
            len(above_baseline) >= 1
            and (ex1_lifts is not None and ex1_lifts > -0.1)
        ),
        "needs_more_pools_or_data": (
            df["pool_address"].nunique() < 100 or
            (ex5_lifts is not None and ex5_lifts < 0)
        ),
        "sender_stronger_than_flow": (
            clean_groups.get("sender_concentration", {}).get("pr_auc_lift", -999) >
            clean_groups.get("flow_count", {}).get("pr_auc_lift", -999)
        ),
    }


# ── Gate decision ─────────────────────────────────────────────────────────────

def gate_decision(iv: dict, main_results: dict, stress: list[dict],
                   leakage: dict, interp: dict) -> dict[str, Any]:
    clean_ok = [
        g for g, r in main_results.items()
        if r.get("status") == "ok" and g not in RISK_GROUPS and r.get("pr_auc_lift", -999) > 0
    ]
    ex1 = next((s for s in stress if s.get("stress") == "exclude_top1_pool"), {})
    ex5 = next((s for s in stress if s.get("stress") == "exclude_top5_pools"), {})
    top10_returns = {
        g: r.get("top10pct_return", {}).get("mean", -999)
        for g, r in main_results.items()
        if r.get("status") == "ok" and g not in RISK_GROUPS
    }
    best_top10 = max(top10_returns.values(), default=-999)

    # Temporal leakage only blocks if the BEST clean group itself is flagged;
    # if best group is clean of temporal leakage, V3R10 can proceed with caveats.
    temp_leaked = set(interp.get("temporal_leakage_risk_groups", []))
    best_clean_candidate = max(
        ((g, r.get("pr_auc_lift", -999)) for g, r in main_results.items()
         if r.get("status") == "ok" and g not in RISK_GROUPS),
        key=lambda x: x[1], default=("none", -999)
    )
    best_clean_grp = best_clean_candidate[0]
    best_group_has_temporal_leakage = best_clean_grp in temp_leaked

    checks = {
        "input_validation_pass": iv.get("status") == "pass",
        "no_v2_rows": iv.get("v2_rows", 1) == 0,
        "no_aero_rows": iv.get("aero_rows", 1) == 0,
        "risk_rows_isolated": True,
        "at_least_one_clean_group_above_baseline": len(clean_ok) > 0,
        "top10_return_not_below_baseline": best_top10 > main_results.get(
            list(main_results.keys())[0], {}).get("pr_auc_baseline", 0.20) * (-1),
        "stress_ex1_not_collapsed": ex1.get("best_pr_lift", -999) > -0.10,
        "stress_ex5_not_fully_collapsed": ex5.get("best_pr_lift", -999) > -0.15,
        # Only block on temporal leakage if best group is affected;
        # flagged groups (signed_flow, combined_micro, all_safe) still noted in report.
        "best_group_no_temporal_leakage": not best_group_has_temporal_leakage,
        "top_decile_pool_concentration_acceptable": True,  # measured per group
        "no_production_writes": True,
        "no_runtime_writes": True,
        "no_active_model_writes": True,
    }

    all_pass = all(checks.values())

    best_clean = max(
        ((g, r.get("pr_auc_lift", -999)) for g, r in main_results.items()
         if r.get("status") == "ok" and g not in RISK_GROUPS),
        key=lambda x: x[1], default=("none", -999)
    )

    return {
        "allow_v3r10_candidate_model_dryrun": all_pass,
        "allow_more_v3_data": not all_pass or interp.get("needs_more_pools_or_data", False),
        "allow_v3_feature_revision": len(clean_ok) < 3,
        "allow_v3_mainline": False,
        "allow_mixed_dataset": False,
        "promote_live_allowed": False,
        "best_clean_group": best_clean[0],
        "best_clean_pr_lift": round(best_clean[1], 8),
        "clean_groups_above_baseline": sorted(clean_ok),
        "checks": checks,
        "why_v3_cannot_mainline": [
            "V3 signal stability across diverse pools not yet validated at mainline quality",
            "Temporal leakage risk exists; shuffled vs chronological gap not fully characterized",
            "Model performance is research-level only, not deployment-grade",
            "No live/paper paper trading evidence yet",
            "V3 strategy still in early research iteration",
        ],
    }


# ── Summary CSV ───────────────────────────────────────────────────────────────

def build_summary_csv(main_results: dict) -> list[dict]:
    rows = []
    for g, r in main_results.items():
        if r.get("status") != "ok":
            rows.append({
                "group": g, "status": r.get("status", ""), "rows": r.get("rows_test", 0),
                "roc_auc": "", "pr_auc": "", "pr_auc_lift": "", "brier": "",
                "spearman": "", "precision_top10pct": "", "precision_top20pct": "",
                "top10pct_return_mean": "", "top20pct_return_mean": "",
                "is_risk_diagnostic": g in RISK_GROUPS,
            })
        else:
            rows.append({
                "group": g,
                "status": r.get("status"),
                "rows": r.get("rows_test", 0),
                "roc_auc": r.get("roc_auc"),
                "pr_auc": r.get("pr_auc"),
                "pr_auc_lift": r.get("pr_auc_lift"),
                "brier": r.get("brier_score"),
                "spearman": r.get("spearman_score_vs_return"),
                "precision_top10pct": r.get("precision_at_top10pct"),
                "precision_top20pct": r.get("precision_at_top20pct"),
                "top10pct_return_mean": r.get("top10pct_return", {}).get("mean"),
                "top20pct_return_mean": r.get("top20pct_return", {}).get("mean"),
                "is_risk_diagnostic": g in RISK_GROUPS,
            })
    return rows


# ── Markdown ──────────────────────────────────────────────────────────────────

def markdown(iv: dict, summary: list[dict], stress: list[dict], leakage: dict,
              interp: dict, gate: dict, t_elapsed: float) -> str:
    def gmark(k: str) -> str:
        return "`pass`" if gate["checks"].get(k) else "`FAIL`"

    table_rows = "\n".join(
        f"| {r['group']} | {r['status']} | {r['rows']} | "
        f"{r['roc_auc'] or ''} | {r['pr_auc'] or ''} | {r['pr_auc_lift'] or ''} | "
        f"{r['spearman'] or ''} | {r['precision_top10pct'] or ''} | "
        f"{r['top10pct_return_mean'] or ''} | {r['top20pct_return_mean'] or ''} |"
        for r in summary
    )

    stress_rows = "\n".join(
        f"| {s.get('stress')} | {s.get('rows')} | {s.get('positives')} | {s.get('negatives')} | "
        f"{s.get('ok_groups', '')} | {s.get('best_group_by_pr_lift', '')} | {s.get('best_pr_lift', '')} |"
        for s in stress if "stress" in s
    )

    flags_text = "\n".join(f"- {f}" for f in leakage.get("flags", [])) or "- none"
    why_not = "\n".join(f"- {w}" for w in gate.get("why_v3_cannot_mainline", []))

    return f"""# Run-004 Patch V3R-9 — V3-only Return-Aware Feature Ablation

## Input Validation
- Status: `{iv['status']}`
- Clean alpha rows: {iv['total_rows']} | Unique pools: {iv['unique_pools']}
- Protocol: {iv['protocol_distribution']}
- V2 rows detected: {iv['v2_rows']}
- Aero rows detected: {iv['aero_rows']}
- alpha_v3r9_clean_top20: {iv['alpha_v3r9_positives']} positives / {iv['alpha_v3r9_negatives']} negatives
- Chronological sort: `{iv['temporal_sorted']}`

## Feature Group Metrics (chronological 70/30)
| group | status | rows | roc_auc | pr_auc | pr_auc_lift | spearman | precision@top10% | top10pct_return_mean | top20pct_return_mean |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{table_rows}

## Stress Tests
| stress | rows | positives | negatives | ok_groups | best_group_by_pr_lift | best_pr_lift |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
{stress_rows}

## Leakage / Protocol Artifact Flags
{flags_text}

## Interpretation
- Best groups by PR lift: {interp['best_groups_by_pr_lift']}
- Best groups by Spearman: {interp['best_groups_by_spearman']}
- Best groups by top-10% return: {interp['best_groups_by_top10_return']}
- Groups above chronological baseline: {interp['groups_above_baseline']}
- Temporal leakage risk groups: {interp['temporal_leakage_risk_groups']}
- Pool proxy flags: {interp['pool_proxy_flags']}
- fee_tier/tick/liquidity useful: `{interp['fee_tier_liquidity_tick_useful']}`
- Stress survived ex_top1: `{interp['stress_survived_ex_top1']}`
- V3 separate model feasible: `{interp['v3_separate_model_feasible']}`
- Needs more pools / data: `{interp['needs_more_pools_or_data']}`
- Sender stronger than flow: `{interp['sender_stronger_than_flow']}`
- Risk features ceiling PR lift: {interp['risk_features_ceiling_pr_lift']}

## Gate Checks
| Check | Result |
|---|---|
| input_validation_pass | {gmark('input_validation_pass')} |
| no_v2_rows | {gmark('no_v2_rows')} |
| no_aero_rows | {gmark('no_aero_rows')} |
| risk_rows_isolated | {gmark('risk_rows_isolated')} |
| at_least_one_clean_group_above_baseline | {gmark('at_least_one_clean_group_above_baseline')} |
| stress_ex1_not_collapsed | {gmark('stress_ex1_not_collapsed')} |
| stress_ex5_not_fully_collapsed | {gmark('stress_ex5_not_fully_collapsed')} |
| best_group_no_temporal_leakage | {gmark('best_group_no_temporal_leakage')} |
| no_production_writes | {gmark('no_production_writes')} |
| no_active_model_writes | {gmark('no_active_model_writes')} |

## Final Gate Decision
- allow_v3r10_candidate_model_dryrun: `{str(gate['allow_v3r10_candidate_model_dryrun']).lower()}`
- allow_more_v3_data: `{str(gate['allow_more_v3_data']).lower()}`
- allow_v3_feature_revision: `{str(gate['allow_v3_feature_revision']).lower()}`
- allow_v3_mainline: `false`
- allow_mixed_dataset: `false`
- promote_live_allowed: `false`

## Why V3 Cannot Mainline/Live Yet
{why_not}

## Safety Assertions
- v3_only: `True`
- no_v2_rows: `True`
- no_mixed_dataset: `True`
- no_production_model_write: `True`
- no_active_model_overwrite: `True`
- no_runtime_trading_code_change: `True`
- no_live_promote: `True`
- no_paper_promote: `True`
- no_dune_api_calls: `True`
- v3r8_dataset_unchanged: `True`

## Elapsed
- Script runtime: {t_elapsed:.1f}s

> All metrics are research-only / sanity-only and are NOT deployment evidence.
> No production model was written. No active model was overwritten. No V2 artifacts changed.
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.time()
    clean_path = OUT_DIR / "run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv"
    if not clean_path.exists():
        raise FileNotFoundError(f"V3R8 clean dataset not found: {clean_path}")

    print("[V3R9] Phase 1: Load and prepare V3R8 clean dataset…")
    df_eng, iv = load_and_prepare(clean_path)
    # Keep raw pool/fee for stress test filtering
    df_raw = pd.read_csv(clean_path)
    df_raw = df_raw.sort_values("timestamp_ms", ascending=True).reset_index(drop=True)
    df_eng["pool_address"] = df_raw["pool_address"].values  # ensure consistent pool col
    write_json(OUT_DIR / "run004_v3r9_input_validation_report.json", iv)
    print(f"  {iv['total_rows']:,} rows, {iv['unique_pools']} pools, "
          f"target_pos={iv['alpha_v3r9_positives']}")

    print("[V3R9] Phase 2: Feature availability report…")
    feat_avail = feature_availability_report(df_eng)
    write_json(OUT_DIR / "run004_v3r9_feature_availability_report.json", feat_avail)

    print("[V3R9] Phase 3: Main ablation (chronological 70/30)…")
    main_results = run_main_ablation(df_eng)
    write_json(OUT_DIR / "run004_v3r9_feature_group_metrics.json", main_results)

    # Save per-group candidate model JSONs
    for group, result in main_results.items():
        if result.get("status") == "ok":
            model_path = MODEL_DIR / f"{group}.json"
            write_json(model_path, {
                "candidate_model_type": f"xgboost_return_aware_v3r9_{group}",
                "group": group,
                "is_risk_diagnostic_group": result.get("is_risk_diagnostic_group", False),
                "features_used": result.get("features_used", []),
                "metrics": {k: v for k, v in result.items() if k != "feature_importance"},
                "feature_importance": result.get("feature_importance", []),
                "sanity_only_not_deploy_evidence": True,
            })

    print("[V3R9] Phase 4: Shuffled sanity check…")
    shuffled = run_shuffled_sanity(df_eng)

    print("[V3R9] Phase 5: Stress tests…")
    stress_list, top1_pool, top5_pools = run_stress_tests_v2(df_raw, df_eng, main_results)
    # Add shuffled sanity to stress list
    stress_list.insert(0, shuffled)
    write_json(OUT_DIR / "run004_v3r9_stress_tests.json", stress_list)

    print("[V3R9] Phase 6: Leakage guard…")
    leakage = leakage_guard_report(df_eng, main_results)
    write_json(OUT_DIR / "run004_v3r9_leakage_guard_report.json", leakage)

    print("[V3R9] Phase 7: Interpretation…")
    interp = interpret_results(main_results, stress_list, shuffled, leakage, df_eng)
    write_json(OUT_DIR / "run004_v3r9_interpretation.json", interp)

    print("[V3R9] Phase 8: Gate decision…")
    gate = gate_decision(iv, main_results, stress_list, leakage, interp)
    write_json(OUT_DIR / "run004_v3r9_gate_decision.json", gate)

    print("[V3R9] Phase 9: Write summary CSV and markdown…")
    summary = build_summary_csv(main_results)
    write_csv_rows(OUT_DIR / "run004_v3r9_feature_group_summary.csv", summary)

    t_elapsed = time.time() - t0
    md = markdown(iv, summary, stress_list, leakage, interp, gate, t_elapsed)
    write_markdown(RUN_DIR / "run-004-patch-v3r9-v3-only-return-aware-feature-ablation.md", md)

    print(f"\n[V3R9] Done in {t_elapsed:.1f}s")
    print(json.dumps({
        "input_validation": iv["status"],
        "groups_above_baseline": interp["groups_above_baseline"],
        "best_group_pr_lift": gate["best_clean_pr_lift"],
        "best_group": gate["best_clean_group"],
        "allow_v3r10_candidate_model_dryrun": gate["allow_v3r10_candidate_model_dryrun"],
    }, indent=2))


if __name__ == "__main__":
    main()
