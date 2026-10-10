"""
Run-004 Patch G: V2-only retrain dry-run.

Reads  logs/run004_v2only_prepared_dryrun_canonical.csv
Writes models/signal_model_v2only_run004_patch_g_dryrun.json
       mvp/diagnostics/run004_patch_g_retrain_dryrun_report.json
       mvp/runs/run-004-patch-g-v2only-retrain-dryrun.md
       logs/run004_patch_g_holdout_predictions.csv

Hard prohibitions (enforced in-script):
  - No LIVE_TRADING_MODE
  - No model promotion
  - net_return is not used as a training feature (label-leakage guard)
  - entry_block / exit_block not used as features (time-monotone leakage guard)
  - No random split: strict time-series cut at holdout_lo_ms
  - No V3/Aero data expected (pool_type == v2 enforced)

Env:
  RUN004_G_CANONICAL_CSV  path to canonical CSV
  RUN004_G_HOLDOUT_LO_MS  holdout cutoff (default 1776520584003)
  RUN004_G_N_TREES        default 400
  RUN004_G_MAX_DEPTH      default 6
  RUN004_G_LR             default 0.05
  RUN004_G_SEED           default 42
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
    "1", "true", "yes", "on"
), "LIVE_TRADING_MODE must not be enabled during Patch G dry-run"

import numpy as np

try:
    import xgboost as xgb
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False

try:
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        roc_auc_score, average_precision_score,
        log_loss, brier_score_loss,
        precision_recall_fscore_support, accuracy_score,
        confusion_matrix,
    )
    from sklearn.preprocessing import StandardScaler
    _HAS_SKL = True
except ImportError:
    _HAS_SKL = False

if not (_HAS_XGB or _HAS_SKL):
    print("ERROR: need xgboost or scikit-learn", file=sys.stderr)
    sys.exit(1)


# ── Constants ────────────────────────────────────────────────────────
HOLDOUT_LO_MS_DEFAULT = 1776520584003
LEAKAGE_GUARD_FIELDS = {
    "net_return", "entry_block", "exit_block", "source_row_id",
    "relabel_status", "rpc_calls_this_key", "label_reason",
    "reject_reason", "candidate_status", "pre_check", "topic0",
    "event_kind", "pool_type",
}

POOL_SMOOTH_N = 10     # additive smoothing for pool target encoding


# ── Helper functions ─────────────────────────────────────────────────

def _pct(sv: list[float], q: float) -> float:
    if not sv:
        return float("nan")
    n = len(sv)
    k = (n - 1) * q
    lo, hi = int(k), min(int(k) + 1, n - 1)
    return sv[lo] * (1 - (k - lo)) + sv[hi] * (k - lo)


def _safe(v: float | None, ndigits: int = 6) -> float | None:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return round(float(v), ndigits)


def _parse_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _build_features(
    rows: list[dict[str, str]],
    pool_stats: dict[str, tuple[float, int]] | None,
    global_pos_rate: float,
) -> np.ndarray:
    """
    Feature vector per row (all float):
      0  final_score
      1  log_index (float, raw)
      2  log_index_log1p (log1p of above)
      3  hour_of_day (0–23)
      4  sin_hour  (cyclic encoding)
      5  cos_hour
      6  day_of_week (0–6)
      7  sin_dow
      8  cos_dow
      9  pool_train_rate  (smoothed target encoding from train; NaN → global_pos_rate)
      10 pool_train_log_count

    Leakage fields explicitly excluded:
      net_return, entry_block, exit_block — never included.
    """
    X: list[list[float]] = []
    for r in rows:
        fs = float(r.get("final_score", 0) or 0)
        li = float(r.get("log_index", 0) or 0)
        ts_ms = int(r.get("timestamp", 0) or 0)
        ts_sec = ts_ms // 1000
        hour = (ts_sec % 86400) // 3600
        dow = (ts_sec // 86400) % 7
        sin_h = math.sin(2 * math.pi * hour / 24)
        cos_h = math.cos(2 * math.pi * hour / 24)
        sin_d = math.sin(2 * math.pi * dow / 7)
        cos_d = math.cos(2 * math.pi * dow / 7)
        pool = (r.get("pool_address") or "").lower()
        if pool_stats and pool in pool_stats:
            p_rate, p_cnt = pool_stats[pool]
        else:
            p_rate, p_cnt = global_pos_rate, 0
        X.append([
            fs,
            li,
            math.log1p(li),
            float(hour),
            sin_h,
            cos_h,
            float(dow),
            sin_d,
            cos_d,
            p_rate,
            math.log1p(float(p_cnt)),
        ])
    return np.array(X, dtype=np.float32)


FEATURE_NAMES = [
    "final_score",
    "log_index",
    "log_index_log1p",
    "hour_of_day",
    "sin_hour",
    "cos_hour",
    "day_of_week",
    "sin_dow",
    "cos_dow",
    "pool_train_rate",
    "pool_train_log_count",
]


def _pool_target_encoding(
    train_rows: list[dict[str, str]],
    global_pos_rate: float,
) -> dict[str, tuple[float, int]]:
    """Smoothed pool-level positive rate from training rows only."""
    pool_pos: dict[str, int] = defaultdict(int)
    pool_cnt: dict[str, int] = defaultdict(int)
    for r in train_rows:
        pool = (r.get("pool_address") or "").lower()
        lbl = int(r.get("label", 0) or 0)
        pool_pos[pool] += lbl
        pool_cnt[pool] += 1
    stats: dict[str, tuple[float, int]] = {}
    for pool, cnt in pool_cnt.items():
        smoothed = (pool_pos[pool] + POOL_SMOOTH_N * global_pos_rate) / (cnt + POOL_SMOOTH_N)
        stats[pool] = (smoothed, cnt)
    return stats


def _train_xgb(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    n_trees: int,
    max_depth: int,
    lr: float,
    seed: int,
    scale_pos_weight: float,
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


def _metrics(y_true: np.ndarray, y_prob: np.ndarray, threshold: float = 0.5) -> dict[str, Any]:
    y_pred = (y_prob >= threshold).astype(int)
    prec, rec, f1, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", zero_division=0)
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
        ll = log_loss(y_true, y_prob)
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


def _calibration_buckets(
    y_true: np.ndarray, y_prob: np.ndarray, n_buckets: int = 10
) -> list[dict[str, Any]]:
    edges = np.linspace(0, 1, n_buckets + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        if mask.sum() == 0:
            continue
        out.append({
            "bucket": f"[{lo:.2f},{hi:.2f})",
            "count": int(mask.sum()),
            "mean_pred": _safe(float(y_prob[mask].mean())),
            "actual_pos_rate": _safe(float(y_true[mask].mean())),
        })
    return out


def _threshold_sweep(
    y_true: np.ndarray, y_prob: np.ndarray
) -> list[dict[str, Any]]:
    out = []
    for thr in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        m = _metrics(y_true, y_prob, thr)
        out.append({
            "threshold": thr,
            "precision": m["precision"],
            "recall": m["recall"],
            "f1": m["f1"],
            "n_positive_predicted": m["n_positive_predicted"],
            "coverage": _safe(m["n_positive_predicted"] / max(1, len(y_true))),
        })
    return out


def _decile_analysis(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    net_returns: np.ndarray,
    pool_addrs: list[str],
    label: str = "holdout",
) -> dict[str, Any]:
    idx = np.argsort(y_prob)[::-1]  # descending score
    n = len(idx)
    n_decile = max(1, n // 10)
    n_quintile = max(1, n // 5)

    def _stats(sel_idx: np.ndarray) -> dict[str, Any]:
        yt = y_true[sel_idx]
        nr = net_returns[sel_idx]
        pa = [pool_addrs[i] for i in sel_idx]
        pc = Counter(pa)
        top1_p = pc.most_common(1)[0][1] / max(1, len(sel_idx)) if pc else 0.0
        return {
            "n": int(len(sel_idx)),
            "pos_rate": _safe(float(yt.mean())),
            "avg_net_return": _safe(float(nr.mean())),
            "median_net_return": _safe(float(np.median(nr))),
            "top1_pool_share": _safe(top1_p),
        }

    return {
        "top_decile": _stats(idx[:n_decile]),
        "top_quintile": _stats(idx[:n_quintile]),
        "bottom_decile": _stats(idx[-n_decile:]),
        "all": _stats(idx),
    }


def _sensitivity_remove_pools(
    X_all: np.ndarray,
    y_all: np.ndarray,
    y_prob_all: np.ndarray,
    net_returns_all: np.ndarray,
    pool_addrs_all: list[str],
    top_n: int = 5,
) -> dict[str, Any]:
    from sklearn.metrics import roc_auc_score as ras, average_precision_score as aps
    # identify top pools by count in dataset
    pc = Counter(pool_addrs_all)
    top_pools = {p for p, _ in pc.most_common(top_n)}
    mask = np.array([p not in top_pools for p in pool_addrs_all])
    if mask.sum() < 10:
        return {"n_remaining": int(mask.sum()), "roc_auc": None, "top_decile_avg_net_return": None}
    yt = y_all[mask]
    yp = y_prob_all[mask]
    nr = net_returns_all[mask]
    pa = [pool_addrs_all[i] for i in range(len(pool_addrs_all)) if mask[i]]
    idx = np.argsort(yp)[::-1]
    n_dec = max(1, len(idx) // 10)
    try:
        rauc = _safe(ras(yt, yp))
    except Exception:
        rauc = None
    return {
        "n_remaining": int(mask.sum()),
        "roc_auc": rauc,
        "top_decile_avg_net_return": _safe(float(nr[idx[:n_dec]].mean())),
        "top_decile_pos_rate": _safe(float(yt[idx[:n_dec]].mean())),
        "top1_pool_share_in_top_decile": _safe(
            Counter([pa[i] for i in idx[:n_dec]]).most_common(1)[0][1] / max(1, n_dec)
            if pa else 0.0
        ),
    }


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    canonical_csv = Path(os.environ.get(
        "RUN004_G_CANONICAL_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"),
    ))
    holdout_lo_ms = int(os.environ.get("RUN004_G_HOLDOUT_LO_MS", str(HOLDOUT_LO_MS_DEFAULT)))
    n_trees = int(os.environ.get("RUN004_G_N_TREES", "400"))
    max_depth = int(os.environ.get("RUN004_G_MAX_DEPTH", "6"))
    lr = float(os.environ.get("RUN004_G_LR", "0.05"))
    seed = int(os.environ.get("RUN004_G_SEED", "42"))

    out_model_json = Path(os.environ.get(
        "RUN004_G_MODEL_JSON",
        str(REPO / "models" / "signal_model_v2only_run004_patch_g_dryrun.json"),
    ))
    out_report_json = Path(os.environ.get(
        "RUN004_G_REPORT_JSON",
        str(REPO / "mvp" / "diagnostics" / "run004_patch_g_retrain_dryrun_report.json"),
    ))
    out_report_md = Path(os.environ.get(
        "RUN004_G_REPORT_MD",
        str(REPO / "mvp" / "runs" / "run-004-patch-g-v2only-retrain-dryrun.md"),
    ))
    out_pred_csv = Path(os.environ.get(
        "RUN004_G_HOLDOUT_PRED_CSV",
        str(REPO / "logs" / "run004_patch_g_holdout_predictions.csv"),
    ))

    if not canonical_csv.is_file():
        print(f"ERROR: missing {canonical_csv}", file=sys.stderr)
        return 1

    print(f"loading {canonical_csv}")
    rows = _parse_rows(canonical_csv)

    # ── Enforce V2-only ──────────────────────────────────────────────
    non_v2 = [r for r in rows if (r.get("pool_type") or "").lower() != "v2"]
    if non_v2:
        print(f"WARNING: {len(non_v2)} non-v2 rows found — filtering out", file=sys.stderr)
        rows = [r for r in rows if (r.get("pool_type") or "").lower() == "v2"]

    # ── Time-series split ────────────────────────────────────────────
    train_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) < holdout_lo_ms]
    hold_rows = [r for r in rows if int(r.get("timestamp", 0) or 0) >= holdout_lo_ms]
    print(f"train={len(train_rows)} holdout={len(hold_rows)}")

    if len(train_rows) < 100 or len(hold_rows) < 100:
        print("ERROR: insufficient split", file=sys.stderr)
        return 1

    # ── Labels ──────────────────────────────────────────────────────
    y_train = np.array([int(r.get("label", 0) or 0) for r in train_rows], dtype=np.int32)
    y_hold = np.array([int(r.get("label", 0) or 0) for r in hold_rows], dtype=np.int32)

    train_pos_rate = float(y_train.mean())
    hold_pos_rate = float(y_hold.mean())
    print(f"train_pos_rate={train_pos_rate:.4f} hold_pos_rate={hold_pos_rate:.4f}")

    # ── Pool target encoding (from train only) ───────────────────────
    pool_stats = _pool_target_encoding(train_rows, train_pos_rate)

    # ── Features ────────────────────────────────────────────────────
    X_train = _build_features(train_rows, pool_stats, train_pos_rate)
    X_hold = _build_features(hold_rows, pool_stats, train_pos_rate)

    # Net returns (for analysis only, NEVER a training feature)
    nr_train = np.array([float(r.get("net_return", 0) or 0) for r in train_rows], dtype=np.float64)
    nr_hold = np.array([float(r.get("net_return", 0) or 0) for r in hold_rows], dtype=np.float64)
    pool_hold = [(r.get("pool_address") or "").lower() for r in hold_rows]
    pool_train = [(r.get("pool_address") or "").lower() for r in train_rows]

    # ── Feature null / non-finite check ─────────────────────────────
    null_counts = np.isnan(X_train).sum(axis=0).tolist()
    inf_counts = (~np.isfinite(X_train)).sum(axis=0).tolist()
    feat_check = {
        FEATURE_NAMES[i]: {"null": int(null_counts[i]), "non_finite": int(inf_counts[i])}
        for i in range(len(FEATURE_NAMES))
    }
    bad_feats = [k for k, v in feat_check.items() if v["null"] > 0 or v["non_finite"] > 0]
    if bad_feats:
        print(f"WARNING: null/non-finite in features: {bad_feats}", file=sys.stderr)
        # Replace with 0
        X_train = np.nan_to_num(X_train, nan=0.0, posinf=0.0, neginf=0.0)
        X_hold = np.nan_to_num(X_hold, nan=0.0, posinf=0.0, neginf=0.0)

    scale_pos_weight = (len(y_train) - y_train.sum()) / max(1, y_train.sum())
    print(f"scale_pos_weight={scale_pos_weight:.4f}")

    # ── Train XGBoost model ──────────────────────────────────────────
    print("training XGBoost...")
    if not _HAS_XGB:
        print("ERROR: xgboost required", file=sys.stderr)
        return 1
    model = _train_xgb(X_train, y_train, n_trees, max_depth, lr, seed, scale_pos_weight)
    print("training complete")

    # ── Predictions ─────────────────────────────────────────────────
    p_train = model.predict_proba(X_train)[:, 1]
    p_hold = model.predict_proba(X_hold)[:, 1]

    # ── Train metrics (check for severe overfit) ─────────────────────
    m_train = _metrics(y_train, p_train)
    m_hold = _metrics(y_hold, p_hold)
    print(f"train ROC-AUC={m_train['roc_auc']} hold ROC-AUC={m_hold['roc_auc']}")

    # ── Baselines ────────────────────────────────────────────────────
    # 1. Random: always predict hold_pos_rate
    p_random = np.full(len(y_hold), hold_pos_rate)
    # 2. final_score as direct probability proxy (normalized to [0,1])
    fs_raw = X_hold[:, 0]
    fs_min, fs_max = X_train[:, 0].min(), X_train[:, 0].max()
    p_fs = np.clip((fs_raw - fs_min) / max(1e-9, fs_max - fs_min), 0, 1)
    # 3. final_score-only XGBoost
    X_tr_fs = X_train[:, :1]
    X_ho_fs = X_hold[:, :1]
    m_fs_only = _train_xgb(X_tr_fs, y_train, n_trees, max_depth, lr, seed, scale_pos_weight)
    p_fs_xgb = m_fs_only.predict_proba(X_ho_fs)[:, 1]

    def _baseline_metrics(yp: np.ndarray) -> dict[str, Any]:
        try:
            rauc = _safe(roc_auc_score(y_hold, yp))
        except Exception:
            rauc = None
        try:
            prauc = _safe(average_precision_score(y_hold, yp))
        except Exception:
            prauc = None
        idx = np.argsort(yp)[::-1]
        n_dec = max(1, len(idx) // 10)
        return {
            "roc_auc": rauc,
            "pr_auc": prauc,
            "top_decile_avg_net_return": _safe(float(nr_hold[idx[:n_dec]].mean())),
            "top_decile_pos_rate": _safe(float(y_hold[idx[:n_dec]].mean())),
        }

    baselines = {
        "random_constant": _baseline_metrics(p_random),
        "final_score_normalized": _baseline_metrics(p_fs),
        "xgb_final_score_only": _baseline_metrics(p_fs_xgb),
    }

    # ── Decile / quintile analysis ───────────────────────────────────
    decile_hold = _decile_analysis(y_hold, p_hold, nr_hold, pool_hold, "holdout")
    decile_train = _decile_analysis(y_train, p_train, nr_train, pool_train, "train")

    # ── Remove-top-N sensitivity ─────────────────────────────────────
    sens_top1 = _sensitivity_remove_pools(
        None, y_hold, p_hold, nr_hold, pool_hold, top_n=1)
    sens_top5 = _sensitivity_remove_pools(
        None, y_hold, p_hold, nr_hold, pool_hold, top_n=5)

    # ── Calibration ─────────────────────────────────────────────────
    cal = _calibration_buckets(y_hold, p_hold)

    # ── Threshold sweep ──────────────────────────────────────────────
    thr_sweep = _threshold_sweep(y_hold, p_hold)

    # ── Feature importance ───────────────────────────────────────────
    fi_raw = model.feature_importances_
    fi = {FEATURE_NAMES[i]: _safe(float(fi_raw[i])) for i in range(len(FEATURE_NAMES))}
    fi_sorted = sorted(fi.items(), key=lambda x: -(x[1] or 0))

    # Leakage check: if leakage-adjacent features dominate
    leakage_adj = {"log_index", "hour_of_day", "day_of_week"}  # time-based, potential leakage
    top3_names = {k for k, _ in fi_sorted[:3]}
    fi_leakage_risk = bool(top3_names.issubset(leakage_adj))

    # ── GO/HOLD gates ────────────────────────────────────────────────
    hold_roc = m_hold.get("roc_auc") or 0.0
    hold_prauc = m_hold.get("pr_auc") or 0.0
    top_dec = decile_hold["top_decile"]
    top_dec_nr = top_dec.get("avg_net_return") or 0.0
    top_dec_pr = top_dec.get("pos_rate") or 0.0
    sens5_nr = sens_top5.get("top_decile_avg_net_return")

    # Calibration quality: max deviation between predicted and actual in any bucket
    cal_max_dev = max(
        abs((b.get("mean_pred") or 0) - (b.get("actual_pos_rate") or 0))
        for b in cal
    ) if cal else 1.0

    gates = {
        "roc_auc_ge_056": hold_roc >= 0.56,
        "pr_auc_above_base": hold_prauc > hold_pos_rate,
        "top_decile_nr_positive": top_dec_nr > 0,
        "top_decile_pos_rate_above_base": top_dec_pr > hold_pos_rate,
        "remove_top5_decile_nr_positive": (sens5_nr is not None and sens5_nr > 0),
        "no_single_pool_dominates_top_decile": (top_dec.get("top1_pool_share") or 1.0) <= 0.5,
        "no_severe_overfit": (m_train.get("roc_auc") or 0) - hold_roc <= 0.20,
        "fi_not_leakage_dominated": not fi_leakage_risk,
        "calibration_acceptable": cal_max_dev <= 0.30,
    }

    all_gates_pass = all(gates.values())
    conclusion = "PATCH_G_GO_TO_SHADOW_PAPER_EVAL" if all_gates_pass else (
        "HOLD_DUE_TO_NO_HOLDOUT_EDGE" if not (gates["roc_auc_ge_056"] and gates["pr_auc_above_base"]) else
        "HOLD_DUE_TO_POOL_CONCENTRATION" if not gates["no_single_pool_dominates_top_decile"] else
        "HOLD_DUE_TO_LEAKAGE_RISK" if not gates["fi_not_leakage_dominated"] else
        "HOLD_DUE_TO_BAD_CALIBRATION" if not gates["calibration_acceptable"] else
        "HOLD_DUE_TO_NO_HOLDOUT_EDGE"
    )

    # ── Save holdout predictions CSV ─────────────────────────────────
    out_pred_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_pred_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "tx_hash", "log_index", "pool_address", "timestamp",
            "label", "net_return", "prob_positive",
            "pred_label_05", "score_rank",
        ])
        w.writeheader()
        rank_order = np.argsort(p_hold)[::-1]
        rank_map = {int(rank_order[i]): i + 1 for i in range(len(rank_order))}
        for i, r in enumerate(hold_rows):
            w.writerow({
                "tx_hash": r.get("tx_hash", ""),
                "log_index": r.get("log_index", ""),
                "pool_address": r.get("pool_address", ""),
                "timestamp": r.get("timestamp", ""),
                "label": r.get("label", ""),
                "net_return": r.get("net_return", ""),
                "prob_positive": round(float(p_hold[i]), 8),
                "pred_label_05": int(p_hold[i] >= 0.5),
                "score_rank": rank_map[i],
            })

    # ── Save model spec (weights as JSON — no binary artifact) ───────
    out_model_json.parent.mkdir(parents=True, exist_ok=True)
    model_spec: dict[str, Any] = {
        "model_type": "XGBClassifier",
        "xgboost_version": xgb.__version__ if _HAS_XGB else None,
        "features": FEATURE_NAMES,
        "hyperparams": {
            "n_estimators": n_trees,
            "max_depth": max_depth,
            "learning_rate": lr,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 5,
            "gamma": 0.1,
            "reg_alpha": 0.1,
            "reg_lambda": 1.0,
            "scale_pos_weight": _safe(scale_pos_weight),
            "seed": seed,
        },
        "feature_importance": fi,
        "train_metrics": m_train,
        "holdout_metrics": m_hold,
        "conclusion": conclusion,
        "patch": "G",
        "note": "DRY-RUN ONLY — not promoted, not deployed",
    }
    with out_model_json.open("w", encoding="utf-8") as f:
        json.dump(model_spec, f, indent=2, default=str)

    # ── Full report JSON ─────────────────────────────────────────────
    report: dict[str, Any] = {
        "conclusion": conclusion,
        "conclusion_pass": conclusion == "PATCH_G_GO_TO_SHADOW_PAPER_EVAL",
        "split": {
            "train_rows": len(train_rows),
            "holdout_rows": len(hold_rows),
            "train_positive": int(y_train.sum()),
            "train_negative": int(len(y_train) - y_train.sum()),
            "holdout_positive": int(y_hold.sum()),
            "holdout_negative": int(len(y_hold) - y_hold.sum()),
            "train_pos_rate": _safe(train_pos_rate),
            "holdout_pos_rate": _safe(hold_pos_rate),
            "holdout_lo_ms": holdout_lo_ms,
            "split_method": "time_series_strict",
        },
        "features": {
            "list": FEATURE_NAMES,
            "null_non_finite_check": feat_check,
            "leakage_guard": sorted(LEAKAGE_GUARD_FIELDS),
        },
        "train_metrics": m_train,
        "holdout_metrics": m_hold,
        "baselines": baselines,
        "decile_analysis_holdout": decile_hold,
        "decile_analysis_train": decile_train,
        "sensitivity": {
            "remove_top1_pool": sens_top1,
            "remove_top5_pools": sens_top5,
        },
        "calibration_buckets": cal,
        "threshold_sweep": thr_sweep,
        "feature_importance": fi,
        "feature_importance_sorted": fi_sorted,
        "gates": gates,
        "gate_values": {
            "hold_roc_auc": _safe(hold_roc),
            "hold_pr_auc": _safe(hold_prauc),
            "top_decile_avg_net_return": _safe(top_dec_nr),
            "top_decile_pos_rate": _safe(top_dec_pr),
            "sens_top5_decile_nr": _safe(sens5_nr) if sens5_nr is not None else None,
            "top_decile_top1_pool_share": _safe(top_dec.get("top1_pool_share")),
            "train_holdout_roc_gap": _safe((m_train.get("roc_auc") or 0) - hold_roc),
            "calibration_max_deviation": _safe(cal_max_dev),
        },
        "model_spec": {
            "type": "XGBClassifier",
            "n_features": len(FEATURE_NAMES),
            "n_trees": n_trees,
            "max_depth": max_depth,
            "note": "DRY-RUN — not promoted, not deployed",
        },
    }

    out_report_json.parent.mkdir(parents=True, exist_ok=True)
    out_report_md.parent.mkdir(parents=True, exist_ok=True)
    with out_report_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # ── Markdown report ──────────────────────────────────────────────
    fi_table = "\n".join(
        f"| `{k}` | {v} |" for k, v in fi_sorted
    )
    cal_table = "\n".join(
        f"| {b['bucket']} | {b['count']} | {b['mean_pred']} | {b['actual_pos_rate']} |"
        for b in cal
    )
    thr_table = "\n".join(
        f"| {t['threshold']} | {t['precision']} | {t['recall']} | {t['f1']} "
        f"| {t['n_positive_predicted']} | {t['coverage']} |"
        for t in thr_sweep
    )
    gate_table = "\n".join(
        f"| `{k}` | {'✔' if v else '✘'} |" for k, v in gates.items()
    )
    baseline_table = "\n".join(
        f"| {nm} | {bv.get('roc_auc')} | {bv.get('pr_auc')} "
        f"| {bv.get('top_decile_avg_net_return')} | {bv.get('top_decile_pos_rate')} |"
        for nm, bv in baselines.items()
    )

    md = f"""# Run-004 Patch G — V2-only Retrain Dry-run

**Conclusion**: `{conclusion}`  
**Model**: XGBClassifier ({n_trees} trees, depth {max_depth}, lr {lr})  
**Dataset**: `logs/run004_v2only_prepared_dryrun_canonical.csv`  
**Note**: DRY-RUN ONLY — not promoted, not deployed.

---

## 1. Train / Holdout Split

| Split | Rows | Positive | Negative | pos_rate |
|-------|-----:|--------:|--------:|-------:|
| Train | {report['split']['train_rows']} | {report['split']['train_positive']} | {report['split']['train_negative']} | {report['split']['train_pos_rate']} |
| Holdout (latest 7d) | {report['split']['holdout_rows']} | {report['split']['holdout_positive']} | {report['split']['holdout_negative']} | {report['split']['holdout_pos_rate']} |

Split method: `time_series_strict` — holdout_lo_ms = {holdout_lo_ms}

---

## 2. Model Metrics

### Holdout

| Metric | Value |
|--------|------:|
| ROC-AUC | **{m_hold['roc_auc']}** |
| PR-AUC | **{m_hold['pr_auc']}** |
| Log loss | {m_hold['log_loss']} |
| Brier score | {m_hold['brier_score']} |
| Accuracy | {m_hold['accuracy']} |
| Precision | {m_hold['precision']} |
| Recall | {m_hold['recall']} |
| F1 | {m_hold['f1']} |

### Train (overfit check)

| Metric | Value |
|--------|------:|
| ROC-AUC | {m_train['roc_auc']} |
| PR-AUC | {m_train['pr_auc']} |
| Train-Holdout ROC gap | {report['gate_values']['train_holdout_roc_gap']} |

### Confusion matrix (holdout, threshold=0.5)

```
{m_hold['confusion_matrix']}
```

---

## 3. Baseline Comparison

| Model | ROC-AUC | PR-AUC | top_dec_avg_nr | top_dec_pos_rate |
|-------|--------:|-------:|-------------:|----------------:|
| **XGBoost (full features)** | **{m_hold['roc_auc']}** | **{m_hold['pr_auc']}** | **{top_dec_nr}** | **{top_dec_pr}** |
{baseline_table}

---

## 4. Decile / Quintile Return Analysis (Holdout)

| Bucket | n | pos_rate | avg_net_return | median_net_return | top1_pool_share |
|--------|--:|--------:|-------------:|----------------:|---------------:|
| Top decile | {decile_hold['top_decile']['n']} | {decile_hold['top_decile']['pos_rate']} | {decile_hold['top_decile']['avg_net_return']} | {decile_hold['top_decile']['median_net_return']} | {decile_hold['top_decile']['top1_pool_share']} |
| Top quintile | {decile_hold['top_quintile']['n']} | {decile_hold['top_quintile']['pos_rate']} | {decile_hold['top_quintile']['avg_net_return']} | {decile_hold['top_quintile']['median_net_return']} | {decile_hold['top_quintile']['top1_pool_share']} |
| Bottom decile | {decile_hold['bottom_decile']['n']} | {decile_hold['bottom_decile']['pos_rate']} | {decile_hold['bottom_decile']['avg_net_return']} | {decile_hold['bottom_decile']['median_net_return']} | {decile_hold['bottom_decile']['top1_pool_share']} |
| All holdout | {decile_hold['all']['n']} | {decile_hold['all']['pos_rate']} | {decile_hold['all']['avg_net_return']} | {decile_hold['all']['median_net_return']} | — |

---

## 5. Remove-top-N Pool Sensitivity (Holdout)

| Sensitivity | n_remaining | ROC-AUC | top_dec_avg_nr | top_dec_pos_rate |
|-------------|----------:|--------:|-------------:|----------------:|
| Remove top-1 pool | {sens_top1.get('n_remaining')} | {sens_top1.get('roc_auc')} | {sens_top1.get('top_decile_avg_net_return')} | {sens_top1.get('top_decile_pos_rate')} |
| Remove top-5 pools | {sens_top5.get('n_remaining')} | {sens_top5.get('roc_auc')} | {sens_top5.get('top_decile_avg_net_return')} | {sens_top5.get('top_decile_pos_rate')} |

---

## 6. Feature Importance

| Feature | Importance |
|---------|----------:|
{fi_table}

---

## 7. Calibration (Holdout)

| Bucket | Count | Mean Pred | Actual Pos Rate |
|--------|------:|----------:|---------------:|
{cal_table}

Max calibration deviation: **{report['gate_values']['calibration_max_deviation']}**

---

## 8. Threshold Sweep (Holdout)

| Threshold | Precision | Recall | F1 | N Predicted | Coverage |
|----------:|----------:|-------:|---:|------------:|--------:|
{thr_table}

---

## 9. Gates

| Gate | Pass |
|------|:----:|
{gate_table}

---

## 10. Artifacts

| Artifact | Path |
|----------|------|
| Model spec (JSON) | `models/signal_model_v2only_run004_patch_g_dryrun.json` |
| Report (JSON) | `mvp/diagnostics/run004_patch_g_retrain_dryrun_report.json` |
| Holdout predictions CSV | `logs/run004_patch_g_holdout_predictions.csv` |
"""
    with out_report_md.open("w", encoding="utf-8") as f:
        f.write(md)

    print(f"conclusion={conclusion}")
    print(f"holdout ROC-AUC={m_hold['roc_auc']} PR-AUC={m_hold['pr_auc']}")
    print(f"top_decile avg_nr={top_dec_nr} pos_rate={top_dec_pr}")
    print(f"all gates pass={all_gates_pass}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
