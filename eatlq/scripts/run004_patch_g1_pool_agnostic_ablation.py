"""
Run-004 Patch G1: Pool-Agnostic Ablation + Feature Failure Diagnosis.

Reads  logs/run004_v2only_prepared_dryrun_canonical.csv
Writes mvp/diagnostics/run004_patch_g1_pool_agnostic_ablation_report.json
       mvp/runs/run-004-patch-g1-pool-agnostic-ablation.md
       logs/run004_patch_g1_ablation_predictions.csv

Hard prohibitions (enforced in-script):
  - No LIVE_TRADING_MODE
  - No model promotion / no overwrite of production model
  - net_return / entry_block / exit_block never used as training features
  - No V3/Aero data (pool_type == v2 enforced)
  - Strict time-series split (no random split)
  - No RPC calls
  - No relabel
  - No hyperparameter tuning to rescue holdout

Env:
  RUN004_G1_CANONICAL_CSV   default logs/run004_v2only_prepared_dryrun_canonical.csv
  RUN004_G1_HOLDOUT_LO_MS   default 1776520584003
  RUN004_G1_SEED             default 42
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# ── Guard: no live mode ──────────────────────────────────────────────
assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on",
), "LIVE_TRADING_MODE must not be enabled during Patch G1 ablation"

import numpy as np

try:
    import xgboost as xgb
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False

try:
    from sklearn.linear_model import LogisticRegression
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

# ── Constants ─────────────────────────────────────────────────────────
HOLDOUT_LO_MS_DEFAULT = 1776520584003
POOL_SMOOTH_N = 10
LEAKAGE_GUARD_FIELDS = {
    "net_return", "entry_block", "exit_block", "source_row_id",
    "relabel_status", "rpc_calls_this_key", "label_reason",
    "reject_reason", "candidate_status", "pre_check", "topic0",
    "event_kind", "pool_type",
}

# Feature group definitions
FEATURE_POOL = ["pool_train_rate", "pool_train_log_count"]
FEATURE_TIME = ["hour_of_day", "sin_hour", "cos_hour", "day_of_week", "sin_dow", "cos_dow"]
FEATURE_BLOCK = ["log_index", "log_index_log1p"]
FEATURE_HEURISTIC_BASE = ["final_score"]
# Sub-scores; included if columns present in CSV (they typically aren't)
FEATURE_HEURISTIC_SUBSCORES = ["whale_val", "momentum_val", "activity_val", "risk_val"]

ALL_FEATURE_NAMES = (
    FEATURE_HEURISTIC_BASE
    + FEATURE_BLOCK
    + FEATURE_TIME
    + FEATURE_POOL
)


# ── Helpers ────────────────────────────────────────────────────────────

def _pct(sv: list[float], q: float) -> float:
    if not sv:
        return float("nan")
    n = len(sv)
    k = (n - 1) * q
    lo, hi = int(k), min(int(k) + 1, n - 1)
    return sv[lo] * (1 - (k - lo)) + sv[hi] * (k - lo)


def _safe(v: Any, ndigits: int = 6) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), ndigits)
    return v


def _parse_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _pool_target_encoding(
    train_rows: list[dict[str, str]],
    global_pos_rate: float,
) -> dict[str, tuple[float, int]]:
    pool_pos: dict[str, int] = defaultdict(int)
    pool_cnt: dict[str, int] = defaultdict(int)
    for r in train_rows:
        pool = (r.get("pool_address") or "").lower()
        pool_pos[pool] += int(r.get("label", 0) or 0)
        pool_cnt[pool] += 1
    stats: dict[str, tuple[float, int]] = {}
    for pool, cnt in pool_cnt.items():
        smoothed = (pool_pos[pool] + POOL_SMOOTH_N * global_pos_rate) / (cnt + POOL_SMOOTH_N)
        stats[pool] = (smoothed, cnt)
    return stats


def _row_to_full_feature_dict(
    r: dict[str, str],
    pool_stats: dict[str, tuple[float, int]] | None,
    global_pos_rate: float,
    available_subscores: list[str],
) -> dict[str, float]:
    """Compute every possible feature for a single row as a named dict."""
    fs = float(r.get("final_score", 0) or 0)
    li = float(r.get("log_index", 0) or 0)
    ts_ms = int(r.get("timestamp", 0) or 0)
    ts_sec = ts_ms // 1000
    hour = (ts_sec % 86400) // 3600
    dow = (ts_sec // 86400) % 7
    pool = (r.get("pool_address") or "").lower()

    if pool_stats and pool in pool_stats:
        p_rate, p_cnt = pool_stats[pool]
    else:
        p_rate, p_cnt = global_pos_rate, 0

    fd: dict[str, float] = {
        "final_score": fs,
        "log_index": li,
        "log_index_log1p": math.log1p(li),
        "hour_of_day": float(hour),
        "sin_hour": math.sin(2 * math.pi * hour / 24),
        "cos_hour": math.cos(2 * math.pi * hour / 24),
        "day_of_week": float(dow),
        "sin_dow": math.sin(2 * math.pi * dow / 7),
        "cos_dow": math.cos(2 * math.pi * dow / 7),
        "pool_train_rate": p_rate,
        "pool_train_log_count": math.log1p(float(p_cnt)),
    }
    for col in available_subscores:
        fd[col] = float(r.get(col, 0) or 0)
    return fd


def _build_matrix(
    rows: list[dict[str, str]],
    feature_names: list[str],
    pool_stats: dict[str, tuple[float, int]] | None,
    global_pos_rate: float,
    available_subscores: list[str],
) -> np.ndarray:
    X: list[list[float]] = []
    for r in rows:
        fd = _row_to_full_feature_dict(r, pool_stats, global_pos_rate, available_subscores)
        X.append([fd.get(f, 0.0) for f in feature_names])
    return np.array(X, dtype=np.float32)


# ── Metrics ────────────────────────────────────────────────────────────

def _metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> dict[str, Any]:
    y_pred = (y_prob >= threshold).astype(int)
    prec, rec, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="binary", zero_division=0)
    acc = accuracy_score(y_true, y_pred)
    cm = confusion_matrix(y_true, y_pred).tolist()
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
        "accuracy": _safe(acc),
        "precision": _safe(prec),
        "recall": _safe(rec),
        "f1": _safe(f1),
        "roc_auc": _safe(roc),
        "pr_auc": _safe(prauc),
        "log_loss": _safe(ll),
        "brier_score": _safe(bs),
        "confusion_matrix": cm,
        "threshold": threshold,
        "n_positive_predicted": int(y_pred.sum()),
    }


def _decile_analysis(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    net_returns: np.ndarray,
    pool_addrs: list[str],
) -> dict[str, Any]:
    idx = np.argsort(y_prob)[::-1]
    n = len(idx)
    n_decile = max(1, n // 10)
    n_quintile = max(1, n // 5)

    def _stats(sel: np.ndarray) -> dict[str, Any]:
        yt = y_true[sel]
        nr = net_returns[sel]
        pa = [pool_addrs[i] for i in sel]
        pc = Counter(pa)
        top1_p = pc.most_common(1)[0][1] / max(1, len(sel)) if pc else 0.0
        nr_sorted = sorted(nr.tolist())
        return {
            "n": int(len(sel)),
            "pos_rate": _safe(float(yt.mean())),
            "avg_net_return": _safe(float(nr.mean())),
            "median_net_return": _safe(float(np.median(nr))),
            "p25_net_return": _safe(_pct(nr_sorted, 0.25)),
            "top1_pool_share": _safe(top1_p),
        }

    return {
        "top_decile": _stats(idx[:n_decile]),
        "top_quintile": _stats(idx[:n_quintile]),
        "bottom_decile": _stats(idx[-n_decile:]),
        "all": _stats(idx),
    }


def _sensitivity_remove_pools(
    y_all: np.ndarray,
    y_prob_all: np.ndarray,
    net_returns_all: np.ndarray,
    pool_addrs_all: list[str],
    top_n: int = 5,
) -> dict[str, Any]:
    pc = Counter(pool_addrs_all)
    top_pools = {p for p, _ in pc.most_common(top_n)}
    mask = np.array([p not in top_pools for p in pool_addrs_all])
    n_rem = int(mask.sum())
    if n_rem < 10:
        return {
            "n_remaining": n_rem,
            "roc_auc": None,
            "top_decile_avg_net_return": None,
            "top_decile_pos_rate": None,
            "top1_pool_share_in_top_decile": None,
        }
    yt = y_all[mask]
    yp = y_prob_all[mask]
    nr = net_returns_all[mask]
    pa = [pool_addrs_all[i] for i in range(len(pool_addrs_all)) if mask[i]]
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
        "top_decile_avg_net_return": _safe(float(nr[idx[:n_dec]].mean())),
        "top_decile_pos_rate": _safe(float(yt[idx[:n_dec]].mean())),
        "top1_pool_share_in_top_decile": _safe(
            Counter(top_pa).most_common(1)[0][1] / max(1, n_dec) if top_pa else 0.0
        ),
    }


def _calibration(
    y_true: np.ndarray, y_prob: np.ndarray, n_buckets: int = 10
) -> tuple[list[dict[str, Any]], float]:
    edges = np.linspace(0, 1, n_buckets + 1)
    buckets = []
    max_dev = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if mask.sum() == 0:
            continue
        mp = float(y_prob[mask].mean())
        ar = float(y_true[mask].mean())
        dev = abs(mp - ar)
        if dev > max_dev:
            max_dev = dev
        buckets.append({
            "bucket": f"[{lo:.2f},{hi:.2f})",
            "count": int(mask.sum()),
            "mean_pred": _safe(mp),
            "actual_pos_rate": _safe(ar),
        })
    return buckets, max_dev


# ── Model trainers ─────────────────────────────────────────────────────

def _train_xgb(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    n_trees: int = 400,
    max_depth: int = 6,
    lr: float = 0.05,
    seed: int = 42,
    scale_pos_weight: float = 1.0,
) -> "xgb.XGBClassifier":
    model = xgb.XGBClassifier(
        n_estimators=n_trees,
        max_depth=max_depth,
        learning_rate=lr,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        gamma=0.1,
        reg_alpha=0.1,
        reg_lambda=1.0,
        scale_pos_weight=scale_pos_weight,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=seed,
        n_jobs=2,
        verbosity=0,
    )
    model.fit(X_tr, y_tr)
    return model


def _train_logreg(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    seed: int = 42,
) -> tuple["LogisticRegression", "StandardScaler"]:
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X_tr)
    model = LogisticRegression(
        max_iter=1000,
        random_state=seed,
        C=1.0,
        solver="lbfgs",
    )
    model.fit(Xs, y_tr)
    return model, scaler


# ── Full experiment runner ──────────────────────────────────────────────

def _run_experiment(
    exp_name: str,
    exp_label: str,
    feature_names: list[str],
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    X_ho: np.ndarray,
    y_ho: np.ndarray,
    nr_ho: np.ndarray,
    pool_ho: list[str],
    scale_pos_weight: float,
    seed: int,
    model_type: str = "xgb",
    n_trees: int = 400,
    max_depth: int = 6,
    lr: float = 0.05,
) -> dict[str, Any]:
    """Train a model and collect all required metrics. Returns a result dict."""
    print(f"  [{exp_name}] training {model_type} on {len(feature_names)} features: {feature_names}")

    # Guard NaN/inf
    X_tr = np.nan_to_num(X_tr, nan=0.0, posinf=0.0, neginf=0.0)
    X_ho = np.nan_to_num(X_ho, nan=0.0, posinf=0.0, neginf=0.0)

    fi: dict[str, float] | None = None
    coef: dict[str, float] | None = None

    if model_type == "xgb":
        model = _train_xgb(X_tr, y_tr, n_trees, max_depth, lr, seed, scale_pos_weight)
        p_ho = model.predict_proba(X_ho)[:, 1]
        p_tr = model.predict_proba(X_tr)[:, 1]
        fi_raw = model.feature_importances_
        fi = {feature_names[i]: _safe(float(fi_raw[i])) for i in range(len(feature_names))}
    elif model_type == "logreg":
        m, scaler = _train_logreg(X_tr, y_tr, seed)
        p_ho = m.predict_proba(scaler.transform(X_ho))[:, 1]
        p_tr = m.predict_proba(scaler.transform(X_tr))[:, 1]
        coef = {feature_names[i]: _safe(float(m.coef_[0][i])) for i in range(len(feature_names))}
    else:
        raise ValueError(f"Unknown model_type: {model_type}")

    m_ho = _metrics(y_ho, p_ho)
    m_tr = _metrics(y_tr, p_tr)

    dec = _decile_analysis(y_ho, p_ho, nr_ho, pool_ho)
    sens1 = _sensitivity_remove_pools(y_ho, p_ho, nr_ho, pool_ho, top_n=1)
    sens5 = _sensitivity_remove_pools(y_ho, p_ho, nr_ho, pool_ho, top_n=5)
    cal_buckets, cal_max_dev = _calibration(y_ho, p_ho)

    result: dict[str, Any] = {
        "name": exp_name,
        "label": exp_label,
        "model_type": model_type,
        "features": feature_names,
        "n_features": len(feature_names),
        "hyperparams": {
            "n_trees": n_trees if model_type == "xgb" else None,
            "max_depth": max_depth if model_type == "xgb" else None,
            "lr": lr if model_type == "xgb" else None,
        },
        "holdout_metrics": m_ho,
        "train_metrics": m_tr,
        "train_holdout_roc_gap": _safe(
            (m_tr.get("roc_auc") or 0.0) - (m_ho.get("roc_auc") or 0.0)
        ),
        "decile_analysis": dec,
        "sensitivity": {
            "remove_top1_pool": sens1,
            "remove_top5_pools": sens5,
        },
        "calibration_buckets": cal_buckets,
        "calibration_max_deviation": _safe(cal_max_dev),
        "feature_importance": fi,
        "feature_coefficients": coef,
        "y_prob": p_ho,  # kept in memory for predictions CSV; stripped before JSON
    }

    roc = m_ho.get("roc_auc") or 0.0
    pr = m_ho.get("pr_auc") or 0.0
    print(f"  [{exp_name}] holdout ROC-AUC={roc:.4f} PR-AUC={pr:.4f} "
          f"top_dec_pos={dec['top_decile']['pos_rate']} "
          f"top_dec_top1pool={dec['top_decile']['top1_pool_share']}")
    return result


def _run_score_baseline(
    name: str,
    label: str,
    scores: np.ndarray,
    y_ho: np.ndarray,
    nr_ho: np.ndarray,
    pool_ho: list[str],
) -> dict[str, Any]:
    """Evaluate a pre-computed score array as a baseline (no model training)."""
    m_ho = _metrics(y_ho, scores)
    dec = _decile_analysis(y_ho, scores, nr_ho, pool_ho)
    sens1 = _sensitivity_remove_pools(y_ho, scores, nr_ho, pool_ho, top_n=1)
    sens5 = _sensitivity_remove_pools(y_ho, scores, nr_ho, pool_ho, top_n=5)
    cal_buckets, cal_max_dev = _calibration(y_ho, scores)

    return {
        "name": name,
        "label": label,
        "model_type": "baseline",
        "features": [],
        "n_features": 0,
        "hyperparams": {},
        "holdout_metrics": m_ho,
        "train_metrics": None,
        "train_holdout_roc_gap": None,
        "decile_analysis": dec,
        "sensitivity": {
            "remove_top1_pool": sens1,
            "remove_top5_pools": sens5,
        },
        "calibration_buckets": cal_buckets,
        "calibration_max_deviation": _safe(cal_max_dev),
        "feature_importance": None,
        "feature_coefficients": None,
        "y_prob": scores,
    }


# ── Gate evaluation ────────────────────────────────────────────────────

def _eval_gates_pool_agnostic(
    result: dict[str, Any],
    holdout_base_pos_rate: float,
    baseline_median_nr: float,
) -> dict[str, Any]:
    """Check Patch G1 success gates for a single pool-agnostic experiment."""
    m = result["holdout_metrics"]
    dec = result["decile_analysis"]["top_decile"]
    sens5 = result["sensitivity"]["remove_top5_pools"]
    cal_dev = result.get("calibration_max_deviation") or 1.0

    roc = m.get("roc_auc") or 0.0
    dec_pos = dec.get("pos_rate") or 0.0
    dec_med_nr = dec.get("median_net_return")
    dec_top1 = dec.get("top1_pool_share") or 1.0
    sens5_dec_pos = sens5.get("top_decile_pos_rate") or 0.0

    med_better = (
        dec_med_nr is not None
        and (dec_med_nr >= 0 or dec_med_nr > baseline_median_nr + 0.05)
    )

    gates = {
        "roc_auc_ge_053": roc >= 0.53,
        "top_decile_pos_rate_above_base": dec_pos > holdout_base_pos_rate,
        "top_decile_median_nr_acceptable": med_better,
        "top_decile_top1_pool_share_le_035": dec_top1 <= 0.35,
        "sens_top5_dec_pos_above_base": sens5_dec_pos > holdout_base_pos_rate,
        "calibration_acceptable": cal_dev <= 0.40,
    }
    passes = all(gates.values())
    return {"gates": gates, "all_pass": passes}


# ── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    canonical_csv = Path(os.environ.get(
        "RUN004_G1_CANONICAL_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"),
    ))
    holdout_lo_ms = int(os.environ.get("RUN004_G1_HOLDOUT_LO_MS", str(HOLDOUT_LO_MS_DEFAULT)))
    seed = int(os.environ.get("RUN004_G1_SEED", "42"))

    out_report_json = Path(os.environ.get(
        "RUN004_G1_REPORT_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_g1_pool_agnostic_ablation_report.json"),
    ))
    out_report_md = Path(os.environ.get(
        "RUN004_G1_REPORT_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-g1-pool-agnostic-ablation.md"),
    ))
    out_pred_csv = Path(os.environ.get(
        "RUN004_G1_PRED_CSV",
        str(REPO / "logs" / "run004_patch_g1_ablation_predictions.csv"),
    ))

    if not canonical_csv.is_file():
        print(f"ERROR: missing {canonical_csv}", file=sys.stderr)
        return 1

    print(f"loading {canonical_csv}")
    rows = _parse_rows(canonical_csv)

    # Enforce V2-only
    non_v2 = [r for r in rows if (r.get("pool_type") or "").lower() != "v2"]
    if non_v2:
        print(f"WARNING: {len(non_v2)} non-v2 rows filtered out", file=sys.stderr)
        rows = [r for r in rows if (r.get("pool_type") or "").lower() == "v2"]

    # Time-series split
    train_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) < holdout_lo_ms]
    hold_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) >= holdout_lo_ms]
    print(f"train={len(train_rows)} holdout={len(hold_rows)}")
    if len(train_rows) < 100 or len(hold_rows) < 100:
        print("ERROR: insufficient split", file=sys.stderr)
        return 1

    y_train = np.array([int(r.get("label", 0) or 0) for r in train_rows], dtype=np.int32)
    y_hold = np.array([int(r.get("label", 0) or 0) for r in hold_rows], dtype=np.int32)
    nr_hold = np.array([float(r.get("net_return", 0) or 0) for r in hold_rows], dtype=np.float64)
    nr_train = np.array([float(r.get("net_return", 0) or 0) for r in train_rows], dtype=np.float64)
    pool_hold = [(r.get("pool_address") or "").lower() for r in hold_rows]
    pool_train = [(r.get("pool_address") or "").lower() for r in train_rows]

    train_pos_rate = float(y_train.mean())
    hold_pos_rate = float(y_hold.mean())
    hold_base_median_nr = float(np.median(nr_hold))
    print(f"train_pos_rate={train_pos_rate:.4f} hold_pos_rate={hold_pos_rate:.4f}")

    scale_pos_weight = (len(y_train) - y_train.sum()) / max(1, y_train.sum())

    # Detect available sub-score columns (whale_val, etc.)
    available_subscores = [
        c for c in FEATURE_HEURISTIC_SUBSCORES
        if c in (train_rows[0] if train_rows else {})
    ]
    if available_subscores:
        print(f"heuristic sub-scores available: {available_subscores}")
    else:
        print("heuristic sub-scores NOT available in CSV (only final_score)")

    # Pool target encoding (from train only)
    pool_stats = _pool_target_encoding(train_rows, train_pos_rate)

    # ── Pre-build feature matrices for all feature groups ───────────────
    def _X(feat_list: list[str], rows_: list[dict]) -> np.ndarray:
        return _build_matrix(rows_, feat_list, pool_stats, train_pos_rate, available_subscores)

    # Full feature set (Patch G replication)
    FULL_FEATS = ALL_FEATURE_NAMES
    NO_POOL_FEATS = [f for f in FULL_FEATS if f not in FEATURE_POOL]
    NO_TIME_FEATS = [f for f in FULL_FEATS if f not in FEATURE_TIME]
    NO_POOL_NO_TIME_FEATS = FEATURE_HEURISTIC_BASE + FEATURE_BLOCK  # [final_score, log_index, log_index_log1p]
    FINAL_SCORE_ONLY_FEATS = ["final_score"]
    HEURISTIC_FEATS = FEATURE_HEURISTIC_BASE + available_subscores
    POOL_INVARIANT_FEATS = FEATURE_HEURISTIC_BASE + FEATURE_BLOCK  # pool-invariant numeric

    # Pre-compute matrices
    print("building feature matrices...")
    matrices: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "full": (_X(FULL_FEATS, train_rows), _X(FULL_FEATS, hold_rows)),
        "no_pool": (_X(NO_POOL_FEATS, train_rows), _X(NO_POOL_FEATS, hold_rows)),
        "no_time": (_X(NO_TIME_FEATS, train_rows), _X(NO_TIME_FEATS, hold_rows)),
        "no_pool_no_time": (_X(NO_POOL_NO_TIME_FEATS, train_rows), _X(NO_POOL_NO_TIME_FEATS, hold_rows)),
        "final_score_only": (_X(FINAL_SCORE_ONLY_FEATS, train_rows), _X(FINAL_SCORE_ONLY_FEATS, hold_rows)),
        "heuristic": (_X(HEURISTIC_FEATS, train_rows), _X(HEURISTIC_FEATS, hold_rows)),
        "pool_invariant": (_X(POOL_INVARIANT_FEATS, train_rows), _X(POOL_INVARIANT_FEATS, hold_rows)),
    }

    # ── Section A: Baselines ────────────────────────────────────────────
    print("\n=== Section A: Baselines ===")
    experiments: list[dict[str, Any]] = []

    # A1: Constant/random baseline
    p_random = np.full(len(y_hold), hold_pos_rate, dtype=np.float64)
    experiments.append(_run_score_baseline(
        "A1_constant_baseline",
        "Constant predict holdout_pos_rate",
        p_random, y_hold, nr_hold, pool_hold,
    ))

    # A2: final_score rank baseline (normalized to [0,1])
    fs_hold_raw = matrices["final_score_only"][1][:, 0]
    fs_tr_raw = matrices["final_score_only"][0][:, 0]
    fs_min, fs_max = fs_tr_raw.min(), fs_tr_raw.max()
    p_fs_norm = np.clip((fs_hold_raw - fs_min) / max(1e-9, fs_max - fs_min), 0.0, 1.0)
    experiments.append(_run_score_baseline(
        "A2_final_score_rank_baseline",
        "final_score normalized to [0,1]",
        p_fs_norm.astype(np.float64), y_hold, nr_hold, pool_hold,
    ))

    # A3: final_score-only logistic regression
    r_a3 = _run_experiment(
        "A3_logreg_final_score_only",
        "LogisticRegression on [final_score]",
        FINAL_SCORE_ONLY_FEATS,
        matrices["final_score_only"][0], y_train,
        matrices["final_score_only"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="logreg",
    )
    experiments.append(r_a3)

    # A4: final_score-only shallow XGBoost
    r_a4 = _run_experiment(
        "A4_xgb_final_score_only_shallow",
        "XGBoost shallow (depth=3, 100 trees) on [final_score]",
        FINAL_SCORE_ONLY_FEATS,
        matrices["final_score_only"][0], y_train,
        matrices["final_score_only"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=100, max_depth=3,
    )
    experiments.append(r_a4)

    # A5: heuristic components only (final_score + sub-scores if available)
    if available_subscores:
        heuristic_label = f"XGBoost on heuristic features: {HEURISTIC_FEATS}"
    else:
        heuristic_label = "XGBoost on [final_score] only (sub-scores absent in CSV)"
    r_a5 = _run_experiment(
        "A5_heuristic_only",
        heuristic_label,
        HEURISTIC_FEATS,
        matrices["heuristic"][0], y_train,
        matrices["heuristic"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=6,
    )
    experiments.append(r_a5)

    # ── Section B: Ablation models ──────────────────────────────────────
    print("\n=== Section B: Ablation models ===")

    # B1: Full model reproduced (identical to Patch G)
    r_b1 = _run_experiment(
        "B1_xgb_full",
        "XGBoost full model (all 11 features — Patch G replication)",
        FULL_FEATS,
        matrices["full"][0], y_train,
        matrices["full"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=6,
    )
    experiments.append(r_b1)

    # B2: No pool features
    r_b2 = _run_experiment(
        "B2_xgb_no_pool",
        "XGBoost — no pool features (no pool_train_rate, no pool_train_log_count)",
        NO_POOL_FEATS,
        matrices["no_pool"][0], y_train,
        matrices["no_pool"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=6,
    )
    experiments.append(r_b2)

    # B3: No time features
    r_b3 = _run_experiment(
        "B3_xgb_no_time",
        "XGBoost — no time features (no hour/dow/sin/cos)",
        NO_TIME_FEATS,
        matrices["no_time"][0], y_train,
        matrices["no_time"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=6,
    )
    experiments.append(r_b3)

    # B4: No pool + no time features
    r_b4 = _run_experiment(
        "B4_xgb_no_pool_no_time",
        "XGBoost — no pool, no time features (only final_score + block pos)",
        NO_POOL_NO_TIME_FEATS,
        matrices["no_pool_no_time"][0], y_train,
        matrices["no_pool_no_time"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=6,
    )
    experiments.append(r_b4)

    # B5: No pool + no time + no log_index
    r_b5 = _run_experiment(
        "B5_xgb_no_pool_no_time_no_logindex",
        "XGBoost — no pool, no time, no block pos (only final_score)",
        FINAL_SCORE_ONLY_FEATS,
        matrices["final_score_only"][0], y_train,
        matrices["final_score_only"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=6,
    )
    experiments.append(r_b5)

    # B6: Only pool-invariant numerical features (full XGB, depth=4)
    r_b6 = _run_experiment(
        "B6_xgb_pool_invariant_numerical",
        "XGBoost (depth=4) — only pool-invariant: final_score + block pos",
        POOL_INVARIANT_FEATS,
        matrices["pool_invariant"][0], y_train,
        matrices["pool_invariant"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=400, max_depth=4,
    )
    experiments.append(r_b6)

    # B7: Shallow tree pool-agnostic model
    r_b7 = _run_experiment(
        "B7_xgb_shallow_pool_agnostic",
        "XGBoost shallow (depth=2, 50 trees) — pool-invariant features",
        POOL_INVARIANT_FEATS,
        matrices["pool_invariant"][0], y_train,
        matrices["pool_invariant"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="xgb",
        n_trees=50, max_depth=2,
    )
    experiments.append(r_b7)

    # B8: Monotonic/simple model — logistic regression on pool-invariant
    r_b8 = _run_experiment(
        "B8_logreg_pool_agnostic",
        "LogisticRegression — pool-invariant: final_score + block pos",
        POOL_INVARIANT_FEATS,
        matrices["pool_invariant"][0], y_train,
        matrices["pool_invariant"][1], y_hold,
        nr_hold, pool_hold,
        scale_pos_weight, seed,
        model_type="logreg",
    )
    experiments.append(r_b8)

    # ── Gate evaluation ────────────────────────────────────────────────
    pool_agnostic_exp_names = {
        "A3_logreg_final_score_only",
        "A4_xgb_final_score_only_shallow",
        "A5_heuristic_only",
        "B2_xgb_no_pool",
        "B4_xgb_no_pool_no_time",
        "B5_xgb_no_pool_no_time_no_logindex",
        "B6_xgb_pool_invariant_numerical",
        "B7_xgb_shallow_pool_agnostic",
        "B8_logreg_pool_agnostic",
    }

    gate_results: dict[str, dict[str, Any]] = {}
    for exp in experiments:
        nm = exp["name"]
        if nm in pool_agnostic_exp_names:
            gate_results[nm] = _eval_gates_pool_agnostic(
                exp, hold_pos_rate, hold_base_median_nr
            )

    any_signal_found = any(g["all_pass"] for g in gate_results.values())

    # ── Rankings ──────────────────────────────────────────────────────
    def _roc(e: dict) -> float:
        return e["holdout_metrics"].get("roc_auc") or 0.0

    def _dec_pos(e: dict) -> float:
        return e["decile_analysis"]["top_decile"].get("pos_rate") or 0.0

    def _sens5_roc(e: dict) -> float:
        return e["sensitivity"]["remove_top5_pools"].get("roc_auc") or 0.0

    best_by_roc = max(experiments, key=_roc)
    best_by_dec_pos = max(experiments, key=_dec_pos)
    best_by_sens5 = max(experiments, key=_sens5_roc)

    # best pool-agnostic by ROC
    pa_exps = [e for e in experiments if e["name"] in pool_agnostic_exp_names]
    best_pa_by_roc = max(pa_exps, key=_roc) if pa_exps else None

    # ── Diagnosis questions ────────────────────────────────────────────
    # 1. Did removing pool features improve or worsen holdout?
    roc_full = _roc(r_b1)
    roc_no_pool = _roc(r_b2)
    pool_removal_effect = "improved" if roc_no_pool > roc_full else "worsened"

    # 2. Did removing time features improve or worsen holdout?
    roc_no_time = _roc(r_b3)
    time_removal_effect = "improved" if roc_no_time > roc_full else "worsened"

    # 3. Does final_score contain any usable ranking signal?
    roc_fs_rank = _roc(experiments[1])  # A2
    final_score_signal = roc_fs_rank > 0.50 or _roc(r_a3) > 0.50 or _roc(r_a4) > 0.50
    final_score_note = (
        f"A2 rank ROC={roc_fs_rank:.4f}, "
        f"A3 logreg ROC={_roc(r_a3):.4f}, "
        f"A4 xgb ROC={_roc(r_a4):.4f}"
    )

    # 4. Are average returns driven by extreme right-tail outliers?
    td_full = r_b1["decile_analysis"]["top_decile"]
    avg_nr = td_full.get("avg_net_return") or 0.0
    med_nr = td_full.get("median_net_return") or 0.0
    return_tail_artifact = avg_nr > 0 and med_nr < 0
    return_tail_note = f"top-decile avg_nr={avg_nr:.4f} median_nr={med_nr:.4f}"

    # 5. Is classification target worse than regression (use of median/mean)?
    # Approximate: if top-decile avg>0 but median<0, means skewed distribution; classification noisy
    classification_noisy = return_tail_artifact

    # 6. Should Patch G2 focus on new pool-invariant features?
    best_pa_roc = _roc(best_pa_by_roc) if best_pa_by_roc else 0.0
    need_new_features = best_pa_roc < 0.53

    # 7. Should Aero vAMM research start in parallel?
    start_aero_parallel = best_pa_roc < 0.52 and not any_signal_found

    # ── Conclusion ─────────────────────────────────────────────────────
    if any_signal_found:
        conclusion = "PATCH_G1_POOL_AGNOSTIC_SIGNAL_FOUND"
    elif best_pa_roc < 0.52:
        # Check dominant failure mode
        final_score_usable = _roc(r_a4) > 0.52 or _roc(r_a3) > 0.52
        if not final_score_usable:
            conclusion = "HOLD_DUE_TO_FINAL_SCORE_NOT_PREDICTIVE"
        elif return_tail_artifact:
            conclusion = "HOLD_DUE_TO_RETURN_TAIL_ARTIFACT"
        else:
            conclusion = "HOLD_DUE_TO_NO_POOL_AGNOSTIC_SIGNAL"
    else:
        # Some ROC improvement but didn't pass all gates
        top1_full = r_b1["decile_analysis"]["top_decile"].get("top1_pool_share") or 1.0
        best_top1 = best_pa_by_roc["decile_analysis"]["top_decile"].get("top1_pool_share") or 1.0
        if best_top1 > 0.50:
            conclusion = "HOLD_DUE_TO_POOL_ROTATION_DOMINANCE"
        else:
            conclusion = "HOLD_DUE_TO_NO_POOL_AGNOSTIC_SIGNAL"

    # ── Save holdout predictions CSV (best pool-agnostic model) ────────
    best_pred_exp = best_pa_by_roc if best_pa_by_roc else best_by_roc
    p_best = best_pred_exp.get("y_prob")
    out_pred_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_pred_csv.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "tx_hash", "log_index", "pool_address", "timestamp",
            "label", "net_return",
            "prob_positive_best", "pred_label_05", "score_rank",
            "best_model",
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        rank_order = np.argsort(p_best)[::-1]
        rank_map = {int(rank_order[i]): i + 1 for i in range(len(rank_order))}
        for i, r in enumerate(hold_rows):
            w.writerow({
                "tx_hash": r.get("tx_hash", ""),
                "log_index": r.get("log_index", ""),
                "pool_address": r.get("pool_address", ""),
                "timestamp": r.get("timestamp", ""),
                "label": r.get("label", ""),
                "net_return": r.get("net_return", ""),
                "prob_positive_best": round(float(p_best[i]), 8),
                "pred_label_05": int(p_best[i] >= 0.5),
                "score_rank": rank_map[i],
                "best_model": best_pred_exp["name"],
            })

    # Strip y_prob arrays before JSON serialization
    for exp in experiments:
        exp.pop("y_prob", None)

    # ── Build report ──────────────────────────────────────────────────
    report: dict[str, Any] = {
        "patch": "G1",
        "conclusion": conclusion,
        "conclusion_pass": any_signal_found,
        "holdout_base_pos_rate": _safe(hold_pos_rate),
        "holdout_base_median_nr": _safe(hold_base_median_nr),
        "split": {
            "train_rows": len(train_rows),
            "holdout_rows": len(hold_rows),
            "train_pos_rate": _safe(train_pos_rate),
            "holdout_pos_rate": _safe(hold_pos_rate),
            "holdout_lo_ms": holdout_lo_ms,
            "split_method": "time_series_strict",
        },
        "experiments": experiments,
        "gate_results": gate_results,
        "rankings": {
            "best_by_holdout_roc_auc": {
                "name": best_by_roc["name"],
                "roc_auc": _roc(best_by_roc),
            },
            "best_by_top_decile_pos_rate": {
                "name": best_by_dec_pos["name"],
                "top_decile_pos_rate": _dec_pos(best_by_dec_pos),
            },
            "best_by_remove_top5_roc": {
                "name": best_by_sens5["name"],
                "remove_top5_roc_auc": _sens5_roc(best_by_sens5),
            },
            "best_pool_agnostic_by_roc": {
                "name": best_pa_by_roc["name"] if best_pa_by_roc else None,
                "roc_auc": _roc(best_pa_by_roc) if best_pa_by_roc else None,
            },
        },
        "diagnosis": {
            "q1_pool_removal_effect": pool_removal_effect,
            "q1_roc_full": _safe(roc_full),
            "q1_roc_no_pool": _safe(roc_no_pool),
            "q2_time_removal_effect": time_removal_effect,
            "q2_roc_no_time": _safe(roc_no_time),
            "q3_final_score_has_signal": final_score_signal,
            "q3_note": final_score_note,
            "q4_return_tail_artifact": return_tail_artifact,
            "q4_note": return_tail_note,
            "q5_classification_target_noisy": classification_noisy,
            "q6_should_redesign_features": need_new_features,
            "q7_should_start_aero_parallel": start_aero_parallel,
        },
        "success_gates_summary": {
            "any_pool_agnostic_signal_found": any_signal_found,
            "best_pool_agnostic_roc": _safe(best_pa_roc),
            "threshold_roc": 0.53,
        },
        "leakage_guard": sorted(LEAKAGE_GUARD_FIELDS),
        "note": "DRY-RUN ONLY — Patch G1 offline analysis, not promoted, not deployed",
    }

    out_report_json.parent.mkdir(parents=True, exist_ok=True)
    with out_report_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {out_report_json}")

    # ── Markdown report ────────────────────────────────────────────────
    out_report_md.parent.mkdir(parents=True, exist_ok=True)

    def _exp_row(e: dict[str, Any]) -> str:
        m = e["holdout_metrics"]
        dec = e["decile_analysis"]["top_decile"]
        sens5 = e["sensitivity"]["remove_top5_pools"]
        g = gate_results.get(e["name"])
        gate_pass = ("✔" if g["all_pass"] else "✘") if g else "—"
        return (
            f"| `{e['name']}` "
            f"| {m.get('roc_auc')} "
            f"| {m.get('pr_auc')} "
            f"| {m.get('accuracy')} "
            f"| {m.get('precision')} "
            f"| {m.get('recall')} "
            f"| {m.get('f1')} "
            f"| {m.get('log_loss')} "
            f"| {m.get('brier_score')} "
            f"| {dec.get('pos_rate')} "
            f"| {dec.get('avg_net_return')} "
            f"| {dec.get('median_net_return')} "
            f"| {dec.get('p25_net_return')} "
            f"| {dec.get('top1_pool_share')} "
            f"| {sens5.get('roc_auc')} "
            f"| {e.get('calibration_max_deviation')} "
            f"| {gate_pass} |"
        )

    def _exp_section(e: dict[str, Any]) -> str:
        m = e["holdout_metrics"]
        dec_td = e["decile_analysis"]["top_decile"]
        dec_tq = e["decile_analysis"]["top_quintile"]
        dec_all = e["decile_analysis"]["all"]
        sens1 = e["sensitivity"]["remove_top1_pool"]
        sens5 = e["sensitivity"]["remove_top5_pools"]
        fi_str = ""
        if e.get("feature_importance"):
            fi_sorted = sorted(e["feature_importance"].items(), key=lambda x: -(x[1] or 0))
            fi_str = "\n\n**Feature Importance:**\n\n| Feature | Importance |\n|---------|----------:|\n"
            fi_str += "\n".join(f"| `{k}` | {v} |" for k, v in fi_sorted)
        if e.get("feature_coefficients"):
            coef_sorted = sorted(e["feature_coefficients"].items(), key=lambda x: -(abs(x[1] or 0)))
            fi_str = "\n\n**Coefficients:**\n\n| Feature | Coef |\n|---------|-----:|\n"
            fi_str += "\n".join(f"| `{k}` | {v} |" for k, v in coef_sorted)
        g = gate_results.get(e["name"])
        gate_str = ""
        if g:
            gate_str = "\n\n**Gates:**\n\n| Gate | Pass |\n|------|:----:|\n"
            gate_str += "\n".join(
                f"| `{gk}` | {'✔' if gv else '✘'} |"
                for gk, gv in g["gates"].items()
            )
            gate_str += f"\n\n**All gates pass:** {'✔ YES' if g['all_pass'] else '✘ NO'}"
        tr_roc = None
        if e.get("train_metrics"):
            tr_roc = e["train_metrics"].get("roc_auc")

        return f"""
### {e['name']}

> {e['label']}

**Model:** {e['model_type']} | **Features ({e['n_features']}):** `{e['features']}`

| Metric | Holdout | Train |
|--------|--------:|------:|
| ROC-AUC | **{m.get('roc_auc')}** | {tr_roc} |
| PR-AUC | {m.get('pr_auc')} | — |
| Log loss | {m.get('log_loss')} | — |
| Brier score | {m.get('brier_score')} | — |
| Accuracy | {m.get('accuracy')} | — |
| Precision | {m.get('precision')} | — |
| Recall | {m.get('recall')} | — |
| F1 | {m.get('f1')} | — |
| Calibration max dev | {e.get('calibration_max_deviation')} | — |

**Decile / Quintile Analysis (Holdout):**

| Bucket | n | pos_rate | avg_nr | median_nr | p25_nr | top1_pool_share |
|--------|--:|--------:|-------:|----------:|-------:|---------------:|
| Top decile | {dec_td['n']} | {dec_td['pos_rate']} | {dec_td['avg_net_return']} | {dec_td['median_net_return']} | {dec_td['p25_net_return']} | {dec_td['top1_pool_share']} |
| Top quintile | {dec_tq['n']} | {dec_tq['pos_rate']} | {dec_tq['avg_net_return']} | {dec_tq['median_net_return']} | {dec_tq['p25_net_return']} | {dec_tq['top1_pool_share']} |
| All holdout | {dec_all['n']} | {dec_all['pos_rate']} | {dec_all['avg_net_return']} | {dec_all['median_net_return']} | — | — |

**Sensitivity:**

| Remove | n_remaining | ROC-AUC | top_dec_pos | top1_share_in_dec |
|--------|----------:|--------:|------------:|------------------:|
| Top-1 pool | {sens1.get('n_remaining')} | {sens1.get('roc_auc')} | {sens1.get('top_decile_pos_rate')} | {sens1.get('top1_pool_share_in_top_decile')} |
| Top-5 pools | {sens5.get('n_remaining')} | {sens5.get('roc_auc')} | {sens5.get('top_decile_pos_rate')} | {sens5.get('top1_pool_share_in_top_decile')} |
{fi_str}{gate_str}
"""

    summary_rows = "\n".join(_exp_row(e) for e in experiments)
    all_sections = "\n".join(_exp_section(e) for e in experiments)
    diag = report["diagnosis"]
    ranks = report["rankings"]

    md = f"""# Run-004 Patch G1 — Pool-Agnostic Ablation + Feature Failure Diagnosis

**Conclusion**: `{conclusion}`  
**Conclusion pass**: `{any_signal_found}`  
**Date**: offline analysis — DRY-RUN ONLY, not promoted, not deployed.

---

## 0. Context

Previous result (Patch G): `HOLD_DUE_TO_NO_HOLDOUT_EDGE`
- Train ROC-AUC = 0.9115 / Holdout ROC-AUC = 0.4823 → gap = 0.429
- pool_train_rate importance = 0.292; time features dominate
- Only 5/239 training pools overlap with holdout
- 51% holdout rows from unseen pools
- final_score correlation with label ≈ −0.05

**Goal:** diagnose whether any pool-agnostic signal remains after removing pool/time leakage-like features.

---

## 1. Split

| Split | Rows | pos_rate |
|-------|-----:|--------:|
| Train | {len(train_rows)} | {_safe(train_pos_rate)} |
| Holdout | {len(hold_rows)} | {_safe(hold_pos_rate)} |

Holdout base median net_return: `{_safe(hold_base_median_nr)}`

---

## 2. Experiment Summary

| Experiment | ROC-AUC | PR-AUC | Acc | Prec | Rec | F1 | LogLoss | Brier | TopDec_pos | TopDec_avgNR | TopDec_medNR | TopDec_p25NR | TopDec_top1pool | Sens5_ROC | Cal_MaxDev | Gates |
|------------|--------:|-------:|----:|-----:|----:|---:|--------:|------:|-----------:|------------:|------------:|-------------:|---------------:|----------:|-----------:|:-----:|
{summary_rows}

Holdout base positive_rate = `{_safe(hold_pos_rate)}`

---

## 3. Rankings

| Category | Best Experiment | Value |
|----------|----------------|------:|
| Best holdout ROC-AUC (all) | `{ranks['best_by_holdout_roc_auc']['name']}` | {ranks['best_by_holdout_roc_auc']['roc_auc']} |
| Best top-decile pos_rate (all) | `{ranks['best_by_top_decile_pos_rate']['name']}` | {ranks['best_by_top_decile_pos_rate']['top_decile_pos_rate']} |
| Best after removing top-5 pools | `{ranks['best_by_remove_top5_roc']['name']}` | {ranks['best_by_remove_top5_roc']['remove_top5_roc_auc']} |
| Best pool-agnostic by ROC | `{ranks['best_pool_agnostic_by_roc']['name']}` | {ranks['best_pool_agnostic_by_roc']['roc_auc']} |

---

## 4. Success Gates (Patch G1 — pool-agnostic threshold ≥ 0.53)

Any pool-agnostic signal found: **{'YES ✔' if any_signal_found else 'NO ✘'}**
Best pool-agnostic ROC-AUC: **{_safe(best_pa_roc)}** (threshold: 0.53)

---

## 5. Per-Experiment Detail

{all_sections}

---

## 6. Diagnosis

| Question | Answer |
|----------|--------|
| Q1: Did removing pool features improve holdout? | **{diag['q1_pool_removal_effect']}** (ROC: full={diag['q1_roc_full']} → no-pool={diag['q1_roc_no_pool']}) |
| Q2: Did removing time features improve holdout? | **{diag['q2_time_removal_effect']}** (ROC: full={diag['q1_roc_full']} → no-time={diag['q2_roc_no_time']}) |
| Q3: Does final_score have usable ranking signal? | **{'YES' if diag['q3_final_score_has_signal'] else 'NO'}** — {diag['q3_note']} |
| Q4: Are avg returns driven by right-tail outliers? | **{'YES' if diag['q4_return_tail_artifact'] else 'NO'}** — {diag['q4_note']} |
| Q5: Is classification target noisier than regression? | **{'YES' if diag['q5_classification_target_noisy'] else 'NO'}** |
| Q6: Should Patch G2 focus on new pool-invariant features? | **{'YES' if diag['q6_should_redesign_features'] else 'MAYBE'}** |
| Q7: Should Aero vAMM research start in parallel? | **{'YES' if diag['q7_should_start_aero_parallel'] else 'CONSIDER'}** |

---

## 7. Recommendation

**Conclusion: `{conclusion}`**

{'### Pool-agnostic signal found — proceed to G2 feature validation.' if any_signal_found else '### No pool-agnostic signal found at current feature level.'}

- **Patch G2 feature redesign**: {'Recommended — build pool-invariant features (reserves ratio, price impact, block congestion)' if diag['q6_should_redesign_features'] else 'May not help — signal deficit is fundamental'}
- **Abandon V2 model**: {'Do NOT abandon — signal exists in pool-agnostic features' if any_signal_found else 'Consider unless G2 finds new features'}
- **Start Aero vAMM research in parallel**: {'YES — V2 pool-agnostic signal is insufficient to justify exclusive V2 focus' if diag['q7_should_start_aero_parallel'] else 'Optional — focus on V2 G2 first'}

---

## 8. Artifacts

| Artifact | Path |
|----------|------|
| Ablation report (JSON) | `mvp/diagnostics/run004_patch_g1_pool_agnostic_ablation_report.json` |
| This report (MD) | `mvp/runs/run-004-patch-g1-pool-agnostic-ablation.md` |
| Best-model predictions CSV | `logs/run004_patch_g1_ablation_predictions.csv` |
| Input canonical CSV | `logs/run004_v2only_prepared_dryrun_canonical.csv` |
| Patch G report (reference) | `mvp/diagnostics/run004_patch_g_retrain_dryrun_report.json` |
"""

    with out_report_md.open("w", encoding="utf-8") as f:
        f.write(md)
    print(f"wrote {out_report_md}")
    print(f"wrote {out_pred_csv}")

    print(f"\n=== CONCLUSION: {conclusion} ===")
    print(f"any_signal_found={any_signal_found}")
    print(f"best_pool_agnostic_roc={_safe(best_pa_roc)}")
    print(f"best_overall_roc={_roc(best_by_roc):.4f} ({best_by_roc['name']})")
    print(f"best_top_dec_pos_rate={_dec_pos(best_by_dec_pos):.4f} ({best_by_dec_pos['name']})")
    print(f"Q1 pool removal: {pool_removal_effect}")
    print(f"Q2 time removal: {time_removal_effect}")
    print(f"Q3 final_score signal: {final_score_signal} ({final_score_note})")
    print(f"Q4 return tail artifact: {return_tail_artifact} ({return_tail_note})")
    print(f"Q7 start Aero parallel: {start_aero_parallel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
