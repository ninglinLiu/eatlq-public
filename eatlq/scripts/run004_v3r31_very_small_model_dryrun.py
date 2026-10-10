"""Run-004 Patch V3R-31 - V3 Very Small Model Dry-Run (research-only / read-only).

Reuses V3R-29B / V3R-30 artifacts. Performs an HONEST very-small-model dry-run
against the 151-row rebalanced clean timestamp dataset to test whether minimal
pool-disjoint and token-disjoint sortable signal exists for the 5m and 30m
horizons. Outputs a strict pass/fail style decision; explicitly forbids larger
expansion, model research, mainline, live, shadow, promote, deployable model
artifacts, or writing to services/strategy/models.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1",
    "true",
    "yes",
    "on",
), "LIVE_TRADING_MODE must not be active for V3R-31 dry-run."

V3R29B_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_summary.json"
V3R29B_ROWS = ROOT / "mvp/diagnostics/run004_v3r29b_small_timestamp_dataset_rebalance_rows.csv"
V3R29B_REPORT = ROOT / "mvp/runs/run-004-patch-v3r29b-small-timestamp-dataset-rebalance.md"
V3R30_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r30_pre_model_dataset_audit_summary.json"
V3R29_SUMMARY = ROOT / "mvp/diagnostics/run004_v3r29_small_timestamp_dataset_expansion_summary.json"

OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r31_very_small_model_dryrun_summary.json"
OUT_CSV = ROOT / "mvp/diagnostics/run004_v3r31_very_small_model_dryrun_rows.csv"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r31-very-small-model-dryrun.md"

SOURCE_RUN = "run004_v3r31_very_small_model_dryrun"

EXPECTED_INPUT_ROWS = 151
EXPECTED_POOL_COUNT = 26
EXPECTED_TOKEN_COUNT = 26
EXPECTED_30M_CHRONO_DRIFT = 0.16
SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD = 50

TOP_BUCKET_FRACTION = 0.20
SIGNAL_AUC_THRESHOLD = 0.55
SHUFFLED_BUFFER = 0.05
CONCENTRATION_FAIL_THRESHOLD = 0.50
MIN_HOLDOUT_POSITIVES = 5
MIN_HOLDOUT_NEGATIVES = 5
MIN_FEATURE_COUNT = 3

LR_ITER = 800
LR_LR = 0.15
LR_L2 = 1.0
SEEDS = [0, 1, 2, 3, 4]
SHUFFLE_SEEDS = [11, 23, 37, 53, 71]

BANNED_FEATURE_COLUMNS = {
    "label_return_5m",
    "label_return_30m",
    "return_5m",
    "return_30m",
    "positive_5m",
    "positive_30m",
    "target_5m",
    "target_30m",
    "exit_block_5m",
    "exit_block_30m",
    "exit_timestamp_5m",
    "exit_timestamp_30m",
    "fixed_block_return",
    "fixed_block_return_control",
    "same_block_return",
    "same_block_sanity_return",
    "same_block_is_label",
    "sign_flip",
    "sign_flip_5m_30m",
    "near_zero_flip",
    "outlier_flag",
    "chronological_split",
    "pool_split",
    "token_split",
    "source_path",
    "source_run",
    "pool_address",
    "token_address",
    "tx_hash",
    "log_index",
    "token0",
    "token1",
    "trainable_candidate_flag",
    "research_clean_timestamp_candidate",
    "timestamp_horizon_primary_label",
    "timestamp_quote_5m_ok",
    "timestamp_quote_30m_ok",
    "state_changed",
    "observed_direction",
}


def _safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_csv(path):
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_json(path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_features(rows):
    """Construct entry-time-only feature matrix.

    All inputs come from columns observable at or before the entry block:
    fee tier, entry block / timestamp, selected WETH notional. No label,
    no exit, no split, no identity (pool/token/source/tx) field is used.
    Constant or near-constant features are dropped.
    """
    fees_int = [_safe_int(r.get("fee")) for r in rows]
    fee_levels = sorted({f for f in fees_int if f > 0})

    timestamps = [_safe_int(r.get("entry_timestamp")) for r in rows]
    blocks = [_safe_int(r.get("entry_block")) for r in rows]
    notionals = [_safe_float(r.get("selected_notional_weth")) for r in rows]

    ts_min, ts_max = min(timestamps), max(timestamps)
    blk_min, blk_max = min(blocks), max(blocks)
    n_min, n_max = min(notionals), max(notionals)

    ts_span = max(1, ts_max - ts_min)
    blk_span = max(1, blk_max - blk_min)
    n_span = max(1e-30, n_max - n_min)

    notional_cap_value = max(notionals)
    notional_at_cap_count = sum(1 for v in notionals if abs(v - notional_cap_value) <= 1e-15)

    candidate_columns = []
    candidate_columns.extend([f"fee_oh_{lvl}" for lvl in fee_levels])
    candidate_columns.extend([
        "entry_timestamp_norm",
        "entry_block_norm",
        "hour_of_day_norm",
        "day_of_week_norm",
        "selected_notional_weth_norm",
        "notional_at_cap",
    ])

    raw_matrix = []
    for r in rows:
        fee_v = _safe_int(r.get("fee"))
        ts = _safe_int(r.get("entry_timestamp"))
        blk = _safe_int(r.get("entry_block"))
        notional = _safe_float(r.get("selected_notional_weth"))

        feats = []
        for lvl in fee_levels:
            feats.append(1.0 if fee_v == lvl else 0.0)
        feats.append((ts - ts_min) / ts_span)
        feats.append((blk - blk_min) / blk_span)
        feats.append(((ts % 86400) / 86400.0))
        feats.append(((ts // 86400) % 7) / 7.0)
        feats.append((notional - n_min) / n_span if n_span > 0 else 0.0)
        feats.append(1.0 if abs(notional - notional_cap_value) <= 1e-15 else 0.0)
        raw_matrix.append(feats)

    keep_idx = []
    for j in range(len(candidate_columns)):
        col = [row[j] for row in raw_matrix]
        if max(col) - min(col) > 1e-12:
            keep_idx.append(j)

    feature_columns = [candidate_columns[j] for j in keep_idx]
    matrix = [[row[j] for j in keep_idx] for row in raw_matrix]

    meta = {
        "fee_levels": fee_levels,
        "ts_min": ts_min,
        "ts_max": ts_max,
        "ts_span_seconds": ts_max - ts_min,
        "blk_min": blk_min,
        "blk_max": blk_max,
        "blk_span": blk_max - blk_min,
        "notional_min": n_min,
        "notional_max": n_max,
        "notional_unique_count": len(set(notionals)),
        "notional_cap_value": notional_cap_value,
        "notional_at_cap_count": notional_at_cap_count,
        "candidate_feature_columns": candidate_columns,
        "dropped_constant_columns": [
            candidate_columns[j] for j in range(len(candidate_columns)) if j not in keep_idx
        ],
        "kept_feature_columns": feature_columns,
    }
    return matrix, feature_columns, meta


def build_targets(rows):
    r5 = [_safe_float(r.get("label_return_5m")) for r in rows]
    r30 = [_safe_float(r.get("label_return_30m")) for r in rows]
    y5 = [1 if v > 0 else 0 for v in r5]
    y30 = [1 if v > 0 else 0 for v in r30]
    return y5, y30, r5, r30


def split_indices(rows, split_field):
    train = [i for i, r in enumerate(rows) if r.get(split_field) == "train"]
    holdout = [i for i, r in enumerate(rows) if r.get(split_field) == "holdout"]
    return train, holdout


def _sigmoid(z):
    z = max(min(z, 30.0), -30.0)
    return 1.0 / (1.0 + math.exp(-z))


def logistic_regression(X, y, n_iter=LR_ITER, learning_rate=LR_LR, l2=LR_L2, seed=None):
    """L2-regularized logistic regression via batch gradient descent (pure python)."""
    n = len(X)
    if n == 0:
        return []
    d = len(X[0])
    rng = random.Random(seed if seed is not None else 0)
    w = [rng.uniform(-0.01, 0.01) for _ in range(d + 1)]
    for _ in range(n_iter):
        grad = [0.0] * (d + 1)
        for i in range(n):
            xi = X[i]
            z = w[0]
            for j in range(d):
                z += w[j + 1] * xi[j]
            p = _sigmoid(z)
            err = p - y[i]
            grad[0] += err
            for j in range(d):
                grad[j + 1] += err * xi[j]
        for j in range(d):
            grad[j + 1] += l2 * w[j + 1]
        for j in range(d + 1):
            w[j] -= learning_rate * grad[j] / max(1, n)
    return w


def predict_proba(X, w):
    if not w or not X:
        return [0.5] * len(X)
    d = len(w) - 1
    out = []
    for x in X:
        z = w[0]
        for j in range(d):
            z += w[j + 1] * x[j]
        out.append(_sigmoid(z))
    return out


def decision_stump(X, y):
    """Find the best (feature, threshold) split minimizing 0/1 misclassification."""
    n = len(X)
    if n == 0:
        return None
    d = len(X[0])
    best = None
    for j in range(d):
        values = sorted({X[i][j] for i in range(n)})
        if len(values) < 2:
            continue
        for k in range(len(values) - 1):
            threshold = (values[k] + values[k + 1]) / 2.0
            left_y = [y[i] for i in range(n) if X[i][j] <= threshold]
            right_y = [y[i] for i in range(n) if X[i][j] > threshold]
            if not left_y or not right_y:
                continue
            left_pos = sum(left_y)
            right_pos = sum(right_y)
            left_err = min(left_pos, len(left_y) - left_pos)
            right_err = min(right_pos, len(right_y) - right_pos)
            total_err = left_err + right_err
            if best is None or total_err < best["err"]:
                best = {
                    "feature_idx": j,
                    "threshold": threshold,
                    "left_pos_rate": left_pos / max(1, len(left_y)),
                    "right_pos_rate": right_pos / max(1, len(right_y)),
                    "left_count": len(left_y),
                    "right_count": len(right_y),
                    "err": total_err,
                }
    return best


def predict_stump(X, stump):
    if stump is None:
        return [0.5] * len(X)
    j = stump["feature_idx"]
    threshold = stump["threshold"]
    left = stump["left_pos_rate"]
    right = stump["right_pos_rate"]
    return [left if x[j] <= threshold else right for x in X]


def roc_auc(y, scores):
    if not y or len(y) != len(scores):
        return None
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    paired = sorted(enumerate(scores), key=lambda t: t[1])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(paired):
        j = i
        while j + 1 < len(paired) and paired[j + 1][1] == paired[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[paired[k][0]] = avg_rank
        i = j + 1
    rank_sum_pos = sum(ranks[k] for k in range(len(y)) if y[k] == 1)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def average_precision(y, scores):
    if not y or len(y) != len(scores):
        return None
    n_pos = sum(y)
    if n_pos == 0:
        return None
    paired = sorted(zip(scores, y), key=lambda t: -t[0])
    tp = 0
    fp = 0
    ap = 0.0
    prev_recall = 0.0
    for _, yi in paired:
        if yi == 1:
            tp += 1
        else:
            fp += 1
        precision = tp / max(1, tp + fp)
        recall = tp / n_pos
        if recall > prev_recall:
            ap += precision * (recall - prev_recall)
            prev_recall = recall
    return ap


def positive_rate(y):
    if not y:
        return None
    return sum(y) / len(y)


def compute_concentration(rows_subset):
    """Audit dimensional concentration (top1 share for source/pool/token/fee)."""
    if not rows_subset:
        return {
            "n": 0,
            "source_top_share": None,
            "pool_top_share": None,
            "token_top_share": None,
            "fee_top_share": None,
            "source_unique_count": 0,
            "pool_unique_count": 0,
            "token_unique_count": 0,
            "fee_unique_count": 0,
            "source_top_label": None,
            "pool_top_label": None,
            "token_top_label": None,
            "fee_top_label": None,
        }
    n = len(rows_subset)
    src = Counter(r.get("source_path", "") for r in rows_subset)
    pool = Counter(r.get("pool_address", "") for r in rows_subset)
    tok = Counter(r.get("token_address", "") for r in rows_subset)
    fee = Counter(str(r.get("fee", "")) for r in rows_subset)
    return {
        "n": n,
        "source_top_share": round(src.most_common(1)[0][1] / n, 4),
        "pool_top_share": round(pool.most_common(1)[0][1] / n, 4),
        "token_top_share": round(tok.most_common(1)[0][1] / n, 4),
        "fee_top_share": round(fee.most_common(1)[0][1] / n, 4),
        "source_unique_count": len(src),
        "pool_unique_count": len(pool),
        "token_unique_count": len(tok),
        "fee_unique_count": len(fee),
        "source_top_label": src.most_common(1)[0][0],
        "pool_top_label": pool.most_common(1)[0][0],
        "token_top_label": tok.most_common(1)[0][0],
        "fee_top_label": fee.most_common(1)[0][0],
    }


def _round(x, n=4):
    return None if x is None else round(x, n)


def evaluate_split_target(rows, X, feature_cols, y, ret, split_field, target_name):
    """Run LR (multi-seed) + stump + baselines + shuffled-label sanity + top-bucket audit."""
    train_idx, holdout_idx = split_indices(rows, split_field)
    n_train = len(train_idx)
    n_holdout = len(holdout_idx)

    X_train = [X[i] for i in train_idx]
    y_train = [y[i] for i in train_idx]
    X_holdout = [X[i] for i in holdout_idx]
    y_holdout = [y[i] for i in holdout_idx]
    ret_holdout = [ret[i] for i in holdout_idx]
    rows_holdout = [rows[i] for i in holdout_idx]

    train_pos = sum(y_train)
    holdout_pos = sum(y_holdout)
    train_pos_rate = positive_rate(y_train)
    holdout_pos_rate = positive_rate(y_holdout)
    pos_rate_drift = (
        round(holdout_pos_rate - train_pos_rate, 4)
        if (train_pos_rate is not None and holdout_pos_rate is not None)
        else None
    )

    severe_sample_size_risk = (
        holdout_pos < MIN_HOLDOUT_POSITIVES
        or (n_holdout - holdout_pos) < MIN_HOLDOUT_NEGATIVES
        or train_pos < MIN_HOLDOUT_POSITIVES
    )

    lr_aucs = []
    lr_aps = []
    lr_predss = []
    for s in SEEDS:
        w = logistic_regression(X_train, y_train, seed=s)
        preds = predict_proba(X_holdout, w)
        lr_predss.append(preds)
        lr_aucs.append(roc_auc(y_holdout, preds))
        lr_aps.append(average_precision(y_holdout, preds))
    lr_auc_valid = [a for a in lr_aucs if a is not None]
    lr_ap_valid = [a for a in lr_aps if a is not None]
    lr_auc_mean = mean(lr_auc_valid) if lr_auc_valid else None
    lr_auc_stdev = pstdev(lr_auc_valid) if len(lr_auc_valid) > 1 else 0.0
    lr_ap_mean = mean(lr_ap_valid) if lr_ap_valid else None

    stump = decision_stump(X_train, y_train)
    stump_preds = predict_stump(X_holdout, stump)
    stump_auc = roc_auc(y_holdout, stump_preds)
    stump_ap = average_precision(y_holdout, stump_preds)
    if stump:
        stump_info = {
            "feature": feature_cols[stump["feature_idx"]],
            "threshold": round(stump["threshold"], 6),
            "left_pos_rate": round(stump["left_pos_rate"], 4),
            "right_pos_rate": round(stump["right_pos_rate"], 4),
            "left_count": stump["left_count"],
            "right_count": stump["right_count"],
            "train_misclassified": stump["err"],
        }
    else:
        stump_info = None

    shuffled_aucs = []
    for s in SHUFFLE_SEEDS:
        rng = random.Random(s)
        shuffled_y = list(y_train)
        rng.shuffle(shuffled_y)
        w = logistic_regression(X_train, shuffled_y, seed=s)
        preds = predict_proba(X_holdout, w)
        a = roc_auc(y_holdout, preds)
        if a is not None:
            shuffled_aucs.append(a)
    shuffled_auc_mean = mean(shuffled_aucs) if shuffled_aucs else None
    shuffled_auc_max = max(shuffled_aucs) if shuffled_aucs else None
    shuffled_auc_min = min(shuffled_aucs) if shuffled_aucs else None

    random_auc = 0.5
    majority_class = 1 if train_pos > (n_train - train_pos) else 0
    majority_auc = 0.5
    if X_holdout and X_holdout[0]:
        rank_score_preds = [x[0] for x in X_holdout]
    else:
        rank_score_preds = [0.5] * n_holdout
    simple_score_auc = roc_auc(y_holdout, rank_score_preds)

    if lr_predss:
        primary_preds = [mean(p[i] for p in lr_predss) for i in range(n_holdout)]
    else:
        primary_preds = stump_preds
    top_n = max(1, int(round(n_holdout * TOP_BUCKET_FRACTION)))
    sorted_idx = sorted(range(n_holdout), key=lambda i: -primary_preds[i])
    top_idx = sorted_idx[:top_n]
    top_rows = [rows_holdout[i] for i in top_idx]
    top_concentration = compute_concentration(top_rows)
    top_mean_return = mean(ret_holdout[i] for i in top_idx) if top_idx else None
    top_pos_rate = sum(y_holdout[i] for i in top_idx) / top_n if top_n > 0 else None

    real_minus_shuffled = (
        round(lr_auc_mean - shuffled_auc_mean, 4)
        if (lr_auc_mean is not None and shuffled_auc_mean is not None)
        else None
    )

    signal_detected_here = (
        lr_auc_mean is not None
        and shuffled_auc_mean is not None
        and lr_auc_mean >= SIGNAL_AUC_THRESHOLD
        and (lr_auc_mean - shuffled_auc_mean) >= SHUFFLED_BUFFER
    )

    concentration_pass = all(
        (top_concentration[k] is None) or (top_concentration[k] <= CONCENTRATION_FAIL_THRESHOLD)
        for k in ("source_top_share", "pool_top_share", "token_top_share", "fee_top_share")
    )

    return {
        "split_field": split_field,
        "target_name": target_name,
        "train_rows": n_train,
        "holdout_rows": n_holdout,
        "train_positive": train_pos,
        "train_negative": n_train - train_pos,
        "holdout_positive": holdout_pos,
        "holdout_negative": n_holdout - holdout_pos,
        "train_positive_rate": _round(train_pos_rate, 4),
        "holdout_positive_rate": _round(holdout_pos_rate, 4),
        "positive_rate_drift": pos_rate_drift,
        "severe_sample_size_risk": severe_sample_size_risk,
        "lr_auc_mean": _round(lr_auc_mean),
        "lr_auc_stdev": _round(lr_auc_stdev),
        "lr_auc_per_seed": [_round(a) for a in lr_aucs],
        "lr_ap_mean": _round(lr_ap_mean),
        "stump_auc": _round(stump_auc),
        "stump_ap": _round(stump_ap),
        "stump": stump_info,
        "shuffled_auc_mean": _round(shuffled_auc_mean),
        "shuffled_auc_max": _round(shuffled_auc_max),
        "shuffled_auc_min": _round(shuffled_auc_min),
        "shuffled_auc_per_seed": [_round(a) for a in shuffled_aucs],
        "random_auc": random_auc,
        "majority_auc": majority_auc,
        "majority_class": majority_class,
        "simple_score_auc": _round(simple_score_auc),
        "real_minus_shuffled": real_minus_shuffled,
        "top_bucket_n": top_n,
        "top_bucket_concentration": top_concentration,
        "top_bucket_mean_return": _round(top_mean_return, 6) if top_mean_return is not None else None,
        "top_bucket_positive_rate": _round(top_pos_rate, 4) if top_pos_rate is not None else None,
        "signal_detected_here": signal_detected_here,
        "concentration_passes_here": concentration_pass,
        "primary_preds": primary_preds,
        "top_holdout_indices": [holdout_idx[i] for i in top_idx],
        "holdout_indices": holdout_idx,
    }


def split_independence_audit(rows):
    """Check whether pool_split, token_split, chronological_split partitions actually
    differ from each other. If two splits assign the same train/holdout label to the
    same rows, they are NOT independent and any "pool-disjoint vs chronological"
    comparison is degenerate.
    """
    n = len(rows)
    fields = ("pool_split", "token_split", "chronological_split")
    holdout_sets = {f: frozenset(i for i, r in enumerate(rows) if r.get(f) == "holdout") for f in fields}
    train_sets = {f: frozenset(i for i, r in enumerate(rows) if r.get(f) == "train") for f in fields}

    pairs = []
    for a in fields:
        for b in fields:
            if a >= b:
                continue
            ha = holdout_sets[a]
            hb = holdout_sets[b]
            inter = len(ha & hb)
            union = len(ha | hb) or 1
            jaccard_holdout = round(inter / union, 4)
            disagree = len(ha.symmetric_difference(hb))
            pairs.append({
                "split_a": a,
                "split_b": b,
                "holdout_jaccard": jaccard_holdout,
                "rows_disagreeing": disagree,
                "identical_partition": ha == hb,
            })
    all_three_identical = (
        holdout_sets["pool_split"] == holdout_sets["token_split"] == holdout_sets["chronological_split"]
    )
    pool_token_identical = holdout_sets["pool_split"] == holdout_sets["token_split"]
    return {
        "pairs": pairs,
        "all_three_identical": all_three_identical,
        "pool_token_identical": pool_token_identical,
        "rows_total": n,
        "holdout_counts": {f: len(holdout_sets[f]) for f in fields},
    }


def aggregate_signal(target_results):
    """Combine pool/token/chronological evaluations into a target-level decision."""
    pool = target_results.get("pool_disjoint", {})
    token = target_results.get("token_disjoint", {})
    chrono = target_results.get("chronological", {})

    pool_signal = bool(pool.get("signal_detected_here"))
    token_signal = bool(token.get("signal_detected_here"))
    chrono_signal = bool(chrono.get("signal_detected_here"))

    primary_signal_detected = pool_signal and token_signal
    chrono_only_signal = chrono_signal and not (pool_signal or token_signal)
    splits_contradict = pool_signal != token_signal

    return {
        "pool_disjoint_signal": pool_signal,
        "token_disjoint_signal": token_signal,
        "chronological_signal": chrono_signal,
        "primary_signal_detected": primary_signal_detected,
        "chronological_only_signal": chrono_only_signal,
        "splits_contradict": splits_contradict,
    }


def _strip_runtime_arrays(rec):
    """Drop heavy per-row arrays from the per-split record before JSON serialization."""
    keep = dict(rec)
    keep.pop("primary_preds", None)
    keep.pop("top_holdout_indices", None)
    keep.pop("holdout_indices", None)
    return keep


def render_split_target(rec):
    tc = rec["top_bucket_concentration"]
    return (
        f"  - split={rec['split_field']} target={rec['target_name']} "
        f"train={rec['train_rows']} (pos={rec['train_positive']}, neg={rec['train_negative']}), "
        f"holdout={rec['holdout_rows']} (pos={rec['holdout_positive']}, neg={rec['holdout_negative']}), "
        f"holdout_pos_rate={rec['holdout_positive_rate']}, drift={rec['positive_rate_drift']}, "
        f"lr_auc_mean={rec['lr_auc_mean']} (+/-{rec['lr_auc_stdev']}), "
        f"stump_auc={rec['stump_auc']}, "
        f"shuffled_auc_mean={rec['shuffled_auc_mean']} "
        f"(real-shuffled={rec['real_minus_shuffled']}), "
        f"simple_score_auc={rec['simple_score_auc']}, "
        f"top_bucket_n={rec['top_bucket_n']}, "
        f"top_bucket_pool/token/source/fee_top_share="
        f"{tc['pool_top_share']}/{tc['token_top_share']}/{tc['source_top_share']}/{tc['fee_top_share']}, "
        f"top_bucket_pos_rate={rec['top_bucket_positive_rate']}, "
        f"signal_here={rec['signal_detected_here']}, "
        f"conc_ok_here={rec['concentration_passes_here']}, "
        f"severe_sample_size_risk={rec['severe_sample_size_risk']}"
    )


def render_md(summary):
    decision = summary["decision_flags"]
    decision_label = summary["decision_label"]

    lines = []
    lines.append("# Run-004 Patch V3R-31 - V3 Very Small Model Dry-Run (research-only)")
    lines.append("")
    lines.append("## Mandate (HONEST FIRST)")
    lines.append("")
    lines.append(
        "Reuse V3R-29B 151-row rebalanced clean timestamp dataset. No re-quote, "
        "no source rescan, no expansion, no relabel. Only check minimal sortable "
        "signal under pool-disjoint and token-disjoint splits. Chronological is "
        "secondary reference only. Larger expansion / model research / mainline / "
        "live / shadow / promote / production model artifact remain forbidden."
    )
    lines.append("")
    lines.append(f"**Decision:** `{decision_label}`")
    lines.append("")
    lines.append("## Inputs")
    lines.append("")
    for k, v in summary["inputs"].items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    lines.append("## Dataset constants (preserved from V3R-29B / V3R-30)")
    lines.append("")
    lines.append(f"- input_rows: {summary['input_rows']}")
    lines.append(
        f"- pool_count: {summary['pool_count']}, token_count: {summary['token_count']}"
    )
    lines.append(f"- sample_size_risk: {summary['sample_size_risk']}")
    lines.append(
        f"- chronological_30m_positive_rate_drift (V3R-29B audit): "
        f"{summary['chronological_30m_positive_rate_drift']}"
    )
    lines.append(f"- sign_flip_share: {summary['sign_flip_share']}")
    lines.append(f"- duplicate_conflict_count: {summary['duplicate_conflict_count']}")
    lines.append("")
    lines.append("## Feature audit")
    lines.append("")
    lines.append(f"- candidate_columns_considered: {summary['feature_candidate_columns']}")
    lines.append(f"- feature_columns_used: {summary['feature_columns_used']}")
    lines.append(f"- dropped_constant_columns: {summary['dropped_constant_columns']}")
    lines.append(f"- banned_columns_removed: {summary['banned_columns_removed']}")
    lines.append(f"- effective_feature_count: {summary['effective_feature_count']}")
    lines.append(f"- feature_insufficient: {summary['feature_insufficient']}")
    lines.append("")
    lines.append("## Split independence audit (HONEST FIRST)")
    lines.append("")
    sia = summary["split_summaries"]["split_independence_audit"]
    lines.append(
        f"- splits_collapse_detected: {summary['split_summaries']['splits_collapse_detected']}"
    )
    lines.append(
        f"- pool_token_identical: {sia['pool_token_identical']}, "
        f"all_three_identical: {sia['all_three_identical']}"
    )
    lines.append(f"- holdout_counts: {sia['holdout_counts']}")
    for p in sia["pairs"]:
        lines.append(
            f"  - {p['split_a']} vs {p['split_b']}: holdout_jaccard={p['holdout_jaccard']}, "
            f"rows_disagreeing={p['rows_disagreeing']}, identical_partition={p['identical_partition']}"
        )
    lines.append("")
    lines.append("## Per-split / per-target results")
    lines.append("")
    for target_name in ("5m", "30m"):
        lines.append(f"### Target: {target_name}")
        lines.append("")
        rec_dict = summary[f"target_{target_name}_summary"]["per_split"]
        for split_name in ("pool_disjoint", "token_disjoint", "chronological"):
            rec = rec_dict[split_name]
            lines.append(render_split_target(rec))
        sig = summary[f"target_{target_name}_summary"]["signal_aggregate"]
        lines.append(
            f"  - aggregate: pool_signal={sig['pool_disjoint_signal']}, "
            f"token_signal={sig['token_disjoint_signal']}, "
            f"chronological_signal={sig['chronological_signal']}, "
            f"primary_signal_detected={sig['primary_signal_detected']}, "
            f"chronological_only_signal={sig['chronological_only_signal']}, "
            f"splits_contradict={sig['splits_contradict']}"
        )
        lines.append("")
    lines.append("## Baseline / sanity")
    lines.append("")
    for line in summary["baseline_results_summary_lines"]:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## Top-bucket concentration cross-summary")
    lines.append("")
    for line in summary["concentration_audit_lines"]:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## Decision flags")
    lines.append("")
    for k, v in decision.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Plain-language verdict (per V3R-31 mandate)")
    lines.append("")
    for line in summary["plain_language_verdict"]:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## Forbidden actions reaffirmed")
    lines.append("")
    lines.append(
        "- `larger_expansion / formal model research / mainline / live / shadow / "
        "promote` remain disabled regardless of this dry-run outcome."
    )
    lines.append(
        "- Even on `PASSED_VERY_SMALL_MODEL_DRYRUN`, the only allowed next step is "
        "`V3R-32 dry-run review`. **No** V3 alpha is declared."
    )
    lines.append(
        "- No production model artifact is generated. No write into "
        "`services/`, `strategy/`, or `models/`."
    )
    lines.append("")
    return "\n".join(lines)


def _write_per_row_csv(rows, t5, t30, path):
    """Per-row predictions for inspection only (research artifact, NOT a model file)."""
    n = len(rows)
    pred_5m_pool = [None] * n
    pred_5m_token = [None] * n
    pred_5m_chrono = [None] * n
    pred_30m_pool = [None] * n
    pred_30m_token = [None] * n
    pred_30m_chrono = [None] * n
    in_top_5m_pool = [0] * n
    in_top_5m_token = [0] * n
    in_top_30m_pool = [0] * n
    in_top_30m_token = [0] * n

    def _fill(rec, pred_list, top_list=None):
        ho_idx = rec["holdout_indices"]
        for k, idx in enumerate(ho_idx):
            pred_list[idx] = round(rec["primary_preds"][k], 6)
        if top_list is not None:
            for idx in rec["top_holdout_indices"]:
                top_list[idx] = 1

    _fill(t5["pool_disjoint"], pred_5m_pool, in_top_5m_pool)
    _fill(t5["token_disjoint"], pred_5m_token, in_top_5m_token)
    _fill(t5["chronological"], pred_5m_chrono)
    _fill(t30["pool_disjoint"], pred_30m_pool, in_top_30m_pool)
    _fill(t30["token_disjoint"], pred_30m_token, in_top_30m_token)
    _fill(t30["chronological"], pred_30m_chrono)

    fieldnames = [
        "row_index",
        "tx_hash",
        "log_index",
        "source_path",
        "pool_address",
        "token_address",
        "fee",
        "entry_block",
        "entry_timestamp",
        "selected_notional_weth",
        "label_return_5m",
        "label_return_30m",
        "target_5m",
        "target_30m",
        "chronological_split",
        "pool_split",
        "token_split",
        "pred_lr_5m_pool_disjoint",
        "pred_lr_30m_pool_disjoint",
        "pred_lr_5m_token_disjoint",
        "pred_lr_30m_token_disjoint",
        "pred_lr_5m_chronological",
        "pred_lr_30m_chronological",
        "in_top_bucket_5m_pool_disjoint",
        "in_top_bucket_30m_pool_disjoint",
        "in_top_bucket_5m_token_disjoint",
        "in_top_bucket_30m_token_disjoint",
        "research_only",
        "deployable_model_artifact",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for i, r in enumerate(rows):
            target_5m = 1 if _safe_float(r.get("label_return_5m")) > 0 else 0
            target_30m = 1 if _safe_float(r.get("label_return_30m")) > 0 else 0
            writer.writerow(
                {
                    "row_index": i,
                    "tx_hash": r.get("tx_hash", ""),
                    "log_index": r.get("log_index", ""),
                    "source_path": r.get("source_path", ""),
                    "pool_address": r.get("pool_address", ""),
                    "token_address": r.get("token_address", ""),
                    "fee": r.get("fee", ""),
                    "entry_block": r.get("entry_block", ""),
                    "entry_timestamp": r.get("entry_timestamp", ""),
                    "selected_notional_weth": r.get("selected_notional_weth", ""),
                    "label_return_5m": r.get("label_return_5m", ""),
                    "label_return_30m": r.get("label_return_30m", ""),
                    "target_5m": target_5m,
                    "target_30m": target_30m,
                    "chronological_split": r.get("chronological_split", ""),
                    "pool_split": r.get("pool_split", ""),
                    "token_split": r.get("token_split", ""),
                    "pred_lr_5m_pool_disjoint": pred_5m_pool[i],
                    "pred_lr_30m_pool_disjoint": pred_30m_pool[i],
                    "pred_lr_5m_token_disjoint": pred_5m_token[i],
                    "pred_lr_30m_token_disjoint": pred_30m_token[i],
                    "pred_lr_5m_chronological": pred_5m_chrono[i],
                    "pred_lr_30m_chronological": pred_30m_chrono[i],
                    "in_top_bucket_5m_pool_disjoint": in_top_5m_pool[i],
                    "in_top_bucket_30m_pool_disjoint": in_top_30m_pool[i],
                    "in_top_bucket_5m_token_disjoint": in_top_5m_token[i],
                    "in_top_bucket_30m_token_disjoint": in_top_30m_token[i],
                    "research_only": True,
                    "deployable_model_artifact": False,
                }
            )


def main():
    if not V3R29B_ROWS.exists():
        print(f"missing {V3R29B_ROWS}", file=sys.stderr)
        return 2

    rows = load_csv(V3R29B_ROWS)
    v3r29b_summary = load_json(V3R29B_SUMMARY) if V3R29B_SUMMARY.exists() else {}
    v3r30_summary = load_json(V3R30_SUMMARY) if V3R30_SUMMARY.exists() else {}
    v3r29_summary = load_json(V3R29_SUMMARY) if V3R29_SUMMARY.exists() else {}

    csv_columns = list(rows[0].keys()) if rows else []
    banned_present_in_csv = sorted(set(csv_columns) & BANNED_FEATURE_COLUMNS)

    X, feature_cols, feature_meta = build_features(rows)
    leaked_features_used = sorted(set(feature_cols) & BANNED_FEATURE_COLUMNS)
    leakage_guard_passed = not leaked_features_used
    feature_insufficient = len(feature_cols) < MIN_FEATURE_COUNT

    y5, y30, r5, r30 = build_targets(rows)

    duplicate_conflict_count = int(v3r29b_summary.get("duplicate_conflict_count", 0))
    pool_count = int(v3r29b_summary.get("pool_count", EXPECTED_POOL_COUNT))
    token_count = int(v3r29b_summary.get("token_count", EXPECTED_TOKEN_COUNT))
    sample_size_risk = (
        pool_count < SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD
        or token_count < SAMPLE_SIZE_POOL_TOKEN_RISK_THRESHOLD
        or len(rows) <= 200
    )

    sign_flip_count = sum(
        1 for r in rows if str(r.get("sign_flip_5m_30m", "")).lower() == "true"
    )
    sign_flip_share = round(sign_flip_count / max(1, len(rows)), 4)

    chronological_30m_drift_v3r29b = float(EXPECTED_30M_CHRONO_DRIFT)
    if isinstance(v3r29b_summary.get("split_audit"), dict):
        try:
            ch = v3r29b_summary["split_audit"]["chronological"]["target_30m"]
            chronological_30m_drift_v3r29b = round(
                float(ch.get("holdout_positive_rate", 0.0))
                - float(ch.get("train_positive_rate", 0.0)),
                4,
            )
        except Exception:
            pass

    target_5m_per_split = {}
    target_30m_per_split = {}
    splits_to_run = [
        ("pool_disjoint", "pool_split"),
        ("token_disjoint", "token_split"),
        ("chronological", "chronological_split"),
    ]
    for split_name, split_field in splits_to_run:
        target_5m_per_split[split_name] = evaluate_split_target(
            rows, X, feature_cols, y5, r5, split_field, "5m"
        )
        target_30m_per_split[split_name] = evaluate_split_target(
            rows, X, feature_cols, y30, r30, split_field, "30m"
        )

    sig5 = aggregate_signal(target_5m_per_split)
    sig30 = aggregate_signal(target_30m_per_split)

    split_independence = split_independence_audit(rows)
    splits_collapse_detected = (
        split_independence["all_three_identical"]
        or split_independence["pool_token_identical"]
    )

    pool_disjoint_feasible = (
        target_5m_per_split["pool_disjoint"]["holdout_rows"] >= 30
        and not target_5m_per_split["pool_disjoint"]["severe_sample_size_risk"]
        and target_30m_per_split["pool_disjoint"]["holdout_rows"] >= 30
        and not target_30m_per_split["pool_disjoint"]["severe_sample_size_risk"]
    )
    token_disjoint_feasible = (
        target_5m_per_split["token_disjoint"]["holdout_rows"] >= 30
        and not target_5m_per_split["token_disjoint"]["severe_sample_size_risk"]
        and target_30m_per_split["token_disjoint"]["holdout_rows"] >= 30
        and not target_30m_per_split["token_disjoint"]["severe_sample_size_risk"]
    )

    target_5m_signal_detected = (
        bool(sig5["primary_signal_detected"]) and not sig5["splits_contradict"]
    )
    target_30m_signal_detected = (
        bool(sig30["primary_signal_detected"]) and not sig30["splits_contradict"]
    )
    dual_horizon_consistency_passed = (
        target_5m_signal_detected == target_30m_signal_detected
    )

    concentration_passes_5m = all(
        target_5m_per_split[s]["concentration_passes_here"]
        for s in ("pool_disjoint", "token_disjoint")
    )
    concentration_passes_30m = all(
        target_30m_per_split[s]["concentration_passes_here"]
        for s in ("pool_disjoint", "token_disjoint")
    )
    concentration_guard_passed = concentration_passes_5m and concentration_passes_30m

    pool_disjoint_passed = (
        pool_disjoint_feasible
        and target_5m_per_split["pool_disjoint"]["concentration_passes_here"]
        and target_30m_per_split["pool_disjoint"]["concentration_passes_here"]
    )
    token_disjoint_passed = (
        token_disjoint_feasible
        and target_5m_per_split["token_disjoint"]["concentration_passes_here"]
        and target_30m_per_split["token_disjoint"]["concentration_passes_here"]
    )

    chronological_only_dependency = (
        sig5["chronological_only_signal"] or sig30["chronological_only_signal"]
    )

    very_small_model_dryrun_passed = (
        leakage_guard_passed
        and not feature_insufficient
        and pool_disjoint_feasible
        and token_disjoint_feasible
        and dual_horizon_consistency_passed
        and target_5m_signal_detected
        and target_30m_signal_detected
        and concentration_guard_passed
        and not chronological_only_dependency
        and not splits_collapse_detected
        and len(rows) == EXPECTED_INPUT_ROWS
        and duplicate_conflict_count == 0
    )

    if not leakage_guard_passed:
        decision_label = "FAILED_LEAKAGE_GUARD"
    elif feature_insufficient:
        decision_label = "FAILED_FEATURE_INSUFFICIENT"
    elif (
        target_5m_per_split["pool_disjoint"]["severe_sample_size_risk"]
        or target_5m_per_split["token_disjoint"]["severe_sample_size_risk"]
        or target_30m_per_split["pool_disjoint"]["severe_sample_size_risk"]
        or target_30m_per_split["token_disjoint"]["severe_sample_size_risk"]
    ):
        decision_label = "FAILED_DATA_INSUFFICIENT"
    elif sig5["splits_contradict"] or sig30["splits_contradict"]:
        decision_label = "FAILED_TARGET_INSTABILITY"
    elif not dual_horizon_consistency_passed:
        decision_label = "FAILED_TARGET_INSTABILITY"
    elif not concentration_guard_passed and (
        target_5m_signal_detected or target_30m_signal_detected
    ):
        decision_label = "FAILED_CONCENTRATED_SIGNAL"
    elif chronological_only_dependency:
        decision_label = "FAILED_MODEL_DRYRUN"
    elif very_small_model_dryrun_passed:
        decision_label = "PASSED_VERY_SMALL_MODEL_DRYRUN"
    else:
        decision_label = "FAILED_MODEL_DRYRUN"

    concentration_audit_lines = []
    for target_name, per_split in (("5m", target_5m_per_split), ("30m", target_30m_per_split)):
        for split_name in ("pool_disjoint", "token_disjoint", "chronological"):
            rec = per_split[split_name]
            tc = rec["top_bucket_concentration"]
            concentration_audit_lines.append(
                f"target={target_name} split={split_name} top_n={rec['top_bucket_n']} "
                f"pool_top={tc['pool_top_share']} ({tc['pool_unique_count']} unique), "
                f"token_top={tc['token_top_share']} ({tc['token_unique_count']} unique), "
                f"source_top={tc['source_top_share']} ({tc['source_unique_count']} unique), "
                f"fee_top={tc['fee_top_share']} ({tc['fee_unique_count']} unique), "
                f"top_bucket_mean_return={rec['top_bucket_mean_return']}, "
                f"top_bucket_pos_rate={rec['top_bucket_positive_rate']}"
            )

    baseline_results_summary_lines = []
    for target_name, per_split in (("5m", target_5m_per_split), ("30m", target_30m_per_split)):
        for split_name in ("pool_disjoint", "token_disjoint", "chronological"):
            rec = per_split[split_name]
            baseline_results_summary_lines.append(
                f"target={target_name} split={split_name}: "
                f"random={rec['random_auc']}, majority={rec['majority_auc']}, "
                f"simple_score={rec['simple_score_auc']}, "
                f"shuffled_label_mean={rec['shuffled_auc_mean']} "
                f"(min={rec['shuffled_auc_min']}, max={rec['shuffled_auc_max']}), "
                f"real_minus_shuffled={rec['real_minus_shuffled']}"
            )

    real_above_shuffled_5m = all(
        (target_5m_per_split[s]["real_minus_shuffled"] or 0) >= SHUFFLED_BUFFER
        for s in ("pool_disjoint", "token_disjoint")
    )
    real_above_shuffled_30m = all(
        (target_30m_per_split[s]["real_minus_shuffled"] or 0) >= SHUFFLED_BUFFER
        for s in ("pool_disjoint", "token_disjoint")
    )

    plain_language_verdict = []
    plain_language_verdict.append(
        f"1) 151-row rebalanced dataset has minimal sortable signal? "
        f"5m={target_5m_signal_detected}, 30m={target_30m_signal_detected}."
    )
    plain_language_verdict.append(
        f"2) Signal consistent across 5m and 30m? "
        f"dual_horizon_consistency_passed={dual_horizon_consistency_passed}."
    )
    plain_language_verdict.append(
        f"3) Result depends on chronological split? "
        f"chronological_only_dependency={chronological_only_dependency}."
    )
    plain_language_verdict.append(
        f"4) Result dominated by single pool/token/source/fee? "
        f"concentration_guard_passed={concentration_guard_passed}."
    )
    plain_language_verdict.append(
        f"5) Shuffled-label sanity passes (real beats shuffled by >= "
        f"{SHUFFLED_BUFFER})? 5m={real_above_shuffled_5m}, 30m={real_above_shuffled_30m}."
    )
    plain_language_verdict.append(
        f"6) sample_size_risk still serious? sample_size_risk={sample_size_risk}, "
        f"pool_count/token_count={pool_count}/{token_count} (ideal>=50). "
        f"splits_collapse_detected={splits_collapse_detected} "
        f"(pool_token_identical={split_independence['pool_token_identical']}, "
        f"all_three_identical={split_independence['all_three_identical']})."
    )
    if very_small_model_dryrun_passed:
        plain_language_verdict.append(
            "7) Next step: V3R-32 dry-run review (review-only). No larger expansion / "
            "model research / mainline / live."
        )
    else:
        plain_language_verdict.append(
            "7) Next step: do NOT proceed to V3R-32. Return to V3R-29B / V3R-30 data "
            "repair (or accept that 151-row v3 dataset has no minimal signal)."
        )
    plain_language_verdict.append(
        "8) larger_expansion=False, model_research=False, mainline=False, live=False, "
        "shadow=False, promote=False, deployable_model_artifact=False (forbidden in all paths)."
    )

    decision_flags = {
        "very_small_model_dryrun_passed": bool(very_small_model_dryrun_passed),
        "target_5m_signal_detected": bool(target_5m_signal_detected),
        "target_30m_signal_detected": bool(target_30m_signal_detected),
        "dual_horizon_consistency_passed": bool(dual_horizon_consistency_passed),
        "leakage_guard_passed": bool(leakage_guard_passed),
        "pool_disjoint_passed": bool(pool_disjoint_passed),
        "token_disjoint_passed": bool(token_disjoint_passed),
        "concentration_guard_passed": bool(concentration_guard_passed),
        "shuffled_label_sanity_passed_5m": bool(real_above_shuffled_5m),
        "shuffled_label_sanity_passed_30m": bool(real_above_shuffled_30m),
        "chronological_only_dependency": bool(chronological_only_dependency),
        "feature_insufficient": bool(feature_insufficient),
        "sample_size_risk": bool(sample_size_risk),
        "duplicate_conflict_count_zero": duplicate_conflict_count == 0,
        "input_rows_match_expected": len(rows) == EXPECTED_INPUT_ROWS,
        "deployable_model_artifact_generated": False,
        "wrote_into_services_or_strategy_or_models": False,
        "splits_collapse_detected": bool(splits_collapse_detected),
        "pool_token_split_identical": bool(split_independence["pool_token_identical"]),
        "all_three_splits_identical": bool(split_independence["all_three_identical"]),
        "allow_v3r32_dryrun_review": bool(very_small_model_dryrun_passed),
        "allow_larger_expansion": False,
        "allow_model_research": False,
        "allow_mainline": False,
        "allow_live": False,
        "allow_shadow": False,
        "allow_promote": False,
    }

    summary = {
        "run": SOURCE_RUN,
        "research_only": True,
        "no_model_artifacts": True,
        "no_feature_research": True,
        "no_larger_expansion": True,
        "no_mainline": True,
        "no_live": True,
        "no_shadow": True,
        "no_promote": True,
        "no_source_rescan": True,
        "no_requote": True,
        "no_relabel": True,
        "no_horizon_change": True,
        "inputs": {
            "v3r29b_summary": str(V3R29B_SUMMARY.relative_to(ROOT)),
            "v3r29b_rows": str(V3R29B_ROWS.relative_to(ROOT)),
            "v3r29b_report": str(V3R29B_REPORT.relative_to(ROOT)),
            "v3r30_summary": str(V3R30_SUMMARY.relative_to(ROOT)),
            "v3r29_summary": (
                str(V3R29_SUMMARY.relative_to(ROOT)) if V3R29_SUMMARY.exists() else None
            ),
        },
        "input_rows": len(rows),
        "duplicate_conflict_count": duplicate_conflict_count,
        "pool_count": pool_count,
        "token_count": token_count,
        "sample_size_risk": bool(sample_size_risk),
        "sign_flip_share": sign_flip_share,
        "chronological_30m_positive_rate_drift": chronological_30m_drift_v3r29b,
        "csv_columns": csv_columns,
        "banned_columns_present_in_csv": banned_present_in_csv,
        "banned_columns_removed": sorted(BANNED_FEATURE_COLUMNS),
        "banned_feature_columns_count": len(BANNED_FEATURE_COLUMNS),
        "feature_candidate_columns": feature_meta["candidate_feature_columns"],
        "dropped_constant_columns": feature_meta["dropped_constant_columns"],
        "feature_columns_used": feature_cols,
        "effective_feature_count": len(feature_cols),
        "feature_insufficient": bool(feature_insufficient),
        "feature_meta": {
            "fee_levels": feature_meta["fee_levels"],
            "ts_span_seconds": feature_meta["ts_span_seconds"],
            "blk_span": feature_meta["blk_span"],
            "notional_unique_count": feature_meta["notional_unique_count"],
            "notional_at_cap_count": feature_meta["notional_at_cap_count"],
            "notional_min": feature_meta["notional_min"],
            "notional_max": feature_meta["notional_max"],
        },
        "target_5m_summary": {
            "positive_count": sum(y5),
            "positive_rate": round(sum(y5) / max(1, len(y5)), 4),
            "per_split": {
                k: _strip_runtime_arrays(v) for k, v in target_5m_per_split.items()
            },
            "signal_aggregate": sig5,
        },
        "target_30m_summary": {
            "positive_count": sum(y30),
            "positive_rate": round(sum(y30) / max(1, len(y30)), 4),
            "per_split": {
                k: _strip_runtime_arrays(v) for k, v in target_30m_per_split.items()
            },
            "signal_aggregate": sig30,
        },
        "split_summaries": {
            "splits_run": [s for s, _ in splits_to_run],
            "primary_splits": ["pool_disjoint", "token_disjoint"],
            "reference_splits": ["chronological"],
            "pool_disjoint_feasible": bool(pool_disjoint_feasible),
            "token_disjoint_feasible": bool(token_disjoint_feasible),
            "split_independence_audit": split_independence,
            "splits_collapse_detected": bool(splits_collapse_detected),
        },
        "model_results_5m": {
            split_name: {
                "lr_auc_mean": rec["lr_auc_mean"],
                "lr_auc_per_seed": rec["lr_auc_per_seed"],
                "stump_auc": rec["stump_auc"],
                "lr_ap_mean": rec["lr_ap_mean"],
                "stump_ap": rec["stump_ap"],
                "real_minus_shuffled": rec["real_minus_shuffled"],
            }
            for split_name, rec in target_5m_per_split.items()
        },
        "model_results_30m": {
            split_name: {
                "lr_auc_mean": rec["lr_auc_mean"],
                "lr_auc_per_seed": rec["lr_auc_per_seed"],
                "stump_auc": rec["stump_auc"],
                "lr_ap_mean": rec["lr_ap_mean"],
                "stump_ap": rec["stump_ap"],
                "real_minus_shuffled": rec["real_minus_shuffled"],
            }
            for split_name, rec in target_30m_per_split.items()
        },
        "baseline_results": {
            split_name + "_" + target_name: {
                "random_auc": rec["random_auc"],
                "majority_auc": rec["majority_auc"],
                "majority_class": rec["majority_class"],
                "simple_score_auc": rec["simple_score_auc"],
            }
            for target_name, per_split in (
                ("5m", target_5m_per_split),
                ("30m", target_30m_per_split),
            )
            for split_name, rec in per_split.items()
        },
        "shuffled_label_results": {
            split_name + "_" + target_name: {
                "shuffled_auc_mean": rec["shuffled_auc_mean"],
                "shuffled_auc_min": rec["shuffled_auc_min"],
                "shuffled_auc_max": rec["shuffled_auc_max"],
                "shuffled_auc_per_seed": rec["shuffled_auc_per_seed"],
                "real_minus_shuffled": rec["real_minus_shuffled"],
            }
            for target_name, per_split in (
                ("5m", target_5m_per_split),
                ("30m", target_30m_per_split),
            )
            for split_name, rec in per_split.items()
        },
        "top_bucket_concentration": {
            split_name + "_" + target_name: rec["top_bucket_concentration"]
            for target_name, per_split in (
                ("5m", target_5m_per_split),
                ("30m", target_30m_per_split),
            )
            for split_name, rec in per_split.items()
        },
        "dual_horizon_consistency": {
            "target_5m_signal_detected": bool(target_5m_signal_detected),
            "target_30m_signal_detected": bool(target_30m_signal_detected),
            "passed": bool(dual_horizon_consistency_passed),
            "5m_aggregate": sig5,
            "30m_aggregate": sig30,
        },
        "concentration_audit_lines": concentration_audit_lines,
        "baseline_results_summary_lines": baseline_results_summary_lines,
        "plain_language_verdict": plain_language_verdict,
        "decision_flags": decision_flags,
        "decision_label": decision_label,
        "thresholds": {
            "signal_auc_threshold": SIGNAL_AUC_THRESHOLD,
            "shuffled_buffer": SHUFFLED_BUFFER,
            "concentration_fail_threshold": CONCENTRATION_FAIL_THRESHOLD,
            "min_holdout_positives": MIN_HOLDOUT_POSITIVES,
            "min_holdout_negatives": MIN_HOLDOUT_NEGATIVES,
            "min_feature_count": MIN_FEATURE_COUNT,
            "top_bucket_fraction": TOP_BUCKET_FRACTION,
            "lr_iter": LR_ITER,
            "lr_learning_rate": LR_LR,
            "lr_l2": LR_L2,
            "seeds": SEEDS,
            "shuffle_seeds": SHUFFLE_SEEDS,
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)

    with OUT_JSON.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=False)

    _write_per_row_csv(rows, target_5m_per_split, target_30m_per_split, OUT_CSV)

    md = render_md(summary)
    OUT_MD.write_text(md, encoding="utf-8")

    print(f"V3R-31 decision: {decision_label}")
    print(f"  input_rows={len(rows)}; pool_count={pool_count}; token_count={token_count}")
    print(f"  feature_columns_used={feature_cols}")
    for tname, per_split in (("5m", target_5m_per_split), ("30m", target_30m_per_split)):
        for split_name in ("pool_disjoint", "token_disjoint", "chronological"):
            rec = per_split[split_name]
            print(
                f"  {split_name}/{tname}: lr_auc={rec['lr_auc_mean']} "
                f"shuffled={rec['shuffled_auc_mean']} delta={rec['real_minus_shuffled']} "
                f"signal_here={rec['signal_detected_here']} "
                f"conc_ok={rec['concentration_passes_here']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
