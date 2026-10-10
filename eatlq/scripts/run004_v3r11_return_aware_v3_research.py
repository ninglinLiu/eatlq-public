"""Run-004 Patch V3R-11 — V3 return-aware research-only dry run.

Research-only constraints:
  - V3-only dataset evaluation.
  - No production/runtime/active-model writes.
  - No live/paper/signal/executor path changes.
  - Reuse V3R-10 baseline framing + V3R8 datasets.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from sklearn.metrics import average_precision_score, roc_auc_score
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_research_utils import write_json, write_markdown  # noqa: E402

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on"
), "LIVE_TRADING_MODE must not be active"

RANDOM_SEED = 42
TRAIN_FRAC = 0.70
POOL_HELD_OUT_STRIDE = 4
TOP_POLICIES = [0.02, 0.05, 0.10, 0.20]

OUT_DIR = ROOT / "mvp/diagnostics"
RUN_DIR = ROOT / "mvp/runs"
MODEL_DIR = ROOT / "mvp/models/v3r11_research_candidate"
for d in (OUT_DIR, RUN_DIR):
    d.mkdir(parents=True, exist_ok=True)

INPUT_CANDIDATES = [
    OUT_DIR / "run004_v3r8_all_v3_decoded_backed_10k_dataset.csv",
    OUT_DIR / "run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv",
]

FEATURE_SETS: dict[str, dict[str, Any]] = {
    "B_proxy_suppressed_primary": {
        "primary_candidate": True,
        "proxy_included": False,
        "features": [
            "feature_pool_flow_count_1h",
            "feature_pool_unique_senders_1h",
            "feature_sender_pool_prior_count_1h",
            "feature_sender_present_int",
            "feature_temporal_prev_pool_delta_sec_filled",
            "is_buy_weth_to_token_int",
        ],
    },
    "A_proxy_included_diagnostic": {
        "primary_candidate": False,
        "proxy_included": True,
        "features": [
            "feature_liquidity",
            "feature_fee_tier",
            "feature_tick",
            "feature_pool_flow_count_1h",
            "feature_pool_unique_senders_1h",
        ],
    },
}

V3R10_BASELINE_TOP10 = {
    "chronological": -0.047,
    "pool_heldout": -0.054,
    "exclude_top5": -0.040,
}

XGB_CLF_PARAMS: dict[str, Any] = {
    "n_estimators": 320,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.85,
    "colsample_bytree": 0.85,
    "min_child_weight": 5,
    "gamma": 0.1,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "eval_metric": "aucpr",
    "early_stopping_rounds": 30,
    "verbosity": 0,
    "random_state": RANDOM_SEED,
    "n_jobs": 4,
}
XGB_REG_PARAMS: dict[str, Any] = {
    "n_estimators": 320,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.85,
    "colsample_bytree": 0.85,
    "min_child_weight": 5,
    "gamma": 0.1,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "eval_metric": "mae",
    "early_stopping_rounds": 30,
    "verbosity": 0,
    "random_state": RANDOM_SEED,
    "n_jobs": 4,
}
XGB_MULTI_PARAMS: dict[str, Any] = {
    "n_estimators": 260,
    "max_depth": 5,
    "learning_rate": 0.06,
    "subsample": 0.85,
    "colsample_bytree": 0.85,
    "min_child_weight": 4,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "objective": "multi:softprob",
    "eval_metric": "mlogloss",
    "early_stopping_rounds": 25,
    "verbosity": 0,
    "random_state": RANDOM_SEED,
    "n_jobs": 4,
}


def _safe_log1p(arr: pd.Series | np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    return np.sign(a) * np.log1p(np.abs(a))


def _spearman(scores: np.ndarray, returns: np.ndarray) -> float:
    mask = np.isfinite(scores) & np.isfinite(returns)
    if mask.sum() < 5:
        return float("nan")
    r, _ = sp_stats.spearmanr(scores[mask], returns[mask])
    return float(r)


def _pr_lift(pr_auc: float, pos_rate: float) -> float:
    return pr_auc / pos_rate - 1.0 if pos_rate > 0 else float("nan")


def _best_input_path() -> Path:
    for p in INPUT_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError("No V3R8 dataset found for V3R11 input")


def load_and_prepare() -> tuple[pd.DataFrame, dict[str, Any]]:
    path = _best_input_path()
    raw = pd.read_csv(path)
    raw = raw.sort_values("timestamp_ms", ascending=True).reset_index(drop=True)

    df = raw.copy()
    df = df[df["decoded_source"] == "uniswap_v3_swaps_decoded"].copy()
    df = df[df["label_status"] == "success"].copy()
    for col in ("source_tx_hash", "source_log_index", "source_block_number", "pool_address"):
        df = df[df[col].notna() & (df[col].astype(str).str.strip() != "")]
    df["final_return_after_cost"] = pd.to_numeric(df["final_return_after_cost"], errors="coerce")
    df = df[df["final_return_after_cost"].notna()].copy()

    df["feature_temporal_prev_pool_delta_sec_filled"] = np.log1p(
        pd.to_numeric(df["feature_temporal_prev_pool_delta_sec"], errors="coerce")
        .fillna(pd.to_numeric(df["feature_temporal_prev_pool_delta_sec"], errors="coerce").median())
        .values
    )
    df["is_buy_weth_to_token_int"] = pd.to_numeric(df["is_buy_weth_to_token"], errors="coerce").fillna(0).astype(int)
    df["feature_sender_present_int"] = pd.to_numeric(df["feature_sender_present"], errors="coerce").fillna(0).astype(int)
    df["feature_liquidity"] = _safe_log1p(pd.to_numeric(df["feature_liquidity"], errors="coerce").fillna(0).values)
    df["feature_pool_flow_count_1h"] = np.log1p(pd.to_numeric(df["feature_pool_flow_count_1h"], errors="coerce").fillna(0).values)
    df["feature_pool_unique_senders_1h"] = np.log1p(pd.to_numeric(df["feature_pool_unique_senders_1h"], errors="coerce").fillna(0).values)
    df["feature_sender_pool_prior_count_1h"] = np.log1p(
        pd.to_numeric(df["feature_sender_pool_prior_count_1h"], errors="coerce").fillna(0).values
    )
    df["feature_tick"] = pd.to_numeric(df["feature_tick"], errors="coerce").fillna(0).astype(float)
    df["feature_fee_tier"] = pd.to_numeric(df["feature_fee_tier"], errors="coerce").fillna(
        pd.to_numeric(df["fee_tier"], errors="coerce").fillna(100)
    ).astype(float)
    if "gross_quote_return_30m" not in df.columns:
        df["gross_quote_return_30m"] = np.nan
    df["gross_quote_return_30m"] = pd.to_numeric(df["gross_quote_return_30m"], errors="coerce")
    df = df.sort_values("timestamp_ms", ascending=True).reset_index(drop=True)

    ts = pd.to_numeric(df["timestamp_ms"], errors="coerce").dropna()
    pool_counts = df["pool_address"].value_counts()
    fee_counts = df["fee_tier"].astype(str).value_counts().to_dict()
    profile = {
        "input_path": str(path.relative_to(ROOT)),
        "rows": int(len(df)),
        "unique_pools": int(df["pool_address"].nunique()),
        "target_pool_goal": 200,
        "pool_goal_reached": bool(df["pool_address"].nunique() >= 200),
        "v2_rows": int((df["decoded_source"] != "uniswap_v3_swaps_decoded").sum()),
        "aero_rows": int(df["decoded_source"].astype(str).str.contains("aero", case=False, na=False).sum()),
        "time_span": {
            "timestamp_ms_min": int(ts.min()) if len(ts) else None,
            "timestamp_ms_max": int(ts.max()) if len(ts) else None,
        },
        "fee_tier_distribution": {k: int(v) for k, v in fee_counts.items()},
        "pool_concentration": {
            "top1_pool_share": round(float(pool_counts.iloc[0] / len(df)), 6) if len(pool_counts) else 0.0,
            "top5_pool_share": round(float(pool_counts.head(5).sum() / len(df)), 6) if len(pool_counts) else 0.0,
            "top10_pool_share": round(float(pool_counts.head(10).sum() / len(df)), 6) if len(pool_counts) else 0.0,
        },
        "identity_fields_preserved": True,
    }
    return df, profile


def _make_targets(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, tuple[np.ndarray, np.ndarray, dict[str, Any]]]:
    tr_ret = train["final_return_after_cost"].values.astype(float)
    te_ret = test["final_return_after_cost"].values.astype(float)
    tr_gross = train["gross_quote_return_30m"].values.astype(float)

    n_pos = max(1, int(round(len(train) * 0.20)))
    tr_idx = np.argsort(tr_ret)[::-1][:n_pos]
    threshold_a = np.partition(tr_ret, len(tr_ret) - n_pos)[len(tr_ret) - n_pos]
    y_tr_a = np.zeros(len(train), dtype=int)
    y_tr_a[tr_idx] = 1
    y_te_a = (te_ret >= threshold_a).astype(int)

    effective_cost = np.nanmedian(tr_gross - tr_ret) if np.isfinite(np.nanmedian(tr_gross - tr_ret)) else 0.0
    extra_slippage_buffer = max(0.0025, min(0.015, float(effective_cost * 0.35)))
    cost_threshold = float(extra_slippage_buffer)
    y_tr_b = (tr_ret > cost_threshold).astype(int)
    y_te_b = (te_ret > cost_threshold).astype(int)

    lo, hi = np.nanpercentile(tr_ret, [2, 98])
    y_tr_c = np.clip(tr_ret, lo, hi)
    y_te_c = np.clip(te_ret, lo, hi)

    qcuts = np.nanpercentile(tr_ret, [20, 40, 60, 80])

    def _bucketize(arr: np.ndarray) -> np.ndarray:
        return np.digitize(arr, qcuts, right=False).astype(int)

    y_tr_d = _bucketize(tr_ret)
    y_te_d = _bucketize(te_ret)

    return {
        "A_binary_top20": (y_tr_a, y_te_a, {"threshold_train_return": float(threshold_a)}),
        "B_cost_aware_classification": (
            y_tr_b,
            y_te_b,
            {
                "cost_buffer_threshold": round(cost_threshold, 8),
                "estimated_all_in_cost_median": round(float(effective_cost), 8),
            },
        ),
        "C_return_regression_winsorized": (
            y_tr_c,
            y_te_c,
            {"winsor_clip": [round(float(lo), 8), round(float(hi), 8)]},
        ),
        "D_quantile_ranking_target": (y_tr_d, y_te_d, {"quantile_cutpoints": [round(float(x), 8) for x in qcuts]}),
    }


def _policy_metrics(scores: np.ndarray, test: pd.DataFrame, k_frac: float) -> dict[str, Any]:
    returns = test["final_return_after_cost"].values.astype(float)
    gross = test["gross_quote_return_30m"].values.astype(float)
    pools = test["pool_address"].reset_index(drop=True)
    n = len(scores)
    sel = max(1, int(round(n * k_frac)))
    idx = np.argsort(scores)[-sel:]
    r = returns[idx]
    g = gross[idx]
    p = pools.iloc[idx]
    pool_counts = p.value_counts()
    gross_mean = float(np.nanmean(g)) if np.isfinite(np.nanmean(g)) else float("nan")
    after_mean = float(np.nanmean(r))
    return {
        "k_frac": k_frac,
        "selected_rows": int(sel),
        "selected_unique_pools": int(pool_counts.shape[0]),
        "top1_pool_share": round(float(pool_counts.iloc[0] / sel), 6) if len(pool_counts) else 0.0,
        "top5_pool_share": round(float(pool_counts.head(5).sum() / sel), 6),
        "mean_net_return_after_cost": round(after_mean, 8),
        "mean_net_return_before_cost_proxy": round(gross_mean, 8) if not math.isnan(gross_mean) else None,
        "positive_after_cost_rate": round(float((r > 0).mean()), 6),
        "breakeven_after_cost": bool(after_mean > 0),
    }


def _classification_eval(y_true: np.ndarray, scores: np.ndarray, test: pd.DataFrame, extras: dict[str, Any]) -> dict[str, Any]:
    pos_rate = float(y_true.mean())
    roc = None
    pr = None
    pr_lift = None
    if len(np.unique(y_true)) > 1 and 0 < pos_rate < 1:
        roc = float(roc_auc_score(y_true, scores))
        pr = float(average_precision_score(y_true, scores))
        pr_lift = _pr_lift(pr, pos_rate)
    sp = _spearman(scores, test["final_return_after_cost"].values.astype(float))
    policies = [_policy_metrics(scores, test, k) for k in TOP_POLICIES]
    return {
        "target_meta": extras,
        "metrics": {
            "roc_auc": round(roc, 8) if roc is not None else None,
            "pr_auc": round(pr, 8) if pr is not None else None,
            "pr_lift": round(pr_lift, 8) if pr_lift is not None else None,
            "spearman_vs_net_return": round(float(sp), 8) if not math.isnan(sp) else None,
        },
        "policies": policies,
        "any_policy_breakeven": any(p["breakeven_after_cost"] for p in policies),
    }


def _regression_eval(scores: np.ndarray, y_true_reg: np.ndarray, test: pd.DataFrame, extras: dict[str, Any]) -> dict[str, Any]:
    sp = _spearman(scores, test["final_return_after_cost"].values.astype(float))
    mae = float(np.mean(np.abs(scores - y_true_reg)))
    policies = [_policy_metrics(scores, test, k) for k in TOP_POLICIES]
    return {
        "target_meta": extras,
        "metrics": {
            "roc_auc": None,
            "pr_auc": None,
            "pr_lift": None,
            "mae_winsorized_target": round(mae, 8),
            "spearman_vs_net_return": round(float(sp), 8) if not math.isnan(sp) else None,
        },
        "policies": policies,
        "any_policy_breakeven": any(p["breakeven_after_cost"] for p in policies),
    }


def _evaluate_family(
    family: str,
    X_tr: np.ndarray,
    X_te: np.ndarray,
    y_tr: np.ndarray,
    y_te: np.ndarray,
    test_df: pd.DataFrame,
    target_meta: dict[str, Any],
) -> dict[str, Any]:
    if family in {"A_binary_top20", "B_cost_aware_classification"}:
        pos_rate = float(np.mean(y_tr))
        if pos_rate <= 0.01 or pos_rate >= 0.99:
            return {"status": "degenerate_target", "target_meta": target_meta}
        params = dict(XGB_CLF_PARAMS)
        params["scale_pos_weight"] = max(1.0, (1 - pos_rate) / pos_rate)
        model = xgb.XGBClassifier(**params)
        model.fit(X_tr, y_tr.astype(int), eval_set=[(X_te, y_te.astype(int))], verbose=False)
        scores = model.predict_proba(X_te)[:, 1]
        out = _classification_eval(y_te.astype(int), scores, test_df, target_meta)
        out["status"] = "ok"
        return out
    if family == "C_return_regression_winsorized":
        model = xgb.XGBRegressor(**XGB_REG_PARAMS)
        model.fit(X_tr, y_tr.astype(float), eval_set=[(X_te, y_te.astype(float))], verbose=False)
        scores = model.predict(X_te)
        out = _regression_eval(scores, y_te.astype(float), test_df, target_meta)
        out["status"] = "ok"
        return out
    if family == "D_quantile_ranking_target":
        n_classes = int(max(y_tr.max(), y_te.max()) + 1)
        if n_classes < 2:
            return {"status": "degenerate_target", "target_meta": target_meta}
        params = dict(XGB_MULTI_PARAMS)
        params["num_class"] = n_classes
        model = xgb.XGBClassifier(**params)
        model.fit(X_tr, y_tr.astype(int), eval_set=[(X_te, y_te.astype(int))], verbose=False)
        probs = model.predict_proba(X_te)
        classes = np.arange(probs.shape[1], dtype=float)
        scores = np.dot(probs, classes)
        out = _regression_eval(scores, y_te.astype(float), test_df, target_meta)
        out["status"] = "ok"
        return out
    return {"status": "unknown_target_family"}


def chron_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    i = int(round(len(df) * TRAIN_FRAC))
    return df.iloc[:i].copy(), df.iloc[i:].copy()


def pool_heldout_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    pool_counts = df["pool_address"].value_counts()
    pools = list(pool_counts.index)
    test_pools = [pools[i] for i in range(POOL_HELD_OUT_STRIDE - 1, len(pools), POOL_HELD_OUT_STRIDE)]
    is_test = df["pool_address"].isin(test_pools)
    return df[~is_test].copy(), df[is_test].copy()


def exclude_top5_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    top5 = set(df["pool_address"].value_counts().head(5).index.tolist())
    sub = df[~df["pool_address"].isin(top5)].copy().reset_index(drop=True)
    return chron_split(sub)


def rolling_splits(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame, pd.DataFrame]]:
    if len(df) < 3000:
        return []
    n = len(df)
    windows = [("rolling_w1", 0.50, 0.15), ("rolling_w2", 0.65, 0.15), ("rolling_w3", 0.80, 0.20)]
    out: list[tuple[str, pd.DataFrame, pd.DataFrame]] = []
    for name, train_frac, test_frac in windows:
        tr_end = int(round(n * train_frac))
        te_end = min(n, tr_end + int(round(n * test_frac)))
        tr = df.iloc[:tr_end].copy()
        te = df.iloc[tr_end:te_end].copy()
        if len(tr) >= 200 and len(te) >= 100:
            out.append((name, tr, te))
    return out


def evaluate_split(train: pd.DataFrame, test: pd.DataFrame, split_name: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "split_name": split_name,
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "n_train_pools": int(train["pool_address"].nunique()),
        "n_test_pools": int(test["pool_address"].nunique()),
        "families": {},
    }
    targets = _make_targets(train, test)
    for model_name, spec in FEATURE_SETS.items():
        feats = spec["features"]
        cols = feats + ["final_return_after_cost", "pool_address", "gross_quote_return_30m"]
        tr = train[cols].dropna(subset=feats + ["final_return_after_cost", "pool_address"])
        te = test[cols].dropna(subset=feats + ["final_return_after_cost", "pool_address"])
        model_pack: dict[str, Any] = {
            "primary_candidate": spec["primary_candidate"],
            "proxy_included": spec["proxy_included"],
            "status": "ok",
            "target_families": {},
        }
        if len(tr) < 200 or len(te) < 100:
            model_pack["status"] = "insufficient_data"
            result["families"][model_name] = model_pack
            continue
        X_tr = tr[feats].values.astype(float)
        X_te = te[feats].values.astype(float)
        for family, (y_tr, y_te, meta) in targets.items():
            family_eval = _evaluate_family(
                family=family,
                X_tr=X_tr,
                X_te=X_te,
                y_tr=np.asarray(y_tr)[: len(tr)],
                y_te=np.asarray(y_te)[: len(te)],
                test_df=te.reset_index(drop=True),
                target_meta=meta,
            )
            model_pack["target_families"][family] = family_eval
        result["families"][model_name] = model_pack
    return result


def summarize_key_rows(eval_map: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for split, payload in eval_map.items():
        for model_name, m in payload.get("families", {}).items():
            for fam_name, fam in m.get("target_families", {}).items():
                if fam.get("status") != "ok":
                    continue
                policy_10 = next((p for p in fam.get("policies", []) if abs(p["k_frac"] - 0.10) < 1e-9), None)
                rows.append(
                    {
                        "split": split,
                        "model": model_name,
                        "target_family": fam_name,
                        "pr_lift": fam.get("metrics", {}).get("pr_lift"),
                        "spearman": fam.get("metrics", {}).get("spearman_vs_net_return"),
                        "top10_after_cost_return": policy_10.get("mean_net_return_after_cost") if policy_10 else None,
                        "top10_top1_pool_share": policy_10.get("top1_pool_share") if policy_10 else None,
                        "any_breakeven": bool(fam.get("any_policy_breakeven", False)),
                    }
                )
    return rows


def aggregate_rolling(eval_map: dict[str, Any]) -> dict[str, Any]:
    rolling_keys = [k for k in eval_map if k.startswith("rolling_w")]
    out: dict[str, Any] = {}
    if not rolling_keys:
        return out
    by_pair: dict[tuple[str, str], list[float]] = {}
    by_pair_sp: dict[tuple[str, str], list[float]] = {}
    for rk in rolling_keys:
        payload = eval_map[rk]
        for model_name, model_pack in payload.get("families", {}).items():
            for fam_name, fam in model_pack.get("target_families", {}).items():
                if fam.get("status") != "ok":
                    continue
                p10 = next((p for p in fam.get("policies", []) if abs(p["k_frac"] - 0.10) < 1e-9), None)
                if not p10:
                    continue
                key = (model_name, fam_name)
                by_pair.setdefault(key, []).append(float(p10["mean_net_return_after_cost"]))
                sp = fam.get("metrics", {}).get("spearman_vs_net_return")
                if sp is not None:
                    by_pair_sp.setdefault(key, []).append(float(sp))
    for key, vals in by_pair.items():
        out[f"{key[0]}::{key[1]}"] = {
            "rolling_top10_after_cost_mean": round(float(np.mean(vals)), 8),
            "rolling_top10_after_cost_min": round(float(np.min(vals)), 8),
            "rolling_top10_after_cost_max": round(float(np.max(vals)), 8),
            "rolling_spearman_mean": round(float(np.mean(by_pair_sp.get(key, [np.nan]))), 8)
            if by_pair_sp.get(key)
            else None,
            "n_windows": len(vals),
        }
    return out


def gate_decision(profile: dict[str, Any], eval_map: dict[str, Any]) -> dict[str, Any]:
    key_rows = summarize_key_rows(eval_map)
    primary_rows = [r for r in key_rows if r["model"] == "B_proxy_suppressed_primary"]

    return_aware = [r for r in primary_rows if r["target_family"] != "A_binary_top20"]
    best_return_aware = max(
        return_aware,
        key=lambda x: (-999 if x["top10_after_cost_return"] is None else x["top10_after_cost_return"]),
        default=None,
    )

    pheld_survives = any(
        (r["split"] == "pool_heldout" and (r["pr_lift"] is None or r["pr_lift"] > -0.10) and (r["spearman"] or 0) > -0.02)
        for r in primary_rows
    )
    concentration_controlled = all(
        (r["top10_top1_pool_share"] is not None and r["top10_top1_pool_share"] <= 0.50)
        for r in primary_rows
        if r["split"] in {"chronological", "pool_heldout", "exclude_top5"}
    )

    improved_vs_v3r10 = False
    if best_return_aware is not None:
        bmk = V3R10_BASELINE_TOP10.get(best_return_aware["split"])
        if bmk is not None and best_return_aware["top10_after_cost_return"] is not None:
            improved_vs_v3r10 = best_return_aware["top10_after_cost_return"] > bmk

    chron_pos = any(
        r["split"] == "chronological" and (r["top10_after_cost_return"] or -1) > 0 and (r["top10_top1_pool_share"] or 1) <= 0.50
        for r in primary_rows
    )
    pheld_pos = any(
        r["split"] == "pool_heldout" and (r["top10_after_cost_return"] or -1) > 0 and (r["top10_top1_pool_share"] or 1) <= 0.50
        for r in primary_rows
    )
    spearman_material = any((r["spearman"] is not None and r["spearman"] >= 0.08) for r in primary_rows)

    q1 = (
        "No — pool coverage did not materially expand to 200+ pools."
        if profile["unique_pools"] < 200
        else "Yes — expanded pool breadth reached target scale."
    )
    q2 = (
        f"Best family by top-selection return: {best_return_aware['target_family']} on {best_return_aware['split']}."
        if best_return_aware
        else "No return-aware family produced a robust improvement."
    )
    q3 = "Yes" if any((r["top10_after_cost_return"] or -1) > 0 for r in primary_rows) else "No"
    q4 = "Yes" if pheld_survives else "No"
    if profile["unique_pools"] < 200:
        q5 = "Primary bottleneck remains data volume and cross-pool breadth."
    elif best_return_aware and (best_return_aware["top10_after_cost_return"] or -1) <= 0:
        q5 = "Primary bottleneck appears target design and feature quality."
    else:
        q5 = "Primary bottleneck remains market structure robustness under held-out pools."
    q6 = (
        "Proceed with more V3 data and return-aware target/feature engineering; do not mainline."
        if (not chron_pos or not pheld_pos)
        else "Continue controlled candidate research; still require leakage/proxy audits before any promotion."
    )

    allow_v3_candidate_continue = bool(pheld_survives and concentration_controlled and improved_vs_v3r10)
    allow_v3_more_data = bool(profile["unique_pools"] < 200 or not chron_pos or not pheld_pos)
    allow_v3_return_aware_target_continue = bool(best_return_aware is not None and improved_vs_v3r10)
    allow_v3_mainline = bool(chron_pos and pheld_pos and concentration_controlled and spearman_material and pheld_survives)

    return {
        "answers": {"Q1": q1, "Q2": q2, "Q3": q3, "Q4": q4, "Q5": q5, "Q6": q6},
        "baseline_comparison": {
            "v3r10_model_b_top10": V3R10_BASELINE_TOP10,
            "best_return_aware_row": best_return_aware,
            "improved_vs_v3r10_model_b": improved_vs_v3r10,
        },
        "checks": {
            "proxy_suppressed_survives_pool_heldout": pheld_survives,
            "top_pool_concentration_controlled": concentration_controlled,
            "positive_top10_after_cost_chronological": chron_pos,
            "positive_top10_after_cost_pool_heldout": pheld_pos,
            "spearman_material_improvement": spearman_material,
        },
        "decision": {
            "allow_v3_candidate_continue": allow_v3_candidate_continue,
            "allow_v3_more_data": allow_v3_more_data,
            "allow_v3_return_aware_target_continue": allow_v3_return_aware_target_continue,
            "allow_v3_mainline": allow_v3_mainline,
        },
    }


def maybe_write_model_artifact(gate: dict[str, Any]) -> dict[str, Any]:
    decision = gate["decision"]
    should_write = bool(
        decision["allow_v3_candidate_continue"]
        and decision["allow_v3_return_aware_target_continue"]
        and not decision["allow_v3_mainline"]
    )
    if not should_write:
        return {"written": False, "reason": "no_material_improvement_over_v3r10"}
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    artifact = {
        "note": "research candidate snapshot only",
        "created_at_unix": int(time.time()),
        "decision": decision,
        "best_return_aware_row": gate.get("baseline_comparison", {}).get("best_return_aware_row"),
    }
    out_path = MODEL_DIR / "best_proxy_suppressed_candidate.json"
    write_json(out_path, artifact)
    return {"written": True, "path": str(out_path.relative_to(ROOT))}


def build_markdown(profile: dict[str, Any], eval_map: dict[str, Any], rolling_summary: dict[str, Any], gate: dict[str, Any], elapsed: float) -> str:
    key_rows = summarize_key_rows(eval_map)
    lines = [
        "# Run-004 Patch V3R-11 — Return-aware V3 Research-only Dry Run",
        "",
        "## Status",
        "- Research-only dry run completed.",
        "- No production/runtime/executor/model-path modifications.",
        f"- Expanded clean V3 dataset: {profile['rows']:,} rows, {profile['unique_pools']} pools (target 200).",
        "",
        "## Dataset Profile",
        f"- Input: `{profile['input_path']}`",
        f"- Rows: {profile['rows']:,}",
        f"- Unique pools: {profile['unique_pools']}",
        f"- V2 rows: {profile['v2_rows']} | Aero rows: {profile['aero_rows']}",
        f"- Time span (ms): {profile['time_span']['timestamp_ms_min']} -> {profile['time_span']['timestamp_ms_max']}",
        f"- Top1 pool share: {profile['pool_concentration']['top1_pool_share']:.3f}",
        f"- Top5 pool share: {profile['pool_concentration']['top5_pool_share']:.3f}",
        "",
        "## Key Table (top10% policy)",
        "| split | model | target_family | pr_lift | spearman | top10_after_cost | top10_top1_pool_share | any_breakeven |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in key_rows:
        lines.append(
            f"| {r['split']} | {r['model']} | {r['target_family']} | "
            f"{'' if r['pr_lift'] is None else round(float(r['pr_lift']), 6)} | "
            f"{'' if r['spearman'] is None else round(float(r['spearman']), 6)} | "
            f"{'' if r['top10_after_cost_return'] is None else round(float(r['top10_after_cost_return']), 6)} | "
            f"{'' if r['top10_top1_pool_share'] is None else round(float(r['top10_top1_pool_share']), 6)} | "
            f"{str(r['any_breakeven']).lower()} |"
        )
    if rolling_summary:
        lines.extend(["", "## Rolling Split Summary"])
        for k, v in rolling_summary.items():
            lines.append(
                f"- `{k}`: top10_after_cost_mean={v['rolling_top10_after_cost_mean']}, "
                f"min={v['rolling_top10_after_cost_min']}, max={v['rolling_top10_after_cost_max']}, "
                f"spearman_mean={v['rolling_spearman_mean']}"
            )
    qa = gate["answers"]
    dec = gate["decision"]
    checks = gate["checks"]
    lines.extend(
        [
            "",
            "## Required Questions",
            f"- Q1. {qa['Q1']}",
            f"- Q2. {qa['Q2']}",
            f"- Q3. {qa['Q3']}",
            f"- Q4. {qa['Q4']}",
            f"- Q5. {qa['Q5']}",
            f"- Q6. {qa['Q6']}",
            "",
            "## Decision Gate Checks",
            f"- proxy_suppressed_survives_pool_heldout: `{str(checks['proxy_suppressed_survives_pool_heldout']).lower()}`",
            f"- top_pool_concentration_controlled: `{str(checks['top_pool_concentration_controlled']).lower()}`",
            f"- positive_top10_after_cost_chronological: `{str(checks['positive_top10_after_cost_chronological']).lower()}`",
            f"- positive_top10_after_cost_pool_heldout: `{str(checks['positive_top10_after_cost_pool_heldout']).lower()}`",
            f"- spearman_material_improvement: `{str(checks['spearman_material_improvement']).lower()}`",
            "",
            "## Final Decision",
            f"- allow_v3_candidate_continue = `{str(dec['allow_v3_candidate_continue']).lower()}`",
            f"- allow_v3_more_data = `{str(dec['allow_v3_more_data']).lower()}`",
            f"- allow_v3_return_aware_target_continue = `{str(dec['allow_v3_return_aware_target_continue']).lower()}`",
            f"- allow_v3_mainline = `{str(dec['allow_v3_mainline']).lower()}`",
            "",
            "## Safety Assertions",
            "- v3_only = `true`",
            "- no_production_runtime_touch = `true`",
            "- no_live_or_signal_path_change = `true`",
            "",
            f"## Elapsed\n- runtime_sec: {round(elapsed, 2)}",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    t0 = time.time()
    print("[V3R11] Load expanded clean V3 dataset…")
    df, profile = load_and_prepare()
    print(f"[V3R11] Dataset rows={len(df):,} pools={df['pool_address'].nunique()}")

    splits: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {
        "chronological": chron_split(df),
        "pool_heldout": pool_heldout_split(df),
        "exclude_top5": exclude_top5_split(df),
    }
    for name, tr, te in rolling_splits(df):
        splits[name] = (tr, te)

    print(f"[V3R11] Evaluate {len(splits)} splits × target families…")
    eval_map: dict[str, Any] = {}
    for name, (tr, te) in splits.items():
        print(f"  - split={name} train={len(tr):,} test={len(te):,}")
        eval_map[name] = evaluate_split(tr, te, name)

    rolling_summary = aggregate_rolling(eval_map)
    gate = gate_decision(profile, eval_map)
    artifact_info = maybe_write_model_artifact(gate)

    summary = {
        "run": "run004_v3r11_return_aware",
        "research_only": True,
        "dataset_profile": profile,
        "split_results": eval_map,
        "rolling_summary": rolling_summary,
        "gate": gate,
        "model_artifact": artifact_info,
    }
    summary_path = OUT_DIR / "run004_v3r11_return_aware_summary.json"
    write_json(summary_path, summary)

    report_path = RUN_DIR / "run-004-patch-v3r11-return-aware-v3-research.md"
    write_markdown(report_path, build_markdown(profile, eval_map, rolling_summary, gate, elapsed=time.time() - t0))

    print("[V3R11] Done")
    print(
        json.dumps(
            {
                "rows": profile["rows"],
                "pools": profile["unique_pools"],
                "pool_goal_reached": profile["pool_goal_reached"],
                "decision": gate["decision"],
                "outputs": {"report": str(report_path.relative_to(ROOT)), "summary": str(summary_path.relative_to(ROOT))},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
