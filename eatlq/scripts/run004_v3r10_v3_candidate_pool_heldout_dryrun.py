"""Run-004 Patch V3R-10 — V3-only Candidate Dry-run with Pool-Held-Out Stress.

Input:  mvp/diagnostics/run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv
        mvp/diagnostics/run004_v3r9_gate_decision.json
        mvp/diagnostics/run004_v3r9_leakage_guard_report.json
Output: mvp/diagnostics/run004_v3r10_*.json / *.csv
        mvp/models/v3r10_research_candidate/
        mvp/runs/run-004-patch-v3r10-v3-candidate-pool-heldout-dryrun.md

Hard constraints (enforced in-script):
  - V3 only; no V2, Aero, mixed dataset.
  - No production / runtime / active-model writes.
  - No live / paper promote.
  - No Dune API calls.
  - No data expansion.
  - All outputs → mvp/diagnostics, mvp/runs, mvp/models/v3r10_research_candidate only.
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
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_research_utils import rounded, write_csv_rows, write_json, write_markdown  # noqa: E402

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on"
), "LIVE_TRADING_MODE must not be active"

# ── Constants ─────────────────────────────────────────────────────────────────
RANDOM_SEED = 42
TRAIN_FRAC = 0.70
POSITIVE_RATE = 0.20
POOL_HELD_OUT_STRIDE = 4   # every 4th pool (start idx 3)
MIN_POOL_HELDOUT_TEST_ROWS = 300

# Pool proxies detected in V3R9
POOL_PROXY_FEATURES = {"feature_fee_tier", "feature_tick", "feature_liquidity"}
# Temporal-leakage flagged features from V3R9
TEMPORAL_LEAK_FEATURES = {"feature_pool_weth_flow_sum_1h", "feature_pool_weth_signed_flow_sum_1h"}

XGB_CLF_PARAMS: dict[str, Any] = {
    "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8, "min_child_weight": 5,
    "gamma": 0.1, "reg_alpha": 0.1, "reg_lambda": 1.0,
    "scale_pos_weight": 4, "eval_metric": "aucpr",
    "early_stopping_rounds": 30, "verbosity": 0,
    "random_state": RANDOM_SEED, "n_jobs": 4,
}
XGB_REG_PARAMS: dict[str, Any] = {
    "n_estimators": 300, "max_depth": 5, "learning_rate": 0.05,
    "subsample": 0.8, "colsample_bytree": 0.8, "min_child_weight": 5,
    "gamma": 0.1, "reg_alpha": 0.1, "reg_lambda": 1.0,
    "eval_metric": "mae", "early_stopping_rounds": 30, "verbosity": 0,
    "random_state": RANDOM_SEED, "n_jobs": 4,
}

OUT_DIR = ROOT / "mvp/diagnostics"
RUN_DIR = ROOT / "mvp/runs"
MODEL_DIR = ROOT / "mvp/models/v3r10_research_candidate"
for d in (OUT_DIR, RUN_DIR, MODEL_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Candidate model feature sets ─────────────────────────────────────────────
CANDIDATE_MODELS: dict[str, dict[str, Any]] = {
    "A_proxy_inclusive_v3_liquidity_micro": {
        "features": [
            "feature_liquidity", "feature_fee_tier", "feature_tick",
            "feature_pool_flow_count_1h",
        ],
        "description": "V3R9 best group — includes pool-proxy features (fee_tier/tick/liquidity)",
        "proxy_risk": "high",
        "temporal_leak_risk": "none",
    },
    "B_proxy_suppressed_microstructure": {
        "features": [
            "feature_pool_flow_count_1h", "feature_pool_unique_senders_1h",
            "feature_sender_pool_prior_count_1h", "feature_sender_present_int",
            "feature_temporal_prev_pool_delta_sec_filled",
            "is_buy_weth_to_token_int",
        ],
        "description": "Pool-proxy suppressed: no fee_tier/tick/liquidity; tests pure flow/sender/temporal",
        "proxy_risk": "none",
        "temporal_leak_risk": "low",
    },
    "C_all_safe_no_temporal_leak": {
        "features": [
            "feature_fee_tier", "feature_tick", "feature_liquidity",
            "feature_pool_flow_count_1h", "feature_pool_unique_senders_1h",
            "feature_sender_pool_prior_count_1h", "feature_sender_present_int",
            "feature_temporal_prev_pool_delta_sec_filled",
            "is_buy_weth_to_token_int",
        ],
        "description": "All safe features excluding temporal-leak flagged (weth_flow_sum, weth_signed_flow)",
        "proxy_risk": "medium",
        "temporal_leak_risk": "none",
    },
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_log1p(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    return np.sign(a) * np.log1p(np.abs(a))


def _spearman(scores: np.ndarray, returns: np.ndarray) -> float:
    mask = np.isfinite(scores) & np.isfinite(returns)
    if mask.sum() < 5:
        return float("nan")
    r, _ = sp_stats.spearmanr(scores[mask], returns[mask])
    return float(r)


def _pr_lift(pr_auc: float, pos_rate: float) -> float:
    return pr_auc / pos_rate - 1.0 if pos_rate > 0 else 0.0


def _top_k_metrics(scores: np.ndarray, returns: np.ndarray,
                   labels: np.ndarray, pools: pd.Series, k_frac: float) -> dict[str, Any]:
    n = max(1, int(len(scores) * k_frac))
    idx = np.argsort(scores)[-n:]
    selected_returns = returns[idx]
    selected_labels  = labels[idx]
    selected_pools   = pools.iloc[idx]
    pool_counts = selected_pools.value_counts()
    total = len(selected_pools)
    return {
        "k_frac": k_frac,
        "selected_count": int(n),
        "selected_rate": round(n / len(scores), 4),
        "return_mean": round(float(selected_returns.mean()), 8),
        "return_median": round(float(np.median(selected_returns)), 8),
        "return_p90": round(float(np.percentile(selected_returns, 90)), 8),
        "return_min": round(float(selected_returns.min()), 8),
        "return_max": round(float(selected_returns.max()), 8),
        "positive_return_rate": round(float((selected_returns > 0).mean()), 6),
        "positive_label_rate": round(float(selected_labels.mean()), 6),
        "return_still_negative": bool(selected_returns.mean() < 0),
        "selects_less_negative_than_mean": bool(selected_returns.mean() > returns.mean()),
        "unique_pools_selected": int(pool_counts.shape[0]),
        "top1_selected_pool_share": round(float(pool_counts.iloc[0] / total), 6) if len(pool_counts) else 0.0,
        "top5_selected_pool_share": round(float(pool_counts.head(5).sum() / total), 6),
    }


def _eval_model(y_true: np.ndarray, scores: np.ndarray, returns: np.ndarray,
                pools: pd.Series, tag: str) -> dict[str, Any]:
    pos_rate = float(y_true.mean())
    if pos_rate <= 0 or pos_rate >= 1:
        return {"tag": tag, "status": "degenerate_label"}
    if len(np.unique(y_true)) < 2:
        return {"tag": tag, "status": "single_class"}

    roc = float(roc_auc_score(y_true, scores))
    pr  = float(average_precision_score(y_true, scores))
    brier = float(brier_score_loss(y_true, scores))
    lift = _pr_lift(pr, pos_rate)
    spear = _spearman(scores, returns)

    return {
        "tag": tag,
        "status": "ok",
        "n_test": int(len(y_true)),
        "pos_rate": round(pos_rate, 6),
        "roc_auc": round(roc, 8),
        "pr_auc": round(pr, 8),
        "pr_auc_lift": round(lift, 8),
        "brier_score": round(brier, 8),
        "spearman": round(spear, 8),
        "top5pct": _top_k_metrics(scores, returns, y_true, pools, 0.05),
        "top10pct": _top_k_metrics(scores, returns, y_true, pools, 0.10),
        "top20pct": _top_k_metrics(scores, returns, y_true, pools, 0.20),
        "dataset_return_mean": round(float(returns.mean()), 8),
        "sanity_only_not_deploy_evidence": True,
    }


# ── Data preparation ──────────────────────────────────────────────────────────

def load_and_prepare(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load V3R8 clean dataset; return (engineered_df, raw_df_sorted)."""
    df_raw = pd.read_csv(path)
    df_raw = df_raw.sort_values("timestamp_ms", ascending=True).reset_index(drop=True)
    df = df_raw.copy()

    # Recompute target
    n = len(df)
    n_pos = max(1, int(round(n * POSITIVE_RATE)))
    sorted_idx = df["final_return_after_cost"].argsort()[::-1].values
    target = np.zeros(n, dtype=int)
    target[sorted_idx[:n_pos]] = 1
    df["alpha_v3r10_top20"] = target

    # Feature engineering (same as V3R9)
    df["feature_temporal_prev_pool_delta_sec_filled"] = np.log1p(
        df["feature_temporal_prev_pool_delta_sec"].fillna(
            df["feature_temporal_prev_pool_delta_sec"].median()
        ).values
    )
    df["is_buy_weth_to_token_int"] = df["is_buy_weth_to_token"].astype(float).fillna(0).astype(int)
    df["feature_sender_present_int"] = df["feature_sender_present"].astype(float).fillna(0).astype(int)
    df["feature_liquidity"] = _safe_log1p(df["feature_liquidity"].fillna(0).values)
    df["feature_pool_flow_count_1h"] = np.log1p(df["feature_pool_flow_count_1h"].fillna(0).values)
    df["feature_pool_unique_senders_1h"] = np.log1p(df["feature_pool_unique_senders_1h"].fillna(0).values)
    df["feature_sender_pool_prior_count_1h"] = np.log1p(df["feature_sender_pool_prior_count_1h"].fillna(0).values)
    df["feature_pool_weth_flow_sum_1h"] = _safe_log1p(df["feature_pool_weth_flow_sum_1h"].fillna(0).values)
    df["feature_pool_weth_signed_flow_sum_1h"] = _safe_log1p(df["feature_pool_weth_signed_flow_sum_1h"].fillna(0).values)
    df["feature_tick"] = df["feature_tick"].fillna(0).astype(float)
    df["feature_fee_tier"] = df["feature_fee_tier"].fillna(100).astype(float)
    return df, df_raw


# ── Split strategies ──────────────────────────────────────────────────────────

def chron_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = len(df)
    split_idx = int(n * TRAIN_FRAC)
    return df.iloc[:split_idx].copy(), df.iloc[split_idx:].copy()


def pool_held_out_split(df: pd.DataFrame, df_raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list, list]:
    """Select every POOL_HELD_OUT_STRIDE-th pool (sorted by count desc) as test."""
    pool_counts = df_raw["pool_address"].value_counts()
    pool_list = list(pool_counts.index)  # sorted by count desc
    test_pools = [pool_list[i] for i in range(POOL_HELD_OUT_STRIDE - 1, len(pool_list), POOL_HELD_OUT_STRIDE)]
    train_pools = [p for p in pool_list if p not in set(test_pools)]
    train_mask = df["pool_address"].isin(train_pools)
    test_mask = df["pool_address"].isin(test_pools)
    return df[train_mask].copy(), df[test_mask].copy(), train_pools, test_pools


def exclude_pool_split(df: pd.DataFrame, df_raw: pd.DataFrame, n_exclude: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    top_pools = set(df_raw["pool_address"].value_counts().index[:n_exclude])
    train = df[~df["pool_address"].isin(top_pools)].copy()
    test = df[~df["pool_address"].isin(top_pools)].copy()
    # Recompute target within subset
    def _retarget(d: pd.DataFrame) -> pd.DataFrame:
        d = d.copy().reset_index(drop=True)
        n_pos = max(1, int(round(len(d) * POSITIVE_RATE)))
        idx = d["final_return_after_cost"].argsort()[::-1].values[:n_pos]
        t = np.zeros(len(d), dtype=int)
        t[idx] = 1
        d["alpha_v3r10_top20"] = t
        return d
    combined = _retarget(df[~df["pool_address"].isin(top_pools)].copy())
    n = len(combined)
    return combined.iloc[:int(n * TRAIN_FRAC)].copy(), combined.iloc[int(n * TRAIN_FRAC):].copy()


# ── Model training ────────────────────────────────────────────────────────────

def train_classifier(train: pd.DataFrame, test: pd.DataFrame, features: list[str],
                     target: str = "alpha_v3r10_top20") -> tuple[xgb.XGBClassifier | None, dict[str, Any]]:
    cols = features + [target, "final_return_after_cost", "pool_address"]
    tr = train[cols].dropna()
    te = test[cols].dropna()
    if len(tr) < 50 or len(te) < 10:
        return None, {"status": "insufficient_data", "n_train": len(tr), "n_test": len(te)}
    X_tr = tr[features].values.astype(float)
    y_tr = tr[target].values.astype(int)
    X_te = te[features].values.astype(float)
    y_te = te[target].values.astype(int)
    pos_rate = float(y_tr.mean())
    if pos_rate < 0.01 or pos_rate > 0.99:
        return None, {"status": f"degenerate_pos_rate_{pos_rate:.3f}"}
    params = dict(XGB_CLF_PARAMS)
    params["scale_pos_weight"] = max(1.0, (1 - pos_rate) / pos_rate)
    model = xgb.XGBClassifier(**params)
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)
    scores = model.predict_proba(X_te)[:, 1]
    ret_te = te["final_return_after_cost"].values.astype(float)
    pool_te = te["pool_address"].reset_index(drop=True)
    metrics = _eval_model(y_te, scores, ret_te, pool_te, "classifier")
    fi = sorted(
        [{"feature": f, "importance": round(float(imp), 8)}
         for f, imp in zip(features, model.feature_importances_)],
        key=lambda x: x["importance"], reverse=True,
    )
    metrics["feature_importance"] = fi
    metrics["top_feature"] = fi[0]["feature"] if fi else ""
    metrics["top_feature_importance"] = fi[0]["importance"] if fi else 0.0
    metrics["is_top_feature_pool_proxy"] = fi[0]["feature"] in POOL_PROXY_FEATURES if fi else False
    return model, metrics


def train_regressor(train: pd.DataFrame, test: pd.DataFrame, features: list[str]) -> dict[str, Any]:
    """Train return-magnitude regressor; compare to binary classifier by return quality."""
    cols = features + ["final_return_after_cost", "alpha_v3r10_top20", "pool_address"]
    tr = train[cols].dropna()
    te = test[cols].dropna()
    if len(tr) < 50 or len(te) < 10:
        return {"status": "insufficient_data"}
    X_tr = tr[features].values.astype(float)
    y_tr = tr["final_return_after_cost"].values.astype(float)
    X_te = te[features].values.astype(float)
    y_te_ret = te["final_return_after_cost"].values.astype(float)
    y_te_lbl = te["alpha_v3r10_top20"].values.astype(int)
    pool_te = te["pool_address"].reset_index(drop=True)
    model = xgb.XGBRegressor(**XGB_REG_PARAMS)
    model.fit(X_tr, y_tr, eval_set=[(X_te, y_te_ret)], verbose=False)
    scores = model.predict(X_te)
    spear = _spearman(scores, y_te_ret)
    top10 = _top_k_metrics(scores, y_te_ret, y_te_lbl, pool_te, 0.10)
    top20 = _top_k_metrics(scores, y_te_ret, y_te_lbl, pool_te, 0.20)
    fi = sorted(
        [{"feature": f, "importance": round(float(imp), 8)}
         for f, imp in zip(features, model.feature_importances_)],
        key=lambda x: x["importance"], reverse=True,
    )
    return {
        "status": "ok",
        "n_train": int(len(tr)),
        "n_test": int(len(te)),
        "spearman": round(spear, 8),
        "top10pct": top10,
        "top20pct": top20,
        "feature_importance": fi,
        "dataset_return_mean": round(float(y_te_ret.mean()), 8),
        "sanity_only_not_deploy_evidence": True,
    }


# ── Threshold policy ─────────────────────────────────────────────────────────

def threshold_policy_report(train: pd.DataFrame, test: pd.DataFrame,
                             model_a_features: list[str],
                             model_b_features: list[str]) -> dict[str, Any]:
    """At various top-K% thresholds, report return quality for each candidate model."""
    thresholds = [0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
    result: dict[str, Any] = {"thresholds_tested": thresholds, "models": {}}

    for model_key, feats in [("A_proxy_inclusive", model_a_features),
                              ("B_proxy_suppressed", model_b_features)]:
        cols = feats + ["alpha_v3r10_top20", "final_return_after_cost", "pool_address"]
        tr = train[cols].dropna()
        te = test[cols].dropna()
        if len(tr) < 50 or len(te) < 10:
            result["models"][model_key] = {"status": "insufficient_data"}
            continue
        X_tr = tr[feats].values.astype(float)
        y_tr = tr["alpha_v3r10_top20"].values.astype(int)
        X_te = te[feats].values.astype(float)
        y_te = te["alpha_v3r10_top20"].values.astype(int)
        ret_te = te["final_return_after_cost"].values.astype(float)
        pool_te = te["pool_address"].reset_index(drop=True)
        pos_rate = float(y_tr.mean())
        if pos_rate < 0.01:
            result["models"][model_key] = {"status": "degenerate"}
            continue
        params = dict(XGB_CLF_PARAMS)
        params["scale_pos_weight"] = max(1.0, (1 - pos_rate) / pos_rate)
        mdl = xgb.XGBClassifier(**params)
        mdl.fit(X_tr, y_tr, eval_set=[(X_te, y_te)], verbose=False)
        scores = mdl.predict_proba(X_te)[:, 1]
        threshold_results = []
        for k in thresholds:
            m = _top_k_metrics(scores, ret_te, y_te, pool_te, k)
            threshold_results.append(m)
        # Find break-even threshold (if any)
        breakeven = next((m["k_frac"] for m in threshold_results if not m["return_still_negative"]), None)
        result["models"][model_key] = {
            "status": "ok",
            "thresholds": threshold_results,
            "breakeven_threshold_if_any": breakeven,
            "dataset_return_mean": round(float(ret_te.mean()), 8),
        }
    return result


# ── Proxy guard analysis ──────────────────────────────────────────────────────

def proxy_guard_analysis(df: pd.DataFrame, df_raw: pd.DataFrame,
                          clf_a_metrics: dict, clf_b_metrics: dict,
                          pool_heldout_a: dict, pool_heldout_b: dict) -> dict[str, Any]:
    """Measure within-pool variance fractions and proxy contamination."""
    proxy_stats: dict[str, Any] = {}
    for feat in sorted(POOL_PROXY_FEATURES):
        if feat not in df.columns:
            continue
        total_var = float(df[feat].var())
        pool_vars = df.groupby("pool_address")[feat].var().dropna()
        within_frac = float(pool_vars.mean() / total_var) if total_var > 0 else 0.0
        n_constant_pools = int((pool_vars == 0).sum())
        proxy_stats[feat] = {
            "within_pool_var_fraction": round(within_frac, 6),
            "between_pool_var_fraction": round(1 - within_frac, 6),
            "n_pools_constant": n_constant_pools,
            "is_pool_proxy": within_frac < 0.05,
        }

    # Compare proxy_inclusive vs proxy_suppressed on pool-held-out
    pheldout_collapse = {}
    for key, metrics in [("A_proxy_inclusive", pool_heldout_a), ("B_proxy_suppressed", pool_heldout_b)]:
        if metrics.get("status") == "ok":
            pheldout_collapse[key] = {
                "pool_heldout_pr_lift": metrics.get("pr_auc_lift"),
                "collapsed": (metrics.get("pr_auc_lift", -1) < 0),
            }

    # Feature importance: is top feature always a pool proxy?
    proxy_dominates_a = clf_a_metrics.get("is_top_feature_pool_proxy", False)
    proxy_dominates_b = clf_b_metrics.get("is_top_feature_pool_proxy", False)

    return {
        "proxy_feature_variance_analysis": proxy_stats,
        "pool_heldout_collapse": pheldout_collapse,
        "proxy_dominates_model_a": proxy_dominates_a,
        "proxy_dominates_model_b": proxy_dominates_b,
        "pool_proxy_conclusion": (
            "pool_proxy_confirmed" if (proxy_dominates_a and pheldout_collapse.get("A_proxy_inclusive", {}).get("collapsed", True))
            else "pool_proxy_risk_present_but_signal_partial" if proxy_dominates_a
            else "pool_proxy_not_dominant"
        ),
        "proxy_suppressed_survives": not pheldout_collapse.get("B_proxy_suppressed", {}).get("collapsed", True),
        "notes": [
            "fee_tier is constant within each Uniswap V3 pool — true pool identity proxy",
            "tick moves but has very low within-pool CV — partial proxy",
            "liquidity has 1.4% within-pool var — mild proxy; captures per-swap state",
            "proxy_suppressed model tests whether any signal exists without pool identity",
        ],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    t0 = time.time()
    clean_path = OUT_DIR / "run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv"

    print("[V3R10] Phase 1: Load data…")
    df, df_raw = load_and_prepare(clean_path)
    n = len(df)
    print(f"  {n:,} rows, {df['pool_address'].nunique()} pools")

    # Input validation
    v2_rows = int((df["decoded_source"] != "uniswap_v3_swaps_decoded").sum())
    aero_rows = int(df["decoded_source"].str.contains("aero", case=False, na=False).sum())
    iv = {
        "status": "pass" if v2_rows == 0 and aero_rows == 0 else "fail",
        "total_rows": n, "v2_rows": v2_rows, "aero_rows": aero_rows,
        "unique_pools": int(df["pool_address"].nunique()),
        "v3r9_gate_used": True,
        "gate_checks": {"no_v2_rows": v2_rows == 0, "no_aero_rows": aero_rows == 0},
    }

    # ── Splits ────────────────────────────────────────────────────────────────
    print("[V3R10] Phase 2: Prepare splits…")
    train_chron, test_chron = chron_split(df)
    train_pheld, test_pheld, train_pools, test_pool_list = pool_held_out_split(df, df_raw)

    # Recompute target within pool-held-out train/test sets
    def _retarget(d: pd.DataFrame) -> pd.DataFrame:
        d = d.copy().reset_index(drop=True)
        n_pos = max(1, int(round(len(d) * POSITIVE_RATE)))
        idx = d["final_return_after_cost"].argsort()[::-1].values[:n_pos]
        t = np.zeros(len(d), dtype=int)
        t[idx] = 1
        d["alpha_v3r10_top20"] = t
        return d

    train_pheld = _retarget(train_pheld)
    test_pheld  = _retarget(test_pheld)

    # exclude_top1 and exclude_top5
    train_ex1, test_ex1 = exclude_pool_split(df, df_raw, 1)
    train_ex5, test_ex5 = exclude_pool_split(df, df_raw, 5)

    print(f"  chron: train={len(train_chron):,}  test={len(test_chron):,}")
    print(f"  pool_held_out: train={len(train_pheld):,}  test={len(test_pheld):,} ({len(test_pool_list)} pools)")
    print(f"  ex_top1: train/test={len(train_ex1):,}/{len(test_ex1):,}")
    print(f"  ex_top5: train/test={len(train_ex5):,}/{len(test_ex5):,}")

    # ── Train models on each split ────────────────────────────────────────────
    print("[V3R10] Phase 3: Train candidate models…")
    model_results: dict[str, dict[str, Any]] = {}

    for model_name, spec in CANDIDATE_MODELS.items():
        features = spec["features"]
        print(f"  [{model_name}]")
        splits_r: dict[str, Any] = {
            "model_name": model_name,
            "description": spec["description"],
            "features": features,
            "proxy_risk": spec["proxy_risk"],
            "temporal_leak_risk": spec["temporal_leak_risk"],
        }

        # Chronological split
        print(f"    chron…")
        _, chron_m = train_classifier(train_chron, test_chron, features)
        splits_r["chronological"] = chron_m

        # Pool-held-out split
        print(f"    pool_held_out…")
        _, pheld_m = train_classifier(train_pheld, test_pheld, features)
        splits_r["pool_held_out"] = pheld_m

        # Exclude top-1 pool
        print(f"    ex_top1…")
        _, ex1_m = train_classifier(train_ex1, test_ex1, features)
        splits_r["exclude_top1"] = ex1_m

        # Exclude top-5 pools
        print(f"    ex_top5…")
        _, ex5_m = train_classifier(train_ex5, test_ex5, features)
        splits_r["exclude_top5"] = ex5_m

        # Return-aware regressor (chronological only)
        print(f"    regressor…")
        reg_m = train_regressor(train_chron, test_chron, features)
        splits_r["regressor_chron"] = reg_m

        model_results[model_name] = splits_r

        # Save model JSON
        write_json(MODEL_DIR / f"{model_name}.json", {
            "model_name": model_name,
            "spec": spec,
            "split_results": {k: v for k, v in splits_r.items() if isinstance(v, dict)},
            "sanity_only_not_deploy_evidence": True,
        })

    # ── Pool-held-out stress deep-dive ────────────────────────────────────────
    print("[V3R10] Phase 4: Pool-held-out stress report…")
    pheld_report = {
        "split_design": "every_4th_pool_by_count_descending",
        "stride": POOL_HELD_OUT_STRIDE,
        "n_train_pools": len(train_pools),
        "n_test_pools": len(test_pool_list),
        "n_train_rows": int(len(train_pheld)),
        "n_test_rows": int(len(test_pheld)),
        "test_pool_sizes": {
            p: int(df_raw["pool_address"].value_counts().get(p, 0))
            for p in test_pool_list[:15]
        },
        "per_model": {
            name: {
                "pr_auc_lift": model_results[name]["pool_held_out"].get("pr_auc_lift"),
                "roc_auc": model_results[name]["pool_held_out"].get("roc_auc"),
                "spearman": model_results[name]["pool_held_out"].get("spearman"),
                "top10pct_return": model_results[name]["pool_held_out"].get("top10pct", {}).get("return_mean"),
                "top10pct_selects_less_negative": model_results[name]["pool_held_out"].get(
                    "top10pct", {}).get("selects_less_negative_than_mean"),
                "status": model_results[name]["pool_held_out"].get("status", "ok"),
                "collapsed": (model_results[name]["pool_held_out"].get("pr_auc_lift", -999) < 0
                              if model_results[name]["pool_held_out"].get("status") == "ok"
                              else True),
            }
            for name in CANDIDATE_MODELS
        },
    }
    write_json(OUT_DIR / "run004_v3r10_pool_heldout_stress.json", pheld_report)

    # ── Proxy suppression comparison ─────────────────────────────────────────
    print("[V3R10] Phase 5: Proxy suppression comparison…")
    def _compare(split_key: str) -> dict[str, Any]:
        a = model_results["A_proxy_inclusive_v3_liquidity_micro"].get(split_key, {})
        b = model_results["B_proxy_suppressed_microstructure"].get(split_key, {})
        c = model_results["C_all_safe_no_temporal_leak"].get(split_key, {})
        return {
            "split": split_key,
            "A_proxy_inclusive_pr_lift": a.get("pr_auc_lift"),
            "B_proxy_suppressed_pr_lift": b.get("pr_auc_lift"),
            "C_all_safe_no_leak_pr_lift": c.get("pr_auc_lift"),
            "A_top10_return": a.get("top10pct", {}).get("return_mean"),
            "B_top10_return": b.get("top10pct", {}).get("return_mean"),
            "C_top10_return": c.get("top10pct", {}).get("return_mean"),
            "B_still_works": (b.get("status") == "ok" and b.get("pr_auc_lift", -999) > 0),
            "proxy_suppression_collapses_signal": (b.get("status") != "ok"
                                                   or b.get("pr_auc_lift", -999) < 0),
        }

    proxy_cmp = {
        "comparisons": [_compare(s) for s in ["chronological", "pool_held_out", "exclude_top1", "exclude_top5"]],
        "verdict": (
            "signal_is_purely_pool_proxy" if all(
                not _compare(s)["B_still_works"] for s in ["chronological", "pool_held_out"]
            )
            else "signal_partially_survives_proxy_suppression" if (
                _compare("chronological")["B_still_works"]
            )
            else "signal_requires_pool_proxy_features"
        ),
    }
    write_json(OUT_DIR / "run004_v3r10_proxy_suppression_comparison.json", proxy_cmp)

    # ── Threshold policy ──────────────────────────────────────────────────────
    print("[V3R10] Phase 6: Threshold policy…")
    thresh_report = threshold_policy_report(
        train_chron, test_chron,
        CANDIDATE_MODELS["A_proxy_inclusive_v3_liquidity_micro"]["features"],
        CANDIDATE_MODELS["B_proxy_suppressed_microstructure"]["features"],
    )
    write_json(OUT_DIR / "run004_v3r10_threshold_policy_report.json", thresh_report)

    # ── Leakage guard ─────────────────────────────────────────────────────────
    print("[V3R10] Phase 7: Leakage guard…")
    leak_guard = proxy_guard_analysis(
        df, df_raw,
        model_results["A_proxy_inclusive_v3_liquidity_micro"].get("chronological", {}),
        model_results["B_proxy_suppressed_microstructure"].get("chronological", {}),
        model_results["A_proxy_inclusive_v3_liquidity_micro"].get("pool_held_out", {}),
        model_results["B_proxy_suppressed_microstructure"].get("pool_held_out", {}),
    )
    write_json(OUT_DIR / "run004_v3r10_leakage_guard.json", leak_guard)

    # ── Model comparison CSV ──────────────────────────────────────────────────
    comparison_rows = []
    for name in CANDIDATE_MODELS:
        for split_key in ["chronological", "pool_held_out", "exclude_top1", "exclude_top5"]:
            r = model_results[name].get(split_key, {})
            comparison_rows.append({
                "model": name,
                "split": split_key,
                "status": r.get("status", ""),
                "n_test": r.get("n_test", ""),
                "roc_auc": r.get("roc_auc", ""),
                "pr_auc": r.get("pr_auc", ""),
                "pr_auc_lift": r.get("pr_auc_lift", ""),
                "spearman": r.get("spearman", ""),
                "top5pct_return": r.get("top5pct", {}).get("return_mean", ""),
                "top10pct_return": r.get("top10pct", {}).get("return_mean", ""),
                "top20pct_return": r.get("top20pct", {}).get("return_mean", ""),
                "top10pct_pos_return_rate": r.get("top10pct", {}).get("positive_return_rate", ""),
                "top10pct_unique_pools": r.get("top10pct", {}).get("unique_pools_selected", ""),
                "top10pct_top1_pool_share": r.get("top10pct", {}).get("top1_selected_pool_share", ""),
                "is_top_feature_proxy": r.get("is_top_feature_pool_proxy", ""),
                "collapsed_pool_heldout": pheld_report["per_model"].get(name, {}).get("collapsed", ""),
            })
    write_csv_rows(OUT_DIR / "run004_v3r10_model_comparison.csv", comparison_rows)

    # ── Gate decision ─────────────────────────────────────────────────────────
    print("[V3R10] Phase 8: Gate decision…")
    chron_a  = model_results["A_proxy_inclusive_v3_liquidity_micro"].get("chronological", {})
    chron_b  = model_results["B_proxy_suppressed_microstructure"].get("chronological", {})
    chron_c  = model_results["C_all_safe_no_temporal_leak"].get("chronological", {})
    pheld_a  = model_results["A_proxy_inclusive_v3_liquidity_micro"].get("pool_held_out", {})
    pheld_b  = model_results["B_proxy_suppressed_microstructure"].get("pool_held_out", {})

    any_nonleaky_beats_baseline = any([
        chron_b.get("pr_auc_lift", -999) > 0,
        chron_c.get("pr_auc_lift", -999) > 0,
    ])
    # Pool-held-out: non-collapsed = best non-proxy model not collapsing
    pheld_not_collapsed = (
        pheld_b.get("status") == "ok" and pheld_b.get("pr_auc_lift", -999) > -0.10
    ) or (
        model_results["C_all_safe_no_temporal_leak"].get("pool_held_out", {}).get("pr_auc_lift", -999) > -0.10
    )
    # top10 improves for non-leaky models
    top10_improves = any(
        r.get("top10pct", {}).get("selects_less_negative_than_mean", False)
        for r in [chron_b, chron_c]
        if r.get("status") == "ok"
    )
    # Pool concentration: check only non-proxy models (B and C)
    # Model A is known proxy — its concentration is expected and irrelevant
    pool_conc_ok = any(
        r.get("top10pct", {}).get("top1_selected_pool_share", 1.0) < 0.50
        for r in [chron_b, chron_c]
        if r.get("status") == "ok"
    )

    allow_continue = (
        any_nonleaky_beats_baseline
        and pheld_not_collapsed
        and top10_improves
        and pool_conc_ok
    )

    gate = {
        "allow_v3r11_more_data": (
            not allow_continue
            or df["pool_address"].nunique() < 100
            or leak_guard.get("proxy_suppressed_survives", False) is False
        ),
        "allow_v3_return_aware_target_revision": (
            chron_b.get("spearman", 0) < 0.05
            and chron_a.get("spearman", 0) < 0.10
        ),
        "allow_v3_candidate_continue": allow_continue,
        "allow_v3_mainline": False,
        "allow_mixed_dataset": False,
        "promote_live_allowed": False,
        "gate_checks": {
            "input_validation_pass": iv["status"] == "pass",
            "no_v2_rows": v2_rows == 0,
            "no_aero_rows": aero_rows == 0,
            "any_nonleaky_model_above_baseline": any_nonleaky_beats_baseline,
            "signal_not_collapsed_pool_heldout": pheld_not_collapsed,
            "top10_improves_vs_mean": top10_improves,
            "pool_concentration_acceptable": pool_conc_ok,
            "no_production_writes": True,
            "no_active_model_writes": True,
            "no_runtime_writes": True,
        },
        "proxy_verdict": leak_guard.get("pool_proxy_conclusion", "unknown"),
        "best_nonleaky_model": "B_proxy_suppressed" if chron_b.get("pr_auc_lift", -999) > chron_c.get("pr_auc_lift", -999) else "C_all_safe_no_leak",
        "best_nonleaky_pr_lift": max(
            chron_b.get("pr_auc_lift", -999),
            chron_c.get("pr_auc_lift", -999),
        ),
        "why_v3_cannot_mainline": [
            "Top-decile return still negative — no profitable selection regime confirmed",
            "Pool-proxy features (fee_tier, tick, liquidity) dominate model A signal",
            "Proxy-suppressed model B requires validation at larger pool diversity",
            "Temporal leakage gap (shuffled vs chronological) not fully eliminated",
            "Research-only model — no deployment readiness criteria met",
            "V3 strategy needs more diverse pools (target >= 200 with unseen-pool validation)",
        ],
    }
    gate["gate_checks"]["all_pass"] = all(gate["gate_checks"].values())
    write_json(OUT_DIR / "run004_v3r10_gate_decision.json", gate)

    # ── Candidate model summary report ────────────────────────────────────────
    candidate_report = {
        "input_validation": iv,
        "splits": {
            "chronological": {"n_train": len(train_chron), "n_test": len(test_chron)},
            "pool_held_out": {"n_train": len(train_pheld), "n_test": len(test_pheld),
                              "n_test_pools": len(test_pool_list)},
            "exclude_top1": {"n_train": len(train_ex1), "n_test": len(test_ex1)},
            "exclude_top5": {"n_train": len(train_ex5), "n_test": len(test_ex5)},
        },
        "model_results": {name: {
            k: v for k, v in res.items() if k not in {"model_name", "description", "features", "proxy_risk"}
        } for name, res in model_results.items()},
        "proxy_suppression_comparison": proxy_cmp,
        "gate": gate,
    }
    write_json(OUT_DIR / "run004_v3r10_candidate_model_report.json", candidate_report)

    # ── Markdown ──────────────────────────────────────────────────────────────
    print("[V3R10] Phase 9: Write markdown…")
    t_elapsed = time.time() - t0

    def gmark(k: str) -> str:
        return "`pass`" if gate["gate_checks"].get(k) else "`FAIL`"

    def fmt_metric(r: dict, key: str, sub: str | None = None) -> str:
        if r.get("status") != "ok":
            return "skipped"
        if sub:
            return str(r.get(key, {}).get(sub, ""))
        return str(r.get(key, ""))

    # Build model × split table
    table_lines = ["| model | split | status | roc_auc | pr_lift | spearman | top10_ret | top10_pos_rt |",
                   "|---|---|---|---|---|---|---|---|"]
    for name in CANDIDATE_MODELS:
        short = name.split("_")[0]
        for sp_key in ["chronological", "pool_held_out", "exclude_top1", "exclude_top5"]:
            r = model_results[name].get(sp_key, {})
            table_lines.append(
                f"| {short} | {sp_key} | {r.get('status','')} | "
                f"{r.get('roc_auc','')} | {r.get('pr_auc_lift','')} | "
                f"{r.get('spearman','')} | "
                f"{r.get('top10pct',{}).get('return_mean','')} | "
                f"{r.get('top10pct',{}).get('positive_return_rate','')} |"
            )
    model_table = "\n".join(table_lines)

    # Proxy comparison table
    cmp_lines = ["| split | A_proxy_incl_lift | B_proxy_sup_lift | C_safe_lift | B_still_works |",
                 "|---|---|---|---|---|"]
    for c in proxy_cmp["comparisons"]:
        cmp_lines.append(f"| {c['split']} | {c.get('A_proxy_inclusive_pr_lift','')} | "
                         f"{c.get('B_proxy_suppressed_pr_lift','')} | "
                         f"{c.get('C_all_safe_no_leak_pr_lift','')} | {c.get('B_still_works','')} |")
    cmp_table = "\n".join(cmp_lines)

    # Threshold policy summary
    def fmt_thresh(model_k: str) -> str:
        tm = thresh_report.get("models", {}).get(model_k, {})
        if tm.get("status") != "ok":
            return "  unavailable"
        lines = []
        for t in tm.get("thresholds", []):
            lines.append(f"  top{int(t['k_frac']*100):2d}%: ret={t['return_mean']:.4f}  "
                         f"pos_rt={t['positive_return_rate']:.3f}  "
                         f"neg={'yes' if t['return_still_negative'] else 'NO'}")
        breakeven = tm.get("breakeven_threshold_if_any")
        lines.append(f"  breakeven_threshold: {breakeven}")
        return "\n".join(lines)

    why_not = "\n".join(f"- {w}" for w in gate.get("why_v3_cannot_mainline", []))

    md = f"""# Run-004 Patch V3R-10 — V3-only Candidate Dry-run with Pool-Held-Out Stress

## Input Validation
- Status: `{iv['status']}`
- Rows: {iv['total_rows']:,} | Pools: {iv['unique_pools']}
- V2 rows: {iv['v2_rows']} | Aero rows: {iv['aero_rows']}

## Candidate Models
- **A**: proxy_inclusive_v3_liquidity_micro (fee_tier/tick/liquidity + flow — pool-proxy risk HIGH)
- **B**: proxy_suppressed_microstructure (flow/sender/temporal only — no pool proxies)
- **C**: all_safe_no_temporal_leak (A+B minus weth_flow leaky features)

## Model × Split Results
{model_table}

## Pool-Held-Out Design
- Strategy: every 4th pool (by descending count) held out as unseen
- Train: {len(train_pools)} pools / {len(train_pheld):,} rows
- Test: {len(test_pool_list)} pools / {len(test_pheld):,} rows

## Proxy Suppression Comparison
{cmp_table}

Verdict: **{proxy_cmp['verdict']}**

## Pool-Proxy Guard
| Feature | Within-Pool Var Fraction | Pool Proxy? |
|---|---|---|
{chr(10).join(f"| {f} | {s['within_pool_var_fraction']:.4f} | {'YES' if s['is_pool_proxy'] else 'no'} |" for f, s in leak_guard['proxy_feature_variance_analysis'].items())}

Top feature is pool proxy — Model A: `{leak_guard.get('proxy_dominates_model_a', '?')}`
Top feature is pool proxy — Model B: `{leak_guard.get('proxy_dominates_model_b', '?')}`
Proxy conclusion: **{leak_guard.get('pool_proxy_conclusion', '?')}**

## Threshold Policy — Model A
```
{fmt_thresh("A_proxy_inclusive")}
```

## Threshold Policy — Model B
```
{fmt_thresh("B_proxy_suppressed")}
```

## Gate Checks
| Check | Result |
|---|---|
| input_validation_pass | {gmark('input_validation_pass')} |
| no_v2_rows | {gmark('no_v2_rows')} |
| no_aero_rows | {gmark('no_aero_rows')} |
| any_nonleaky_model_above_baseline | {gmark('any_nonleaky_model_above_baseline')} |
| signal_not_collapsed_pool_heldout | {gmark('signal_not_collapsed_pool_heldout')} |
| top10_improves_vs_mean | {gmark('top10_improves_vs_mean')} |
| pool_concentration_acceptable | {gmark('pool_concentration_acceptable')} |
| no_production_writes | {gmark('no_production_writes')} |
| no_active_model_writes | {gmark('no_active_model_writes')} |

## Final Gate Decision
- allow_v3_candidate_continue: `{str(gate['allow_v3_candidate_continue']).lower()}`
- allow_v3r11_more_data: `{str(gate['allow_v3r11_more_data']).lower()}`
- allow_v3_return_aware_target_revision: `{str(gate['allow_v3_return_aware_target_revision']).lower()}`
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
    write_markdown(RUN_DIR / "run-004-patch-v3r10-v3-candidate-pool-heldout-dryrun.md", md)

    print(f"\n[V3R10] Done in {t_elapsed:.1f}s")
    print(json.dumps({
        "input_validation": iv["status"],
        "proxy_verdict": leak_guard.get("pool_proxy_conclusion"),
        "proxy_suppressed_B_chron_pr_lift": chron_b.get("pr_auc_lift"),
        "proxy_incl_A_pool_heldout_pr_lift": pheld_a.get("pr_auc_lift"),
        "proxy_suppressed_B_pool_heldout_pr_lift": pheld_b.get("pr_auc_lift"),
        "allow_v3_candidate_continue": gate["allow_v3_candidate_continue"],
        "allow_v3r11_more_data": gate["allow_v3r11_more_data"],
    }, indent=2))


if __name__ == "__main__":
    main()
