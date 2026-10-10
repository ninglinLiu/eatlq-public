"""
Run-004 Patch G4 — V2 Feature-Enriched Retrain Dry-run.

Trains and evaluates feature-enriched V2-only models using strict latest-7d holdout.
Compares against Patch G2 best model (C2 XGB-d2-t50, ROC=0.5479).

Inputs:
  logs/run004_v2only_prepared_dryrun_feature_enriched.csv

Outputs:
  models/signal_model_v2only_run004_patch_g4_dryrun.json
  logs/run004_patch_g4_holdout_predictions.csv
  mvp/diagnostics/run004_patch_g4_feature_enriched_retrain_report.json
  mvp/runs/run-004-patch-g4-feature-enriched-retrain-dryrun.md

Hard prohibitions (enforced by assertions):
  - No LIVE_TRADING_MODE
  - No pool_address/token_address in model features
  - No label/net_return/exit_block/timestamp in model features
  - No future fields as features
  - No random split
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on",
), "LIVE_TRADING_MODE must not be enabled"

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        roc_auc_score, average_precision_score, accuracy_score,
        precision_score, recall_score, f1_score, log_loss, brier_score_loss,
    )
    from sklearn.preprocessing import StandardScaler
    import xgboost as xgb
except ImportError as e:
    print(f"ERROR: Missing dependency: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ENRICHED_CSV = REPO / "logs" / "run004_v2only_prepared_dryrun_feature_enriched.csv"
OUT_MODEL_JSON = REPO / "models" / "signal_model_v2only_run004_patch_g4_dryrun.json"
OUT_PRED_CSV = REPO / "logs" / "run004_patch_g4_holdout_predictions.csv"
OUT_REPORT_JSON = REPO / "mvp" / "diagnostics" / "run004_patch_g4_feature_enriched_retrain_report.json"
OUT_MD = REPO / "mvp" / "runs" / "run-004-patch-g4-feature-enriched-retrain-dryrun.md"

HOLDOUT_LO_MS = int(os.environ.get("RUN004_G4_HOLDOUT_LO_MS", "1776520584003"))
SEED = int(os.environ.get("RUN004_G4_SEED", "42"))

# ---------------------------------------------------------------------------
# Feature schema — forbidden fields (hard check)
# ---------------------------------------------------------------------------
FORBIDDEN_AS_FEATURES = {
    "label", "net_return", "exit_block", "relabel_status", "label_reason",
    "pool_address", "token_address", "pool_train_rate", "pool_train_log_count",
    "timestamp", "source_row_id", "candidate_status", "reject_reason",
    "gate_status", "pre_check", "rpc_calls_this_key", "tx_hash",
    "entry_block",  # banned: time-adjacent leakage
    # diagnostics-only RPC columns
    "pair_created_block", "pool_age_blocks_at_entry",
    "pool_age_sec_at_entry",  # high null (~96%), derived from pair_created_block
    "fetch_ok_group_a", "fetch_ok_group_b", "fetch_ok_group_c", "fetch_error",
    "weth_is_token0", "pool_has_weth",
}

# ---------------------------------------------------------------------------
# Safe float parser
# ---------------------------------------------------------------------------
def _f(v: str, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Winsorize / clip
# ---------------------------------------------------------------------------
def _winsorize(vals: list[float], p_lo: float = 1.0, p_hi: float = 99.0):
    arr = np.array(vals, dtype=np.float64)
    lo = np.percentile(arr, p_lo)
    hi = np.percentile(arr, p_hi)
    return np.clip(arr, lo, hi), lo, hi


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

# G2 baseline features (15 features)
G2_BASE_FEATURES = [
    "final_score",
    "log_index_log1p",
    "log_index",
    "final_score_logit",
    "final_score_rank_pct",
    "final_score_sq",
    "fs_neg",
    "is_low_fs_p10",
    "is_low_fs_p25",
    "log_index_rank_pct",
    "log_index_sq_log1p",
    "is_low_li_p25",
    "fs_x_li_log1p",
    "fs_logit_x_li_log1p",
    "fs_neg_x_li_log1p",
]

# New RPC features (raw/pre-engineered)
RPC_RAW_FEATURES = [
    "log_reserve_weth",
    "reserve_ratio",
    "swap_size_vs_reserve",
    "liquidity_to_trade_ratio",
    "block_gas_fill_ratio",
    "block_tx_count",
    "is_new_pool_7d",
    "is_new_pool_30d",
]

# Engineered RPC features (added in build_engineered_features)
RPC_ENGINEERED_NAMES = [
    "log1p_swap_vs_reserve",
    "log1p_liq_ratio",
    "log1p_block_tx_count",
    "reserve_ratio_clipped",
    # Interactions
    "fs_x_log_reserve_weth",
    "fs_x_log1p_swap_vs_reserve",
    "fs_x_block_gas_fill",
    "is_new_7d_x_fs",
]

# All enriched features (G2 base + RPC raw + RPC engineered)
ENRICHED_SIMPLE_FEATURES = G2_BASE_FEATURES + [
    "log_reserve_weth", "reserve_ratio_clipped",
    "log1p_swap_vs_reserve", "log1p_liq_ratio", "log1p_block_tx_count",
    "block_gas_fill_ratio", "is_new_pool_7d", "is_new_pool_30d",
]

ENRICHED_EXTENDED_FEATURES = ENRICHED_SIMPLE_FEATURES + [
    "fs_x_log_reserve_weth",
    "fs_x_log1p_swap_vs_reserve",
    "fs_x_block_gas_fill",
    "is_new_7d_x_fs",
]

# Assert no forbidden features in any feature set
for fset_name, fset in [
    ("G2_BASE", G2_BASE_FEATURES),
    ("ENRICHED_SIMPLE", ENRICHED_SIMPLE_FEATURES),
    ("ENRICHED_EXTENDED", ENRICHED_EXTENDED_FEATURES),
]:
    violations = set(fset) & FORBIDDEN_AS_FEATURES
    assert not violations, f"FEATURE_LEAKAGE in {fset_name}: {violations}"


def build_engineered_features(rows: list, clips: dict | None = None) -> tuple[dict, dict]:
    """Compute derived/engineered features for each row. Returns (feature_dict_per_row, clips)."""
    # Collect raw values for clip computation
    swap_vals = [_f(r.get("swap_size_vs_reserve", "")) for r in rows]
    liq_vals = [_f(r.get("liquidity_to_trade_ratio", "")) for r in rows]
    btx_vals = [_f(r.get("block_tx_count", "")) for r in rows]
    rat_vals = [_f(r.get("reserve_ratio", "")) for r in rows]

    if clips is None:
        # Compute clips from this data (training data only)
        _, rat_lo, rat_hi = (None,) + tuple(_winsorize(rat_vals)[1:])
        _, swap_lo, swap_hi = (None,) + tuple(_winsorize(swap_vals, 0.5, 99.5)[1:])
        clips = {
            "reserve_ratio": (rat_lo, rat_hi),
            "swap_vs_reserve": (swap_lo, swap_hi),
        }

    feats: dict = {}
    for i, r in enumerate(rows):
        fs = _f(r.get("final_score", ""))
        lrw = _f(r.get("log_reserve_weth", ""))
        swap = _f(r.get("swap_size_vs_reserve", ""))
        liq = _f(r.get("liquidity_to_trade_ratio", ""))
        btx = _f(r.get("block_tx_count", ""))
        rat = _f(r.get("reserve_ratio", ""))
        bgf = _f(r.get("block_gas_fill_ratio", ""))
        new7d = _f(r.get("is_new_pool_7d", ""))

        swap_c = max(0.0, min(float(clips["swap_vs_reserve"][1]), swap))
        rat_c = max(float(clips["reserve_ratio"][0]), min(float(clips["reserve_ratio"][1]), rat))

        feats[i] = {
            "log1p_swap_vs_reserve": math.log1p(swap_c),
            "log1p_liq_ratio": math.log1p(max(0, liq)),
            "log1p_block_tx_count": math.log1p(max(0, btx)),
            "reserve_ratio_clipped": rat_c,
            "fs_x_log_reserve_weth": fs * lrw,
            "fs_x_log1p_swap_vs_reserve": fs * math.log1p(swap_c),
            "fs_x_block_gas_fill": fs * bgf,
            "is_new_7d_x_fs": new7d * fs,
        }
    return feats, clips


def _build_X(rows: list, feature_names: list, eng_feats: dict) -> np.ndarray:
    """Build feature matrix from rows and engineered features."""
    n = len(rows)
    p = len(feature_names)
    X = np.zeros((n, p), dtype=np.float64)
    for i, r in enumerate(rows):
        for j, fname in enumerate(feature_names):
            if fname in eng_feats.get(i, {}):
                X[i, j] = eng_feats[i][fname]
            else:
                X[i, j] = _f(r.get(fname, ""))
    return X


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _ece(y_true: np.ndarray, y_prob: np.ndarray, min_bucket: int = 100):
    n = len(y_true)
    n_buckets = max(1, n // min_bucket)
    indices = np.argsort(y_prob)
    buckets = np.array_split(indices, n_buckets)
    ece, max_dev = 0.0, 0.0
    for bucket in buckets:
        if len(bucket) == 0:
            continue
        avg_pred = float(y_prob[bucket].mean())
        avg_actual = float(y_true[bucket].mean())
        dev = abs(avg_pred - avg_actual)
        ece += dev * len(bucket) / n
        max_dev = max(max_dev, dev)
    return round(ece, 5), round(max_dev, 5)


def _top_k_metrics(y_prob: np.ndarray, y_true: np.ndarray, net_ret: np.ndarray,
                   pools: list, k_pct: float = 0.1):
    k = max(1, int(len(y_prob) * k_pct))
    top_idx = np.argsort(y_prob)[-k:]
    top_y = y_true[top_idx]
    top_r = net_ret[top_idx]
    top_p = [pools[i] for i in top_idx]
    pc = Counter(top_p)
    top1 = pc.most_common(1)[0][1] / k if pc else 0
    return {
        "k": k,
        "positive_rate": round(float(top_y.mean()), 5),
        "avg_net_return": round(float(top_r.mean()), 6),
        "median_net_return": round(float(np.median(top_r)), 6),
        "p25_net_return": round(float(np.percentile(top_r, 25)), 6),
        "top1_pool_share": round(top1, 4),
    }


def _remove_top_pools_metrics(y_prob: np.ndarray, y_true: np.ndarray,
                               net_ret: np.ndarray, pools: list, n: int = 5):
    pc = Counter(pools)
    top_pools = {p for p, _ in pc.most_common(n)}
    mask = np.array([p not in top_pools for p in pools])
    if mask.sum() < 50 or len(set(y_true[mask])) < 2:
        return None
    yp_f, yt_f, nr_f = y_prob[mask], y_true[mask], net_ret[mask]
    po_f = [p for p, m in zip(pools, mask) if m]
    k = max(1, int(len(yp_f) * 0.1))
    top_idx = np.argsort(yp_f)[-k:]
    return {
        "n_removed": n,
        "n_rows_remaining": int(mask.sum()),
        "roc_auc": round(float(roc_auc_score(yt_f, yp_f)), 5),
        "top_decile_positive_rate": round(float(yt_f[top_idx].mean()), 5),
    }


def _seen_unseen(y_prob: np.ndarray, y_true: np.ndarray, ho_pools: list,
                 train_pools_set: set):
    seen = np.array([p in train_pools_set for p in ho_pools])
    unseen = ~seen
    out = {"seen_n": int(seen.sum()), "unseen_n": int(unseen.sum())}
    for nm, mask in [("seen", seen), ("unseen", unseen)]:
        if mask.sum() > 20 and len(set(y_true[mask])) > 1:
            out[f"{nm}_roc_auc"] = round(float(roc_auc_score(y_true[mask], y_prob[mask])), 5)
        else:
            out[f"{nm}_roc_auc"] = None
    return out


def _permutation_importance(model, X: np.ndarray, y: np.ndarray,
                             feature_names: list, n_rep: int = 1,
                             rng_seed: int = 42) -> list[dict]:
    rng = random.Random(rng_seed)
    base_auc = roc_auc_score(y, model.predict_proba(X)[:, 1])
    results = []
    for j, fname in enumerate(feature_names):
        drops = []
        for _ in range(n_rep):
            X_perm = X.copy()
            perm = list(range(len(X_perm)))
            rng.shuffle(perm)
            X_perm[:, j] = X_perm[perm, j]
            perm_auc = roc_auc_score(y, model.predict_proba(X_perm)[:, 1])
            drops.append(base_auc - perm_auc)
        results.append({"feature": fname, "importance_drop": round(float(np.mean(drops)), 5)})
    results.sort(key=lambda x: -x["importance_drop"])
    return results


def _eval_model(model, X_ho: np.ndarray, y_ho: np.ndarray, net_ret_ho: np.ndarray,
                ho_pools: list, train_pools_set: set, base_pos_rate: float,
                X_tr: np.ndarray, y_tr: np.ndarray,
                feature_names: list, model_id: str) -> dict:
    y_prob_ho = model.predict_proba(X_ho)[:, 1]
    y_prob_tr = model.predict_proba(X_tr)[:, 1]
    y_pred = (y_prob_ho >= 0.5).astype(int)

    roc = float(roc_auc_score(y_ho, y_prob_ho))
    roc_tr = float(roc_auc_score(y_tr, y_prob_tr))
    pr_auc = float(average_precision_score(y_ho, y_prob_ho))
    ece_val, ece_max = _ece(y_ho, y_prob_ho)

    top_dec = _top_k_metrics(y_prob_ho, y_ho, net_ret_ho, ho_pools, 0.1)
    top_qui = _top_k_metrics(y_prob_ho, y_ho, net_ret_ho, ho_pools, 0.2)
    rm1 = _remove_top_pools_metrics(y_prob_ho, y_ho, net_ret_ho, ho_pools, 1)
    rm5 = _remove_top_pools_metrics(y_prob_ho, y_ho, net_ret_ho, ho_pools, 5)
    seen_unseen = _seen_unseen(y_prob_ho, y_ho, ho_pools, train_pools_set)

    # Feature importance (model native)
    fi = []
    if hasattr(model, "feature_importances_"):
        fi = sorted(
            [{"feature": n, "importance": round(float(v), 5)}
             for n, v in zip(feature_names, model.feature_importances_)],
            key=lambda x: -x["importance"]
        )

    return {
        "model_id": model_id,
        "holdout": {
            "roc_auc": round(roc, 5),
            "pr_auc": round(pr_auc, 5),
            "base_positive_rate": round(float(base_pos_rate), 5),
            "pr_auc_above_base": round(pr_auc - base_pos_rate, 5),
            "accuracy": round(float(accuracy_score(y_ho, y_pred)), 5),
            "precision": round(float(precision_score(y_ho, y_pred, zero_division=0)), 5),
            "recall": round(float(recall_score(y_ho, y_pred, zero_division=0)), 5),
            "f1": round(float(f1_score(y_ho, y_pred, zero_division=0)), 5),
            "log_loss": round(float(log_loss(y_ho, y_prob_ho)), 5),
            "brier_score": round(float(brier_score_loss(y_ho, y_prob_ho)), 5),
            "ece": ece_val,
            "ece_max_deviation": ece_max,
            "train_roc_auc": round(roc_tr, 5),
            "train_holdout_gap": round(roc_tr - roc, 5),
        },
        "top_decile": top_dec,
        "top_quintile": top_qui,
        "remove_top1_pool": rm1,
        "remove_top5_pools": rm5,
        "seen_unseen": seen_unseen,
        "feature_importance": fi[:20],
    }


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------
G2_C2_REFERENCE = {
    "roc_auc": 0.5479,
    "top_decile_pos_rate": 0.5475,
    "top_decile_median_net_return": 0.021,
    "top1_pool_share": 0.371,
}


def _evaluate_gates(best: dict, base_pos_rate: float) -> dict:
    ho = best["holdout"]
    td = best["top_decile"]
    rm5 = best.get("remove_top5_pools") or {}
    su = best.get("seen_unseen") or {}

    roc = ho["roc_auc"]
    tr_gap = ho["train_holdout_gap"]
    ece = ho["ece"]

    gates = {
        "roc_ge_0.55": roc >= 0.55,
        "roc_ge_0.56_preferred": roc >= 0.56,
        "pr_auc_above_base": ho["pr_auc_above_base"] > 0,
        "top_decile_pos_rate_ge_base_plus_0.06": td["positive_rate"] >= base_pos_rate + 0.06,
        "top_decile_median_net_return_ge_0": td["median_net_return"] >= 0,
        "top_decile_p25_not_catastrophic": td["p25_net_return"] > -0.20,
        "top_decile_top1_pool_share_le_0.35": td["top1_pool_share"] <= 0.35,
        "remove_top5_pos_rate_gt_base": (
            rm5.get("top_decile_positive_rate", 0) > base_pos_rate
            if rm5 else False
        ),
        "remove_top5_roc_ge_0.53": rm5.get("roc_auc", 0) >= 0.53 if rm5 else False,
        "unseen_pool_roc_ge_0.53": (su.get("unseen_roc_auc") or 0) >= 0.53,
        "ece_le_0.08": ece <= 0.08,
        "train_holdout_gap_le_0.15": tr_gap <= 0.15,
        "improved_vs_g2_c2": roc > G2_C2_REFERENCE["roc_auc"],
    }

    n_pass = sum(gates.values())
    critical = ["roc_ge_0.55", "top_decile_top1_pool_share_le_0.35",
                "unseen_pool_roc_ge_0.53", "train_holdout_gap_le_0.15"]
    all_critical_pass = all(gates[k] for k in critical)

    preferred_gates = [
        "roc_ge_0.55", "pr_auc_above_base", "top_decile_pos_rate_ge_base_plus_0.06",
        "top_decile_median_net_return_ge_0", "top_decile_p25_not_catastrophic",
        "top_decile_top1_pool_share_le_0.35",
        "remove_top5_pos_rate_gt_base", "remove_top5_roc_ge_0.53",
        "unseen_pool_roc_ge_0.53", "ece_le_0.08", "train_holdout_gap_le_0.15",
    ]
    all_preferred_pass = all(gates[k] for k in preferred_gates)

    if all_preferred_pass and roc >= 0.55:
        conclusion = "PATCH_G4_GO_TO_SHADOW_PAPER_CANDIDATE"
    elif roc >= 0.55 and all_critical_pass:
        conclusion = "PATCH_G4_WEAK_BUT_IMPROVED_NEEDS_MORE_VALIDATION"
    elif td["top1_pool_share"] > 0.35:
        conclusion = "HOLD_DUE_TO_POOL_CONCENTRATION"
    elif (su.get("unseen_roc_auc") or 0) < 0.53:
        conclusion = "HOLD_DUE_TO_UNSEEN_POOL_FAILURE"
    elif tr_gap > 0.15:
        conclusion = "HOLD_DUE_TO_OVERFIT"
    elif ece > 0.08:
        conclusion = "HOLD_DUE_TO_BAD_CALIBRATION"
    else:
        conclusion = "HOLD_DUE_TO_NO_FEATURE_ENRICHED_EDGE"

    return {
        "gates": gates,
        "n_pass": n_pass,
        "n_total": len(gates),
        "all_critical_pass": all_critical_pass,
        "all_preferred_pass": all_preferred_pass,
        "conclusion": conclusion,
        "shadow_paper_allowed": conclusion == "PATCH_G4_GO_TO_SHADOW_PAPER_CANDIDATE",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("Run-004 Patch G4: V2 Feature-Enriched Retrain Dry-run")
    print("=" * 70)

    # Load data
    print(f"[INFO] Loading {ENRICHED_CSV}")
    all_rows = []
    with open(ENRICHED_CSV) as f:
        for r in csv.DictReader(f):
            all_rows.append(r)
    print(f"[INFO] Total rows: {len(all_rows)}")

    # Feature schema validation
    cols_in_csv = set(all_rows[0].keys())
    # Confirm no forbidden field in our model feature sets
    for fset_name, fset in [
        ("G2_BASE", G2_BASE_FEATURES),
        ("ENRICHED_SIMPLE", ENRICHED_SIMPLE_FEATURES),
        ("ENRICHED_EXTENDED", ENRICHED_EXTENDED_FEATURES),
    ]:
        leakage = set(fset) & FORBIDDEN_AS_FEATURES & cols_in_csv
        if leakage:
            print(f"ERROR: FEATURE_LEAKAGE in {fset_name}: {leakage}")
            sys.exit(1)

    print("[INFO] Feature schema validation passed — no leakage detected")

    # Train/holdout split
    train_rows = [r for r in all_rows if int(r.get("timestamp", 0) or 0) < HOLDOUT_LO_MS]
    ho_rows = [r for r in all_rows if int(r.get("timestamp", 0) or 0) >= HOLDOUT_LO_MS]
    print(f"[INFO] Train: {len(train_rows)} | Holdout: {len(ho_rows)} | "
          f"Holdout split at ts={HOLDOUT_LO_MS}")

    y_tr = np.array([int(r["label"]) for r in train_rows])
    y_ho = np.array([int(r["label"]) for r in ho_rows])
    net_ret_ho = np.array([_f(r.get("net_return", "")) for r in ho_rows])
    ho_pools = [r["pool_address"] for r in ho_rows]
    train_pools_set = {r["pool_address"] for r in train_rows}

    base_pos_rate = float(y_ho.mean())
    train_pos_rate = float(y_tr.mean())
    print(f"[INFO] Train pos rate: {train_pos_rate:.4f} | Holdout pos rate: {base_pos_rate:.4f}")

    # Pool coverage
    ho_pools_seen = [p for p in ho_pools if p in train_pools_set]
    ho_pools_unseen = [p for p in ho_pools if p not in train_pools_set]
    print(f"[INFO] Holdout pools: seen={len(set(ho_pools_seen))} / "
          f"unseen={len(set(ho_pools_unseen))}")

    # Build engineered features
    print("[INFO] Building engineered features...")
    tr_eng, clips = build_engineered_features(train_rows, clips=None)
    ho_eng, _ = build_engineered_features(ho_rows, clips=clips)
    print(f"[INFO] Clips: reserve_ratio={clips['reserve_ratio']} "
          f"swap={clips['swap_vs_reserve']}")

    # Feature matrices
    X_tr_g2 = _build_X(train_rows, G2_BASE_FEATURES, tr_eng)
    X_ho_g2 = _build_X(ho_rows, G2_BASE_FEATURES, ho_eng)
    X_tr_simple = _build_X(train_rows, ENRICHED_SIMPLE_FEATURES, tr_eng)
    X_ho_simple = _build_X(ho_rows, ENRICHED_SIMPLE_FEATURES, ho_eng)
    X_tr_ext = _build_X(train_rows, ENRICHED_EXTENDED_FEATURES, tr_eng)
    X_ho_ext = _build_X(ho_rows, ENRICHED_EXTENDED_FEATURES, ho_eng)

    print(f"[INFO] G2 feature shape: {X_tr_g2.shape}")
    print(f"[INFO] Enriched simple shape: {X_tr_simple.shape}")
    print(f"[INFO] Enriched extended shape: {X_tr_ext.shape}")

    # Common XGB params
    xgb_common = dict(
        use_label_encoder=False,
        eval_metric="logloss",
        tree_method="hist",
        random_state=SEED,
        n_jobs=-1,
        verbosity=0,
    )

    # Model definitions
    models_def = [
        # D0: G2 baseline reproduction
        ("D0_g2_baseline",
         xgb.XGBClassifier(max_depth=2, n_estimators=50, learning_rate=0.1, **xgb_common),
         X_tr_g2, X_ho_g2, G2_BASE_FEATURES),
        # D1: LR on enriched simple
        ("D1_lr_enriched",
         None,  # placeholder — LR below
         X_tr_simple, X_ho_simple, ENRICHED_SIMPLE_FEATURES),
        # D2: XGB d=2 t=50 simple
        ("D2_xgb_d2_t50_simple",
         xgb.XGBClassifier(max_depth=2, n_estimators=50, learning_rate=0.1, **xgb_common),
         X_tr_simple, X_ho_simple, ENRICHED_SIMPLE_FEATURES),
        # D3: XGB d=2 t=100 simple
        ("D3_xgb_d2_t100_simple",
         xgb.XGBClassifier(max_depth=2, n_estimators=100, learning_rate=0.1, **xgb_common),
         X_tr_simple, X_ho_simple, ENRICHED_SIMPLE_FEATURES),
        # D4: XGB d=3 t=100 simple
        ("D4_xgb_d3_t100_simple",
         xgb.XGBClassifier(max_depth=3, n_estimators=100, learning_rate=0.05, **xgb_common),
         X_tr_simple, X_ho_simple, ENRICHED_SIMPLE_FEATURES),
        # D5: XGB d=3 t=200 conservative
        ("D5_xgb_d3_t200_conservative",
         xgb.XGBClassifier(max_depth=3, n_estimators=200, learning_rate=0.03,
                            subsample=0.8, colsample_bytree=0.8, **xgb_common),
         X_tr_simple, X_ho_simple, ENRICHED_SIMPLE_FEATURES),
        # D6: XGB d=3 t=100 extended features
        ("D6_xgb_d3_t100_extended",
         xgb.XGBClassifier(max_depth=3, n_estimators=100, learning_rate=0.05, **xgb_common),
         X_tr_ext, X_ho_ext, ENRICHED_EXTENDED_FEATURES),
    ]

    # Train and evaluate all models
    results = []
    for model_id, model, X_tr, X_ho, feat_names in models_def:
        print(f"\n[TRAIN] {model_id} ...")

        if model is None:
            # Logistic Regression
            scaler = StandardScaler()
            X_tr_s = scaler.fit_transform(X_tr)
            X_ho_s = scaler.transform(X_ho)
            model = LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs",
                                       random_state=SEED)
            model.fit(X_tr_s, y_tr)
            # Wrap for uniform predict_proba interface
            class LRWrapper:
                def __init__(self, m, s): self.m = m; self.s = s
                def predict_proba(self, X): return self.m.predict_proba(self.s.transform(X))
                @property
                def feature_importances_(self):
                    return np.abs(self.m.coef_[0])
            model = LRWrapper(model, scaler)
        else:
            model.fit(X_tr, y_tr)

        metrics = _eval_model(
            model, X_ho, y_ho, net_ret_ho, ho_pools, train_pools_set,
            base_pos_rate, X_tr, y_tr, feat_names, model_id
        )
        results.append(metrics)

        roc = metrics["holdout"]["roc_auc"]
        td_pr = metrics["top_decile"]["positive_rate"]
        td_med = metrics["top_decile"]["median_net_return"]
        td_p1 = metrics["top_decile"]["top1_pool_share"]
        print(f"  ROC={roc:.4f} (train={metrics['holdout']['train_roc_auc']:.4f}) "
              f"| top-dec pos={td_pr:.3f} med_ret={td_med:.4f} p1_share={td_p1:.3f}")

    # Permutation importance for best model
    best = max(results, key=lambda x: x["holdout"]["roc_auc"])
    print(f"\n[INFO] Best model: {best['model_id']} ROC={best['holdout']['roc_auc']:.5f}")

    # Re-train best for permutation importance
    best_def = next(d for d in models_def if d[0] == best["model_id"])
    best_model_id, _, X_tr_b, X_ho_b, feat_b = best_def
    if best_model_id == "D1_lr_enriched":
        scaler = StandardScaler()
        X_tr_bs = scaler.fit_transform(X_tr_b)
        X_ho_bs = scaler.transform(X_ho_b)
        best_model = LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs", random_state=SEED)
        best_model.fit(X_tr_bs, y_tr)
        class _LR:
            def __init__(self, m, s): self.m = m; self.s = s
            def predict_proba(self, X): return self.m.predict_proba(self.s.transform(X))
        best_model_wrapped = _LR(best_model, scaler)
    else:
        best_model_wrapped = xgb.XGBClassifier(**{
            k: v for k, v in next(
                d[1].__dict__.items() if hasattr(d[1], '__dict__') else d[1].get_xgb_params().items()
                for d in models_def if d[0] == best_model_id
            )
        })
        # Re-fit best model
        best_raw_def = next(d for d in models_def if d[0] == best_model_id)
        best_model_wrapped = best_raw_def[1]
        best_model_wrapped.fit(X_tr_b, y_tr)

    print("[INFO] Computing permutation importance for best model...")
    perm_imp = _permutation_importance(best_model_wrapped, X_ho_b, y_ho, feat_b, n_rep=1)
    best["permutation_importance"] = perm_imp[:20]

    # Gate evaluation
    gate_result = _evaluate_gates(best, base_pos_rate)
    print(f"\n[GATES] {gate_result['conclusion']} | "
          f"pass {gate_result['n_pass']}/{gate_result['n_total']}")

    # Comparison vs G2 C2
    roc_delta = best["holdout"]["roc_auc"] - G2_C2_REFERENCE["roc_auc"]
    comparison = {
        "vs_g2_c2": {
            "g2_c2_roc_auc": G2_C2_REFERENCE["roc_auc"],
            "g4_best_roc_auc": best["holdout"]["roc_auc"],
            "roc_delta": round(roc_delta, 5),
            "improvement": roc_delta > 0,
            "g2_c2_top_decile_pos_rate": G2_C2_REFERENCE["top_decile_pos_rate"],
            "g4_top_decile_pos_rate": best["top_decile"]["positive_rate"],
            "g2_c2_top1_pool_share": G2_C2_REFERENCE["top1_pool_share"],
            "g4_top1_pool_share": best["top_decile"]["top1_pool_share"],
        }
    }

    # Write holdout predictions CSV
    print("[INFO] Writing holdout predictions CSV...")
    best_def_tuple = next(d for d in models_def if d[0] == best["model_id"])
    y_prob_best = best_model_wrapped.predict_proba(best_def_tuple[3])[:, 1]
    OUT_PRED_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PRED_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp", "pool_address", "tx_hash", "log_index",
            "label", "net_return", "final_score", "g4_score",
        ])
        w.writeheader()
        for row, score in zip(ho_rows, y_prob_best):
            w.writerow({
                "timestamp": row.get("timestamp", ""),
                "pool_address": row.get("pool_address", ""),
                "tx_hash": row.get("tx_hash", ""),
                "log_index": row.get("log_index", ""),
                "label": row.get("label", ""),
                "net_return": row.get("net_return", ""),
                "final_score": row.get("final_score", ""),
                "g4_score": round(float(score), 6),
            })

    # Write model JSON
    OUT_MODEL_JSON.parent.mkdir(parents=True, exist_ok=True)
    model_meta = {
        "model_id": best["model_id"],
        "patch": "G4",
        "feature_names": feat_b,
        "holdout_roc_auc": best["holdout"]["roc_auc"],
        "conclusion": gate_result["conclusion"],
        "shadow_paper_allowed": gate_result["shadow_paper_allowed"],
        "clips": {k: list(v) for k, v in clips.items()},
        "holdout_lo_ms": HOLDOUT_LO_MS,
        "seed": SEED,
    }
    with open(OUT_MODEL_JSON, "w") as f:
        json.dump(model_meta, f, indent=2)

    # Compile full report
    split_info = {
        "train_rows": len(train_rows),
        "holdout_rows": len(ho_rows),
        "train_pos_rate": round(train_pos_rate, 5),
        "holdout_pos_rate": round(base_pos_rate, 5),
        "holdout_lo_ms": HOLDOUT_LO_MS,
        "seen_pool_rows_holdout": sum(1 for p in ho_pools if p in train_pools_set),
        "unseen_pool_rows_holdout": sum(1 for p in ho_pools if p not in train_pools_set),
        "unique_train_pools": len(train_pools_set),
        "unique_holdout_pools": len(set(ho_pools)),
    }
    schema_info = {
        "g2_base_features": G2_BASE_FEATURES,
        "enriched_simple_features": ENRICHED_SIMPLE_FEATURES,
        "enriched_extended_features": ENRICHED_EXTENDED_FEATURES,
        "forbidden_features_checked": sorted(FORBIDDEN_AS_FEATURES),
        "leakage_violations": [],
    }
    report = {
        "patch": "G4",
        "conclusion": gate_result["conclusion"],
        "shadow_paper_allowed": gate_result["shadow_paper_allowed"],
        "split": split_info,
        "schema": schema_info,
        "clips": {k: list(v) for k, v in clips.items()},
        "best_model": best["model_id"],
        "best_model_metrics": best,
        "all_models_summary": [
            {
                "model_id": r["model_id"],
                "roc_auc": r["holdout"]["roc_auc"],
                "pr_auc": r["holdout"]["pr_auc"],
                "train_roc_auc": r["holdout"]["train_roc_auc"],
                "train_holdout_gap": r["holdout"]["train_holdout_gap"],
                "top_decile_pos_rate": r["top_decile"]["positive_rate"],
                "top_decile_median_net_return": r["top_decile"]["median_net_return"],
                "top_decile_top1_pool_share": r["top_decile"]["top1_pool_share"],
                "unseen_roc": r.get("seen_unseen", {}).get("unseen_roc_auc"),
            }
            for r in results
        ],
        "comparison": comparison,
        "gate_result": gate_result,
    }

    OUT_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)

    # Write markdown
    _write_md(report)

    # Final summary
    print()
    print("=" * 70)
    print(f"CONCLUSION: {gate_result['conclusion']}")
    print(f"Shadow/paper allowed: {gate_result['shadow_paper_allowed']}")
    ho = best["holdout"]
    td = best["top_decile"]
    print(f"Best: {best['model_id']}")
    print(f"  ROC-AUC: {ho['roc_auc']:.5f} (train={ho['train_roc_auc']:.5f} gap={ho['train_holdout_gap']:.5f})")
    print(f"  PR-AUC: {ho['pr_auc']:.5f} (base={ho['base_positive_rate']:.5f})")
    print(f"  Top-decile pos: {td['positive_rate']:.5f} | med_ret: {td['median_net_return']:.5f}")
    print(f"  Top-decile p1_pool_share: {td['top1_pool_share']:.4f}")
    print(f"  vs G2 C2 (ROC={G2_C2_REFERENCE['roc_auc']}): delta={roc_delta:+.5f}")
    print(f"  Gates: {gate_result['n_pass']}/{gate_result['n_total']} pass")
    print()
    print("Artifacts:")
    print(f"  {OUT_MODEL_JSON}")
    print(f"  {OUT_PRED_CSV}")
    print(f"  {OUT_REPORT_JSON}")
    print(f"  {OUT_MD}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def _write_md(report: dict):
    b = report["best_model_metrics"]
    ho = b["holdout"]
    td = b["top_decile"]
    tq = b["top_quintile"]
    rm1 = b.get("remove_top1_pool") or {}
    rm5 = b.get("remove_top5_pools") or {}
    su = b.get("seen_unseen") or {}
    g = report["gate_result"]
    cmp = report["comparison"]["vs_g2_c2"]
    split = report["split"]
    conclusion = report["conclusion"]
    go = conclusion == "PATCH_G4_GO_TO_SHADOW_PAPER_CANDIDATE"

    all_mods = "\n".join(
        f"| {r['model_id']} | {r['roc_auc']:.5f} | {r['train_roc_auc']:.5f} | "
        f"{r['train_holdout_gap']:.5f} | {r['top_decile_pos_rate']:.4f} | "
        f"{r['top_decile_median_net_return']:.5f} | {r['top_decile_top1_pool_share']:.4f} | "
        f"{r.get('unseen_roc') or '—'} |"
        for r in report["all_models_summary"]
    )

    fi_rows = "\n".join(
        f"| `{x['feature']}` | {x['importance']:.5f} |"
        for x in b.get("feature_importance", [])[:15]
    )
    perm_rows = "\n".join(
        f"| `{x['feature']}` | {x['importance_drop']:.5f} |"
        for x in b.get("permutation_importance", [])[:15]
    )

    gate_rows = "\n".join(
        f"| {k} | {'PASS' if v else 'FAIL'} |"
        for k, v in g["gates"].items()
    )

    md = f"""# Run-004 Patch G4: Feature-Enriched Retrain Dry-run

**Conclusion**: `{conclusion}`  
**Shadow/Paper Candidate**: {"YES — all critical gates passed" if go else "NO — see gate failures below"}  
**Best Model**: `{report['best_model']}`

---

## 1. Feature Schema Summary

**Allowed model features (G2 base)** ({len(report['schema']['g2_base_features'])}):  
{', '.join(f'`{f}`' for f in report['schema']['g2_base_features'])}

**New RPC features added**:  
`log_reserve_weth`, `reserve_ratio_clipped`, `log1p_swap_vs_reserve`, `log1p_liq_ratio`, `log1p_block_tx_count`, `block_gas_fill_ratio`, `is_new_pool_7d`, `is_new_pool_30d` + interaction terms

**Leakage violations**: {len(report['schema']['leakage_violations'])} (should be 0)

---

## 2. Train/Holdout Split

| Metric | Value |
|--------|-------|
| Train rows | {split['train_rows']:,} |
| Holdout rows | {split['holdout_rows']:,} |
| Train pos rate | {split['train_pos_rate']:.4f} |
| Holdout pos rate | {split['holdout_pos_rate']:.4f} |
| Seen pool rows (holdout) | {split['seen_pool_rows_holdout']:,} |
| Unseen pool rows (holdout) | {split['unseen_pool_rows_holdout']:,} |
| Unique train pools | {split['unique_train_pools']} |
| Unique holdout pools | {split['unique_holdout_pools']} |

---

## 3. All Models Summary

| Model | ROC-AUC | Train-ROC | Gap | Top-Dec Pos | Top-Dec Med Ret | Top1 Share | Unseen ROC |
|-------|---------|-----------|-----|-------------|-----------------|------------|-----------|
{all_mods}

---

## 4. Best Model: `{report['best_model']}`

### Holdout Metrics

| Metric | Value |
|--------|-------|
| ROC-AUC | **{ho['roc_auc']:.5f}** |
| PR-AUC | {ho['pr_auc']:.5f} (base={ho['base_positive_rate']:.5f}) |
| Accuracy | {ho['accuracy']:.5f} |
| Precision | {ho['precision']:.5f} |
| Recall | {ho['recall']:.5f} |
| F1 | {ho['f1']:.5f} |
| Log Loss | {ho['log_loss']:.5f} |
| Brier Score | {ho['brier_score']:.5f} |
| ECE | {ho['ece']:.5f} |
| ECE max deviation | {ho['ece_max_deviation']:.5f} |
| Train ROC-AUC | {ho['train_roc_auc']:.5f} |
| Train/Holdout Gap | {ho['train_holdout_gap']:.5f} |

---

## 5. Comparison vs G2 C2

| Metric | G2 C2 | G4 Best | Delta |
|--------|-------|---------|-------|
| ROC-AUC | {cmp['g2_c2_roc_auc']} | {cmp['g4_best_roc_auc']:.5f} | {cmp['roc_delta']:+.5f} |
| Top-dec pos rate | {cmp['g2_c2_top_decile_pos_rate']} | {td['positive_rate']:.5f} | {td['positive_rate'] - cmp['g2_c2_top_decile_pos_rate']:+.5f} |
| Top-dec top1_pool_share | {cmp['g2_c2_top1_pool_share']} | {cmp['g4_top1_pool_share']:.5f} | {cmp['g4_top1_pool_share'] - cmp['g2_c2_top1_pool_share']:+.5f} |

---

## 6. Top-Decile / Top-Quintile Return Analysis

| Metric | Top-Decile (10%) | Top-Quintile (20%) |
|--------|------------------|--------------------|
| k rows | {td['k']} | {tq['k']} |
| Positive rate | {td['positive_rate']:.5f} | {tq['positive_rate']:.5f} |
| Avg net return | {td['avg_net_return']:.5f} | {tq['avg_net_return']:.5f} |
| Median net return | {td['median_net_return']:.5f} | {tq['median_net_return']:.5f} |
| P25 net return | {td['p25_net_return']:.5f} | {tq['p25_net_return']:.5f} |
| Top1 pool share | {td['top1_pool_share']:.5f} | {tq['top1_pool_share']:.5f} |

---

## 7. Pool Sensitivity

| Test | ROC-AUC | Top-Dec Pos Rate |
|------|---------|-----------------|
| Full holdout | {ho['roc_auc']:.5f} | {td['positive_rate']:.5f} |
| Remove top-1 pool | {rm1.get('roc_auc','—')} | {rm1.get('top_decile_positive_rate','—')} |
| Remove top-5 pools | {rm5.get('roc_auc','—')} | {rm5.get('top_decile_positive_rate','—')} |

---

## 8. Seen vs Unseen Pool Performance

| Group | N rows | ROC-AUC |
|-------|--------|---------|
| Seen pools | {su.get('seen_n','—')} | {su.get('seen_roc_auc','—')} |
| Unseen pools | {su.get('unseen_n','—')} | {su.get('unseen_roc_auc','—')} |

---

## 9. Feature Importance

### Native Importance (top 15)

| Feature | Importance |
|---------|-----------|
{fi_rows}

### Permutation Importance (top 15, ROC drop)

| Feature | ROC Drop |
|---------|---------|
{perm_rows}

---

## 10. Gates

| Gate | Result |
|------|--------|
{gate_rows}

**Gates passed: {g['n_pass']}/{g['n_total']} | Critical all pass: {g['all_critical_pass']} | Preferred all pass: {g['all_preferred_pass']}**

---

## 11. Conclusion

**`{conclusion}`**

{"✅ All critical gates passed. Shadow/paper evaluation is ALLOWED." if go else "⛔ Not all critical gates passed. Shadow/paper evaluation NOT allowed yet."}

### Next Step (Patch H)
{"- Build paper/shadow evaluation pipeline" if go else "- Investigate gate failures before Patch H"}
{"- Do NOT auto-run Patch H — requires explicit user approval" if go else ""}

---

## Artifacts

| File | Path |
|------|------|
| Model JSON | `models/signal_model_v2only_run004_patch_g4_dryrun.json` |
| Holdout predictions | `logs/run004_patch_g4_holdout_predictions.csv` |
| Report JSON | `mvp/diagnostics/run004_patch_g4_feature_enriched_retrain_report.json` |
| Report MD | `mvp/runs/run-004-patch-g4-feature-enriched-retrain-dryrun.md` |
"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_MD, "w") as f:
        f.write(md.strip() + "\n")


if __name__ == "__main__":
    main()
