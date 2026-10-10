"""
Run-004 Patch G2: V2 Pool-Invariant Feature Redesign.

Reads  logs/run004_v2only_prepared_dryrun_canonical.csv
Writes mvp/diagnostics/run004_patch_g2_pool_invariant_feature_report.json
       mvp/runs/run-004-patch-g2-pool-invariant-feature-redesign.md
       logs/run004_patch_g2_holdout_predictions.csv
       models/signal_model_v2only_run004_patch_g2_dryrun.json

Hard prohibitions (enforced in-script):
  - No LIVE_TRADING_MODE
  - No model promotion / no overwrite of production model
  - net_return / entry_block / exit_block never used as training features
  - No pool_train_rate, pool identity, token identity, or target encoding
  - No time-pattern features (hour, dow, sin/cos cycles)
  - No V3/Aero data (pool_type == v2 enforced)
  - Strict time-series split, no random split
  - No RPC calls
  - No hyperparameter tuning to rescue holdout

Patch G1 B7 baseline (reference):
  ROC-AUC=0.5393  PR-AUC=0.4907  top_dec_pos=0.5358
  top_dec_med_nr=+0.012  top1_pool_share=0.332
  remove_top5_roc=0.5419  remove_top5_top_dec_pos=0.5575

Env:
  RUN004_G2_CANONICAL_CSV  default logs/run004_v2only_prepared_dryrun_canonical.csv
  RUN004_G2_HOLDOUT_LO_MS  default 1776520584003
  RUN004_G2_SEED           default 42
  RUN004_G2_N_BOOTSTRAP    default 500
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on",
), "LIVE_TRADING_MODE must not be enabled during Patch G2"

import numpy as np

try:
    import xgboost as xgb
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False

try:
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.ensemble import ExtraTreesClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import (
        roc_auc_score, average_precision_score,
        log_loss, brier_score_loss,
        precision_recall_fscore_support, accuracy_score,
        confusion_matrix,
    )
    _HAS_SKL = True
except ImportError:
    _HAS_SKL = False

if not (_HAS_XGB and _HAS_SKL):
    print("ERROR: xgboost and scikit-learn are required", file=sys.stderr)
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────
HOLDOUT_LO_MS_DEFAULT = 1776520584003

# Patch G1 B7 reference metrics (frozen)
G1_B7_BASELINE = {
    "holdout_roc_auc": 0.53928,
    "holdout_pr_auc": 0.490749,
    "top_decile_pos_rate": 0.535761,
    "top_decile_median_nr": 0.012233,
    "top_decile_top1_pool_share": 0.331599,
    "remove_top5_roc_auc": 0.541857,
    "remove_top5_top_dec_pos": 0.557471,
}

# Feature classification
ALLOWED_POOL_INVARIANT_COLUMNS = {"final_score", "log_index"}
BANNED_POOL_IDENTITY = {"pool_address", "token_address", "pool_train_rate", "pool_train_log_count"}
BANNED_TIME_PATTERN = {"hour_of_day", "day_of_week", "sin_hour", "cos_hour", "sin_dow", "cos_dow", "timestamp"}
BANNED_TARGET_ENCODED = {"pool_train_rate", "pool_train_log_count"}
BANNED_FUTURE_LEAKAGE = {
    "net_return", "entry_block", "exit_block", "label", "label_reason",
    "candidate_status", "reject_reason", "pre_check", "relabel_status",
    "rpc_calls_this_key", "source_row_id", "gate_status",
}
META_COLUMNS = {"tx_hash", "topic0", "event_kind", "pool_type"}

# Leakage guard
LEAKAGE_GUARD = BANNED_FUTURE_LEAKAGE | BANNED_POOL_IDENTITY | BANNED_TIME_PATTERN


# ── Helpers ────────────────────────────────────────────────────────────

def _pct(sv: list[float], q: float) -> float:
    if not sv:
        return float("nan")
    n = len(sv)
    k = (n - 1) * q
    lo, hi = int(k), min(int(k) + 1, n - 1)
    return sv[lo] * (1 - (k - lo)) + sv[hi] * (k - lo)


def _safe(v: Any, nd: int = 6) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), nd)
    return v


def _parse_rows(p: Path) -> list[dict[str, str]]:
    with p.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _rank_pct(values: np.ndarray, sorted_ref: np.ndarray) -> np.ndarray:
    """Map each value to its percentile rank in sorted_ref using binary search."""
    n = len(sorted_ref)
    if n == 0:
        return np.full(len(values), 0.5)
    ranks = np.array([bisect.bisect_left(sorted_ref, float(v)) for v in values], dtype=np.float32)
    return ranks / n


def _logit(x: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    x = np.clip(x, eps, 1.0 - eps)
    return np.log(x / (1.0 - x)).astype(np.float32)


# ── Step A: Schema inspection ──────────────────────────────────────────

def inspect_schema(rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        return {}
    all_cols = list(rows[0].keys())
    schema: dict[str, list[str]] = {
        "allowed_pool_invariant": [],
        "banned_pool_identity": [],
        "banned_time_pattern": [],
        "banned_target_encoded": [],
        "banned_future_or_label_leakage": [],
        "meta_operational": [],
        "unknown_needs_review": [],
    }
    for col in all_cols:
        if col in BANNED_FUTURE_LEAKAGE:
            schema["banned_future_or_label_leakage"].append(col)
        elif col in BANNED_TARGET_ENCODED:
            schema["banned_target_encoded"].append(col)
        elif col in BANNED_POOL_IDENTITY:
            schema["banned_pool_identity"].append(col)
        elif col in BANNED_TIME_PATTERN:
            schema["banned_time_pattern"].append(col)
        elif col in META_COLUMNS:
            schema["meta_operational"].append(col)
        elif col in ALLOWED_POOL_INVARIANT_COLUMNS:
            schema["allowed_pool_invariant"].append(col)
        else:
            schema["unknown_needs_review"].append(col)
    # Numeric stats for allowed columns
    stats: dict[str, Any] = {}
    for col in schema["allowed_pool_invariant"]:
        vals = [float(r.get(col, 0) or 0) for r in rows]
        sv = sorted(vals)
        n = len(sv)
        stats[col] = {
            "min": _safe(sv[0]),
            "p01": _safe(_pct(sv, 0.01)),
            "p10": _safe(_pct(sv, 0.10)),
            "p25": _safe(_pct(sv, 0.25)),
            "median": _safe(_pct(sv, 0.50)),
            "p75": _safe(_pct(sv, 0.75)),
            "p90": _safe(_pct(sv, 0.90)),
            "p99": _safe(_pct(sv, 0.99)),
            "max": _safe(sv[-1]),
            "mean": _safe(sum(vals) / n),
            "n_negative": sum(1 for v in vals if v < 0),
        }
    schema["numeric_stats"] = stats
    schema["note"] = (
        "Only 'final_score' and 'log_index' are available as pool-invariant numeric features. "
        "Heuristic sub-scores (whale_val, momentum_val, activity_val, risk_val), amount_usd, "
        "side, buy_pressure_ratio etc. are NOT present in this CSV. "
        "Feature engineering will derive additional representations from these two base features."
    )
    return schema


# ── Step B: Feature engineering ────────────────────────────────────────

def compute_train_stats(train_rows: list[dict[str, str]]) -> dict[str, Any]:
    """Compute all distribution stats needed for consistent holdout transformation."""
    fs = np.array([float(r.get("final_score", 0) or 0) for r in train_rows], dtype=np.float64)
    li = np.array([float(r.get("log_index", 0) or 0) for r in train_rows], dtype=np.float64)
    fs_sorted = np.sort(fs)
    li_sorted = np.sort(li)
    return {
        "fs_sorted": fs_sorted,
        "li_sorted": li_sorted,
        "fs_p01": float(np.percentile(fs, 1)),
        "fs_p10": float(np.percentile(fs, 10)),
        "fs_p25": float(np.percentile(fs, 25)),
        "fs_p75": float(np.percentile(fs, 75)),
        "fs_p90": float(np.percentile(fs, 90)),
        "fs_p99": float(np.percentile(fs, 99)),
        "fs_min": float(fs.min()),
        "fs_max": float(fs.max()),
        "li_p25": float(np.percentile(li, 25)),
        "li_p75": float(np.percentile(li, 75)),
        "li_p99": float(np.percentile(li, 99)),
        "li_min": float(li.min()),
        "li_max": float(li.max()),
    }


ENGINEERED_FEATURE_NAMES = [
    # Base
    "final_score",
    "log_index_log1p",
    "log_index",
    # final_score derived
    "final_score_logit",        # logit(final_score) — linearizes bounded score
    "final_score_rank_pct",     # percentile rank in training distribution
    "final_score_sq",           # quadratic term for non-linearity
    "fs_neg",                   # 1 - final_score (flipped for linear models)
    "is_low_fs_p10",            # binary: final_score < training p10
    "is_low_fs_p25",            # binary: final_score < training p25
    # log_index derived
    "log_index_rank_pct",       # percentile rank in training distribution
    "log_index_sq_log1p",       # log1p(log_index^2) for heavy tail
    "is_low_li_p25",            # binary: log_index < training p25
    # Interaction terms
    "fs_x_li_log1p",            # final_score * log_index_log1p
    "fs_logit_x_li_log1p",      # final_score_logit * log_index_log1p
    "fs_neg_x_li_log1p",        # (1-final_score) * log_index_log1p
]


def build_features(
    rows: list[dict[str, str]],
    ts: dict[str, Any],
) -> np.ndarray:
    """Build full engineered feature matrix using pre-computed training stats."""
    fs_sorted = ts["fs_sorted"]
    li_sorted = ts["li_sorted"]

    X: list[list[float]] = []
    for r in rows:
        fs = float(r.get("final_score", 0) or 0)
        li = float(r.get("log_index", 0) or 0)
        li_log1p = math.log1p(li)

        fs_logit = float(_logit(np.array([fs]))[0])
        fs_rank_pct = float(_rank_pct(np.array([fs]), fs_sorted)[0])
        li_rank_pct = float(_rank_pct(np.array([li]), li_sorted)[0])

        X.append([
            fs,
            li_log1p,
            li,
            fs_logit,
            fs_rank_pct,
            fs * fs,
            1.0 - fs,
            1.0 if fs < ts["fs_p10"] else 0.0,
            1.0 if fs < ts["fs_p25"] else 0.0,
            li_rank_pct,
            math.log1p(li * li),
            1.0 if li < ts["li_p25"] else 0.0,
            fs * li_log1p,
            fs_logit * li_log1p,
            (1.0 - fs) * li_log1p,
        ])
    return np.array(X, dtype=np.float32)


# Base feature set — matches G1 B7 for direct comparison
BASE_FEATURE_NAMES = ["final_score", "log_index", "log_index_log1p"]


def build_base_features(rows: list[dict[str, str]]) -> np.ndarray:
    X = []
    for r in rows:
        fs = float(r.get("final_score", 0) or 0)
        li = float(r.get("log_index", 0) or 0)
        X.append([fs, li, math.log1p(li)])
    return np.array(X, dtype=np.float32)


# ── Metrics ────────────────────────────────────────────────────────────

def _metrics(y_true: np.ndarray, y_prob: np.ndarray, thr: float = 0.5) -> dict[str, Any]:
    y_pred = (y_prob >= thr).astype(int)
    prec, rec, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="binary", zero_division=0)
    try:
        roc = roc_auc_score(y_true, y_prob)
    except Exception:
        roc = float("nan")
    try:
        prauc = average_precision_score(y_true, y_prob)
    except Exception:
        prauc = float("nan")
    try:
        ll = log_loss(y_true, y_prob, eps=1e-7)
    except Exception:
        ll = float("nan")
    try:
        bs = brier_score_loss(y_true, y_prob)
    except Exception:
        bs = float("nan")
    return {
        "accuracy": _safe(accuracy_score(y_true, y_pred)),
        "precision": _safe(float(prec)),
        "recall": _safe(float(rec)),
        "f1": _safe(float(f1)),
        "roc_auc": _safe(roc),
        "pr_auc": _safe(prauc),
        "log_loss": _safe(ll),
        "brier_score": _safe(bs),
        "confusion_matrix": confusion_matrix(y_true, y_pred).tolist(),
        "threshold": thr,
        "n_positive_predicted": int(y_pred.sum()),
    }


def _decile_analysis(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    nr: np.ndarray,
    pool_addrs: list[str],
) -> dict[str, Any]:
    idx = np.argsort(y_prob)[::-1]
    n = len(idx)
    n_dec = max(1, n // 10)
    n_qnt = max(1, n // 5)

    def _st(sel: np.ndarray) -> dict[str, Any]:
        yt = y_true[sel]; nrr = nr[sel]
        pa = [pool_addrs[i] for i in sel]
        pc = Counter(pa)
        top1 = pc.most_common(1)[0][1] / max(1, len(sel)) if pc else 0.0
        nrs = sorted(nrr.tolist())
        return {
            "n": int(len(sel)),
            "pos_rate": _safe(float(yt.mean())),
            "avg_net_return": _safe(float(nrr.mean())),
            "median_net_return": _safe(float(np.median(nrr))),
            "p25_net_return": _safe(_pct(nrs, 0.25)),
            "top1_pool_share": _safe(top1),
        }

    return {
        "top_decile": _st(idx[:n_dec]),
        "top_quintile": _st(idx[:n_qnt]),
        "bottom_decile": _st(idx[-n_dec:]),
        "all": _st(idx),
    }


def _calibration_quantile(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    min_bucket_size: int = 100,
) -> tuple[list[dict[str, Any]], float, float]:
    """Quantile calibration: equal-sized buckets by predicted probability.
    Returns (buckets, ECE, max_deviation)."""
    n = len(y_true)
    n_buckets = max(1, n // min_bucket_size)
    idx = np.argsort(y_prob)
    bucket_size = max(1, n // n_buckets)

    buckets = []
    total_ece = 0.0
    max_dev = 0.0
    for i in range(0, n, bucket_size):
        sel = idx[i: i + bucket_size]
        if len(sel) < min(20, min_bucket_size // 5):
            continue
        mp = float(y_prob[sel].mean())
        ar = float(y_true[sel].mean())
        dev = abs(mp - ar)
        total_ece += dev * len(sel)
        max_dev = max(max_dev, dev)
        buckets.append({
            "bucket_rank": len(buckets),
            "n": len(sel),
            "mean_pred": _safe(mp),
            "actual_pos_rate": _safe(ar),
            "deviation": _safe(dev),
        })
    ece = total_ece / n if n > 0 else 1.0
    return buckets, _safe(ece), _safe(max_dev)


def _sensitivity_remove_pools(
    y_all: np.ndarray,
    y_prob: np.ndarray,
    nr: np.ndarray,
    pool_addrs: list[str],
    top_n: int,
) -> dict[str, Any]:
    pc = Counter(pool_addrs)
    top_pools = {p for p, _ in pc.most_common(top_n)}
    mask = np.array([p not in top_pools for p in pool_addrs])
    n_rem = int(mask.sum())
    if n_rem < 20:
        return {"n_remaining": n_rem, "roc_auc": None, "top_decile_pos_rate": None,
                "top_decile_avg_net_return": None, "top1_pool_share_in_top_decile": None}
    yt = y_all[mask]; yp = y_prob[mask]; nrr = nr[mask]
    pa = [pool_addrs[i] for i in range(len(pool_addrs)) if mask[i]]
    idx = np.argsort(yp)[::-1]
    n_dec = max(1, len(idx) // 10)
    try:
        rauc = _safe(roc_auc_score(yt, yp))
    except Exception:
        rauc = None
    top_pa = [pa[i] for i in idx[:n_dec]]
    return {
        "n_remaining": n_rem,
        "roc_auc": rauc,
        "top_decile_pos_rate": _safe(float(yt[idx[:n_dec]].mean())),
        "top_decile_avg_net_return": _safe(float(nrr[idx[:n_dec]].mean())),
        "top1_pool_share_in_top_decile": _safe(
            Counter(top_pa).most_common(1)[0][1] / max(1, n_dec) if top_pa else 0.0
        ),
    }


# ── Model trainers ─────────────────────────────────────────────────────

def _train_xgb_clf(
    X: np.ndarray, y: np.ndarray,
    n_trees: int, max_depth: int, lr: float = 0.05,
    seed: int = 42, spw: float = 1.0,
) -> "xgb.XGBClassifier":
    m = xgb.XGBClassifier(
        n_estimators=n_trees, max_depth=max_depth, learning_rate=lr,
        subsample=0.8, colsample_bytree=0.8,
        min_child_weight=3, gamma=0.05,
        reg_alpha=0.1, reg_lambda=1.0,
        scale_pos_weight=spw,
        objective="binary:logistic", eval_metric="logloss",
        random_state=seed, n_jobs=2, verbosity=0,
    )
    m.fit(X, y)
    return m


def _train_xgb_reg(
    X: np.ndarray, y: np.ndarray,
    n_trees: int, max_depth: int, lr: float = 0.05,
    seed: int = 42,
) -> "xgb.XGBRegressor":
    m = xgb.XGBRegressor(
        n_estimators=n_trees, max_depth=max_depth, learning_rate=lr,
        subsample=0.8, colsample_bytree=0.8,
        min_child_weight=3, gamma=0.05,
        reg_alpha=0.1, reg_lambda=1.0,
        objective="reg:squarederror",
        random_state=seed, n_jobs=2, verbosity=0,
    )
    m.fit(X, y)
    return m


def _train_logreg(
    X: np.ndarray, y: np.ndarray, seed: int = 42,
) -> tuple["LogisticRegression", "StandardScaler"]:
    sc = StandardScaler()
    Xs = sc.fit_transform(X)
    m = LogisticRegression(max_iter=2000, random_state=seed, C=1.0, solver="lbfgs")
    m.fit(Xs, y)
    return m, sc


def _train_extratrees(
    X: np.ndarray, y: np.ndarray, seed: int = 42,
) -> "ExtraTreesClassifier":
    m = ExtraTreesClassifier(
        n_estimators=100, max_depth=3, min_samples_leaf=20,
        random_state=seed, n_jobs=2,
    )
    m.fit(X, y)
    return m


# ── Permutation importance ──────────────────────────────────────────────

def _permutation_importance(
    model_predict_fn,
    X_ho: np.ndarray,
    y_ho: np.ndarray,
    feat_names: list[str],
    base_roc: float,
    seed: int = 42,
    n_repeats: int = 5,
) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    result: dict[str, float] = {}
    for i, fname in enumerate(feat_names):
        drops = []
        for _ in range(n_repeats):
            X_perm = X_ho.copy()
            perm_idx = rng.permutation(len(X_perm))
            X_perm[:, i] = X_perm[perm_idx, i]
            try:
                p_perm = model_predict_fn(X_perm)
                r = roc_auc_score(y_ho, p_perm)
                drops.append(base_roc - r)
            except Exception:
                drops.append(0.0)
        result[fname] = _safe(float(np.mean(drops)))
    return dict(sorted(result.items(), key=lambda x: -(x[1] or 0)))


# ── Bootstrap CI ───────────────────────────────────────────────────────

def _bootstrap_top_decile_pos(
    y_ho: np.ndarray,
    y_prob: np.ndarray,
    n_iter: int = 500,
    seed: int = 42,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    samples = []
    n = len(y_ho)
    for _ in range(n_iter):
        idx = rng.integers(0, n, size=n)
        yb = y_ho[idx]; pb = y_prob[idx]
        si = np.argsort(pb)[::-1]
        nd = max(1, n // 10)
        samples.append(float(yb[si[:nd]].mean()))
    samples_sorted = sorted(samples)
    return {
        "n_bootstrap": n_iter,
        "mean": _safe(float(np.mean(samples))),
        "std": _safe(float(np.std(samples))),
        "p025": _safe(_pct(samples_sorted, 0.025)),
        "p50": _safe(_pct(samples_sorted, 0.50)),
        "p975": _safe(_pct(samples_sorted, 0.975)),
    }


# ── Permutation label sanity check ─────────────────────────────────────

def _permutation_label_sanity(
    X_tr: np.ndarray, y_tr: np.ndarray,
    X_ho: np.ndarray, y_ho: np.ndarray,
    spw: float, seed: int,
    n_repeats: int = 3,
) -> dict[str, Any]:
    """Shuffle y_train n_repeats times, retrain, check holdout ROC ~ 0.5."""
    rng = np.random.default_rng(seed + 9999)
    rocs = []
    for _ in range(n_repeats):
        y_shuf = rng.permutation(y_tr)
        m = _train_xgb_clf(X_tr, y_shuf, n_trees=50, max_depth=2,
                           lr=0.05, seed=seed, spw=spw)
        p = m.predict_proba(X_ho)[:, 1]
        try:
            rocs.append(float(roc_auc_score(y_ho, p)))
        except Exception:
            rocs.append(0.5)
    return {
        "n_repeats": n_repeats,
        "roc_auc_shuffled": [_safe(r) for r in rocs],
        "mean_roc": _safe(float(np.mean(rocs))),
        "interpretation": "Should be ~0.5 if model is not memorizing artifacts",
        "pass": all(abs(r - 0.5) < 0.05 for r in rocs),
    }


# ── Experiment runner ───────────────────────────────────────────────────

def run_clf_experiment(
    name: str,
    label: str,
    feat_names: list[str],
    X_tr: np.ndarray, y_tr: np.ndarray,
    X_ho: np.ndarray, y_ho: np.ndarray,
    nr_ho: np.ndarray, pool_ho: list[str],
    spw: float, seed: int,
    model_type: str,
    n_trees: int = 100, max_depth: int = 2, lr: float = 0.05,
    do_perm_importance: bool = False,
) -> dict[str, Any]:
    print(f"  [{name}] {model_type} n_feat={len(feat_names)} n_trees={n_trees if model_type.startswith('xgb') else '-'} depth={max_depth}")

    X_tr = np.nan_to_num(X_tr, nan=0.0, posinf=0.0, neginf=0.0)
    X_ho = np.nan_to_num(X_ho, nan=0.0, posinf=0.0, neginf=0.0)

    predict_fn = None
    fi: dict[str, float] | None = None
    coef: dict[str, float] | None = None

    if model_type == "xgb":
        m = _train_xgb_clf(X_tr, y_tr, n_trees, max_depth, lr, seed, spw)
        p_ho = m.predict_proba(X_ho)[:, 1]
        p_tr = m.predict_proba(X_tr)[:, 1]
        predict_fn = lambda Xq: m.predict_proba(Xq)[:, 1]
        fi_raw = m.feature_importances_
        fi = {feat_names[i]: _safe(float(fi_raw[i])) for i in range(len(feat_names))}
    elif model_type == "logreg":
        m, sc = _train_logreg(X_tr, y_tr, seed)
        p_ho = m.predict_proba(sc.transform(X_ho))[:, 1]
        p_tr = m.predict_proba(sc.transform(X_tr))[:, 1]
        predict_fn = lambda Xq: m.predict_proba(sc.transform(Xq))[:, 1]
        coef = {feat_names[i]: _safe(float(m.coef_[0][i])) for i in range(len(feat_names))}
    elif model_type == "extratrees":
        m = _train_extratrees(X_tr, y_tr, seed)
        p_ho = m.predict_proba(X_ho)[:, 1]
        p_tr = m.predict_proba(X_tr)[:, 1]
        predict_fn = lambda Xq: m.predict_proba(Xq)[:, 1]
        fi_raw = m.feature_importances_
        fi = {feat_names[i]: _safe(float(fi_raw[i])) for i in range(len(feat_names))}
    else:
        raise ValueError(f"Unknown model_type: {model_type}")

    m_ho = _metrics(y_ho, p_ho)
    m_tr = _metrics(y_tr, p_tr)
    dec = _decile_analysis(y_ho, p_ho, nr_ho, pool_ho)
    sens1 = _sensitivity_remove_pools(y_ho, p_ho, nr_ho, pool_ho, top_n=1)
    sens5 = _sensitivity_remove_pools(y_ho, p_ho, nr_ho, pool_ho, top_n=5)
    cal_buckets, ece, cal_max_dev = _calibration_quantile(y_ho, p_ho, min_bucket_size=100)

    perm_imp: dict[str, float] | None = None
    if do_perm_importance and predict_fn is not None:
        base_roc = m_ho.get("roc_auc") or 0.5
        perm_imp = _permutation_importance(predict_fn, X_ho, y_ho, feat_names, base_roc, seed)

    roc = m_ho.get("roc_auc") or 0.0
    print(f"  [{name}] ROC={roc:.4f} PR={m_ho.get('pr_auc'):.4f} "
          f"top_dec_pos={dec['top_decile']['pos_rate']} "
          f"top1_pool={dec['top_decile']['top1_pool_share']} "
          f"ECE={ece}")

    return {
        "name": name,
        "label": label,
        "model_type": model_type,
        "features": feat_names,
        "n_features": len(feat_names),
        "hyperparams": {"n_trees": n_trees, "max_depth": max_depth, "lr": lr},
        "holdout_metrics": m_ho,
        "train_metrics": m_tr,
        "train_holdout_roc_gap": _safe((m_tr.get("roc_auc") or 0.0) - roc),
        "decile_analysis": dec,
        "sensitivity": {
            "remove_top1_pool": sens1,
            "remove_top5_pools": sens5,
        },
        "calibration_quantile_buckets": cal_buckets,
        "ece": ece,
        "calibration_max_deviation": cal_max_dev,
        "feature_importance": fi,
        "feature_coefficients": coef,
        "permutation_importance": perm_imp,
        "y_prob": p_ho,  # kept in memory; stripped before JSON
    }


# ── Regression auxiliary experiment ────────────────────────────────────

def run_regression_experiment(
    feat_names: list[str],
    X_tr: np.ndarray, nr_tr: np.ndarray,
    X_ho: np.ndarray, y_ho: np.ndarray, nr_ho: np.ndarray,
    pool_ho: list[str],
    seed: int,
    clip_nr_max: float = 5.0,
) -> dict[str, Any]:
    """Train XGB regressor on clipped net_return; use predictions as ranker."""
    print("  [C6_xgb_regression_aux] training regression on clipped net_return")
    X_tr = np.nan_to_num(X_tr, nan=0.0, posinf=0.0, neginf=0.0)
    X_ho = np.nan_to_num(X_ho, nan=0.0, posinf=0.0, neginf=0.0)

    # Target: clip net_return, don't cap negative side (losses capped at -1 naturally)
    nr_clipped = np.clip(nr_tr, -1.0, clip_nr_max).astype(np.float32)
    m = _train_xgb_reg(X_tr, nr_clipped, n_trees=100, max_depth=2, lr=0.05, seed=seed)

    pred_tr = m.predict(X_tr).astype(np.float64)
    pred_ho = m.predict(X_ho).astype(np.float64)

    # Use raw prediction value as ranking score for classification metrics
    # Normalize predictions to [0,1] range for compatibility
    p_min, p_max = pred_ho.min(), pred_ho.max()
    if p_max > p_min:
        p_norm = (pred_ho - p_min) / (p_max - p_min)
    else:
        p_norm = np.full_like(pred_ho, 0.5)

    try:
        roc = _safe(roc_auc_score(y_ho, pred_ho))
    except Exception:
        roc = None
    try:
        prauc = _safe(average_precision_score(y_ho, p_norm))
    except Exception:
        prauc = None

    dec = _decile_analysis(y_ho, pred_ho, nr_ho, pool_ho)
    sens5 = _sensitivity_remove_pools(y_ho, pred_ho, nr_ho, pool_ho, top_n=5)
    cal_buckets, ece, cal_max_dev = _calibration_quantile(y_ho, p_norm, min_bucket_size=100)
    fi_raw = m.feature_importances_
    fi = {feat_names[i]: _safe(float(fi_raw[i])) for i in range(len(feat_names))}

    # Rank-correlation of predictions with actual net_return
    from scipy.stats import spearmanr
    try:
        sp_corr, sp_pval = spearmanr(pred_ho, nr_ho)
        sp_corr = _safe(float(sp_corr))
    except Exception:
        sp_corr, sp_pval = None, None

    print(f"  [C6_xgb_regression_aux] ROC={roc} top_dec_pos={dec['top_decile']['pos_rate']} spearman_corr={sp_corr}")

    return {
        "name": "C6_xgb_regression_aux",
        "label": "XGB regressor on clipped net_return; predictions used as ranking score",
        "model_type": "xgb_regressor",
        "features": feat_names,
        "n_features": len(feat_names),
        "target": f"clip(net_return, -1, {clip_nr_max})",
        "roc_auc_of_regression_ranker": roc,
        "pr_auc_normalized": prauc,
        "decile_analysis": dec,
        "sensitivity": {"remove_top5_pools": sens5},
        "calibration_max_deviation": cal_max_dev,
        "ece": ece,
        "feature_importance": fi,
        "spearman_corr_pred_vs_actual_nr": sp_corr,
        "interpretation": (
            "If ROC > 0.5, net_return regression has some ranking power. "
            "If Spearman > 0, predictions weakly order actual returns. "
            "This is an auxiliary diagnostic, not a production model."
        ),
        "y_prob": p_norm,
    }


# ── Seen vs unseen pool analysis ────────────────────────────────────────

def _seen_unseen_analysis(
    y_ho: np.ndarray,
    y_prob: np.ndarray,
    nr_ho: np.ndarray,
    pool_ho: list[str],
    seen_pools: set[str],
) -> dict[str, Any]:
    seen_mask = np.array([p in seen_pools for p in pool_ho])
    unseen_mask = ~seen_mask

    def _eval_subset(mask: np.ndarray, tag: str) -> dict[str, Any]:
        n = int(mask.sum())
        if n < 20:
            return {"n": n, "roc_auc": None, "top_decile_pos_rate": None}
        yt = y_ho[mask]; yp = y_prob[mask]; nrr = nr_ho[mask]
        pa = [pool_ho[i] for i in range(len(pool_ho)) if mask[i]]
        idx = np.argsort(yp)[::-1]
        n_dec = max(1, n // 10)
        try:
            roc = _safe(roc_auc_score(yt, yp))
        except Exception:
            roc = None
        try:
            prauc = _safe(average_precision_score(yt, yp))
        except Exception:
            prauc = None
        pc = Counter(pa)
        top1 = pc.most_common(1)[0][1] / max(1, n) if pc else 0.0
        return {
            "n": n,
            "roc_auc": roc,
            "pr_auc": prauc,
            "pos_rate": _safe(float(yt.mean())),
            "top_decile_pos_rate": _safe(float(yt[idx[:n_dec]].mean())),
            "top_decile_avg_net_return": _safe(float(nrr[idx[:n_dec]].mean())),
            "top_decile_median_net_return": _safe(float(np.median(nrr[idx[:n_dec]]))),
            "top1_pool_share": _safe(top1),
        }

    seen_res = _eval_subset(seen_mask, "seen")
    unseen_res = _eval_subset(unseen_mask, "unseen")
    roc_gap = None
    if seen_res.get("roc_auc") and unseen_res.get("roc_auc"):
        roc_gap = _safe(seen_res["roc_auc"] - unseen_res["roc_auc"])

    return {
        "seen": seen_res,
        "unseen": unseen_res,
        "n_seen_pools_in_train": len(seen_pools),
        "roc_gap_seen_minus_unseen": roc_gap,
        "unseen_pool_failure": (
            roc_gap is not None and roc_gap > 0.10
        ),
    }


# ── Gate evaluation ────────────────────────────────────────────────────

def _eval_gates(
    result: dict[str, Any],
    hold_pos_rate: float,
) -> dict[str, Any]:
    m = result["holdout_metrics"]
    dec = result["decile_analysis"]["top_decile"]
    sens5 = result["sensitivity"]["remove_top5_pools"]
    ece_val = result.get("ece") or 1.0

    roc = m.get("roc_auc") or 0.0
    prauc = m.get("pr_auc") or 0.0
    dec_pos = dec.get("pos_rate") or 0.0
    dec_med = dec.get("median_net_return")
    dec_p25 = dec.get("p25_net_return")
    dec_top1 = dec.get("top1_pool_share") or 1.0
    sens5_pos = sens5.get("top_decile_pos_rate") or 0.0

    gates = {
        "roc_auc_ge_055": roc >= 0.55,
        "pr_auc_above_base": prauc > hold_pos_rate,
        "top_decile_pos_rate_ge_base_plus_006": dec_pos >= hold_pos_rate + 0.06,
        "top_decile_median_nr_ge_0": dec_med is not None and dec_med >= 0,
        "top_decile_p25_nr_not_catastrophic": dec_p25 is not None and dec_p25 >= -0.5,
        "remove_top5_dec_pos_above_base": sens5_pos > hold_pos_rate,
        "top_decile_top1_pool_share_le_035": dec_top1 <= 0.35,
        "ece_acceptable": ece_val <= 0.08,
        "no_severe_overfit": (result.get("train_holdout_roc_gap") or 1.0) <= 0.25,
    }
    return {"gates": gates, "all_pass": all(gates.values())}


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    canonical_csv = Path(os.environ.get(
        "RUN004_G2_CANONICAL_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"),
    ))
    holdout_lo_ms = int(os.environ.get("RUN004_G2_HOLDOUT_LO_MS", str(HOLDOUT_LO_MS_DEFAULT)))
    seed = int(os.environ.get("RUN004_G2_SEED", "42"))
    n_bootstrap = int(os.environ.get("RUN004_G2_N_BOOTSTRAP", "500"))

    out_report_json = Path(os.environ.get(
        "RUN004_G2_REPORT_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_g2_pool_invariant_feature_report.json"),
    ))
    out_report_md = Path(os.environ.get(
        "RUN004_G2_REPORT_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-g2-pool-invariant-feature-redesign.md"),
    ))
    out_pred_csv = Path(os.environ.get(
        "RUN004_G2_PRED_CSV",
        str(REPO / "logs" / "run004_patch_g2_holdout_predictions.csv"),
    ))
    out_model_json = Path(os.environ.get(
        "RUN004_G2_MODEL_JSON",
        str(REPO / "models" / "signal_model_v2only_run004_patch_g2_dryrun.json"),
    ))

    if not canonical_csv.is_file():
        print(f"ERROR: missing {canonical_csv}", file=sys.stderr)
        return 1

    print(f"loading {canonical_csv}")
    rows = _parse_rows(canonical_csv)

    # V2-only guard
    non_v2 = [r for r in rows if (r.get("pool_type") or "").lower() != "v2"]
    if non_v2:
        print(f"WARNING: {len(non_v2)} non-v2 rows filtered", file=sys.stderr)
        rows = [r for r in rows if (r.get("pool_type") or "").lower() == "v2"]

    # ── Step A: Schema inspection ──────────────────────────────────────
    print("Step A: schema inspection")
    schema = inspect_schema(rows)
    for cat, cols in schema.items():
        if isinstance(cols, list):
            print(f"  {cat}: {cols}")

    # ── Time-series split ──────────────────────────────────────────────
    train_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) < holdout_lo_ms]
    hold_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) >= holdout_lo_ms]
    print(f"train={len(train_rows)} holdout={len(hold_rows)}")
    if len(train_rows) < 100 or len(hold_rows) < 100:
        print("ERROR: insufficient split", file=sys.stderr)
        return 1

    y_tr = np.array([int(r.get("label", 0) or 0) for r in train_rows], dtype=np.int32)
    y_ho = np.array([int(r.get("label", 0) or 0) for r in hold_rows], dtype=np.int32)
    nr_tr = np.array([float(r.get("net_return", 0) or 0) for r in train_rows], dtype=np.float64)
    nr_ho = np.array([float(r.get("net_return", 0) or 0) for r in hold_rows], dtype=np.float64)
    pool_ho = [(r.get("pool_address") or "").lower() for r in hold_rows]
    pool_tr = [(r.get("pool_address") or "").lower() for r in train_rows]
    seen_pools = set(pool_tr)

    train_pos_rate = float(y_tr.mean())
    hold_pos_rate = float(y_ho.mean())
    spw = (len(y_tr) - y_tr.sum()) / max(1, y_tr.sum())
    print(f"train_pos_rate={train_pos_rate:.4f} hold_pos_rate={hold_pos_rate:.4f} spw={spw:.3f}")

    # ── Step B: Feature engineering ────────────────────────────────────
    print("Step B: feature engineering")
    ts = compute_train_stats(train_rows)

    # Engineered features (full set)
    X_tr_eng = build_features(train_rows, ts)
    X_ho_eng = build_features(hold_rows, ts)

    # Base features (G1 B7 parity)
    X_tr_base = build_base_features(train_rows)
    X_ho_base = build_base_features(hold_rows)

    print(f"  engineered feature set: {len(ENGINEERED_FEATURE_NAMES)} features")
    print(f"  base feature set: {len(BASE_FEATURE_NAMES)} features (G1 B7 parity)")

    # ── Step C: Train models ────────────────────────────────────────────
    print("\nStep C: training models")
    experiments: list[dict[str, Any]] = []

    # C1: Logistic regression on engineered features
    r_c1 = run_clf_experiment(
        "C1_logreg_engineered",
        "LogisticRegression on all 15 engineered pool-invariant features",
        ENGINEERED_FEATURE_NAMES,
        X_tr_eng, y_tr, X_ho_eng, y_ho,
        nr_ho, pool_ho, spw, seed,
        model_type="logreg",
        do_perm_importance=True,
    )
    experiments.append(r_c1)

    # C2: XGB depth=2, trees=50 on engineered features (+ perm importance)
    r_c2 = run_clf_experiment(
        "C2_xgb_d2_t50_engineered",
        "XGBoost depth=2 trees=50 on engineered features",
        ENGINEERED_FEATURE_NAMES,
        X_tr_eng, y_tr, X_ho_eng, y_ho,
        nr_ho, pool_ho, spw, seed,
        model_type="xgb", n_trees=50, max_depth=2,
        do_perm_importance=True,
    )
    experiments.append(r_c2)

    # C3: XGB depth=2, trees=100 on engineered features
    r_c3 = run_clf_experiment(
        "C3_xgb_d2_t100_engineered",
        "XGBoost depth=2 trees=100 on engineered features",
        ENGINEERED_FEATURE_NAMES,
        X_tr_eng, y_tr, X_ho_eng, y_ho,
        nr_ho, pool_ho, spw, seed,
        model_type="xgb", n_trees=100, max_depth=2,
        do_perm_importance=False,
    )
    experiments.append(r_c3)

    # C4: XGB depth=3, trees=100 on engineered features
    r_c4 = run_clf_experiment(
        "C4_xgb_d3_t100_engineered",
        "XGBoost depth=3 trees=100 on engineered features",
        ENGINEERED_FEATURE_NAMES,
        X_tr_eng, y_tr, X_ho_eng, y_ho,
        nr_ho, pool_ho, spw, seed,
        model_type="xgb", n_trees=100, max_depth=3,
        do_perm_importance=False,
    )
    experiments.append(r_c4)

    # C5: ExtraTrees depth=3 on engineered features (robustness check)
    r_c5 = run_clf_experiment(
        "C5_extratrees_d3_engineered",
        "ExtraTrees depth=3 n=100 on engineered features",
        ENGINEERED_FEATURE_NAMES,
        X_tr_eng, y_tr, X_ho_eng, y_ho,
        nr_ho, pool_ho, spw, seed,
        model_type="extratrees", n_trees=100, max_depth=3,
        do_perm_importance=False,
    )
    experiments.append(r_c5)

    # C6: Regression model on clipped net_return (auxiliary)
    try:
        from scipy.stats import spearmanr as _sp  # noqa: F401
        r_c6 = run_regression_experiment(
            ENGINEERED_FEATURE_NAMES,
            X_tr_eng, nr_tr,
            X_ho_eng, y_ho, nr_ho,
            pool_ho, seed, clip_nr_max=5.0,
        )
        experiments.append(r_c6)
    except ImportError:
        print("  [C6] scipy not available, skipping regression experiment", file=sys.stderr)

    # C7: G1 B7 parity (base features, depth=2, trees=50) — direct comparison
    r_c7 = run_clf_experiment(
        "C7_xgb_d2_t50_base_g1b7_parity",
        "G1-B7 parity: XGBoost depth=2 trees=50 on base features [final_score, log_index, log_index_log1p]",
        BASE_FEATURE_NAMES,
        X_tr_base, y_tr, X_ho_base, y_ho,
        nr_ho, pool_ho, spw, seed,
        model_type="xgb", n_trees=50, max_depth=2,
        do_perm_importance=False,
    )
    experiments.append(r_c7)

    # ── Gate evaluation ────────────────────────────────────────────────
    gate_results: dict[str, dict] = {}
    clf_experiments = [e for e in experiments if e["model_type"] not in ("xgb_regressor",)]
    for exp in clf_experiments:
        gate_results[exp["name"]] = _eval_gates(exp, hold_pos_rate)

    # ── Select best model ──────────────────────────────────────────────
    def _roc(e: dict) -> float:
        if e.get("model_type") == "xgb_regressor":
            return e.get("roc_auc_of_regression_ranker") or 0.0
        return e["holdout_metrics"].get("roc_auc") or 0.0

    clf_only = [e for e in experiments if e.get("model_type") not in ("xgb_regressor",)]
    best_clf = max(clf_only, key=_roc)
    best_overall = max(experiments, key=_roc)
    best_by_dec_pos = max(
        clf_only,
        key=lambda e: e["decile_analysis"]["top_decile"].get("pos_rate") or 0.0,
    )
    best_by_sens5 = max(
        clf_only,
        key=lambda e: e["sensitivity"]["remove_top5_pools"].get("roc_auc") or 0.0,
    )

    any_gate_pass = any(g["all_pass"] for g in gate_results.values())
    best_gate_pass = best_clf["name"] if gate_results.get(best_clf["name"], {}).get("all_pass") else None

    # ── Step E: Robustness tests ────────────────────────────────────────
    print("\nStep E: robustness tests")
    best_p_ho = best_clf["y_prob"]

    # E1/E2: sensitivity already computed per experiment
    # E3: seen vs unseen pool
    print("  E3: seen vs unseen pool analysis")
    seen_unseen = _seen_unseen_analysis(y_ho, best_p_ho, nr_ho, pool_ho, seen_pools)
    print(f"    seen ROC={seen_unseen['seen'].get('roc_auc')} unseen ROC={seen_unseen['unseen'].get('roc_auc')} gap={seen_unseen['roc_gap_seen_minus_unseen']}")

    # E4: bootstrap CI for top-decile pos_rate
    print(f"  E4: bootstrap ({n_bootstrap} iters)")
    bootstrap = _bootstrap_top_decile_pos(y_ho, best_p_ho, n_bootstrap, seed)
    print(f"    top_dec_pos CI=[{bootstrap['p025']}, {bootstrap['p975']}] mean={bootstrap['mean']}")

    # E5: permutation label sanity
    print("  E5: permutation label sanity check")
    perm_sanity = _permutation_label_sanity(
        X_tr_base, y_tr, X_ho_base, y_ho, spw, seed, n_repeats=3
    )
    print(f"    shuffled_label ROC (should be ~0.5): {perm_sanity['roc_auc_shuffled']}")

    # E6: comparison to G1 B7 baseline
    best_roc = _roc(best_clf)
    improvement_vs_g1b7 = _safe(best_roc - G1_B7_BASELINE["holdout_roc_auc"])
    g1b7_comparison = {
        "g1_b7_roc": G1_B7_BASELINE["holdout_roc_auc"],
        "g2_best_roc": _safe(best_roc),
        "delta_roc": improvement_vs_g1b7,
        "improved": (improvement_vs_g1b7 or 0.0) > 0,
        "g1_b7_top_dec_pos": G1_B7_BASELINE["top_decile_pos_rate"],
        "g2_best_top_dec_pos": _safe(
            best_clf["decile_analysis"]["top_decile"].get("pos_rate")
        ),
        "g1_b7_remove_top5_roc": G1_B7_BASELINE["remove_top5_roc_auc"],
        "g2_best_remove_top5_roc": _safe(
            best_clf["sensitivity"]["remove_top5_pools"].get("roc_auc")
        ),
    }

    # ── Conclusion ─────────────────────────────────────────────────────
    best_m = best_clf["holdout_metrics"]
    best_dec = best_clf["decile_analysis"]["top_decile"]
    best_g = gate_results.get(best_clf["name"], {}).get("gates", {})

    if any_gate_pass:
        conclusion = "PATCH_G2_GO_TO_SHADOW_PAPER_CANDIDATE"
    elif best_roc >= 0.53:
        # Check what's blocking
        if not best_g.get("roc_auc_ge_055"):
            conclusion = "PATCH_G2_WEAK_SIGNAL_NEEDS_MORE_FEATURES"
        elif seen_unseen.get("unseen_pool_failure"):
            conclusion = "HOLD_DUE_TO_UNSEEN_POOL_FAILURE"
        elif not best_g.get("top_decile_top1_pool_share_le_035"):
            conclusion = "HOLD_DUE_TO_POOL_CONCENTRATION"
        else:
            conclusion = "PATCH_G2_WEAK_SIGNAL_NEEDS_MORE_FEATURES"
    elif not best_g.get("roc_auc_ge_055") and best_roc < 0.51:
        conclusion = "HOLD_DUE_TO_NO_POOL_INVARIANT_EDGE"
    elif not best_g.get("ece_acceptable"):
        conclusion = "HOLD_DUE_TO_BAD_CALIBRATION"
    else:
        conclusion = "PATCH_G2_WEAK_SIGNAL_NEEDS_MORE_FEATURES"

    shadow_allowed = conclusion == "PATCH_G2_GO_TO_SHADOW_PAPER_CANDIDATE"
    aero_parallel = conclusion in (
        "HOLD_DUE_TO_NO_POOL_INVARIANT_EDGE",
        "PATCH_G2_WEAK_SIGNAL_NEEDS_MORE_FEATURES",
    ) and not any_gate_pass

    # ── Save holdout predictions CSV ────────────────────────────────────
    out_pred_csv.parent.mkdir(parents=True, exist_ok=True)
    rank_order = np.argsort(best_p_ho)[::-1]
    rank_map = {int(rank_order[i]): i + 1 for i in range(len(rank_order))}
    with out_pred_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "tx_hash", "log_index", "pool_address", "timestamp",
            "label", "net_return",
            "prob_positive_g2", "pred_label_05", "score_rank", "best_model",
        ])
        w.writeheader()
        for i, r in enumerate(hold_rows):
            w.writerow({
                "tx_hash": r.get("tx_hash", ""),
                "log_index": r.get("log_index", ""),
                "pool_address": r.get("pool_address", ""),
                "timestamp": r.get("timestamp", ""),
                "label": r.get("label", ""),
                "net_return": r.get("net_return", ""),
                "prob_positive_g2": round(float(best_p_ho[i]), 8),
                "pred_label_05": int(best_p_ho[i] >= 0.5),
                "score_rank": rank_map[i],
                "best_model": best_clf["name"],
            })

    # ── Save model spec JSON ────────────────────────────────────────────
    out_model_json.parent.mkdir(parents=True, exist_ok=True)
    model_spec: dict[str, Any] = {
        "patch": "G2",
        "best_model_name": best_clf["name"],
        "model_type": best_clf["model_type"],
        "features": best_clf["features"],
        "hyperparams": best_clf["hyperparams"],
        "feature_importance": best_clf.get("feature_importance"),
        "permutation_importance": best_clf.get("permutation_importance"),
        "train_stats": {
            k: v.tolist() if isinstance(v, np.ndarray) else v
            for k, v in ts.items()
        },
        "holdout_metrics": best_clf["holdout_metrics"],
        "conclusion": conclusion,
        "note": "DRY-RUN ONLY — not promoted, not deployed",
    }
    with out_model_json.open("w", encoding="utf-8") as f:
        json.dump(model_spec, f, indent=2, default=str)

    # ── Strip y_prob before JSON serialization ──────────────────────────
    for exp in experiments:
        exp.pop("y_prob", None)

    # ── Full report ─────────────────────────────────────────────────────
    report: dict[str, Any] = {
        "patch": "G2",
        "conclusion": conclusion,
        "conclusion_pass": any_gate_pass,
        "shadow_paper_allowed": shadow_allowed,
        "aero_parallel_recommended": aero_parallel,
        "split": {
            "train_rows": len(train_rows),
            "holdout_rows": len(hold_rows),
            "train_pos_rate": _safe(train_pos_rate),
            "holdout_pos_rate": _safe(hold_pos_rate),
            "holdout_lo_ms": holdout_lo_ms,
        },
        "schema_inspection": {k: v for k, v in schema.items() if k != "numeric_stats"},
        "schema_numeric_stats": schema.get("numeric_stats"),
        "engineered_features": ENGINEERED_FEATURE_NAMES,
        "base_features": BASE_FEATURE_NAMES,
        "experiments": experiments,
        "gate_results": gate_results,
        "rankings": {
            "best_clf_by_roc": {"name": best_clf["name"], "roc_auc": _safe(best_roc)},
            "best_by_top_dec_pos": {
                "name": best_by_dec_pos["name"],
                "top_dec_pos": _safe(best_by_dec_pos["decile_analysis"]["top_decile"].get("pos_rate")),
            },
            "best_after_remove_top5": {
                "name": best_by_sens5["name"],
                "remove_top5_roc": _safe(best_by_sens5["sensitivity"]["remove_top5_pools"].get("roc_auc")),
            },
        },
        "robustness": {
            "seen_vs_unseen_pool": seen_unseen,
            "bootstrap_top_dec_pos_ci": bootstrap,
            "permutation_label_sanity": perm_sanity,
        },
        "g1_b7_comparison": g1b7_comparison,
        "leakage_guard": sorted(LEAKAGE_GUARD),
        "note": "DRY-RUN ONLY — Patch G2 offline analysis, not promoted, not deployed",
    }

    out_report_json.parent.mkdir(parents=True, exist_ok=True)
    with out_report_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # ── Markdown report ─────────────────────────────────────────────────
    out_report_md.parent.mkdir(parents=True, exist_ok=True)

    def _exp_summary_row(e: dict) -> str:
        if e.get("model_type") == "xgb_regressor":
            roc_ = e.get("roc_auc_of_regression_ranker")
            dec_ = e["decile_analysis"]["top_decile"]
            return (
                f"| `{e['name']}` (aux) | {roc_} | — | — | — | — | — | — | — "
                f"| {dec_.get('pos_rate')} | {dec_.get('avg_net_return')} | {dec_.get('median_net_return')} "
                f"| {dec_.get('top1_pool_share')} | — | {e.get('ece')} | — |"
            )
        m = e["holdout_metrics"]
        dec_ = e["decile_analysis"]["top_decile"]
        sens5 = e["sensitivity"]["remove_top5_pools"]
        g = gate_results.get(e["name"])
        gp = ("✔" if g["all_pass"] else "✘") if g else "—"
        return (
            f"| `{e['name']}` "
            f"| {m.get('roc_auc')} | {m.get('pr_auc')} "
            f"| {m.get('accuracy')} | {m.get('precision')} | {m.get('recall')} | {m.get('f1')} "
            f"| {m.get('log_loss')} | {m.get('brier_score')} "
            f"| {dec_.get('pos_rate')} | {dec_.get('avg_net_return')} | {dec_.get('median_net_return')} "
            f"| {dec_.get('top1_pool_share')} | {sens5.get('roc_auc')} "
            f"| {e.get('ece')} | {gp} |"
        )

    def _gate_table(exp: dict) -> str:
        g = gate_results.get(exp["name"])
        if not g:
            return "_Not a classifier experiment_"
        rows_str = "\n".join(
            f"| `{gk}` | {'✔' if gv else '✘'} |"
            for gk, gv in g["gates"].items()
        )
        return (
            "| Gate | Pass |\n|------|:----:|\n"
            + rows_str
            + f"\n\n**All gates pass:** {'✔ YES' if g['all_pass'] else '✘ NO'}"
        )

    def _fi_table(exp: dict) -> str:
        fi = exp.get("feature_importance") or exp.get("feature_coefficients")
        if not fi:
            return "_Not available_"
        srt = sorted(fi.items(), key=lambda x: -(abs(x[1]) if x[1] is not None else 0))
        return "| Feature | Score |\n|---------|------:|\n" + "\n".join(
            f"| `{k}` | {v} |" for k, v in srt
        )

    def _perm_table(exp: dict) -> str:
        pi = exp.get("permutation_importance")
        if not pi:
            return "_Not computed for this model_"
        return "| Feature | ROC Drop |\n|---------|----------:|\n" + "\n".join(
            f"| `{k}` | {v} |" for k, v in pi.items()
        )

    def _per_exp_section(e: dict) -> str:
        if e.get("model_type") == "xgb_regressor":
            return f"""
### {e['name']} (auxiliary)

> {e['label']}

| Metric | Value |
|--------|------:|
| ROC-AUC (ranker) | {e.get('roc_auc_of_regression_ranker')} |
| Spearman corr (pred vs actual NR) | {e.get('spearman_corr_pred_vs_actual_nr')} |
| ECE | {e.get('ece')} |
| Top-decile pos_rate | {e['decile_analysis']['top_decile']['pos_rate']} |
| Top-decile median NR | {e['decile_analysis']['top_decile']['median_net_return']} |
| Top1 pool share | {e['decile_analysis']['top_decile']['top1_pool_share']} |

_{e.get('interpretation')}_
"""
        tr_roc = e.get("train_metrics", {}).get("roc_auc") if e.get("train_metrics") else None
        m = e["holdout_metrics"]
        dec_td = e["decile_analysis"]["top_decile"]
        dec_tq = e["decile_analysis"]["top_quintile"]
        sens1 = e["sensitivity"]["remove_top1_pool"]
        sens5 = e["sensitivity"]["remove_top5_pools"]
        return f"""
### {e['name']}

> {e['label']}

**Model:** `{e['model_type']}` | **Features ({e['n_features']}):** `{e['features']}`

| Metric | Holdout | Train |
|--------|--------:|------:|
| ROC-AUC | **{m.get('roc_auc')}** | {tr_roc} |
| PR-AUC | {m.get('pr_auc')} | — |
| Log loss | {m.get('log_loss')} | — |
| Brier | {m.get('brier_score')} | — |
| Accuracy | {m.get('accuracy')} | — |
| Precision | {m.get('precision')} | — |
| Recall | {m.get('recall')} | — |
| F1 | {m.get('f1')} | — |
| ECE | {e.get('ece')} | — |
| Cal max dev | {e.get('calibration_max_deviation')} | — |
| Train-Hold ROC gap | {e.get('train_holdout_roc_gap')} | — |

**Decile Analysis:**

| Bucket | n | pos_rate | avg_nr | median_nr | p25_nr | top1_pool_share |
|--------|--:|--------:|-------:|----------:|-------:|---------------:|
| Top decile | {dec_td['n']} | {dec_td['pos_rate']} | {dec_td['avg_net_return']} | {dec_td['median_net_return']} | {dec_td['p25_net_return']} | {dec_td['top1_pool_share']} |
| Top quintile | {e['decile_analysis']['top_quintile']['n']} | {e['decile_analysis']['top_quintile']['pos_rate']} | {e['decile_analysis']['top_quintile']['avg_net_return']} | {e['decile_analysis']['top_quintile']['median_net_return']} | — | {e['decile_analysis']['top_quintile']['top1_pool_share']} |

**Remove-pool sensitivity:**

| Sensitivity | n_rem | ROC | top_dec_pos | top1_in_dec |
|-------------|------:|----:|------------:|------------:|
| Remove top-1 | {sens1.get('n_remaining')} | {sens1.get('roc_auc')} | {sens1.get('top_decile_pos_rate')} | {sens1.get('top1_pool_share_in_top_decile')} |
| Remove top-5 | {sens5.get('n_remaining')} | {sens5.get('roc_auc')} | {sens5.get('top_decile_pos_rate')} | {sens5.get('top1_pool_share_in_top_decile')} |

**Feature Importance / Coefficients:**

{_fi_table(e)}

**Permutation Importance:**

{_perm_table(e)}

**Gates:**

{_gate_table(e)}
"""

    summary_rows_str = "\n".join(_exp_summary_row(e) for e in experiments)
    all_exp_sections = "\n---\n".join(_per_exp_section(e) for e in experiments)

    cal_table_best = "\n".join(
        f"| {b['bucket_rank']} | {b['n']} | {b['mean_pred']} | {b['actual_pos_rate']} | {b['deviation']} |"
        for b in best_clf.get("calibration_quantile_buckets", [])
    )

    su = seen_unseen
    md = f"""# Run-004 Patch G2 — V2 Pool-Invariant Feature Redesign

**Conclusion**: `{conclusion}`  
**Shadow/Paper candidate**: `{'ALLOWED' if shadow_allowed else 'NOT ALLOWED'}`  
**Aero vAMM parallel**: `{'RECOMMENDED' if aero_parallel else 'OPTIONAL'}`  
**Note**: DRY-RUN ONLY — not promoted, not deployed.

---

## 0. Background

| Patch | Conclusion | Best holdout ROC |
|-------|-----------|----------------:|
| G (full model) | `HOLD_DUE_TO_NO_HOLDOUT_EDGE` | 0.4823 |
| G1 (ablation) | `HOLD_DUE_TO_NO_POOL_AGNOSTIC_SIGNAL` | 0.5393 (B7) |
| **G2 (this)** | **`{conclusion}`** | **{_safe(best_roc)}** |

---

## 1. Schema Inspection (Step A)

| Category | Columns |
|----------|---------|
| allowed_pool_invariant | `{schema['allowed_pool_invariant']}` |
| banned_pool_identity | `{schema['banned_pool_identity']}` |
| banned_time_pattern | `{schema['banned_time_pattern']}` |
| banned_target_encoded | `{schema['banned_target_encoded']}` |
| banned_future_or_label_leakage | `{schema['banned_future_or_label_leakage']}` |
| meta_operational | `{schema.get('meta_operational', [])}` |
| unknown_needs_review | `{schema.get('unknown_needs_review', [])}` |

**Note**: {schema['note']}

### Numeric Distribution (training set)

| Feature | min | p10 | p25 | median | p75 | p90 | max |
|---------|----:|----:|----:|-------:|----:|----:|----:|
| `final_score` | {ts['fs_p01']:.4f}…min | {ts.get('fs_p10', '?'):.4f} | {ts['fs_p25']:.4f} | {_pct(ts['fs_sorted'].tolist(), 0.5):.4f} | {ts['fs_p75']:.4f} | {ts['fs_p90']:.4f} | {ts['fs_max']:.4f} |
| `log_index` | {ts['li_min']:.0f} | — | {ts['li_p25']:.0f} | {_pct(ts['li_sorted'].tolist(), 0.5):.0f} | {ts['li_p75']:.0f} | — | {ts['li_max']:.0f} |

> **Key finding**: `final_score` is heavily right-compressed (p25=0.904, p75=0.928). The main variation is in the left tail (< p10=0.544). `log_index` is widely spread (3–{ts['li_max']:.0f}).

---

## 2. Engineered Features (Step B)

| Feature | Description | Type |
|---------|-------------|------|
| `final_score` | Heuristic composite score | base |
| `log_index_log1p` | log1p(log_index) | base |
| `log_index` | Block event position | base |
| `final_score_logit` | logit(final_score) — linearizes bounded score | derived |
| `final_score_rank_pct` | Percentile rank in training distribution | derived |
| `final_score_sq` | final_score² — quadratic non-linearity | derived |
| `fs_neg` | 1 − final_score (flipped for linear models) | derived |
| `is_low_fs_p10` | final_score < training p10 (binary flag) | binary |
| `is_low_fs_p25` | final_score < training p25 (binary flag) | binary |
| `log_index_rank_pct` | Percentile rank of log_index in training | derived |
| `log_index_sq_log1p` | log1p(log_index²) — heavy tail emphasis | derived |
| `is_low_li_p25` | log_index < training p25 (binary flag) | binary |
| `fs_x_li_log1p` | final_score × log_index_log1p | interaction |
| `fs_logit_x_li_log1p` | logit(final_score) × log_index_log1p | interaction |
| `fs_neg_x_li_log1p` | (1−final_score) × log_index_log1p | interaction |

> **Banned and not constructed**: pool_address, token_address, pool_train_rate, hour_of_day, day_of_week, sin/cos cycles, net_return, entry_block, exit_block.

---

## 3. Experiment Summary

| Experiment | ROC | PR | Acc | Prec | Rec | F1 | LL | Brier | TopDec_pos | TopDec_avgNR | TopDec_medNR | Top1Pool | Sens5_ROC | ECE | Gates |
|------------|----:|---:|----:|-----:|----:|---:|---:|------:|-----------:|------------:|------------:|---------:|----------:|----:|:-----:|
{summary_rows_str}

Holdout base positive_rate = `{_safe(hold_pos_rate)}`  
G1 B7 baseline ROC = `{G1_B7_BASELINE['holdout_roc_auc']}`

---

## 4. Best Model vs G1 B7

| Metric | G1 B7 | G2 Best ({best_clf['name']}) | Delta |
|--------|------:|----------------------------:|------:|
| Holdout ROC-AUC | {G1_B7_BASELINE['holdout_roc_auc']} | {_safe(best_roc)} | {('?' if improvement_vs_g1b7 is None else f'{improvement_vs_g1b7:+.4f}')} |
| Top-decile pos_rate | {G1_B7_BASELINE['top_decile_pos_rate']} | {g1b7_comparison['g2_best_top_dec_pos']} | — |
| Remove-top5 ROC | {G1_B7_BASELINE['remove_top5_roc_auc']} | {g1b7_comparison['g2_best_remove_top5_roc']} | — |

---

## 5. Top-decile / Top-quintile Analysis (Best Model: `{best_clf['name']}`)

| Bucket | n | pos_rate | avg_nr | median_nr | p25_nr | top1_pool_share |
|--------|--:|--------:|-------:|----------:|-------:|---------------:|
| Top decile | {best_clf['decile_analysis']['top_decile']['n']} | {best_clf['decile_analysis']['top_decile']['pos_rate']} | {best_clf['decile_analysis']['top_decile']['avg_net_return']} | {best_clf['decile_analysis']['top_decile']['median_net_return']} | {best_clf['decile_analysis']['top_decile']['p25_net_return']} | {best_clf['decile_analysis']['top_decile']['top1_pool_share']} |
| Top quintile | {best_clf['decile_analysis']['top_quintile']['n']} | {best_clf['decile_analysis']['top_quintile']['pos_rate']} | {best_clf['decile_analysis']['top_quintile']['avg_net_return']} | {best_clf['decile_analysis']['top_quintile']['median_net_return']} | — | {best_clf['decile_analysis']['top_quintile']['top1_pool_share']} |
| Bottom decile | {best_clf['decile_analysis']['bottom_decile']['n']} | {best_clf['decile_analysis']['bottom_decile']['pos_rate']} | {best_clf['decile_analysis']['bottom_decile']['avg_net_return']} | {best_clf['decile_analysis']['bottom_decile']['median_net_return']} | — | {best_clf['decile_analysis']['bottom_decile']['top1_pool_share']} |
| All holdout | {best_clf['decile_analysis']['all']['n']} | {hold_pos_rate:.6f} | — | — | — | — |

---

## 6. Remove Top1/Top5 Pool Sensitivity (Best Model)

| Remove | n_remaining | ROC-AUC | top_dec_pos | top1_in_dec |
|--------|----------:|--------:|------------:|------------:|
| Top-1 pool | {best_clf['sensitivity']['remove_top1_pool'].get('n_remaining')} | {best_clf['sensitivity']['remove_top1_pool'].get('roc_auc')} | {best_clf['sensitivity']['remove_top1_pool'].get('top_decile_pos_rate')} | {best_clf['sensitivity']['remove_top1_pool'].get('top1_pool_share_in_top_decile')} |
| Top-5 pools | {best_clf['sensitivity']['remove_top5_pools'].get('n_remaining')} | {best_clf['sensitivity']['remove_top5_pools'].get('roc_auc')} | {best_clf['sensitivity']['remove_top5_pools'].get('top_decile_pos_rate')} | {best_clf['sensitivity']['remove_top5_pools'].get('top1_pool_share_in_top_decile')} |

---

## 7. Seen vs Unseen Pool Holdout (Best Model)

| Subset | n | ROC-AUC | PR-AUC | pos_rate | top_dec_pos | top_dec_med_nr |
|--------|--:|--------:|-------:|--------:|------------:|---------------:|
| Seen pools | {su['seen']['n']} | {su['seen'].get('roc_auc')} | {su['seen'].get('pr_auc')} | {su['seen'].get('pos_rate')} | {su['seen'].get('top_decile_pos_rate')} | {su['seen'].get('top_decile_median_net_return')} |
| Unseen pools | {su['unseen']['n']} | {su['unseen'].get('roc_auc')} | {su['unseen'].get('pr_auc')} | {su['unseen'].get('pos_rate')} | {su['unseen'].get('top_decile_pos_rate')} | {su['unseen'].get('top_decile_median_net_return')} |

Seen vs unseen ROC gap: `{su['roc_gap_seen_minus_unseen']}` | Unseen pool failure: `{su['unseen_pool_failure']}`

---

## 8. Calibration (Best Model, Quantile Buckets min_size=100)

ECE = **{best_clf.get('ece')}** | Max deviation = **{best_clf.get('calibration_max_deviation')}**

| Bucket rank | n | mean_pred | actual_pos_rate | deviation |
|------------:|--:|----------:|---------------:|----------:|
{cal_table_best}

---

## 9. Bootstrap CI — Top-Decile Positive Rate

| Statistic | Value |
|-----------|------:|
| N bootstrap iters | {bootstrap['n_bootstrap']} |
| Mean | {bootstrap['mean']} |
| Std | {bootstrap['std']} |
| 95% CI | [{bootstrap['p025']}, {bootstrap['p975']}] |
| Holdout base pos_rate | {_safe(hold_pos_rate)} |

CI entirely above base positive_rate: `{(bootstrap['p025'] or 0) > hold_pos_rate}`

---

## 10. Permutation Label Sanity Check

| Repeat | Shuffled-label ROC |
|-------:|------------------:|
{chr(10).join(f'| {i+1} | {r} |' for i, r in enumerate(perm_sanity['roc_auc_shuffled']))}

Mean shuffled ROC: `{perm_sanity['mean_roc']}` | Pass (≈0.5): `{perm_sanity['pass']}`

---

## 11. Gates Summary

| Experiment | All gates pass | ROC≥0.55 | PR>base | TopDec_pos | MedianNR≥0 | P25NR | Sens5 | Top1Pool | ECE | Overfit |
|------------|:--------------:|:--------:|:-------:|:----------:|:----------:|:-----:|:-----:|:--------:|:---:|:-------:|
{chr(10).join(
    '| `' + e['name'] + '` | ' + (
        ('✔' if gate_results[e['name']]['all_pass'] else '✘') + ' | ' +
        ' | '.join(('✔' if v else '✘') for v in gate_results[e['name']]['gates'].values())
    ) + ' |'
    for e in clf_only
    if e['name'] in gate_results
)}

---

## 12. Per-Experiment Detail

{all_exp_sections}

---

## 13. Conclusion

**Conclusion: `{conclusion}`**

| Question | Answer |
|----------|--------|
| ROC-AUC >= 0.55 achieved? | {'YES ✔' if best_roc >= 0.55 else f'NO ✘ (best = {_safe(best_roc)})'} |
| Pool-agnostic signal vs G1 B7? | {'Improved' if (improvement_vs_g1b7 or 0) > 0 else 'Flat/worse'} by {('?' if not improvement_vs_g1b7 else f'{improvement_vs_g1b7:+.4f}')} ROC |
| Shadow/Paper candidate allowed? | {'YES ✔' if shadow_allowed else 'NO ✘'} |
| Aero vAMM research in parallel? | {'YES — recommended' if aero_parallel else 'OPTIONAL'} |
| Unseen pool failure? | {'YES ✘' if su['unseen_pool_failure'] else 'NO ✔'} |
| Permutation sanity pass? | {'YES ✔' if perm_sanity['pass'] else 'WARN'} |

### Recommendation

{'**Feature ceiling reached** — only `final_score` and `log_index` exist in the current CSV. Engineered variants (logit, rank_pct, interactions) provide marginal improvement. To surpass ROC 0.55, new pool-invariant real-time features must be sourced: reserves ratio, swap USD size, price impact, unique buyer count, block fill rate. These require either CSV re-preparation with additional fields or a live feature pipeline.' if best_roc < 0.55 else '**Signal threshold exceeded** — proceed to shadow/paper evaluation.'}

---

## 14. Artifacts

| Artifact | Path |
|----------|------|
| Script | `scripts/run004_patch_g2_pool_invariant_feature_redesign.py` |
| Report (JSON) | `mvp/diagnostics/run004_patch_g2_pool_invariant_feature_report.json` |
| Report (MD) | `mvp/runs/run-004-patch-g2-pool-invariant-feature-redesign.md` |
| Best-model predictions CSV | `logs/run004_patch_g2_holdout_predictions.csv` |
| Model spec (JSON) | `models/signal_model_v2only_run004_patch_g2_dryrun.json` |
"""

    with out_report_md.open("w", encoding="utf-8") as f:
        f.write(md)

    print(f"\nwrote {out_report_json}")
    print(f"wrote {out_report_md}")
    print(f"wrote {out_pred_csv}")
    print(f"wrote {out_model_json}")
    print(f"\n=== CONCLUSION: {conclusion} ===")
    print(f"best_roc={_safe(best_roc)} G1_B7_roc={G1_B7_BASELINE['holdout_roc_auc']} delta={('?' if not improvement_vs_g1b7 else f'{improvement_vs_g1b7:+.4f}')}")
    print(f"shadow_allowed={shadow_allowed} aero_parallel={aero_parallel}")
    print(f"seen_roc={su['seen'].get('roc_auc')} unseen_roc={su['unseen'].get('roc_auc')} gap={su['roc_gap_seen_minus_unseen']}")
    print(f"bootstrap_CI=[{bootstrap['p025']}, {bootstrap['p975']}] vs base={_safe(hold_pos_rate)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
