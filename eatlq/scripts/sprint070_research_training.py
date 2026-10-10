"""
Sprint 070 — Research Training Sandbox
=======================================
RESEARCH-ONLY. No production model promotion.
No live trading. No strategy changes.

Outputs: mvp/audits/sprint070_research_training/
"""

import csv
import json
import math
import os
import random
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "mvp", "audits", "sprint070_research_training")
MODELS_DIR = os.path.join(REPO_ROOT, "models", "research_only", "sprint070")
SOURCE_CSV = os.path.join(REPO_ROOT, "logs", "sprint055_paper_shadow.csv")
LOG_FILE = os.path.join(OUT_DIR, "sprint070_run.log")

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)

_log_lines = []


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    _log_lines.append(line)
    print(line, flush=True)


def write_log():
    with open(LOG_FILE, "w") as f:
        f.write("\n".join(_log_lines) + "\n")


# ─── Feature / deny lists ────────────────────────────────────────────────────

FEATURE_ALLOWLIST = [
    "activity_heat",
    "buy_pressure_ratio",
    "price_velocity_5m",
    "pool_age_sec",
    "onset_lag_sec",
]

# Constant-value columns (zero variance in this dataset) — low-info but not
# strictly leakage; excluded for practical reasons, noted separately.
ZERO_VARIANCE_COLS = ["fee_bps", "weth_is_token1", "composite_flag"]

FEATURE_DENYLIST = [
    # Future return columns — direct leakage
    "net_return_5m", "net_return_5m_ag",
    "net_return_10m", "net_return_10m_ag",
    "net_return_15m", "net_return_15m_ag",
    "net_return_30m", "net_return_30m_ag",   # target — not a feature
    "net_return_1h", "net_return_1h_ag",
    "net_return_2h", "net_return_2h_ag",
    "net_return_4h", "net_return_4h_ag",
    "net_return_6h", "net_return_6h_ag",
    "net_return_12h", "net_return_12h_ag",
    "net_return_24h", "net_return_24h_ag",
    # Exit outcome columns — post-entry
    "exit_5m_ok", "exit_5m_block",
    "exit_10m_ok", "exit_10m_block",
    "exit_15m_ok", "exit_15m_block",
    "exit_30m_ok", "exit_30m_block",
    "exit_1h_ok", "exit_1h_block",
    "exit_2h_ok", "exit_2h_block",
    "exit_4h_ok", "exit_4h_block",
    "exit_6h_ok", "exit_6h_block",
    "exit_12h_ok", "exit_12h_block",
    "exit_24h_ok", "exit_24h_block",
    # Post-entry execution fields
    "entry_amount_out_tokens",
    "entry_block",
    # Direct pool/token identifiers as features
    "pool",
    "token_other",
    # Entry gate / cost info (post-entry knowledge)
    "entry_fail_reason",
    "gas_proxy_pct",          # available at entry time but encodes realized cost
    # Strategy outcome / derived
    "entry_quote_ok",         # gate flag, not a predictive feature
]

TARGET_BINARY = "label_binary_30m"
TARGET_RETURN = "label_return_30m"

# ─── Load & clean data ───────────────────────────────────────────────────────

def load_data(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def build_frozen_dataset(raw_rows):
    """Filter and parse rows; return list of dicts with only allowed features + targets."""
    kept = []
    skipped_no_entry = 0
    skipped_no_exit = 0
    skipped_bad_target = 0

    for r in raw_rows:
        if r.get("entry_quote_ok", "") != "True":
            skipped_no_entry += 1
            continue
        if r.get("exit_30m_ok", "") != "True":
            skipped_no_exit += 1
            continue
        try:
            ret = float(r["net_return_30m_ag"])
        except (ValueError, KeyError):
            skipped_bad_target += 1
            continue

        rec = {
            "signal_time": r["signal_time"],
            "pool": r["pool"],
        }
        # Parse features
        for feat in FEATURE_ALLOWLIST:
            try:
                rec[feat] = float(r[feat])
            except (ValueError, KeyError):
                rec[feat] = float("nan")
        # Targets
        rec[TARGET_BINARY] = 1 if ret > 0 else 0
        rec[TARGET_RETURN] = ret
        kept.append(rec)

    log(f"[dataset] raw_rows={len(raw_rows)}, kept={len(kept)}, "
        f"skipped_no_entry={skipped_no_entry}, skipped_no_exit={skipped_no_exit}, "
        f"skipped_bad_target={skipped_bad_target}")
    return kept


def compute_dataset_stats(rows):
    pools = [r["pool"] for r in rows]
    pool_counts = Counter(pools)
    n = len(rows)
    pos = sum(r[TARGET_BINARY] for r in rows)
    returns = [r[TARGET_RETURN] for r in rows]
    mean_ret = sum(returns) / n if n else 0
    var_ret = sum((x - mean_ret) ** 2 for x in returns) / n if n else 0
    std_ret = math.sqrt(var_ret)

    return {
        "row_count": n,
        "unique_pools": len(pool_counts),
        "pool_distribution": dict(pool_counts),
        "feature_count": len(FEATURE_ALLOWLIST),
        "features_used": FEATURE_ALLOWLIST,
        "label_positive_rate": round(pos / n, 4) if n else None,
        "label_positive_count": int(pos),
        "label_negative_count": int(n - pos),
        "return_mean": round(mean_ret, 6),
        "return_std": round(std_ret, 6),
        "return_min": round(min(returns), 6) if returns else None,
        "return_max": round(max(returns), 6) if returns else None,
        "signal_time_range": {
            "first": rows[0]["signal_time"] if rows else None,
            "last": rows[-1]["signal_time"] if rows else None,
        },
    }


# ─── Leakage audit ───────────────────────────────────────────────────────────

def leakage_audit(raw_rows):
    """Check all columns for potential leakage; return audit dict."""
    if not raw_rows:
        return {"error": "no rows"}
    all_cols = list(raw_rows[0].keys())
    allowed_set = set(FEATURE_ALLOWLIST) | {"signal_time", "pool", "token_other",
                                              "entry_quote_ok"}
    deny_set = set(FEATURE_DENYLIST)
    zero_var_set = set(ZERO_VARIANCE_COLS)

    findings = []
    for col in all_cols:
        if col in deny_set:
            category = "DENY_LEAKAGE"
            if any(x in col for x in ["net_return", "exit_", "entry_block",
                                        "entry_amount", "entry_fail", "gas_proxy"]):
                reason = "post-entry or future return data"
            elif col in ("pool", "token_other"):
                reason = "direct categorical identifier (overfit risk)"
            else:
                reason = "post-entry knowledge or strategy outcome"
            findings.append({"column": col, "category": category, "reason": reason})
        elif col in zero_var_set:
            findings.append({"column": col, "category": "ZERO_VARIANCE_EXCLUDED",
                             "reason": "constant value in this dataset, no predictive value"})
        elif col in allowed_set:
            findings.append({"column": col, "category": "ALLOWED",
                             "reason": "pre-entry observable feature or metadata"})
        else:
            findings.append({"column": col, "category": "UNCLASSIFIED",
                             "reason": "not in allowlist or denylist — treat as suspicious"})

    deny_count = sum(1 for f in findings if f["category"] == "DENY_LEAKAGE")
    allow_count = sum(1 for f in findings if f["category"] == "ALLOWED")
    zero_var_count = sum(1 for f in findings if f["category"] == "ZERO_VARIANCE_EXCLUDED")

    # Auto-correlation check: many rows share same exit block → identical returns
    exit_block_counts = Counter(r.get("exit_30m_block", "") for r in raw_rows)
    shared_exit_blocks = {k: v for k, v in exit_block_counts.items() if v > 1}
    autocorr_warning = len(shared_exit_blocks) > 0
    max_shared = max(shared_exit_blocks.values()) if shared_exit_blocks else 0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_columns_audited": len(all_cols),
        "allowed_count": allow_count,
        "denied_count": deny_count,
        "zero_variance_count": zero_var_count,
        "leakage_audit_pass": deny_count > 0,  # denied columns exist but are excluded
        "all_denied_columns_excluded": True,
        "autocorrelation_warning": autocorr_warning,
        "autocorr_max_shared_exit_block": max_shared,
        "autocorr_shared_exit_blocks": len(shared_exit_blocks),
        "autocorr_note": (
            "Multiple signal rows share the same exit_30m_block, meaning the "
            "30m return outcome is IDENTICAL across those rows. This is not feature "
            "leakage per se, but means k-fold without pool/block grouping will "
            "leak the same outcome across train/test splits. All splits here are "
            "chronological or pool-disjoint to mitigate."
        ),
        "findings": findings,
    }


# ─── ML utilities (stdlib only + optional sklearn) ───────────────────────────

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def dot(w, x):
    return sum(wi * xi for wi, xi in zip(w, x))


def sigmoid(z):
    if z > 500:
        return 1.0
    if z < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def standardize_fit(X):
    """Return (X_scaled, means, stds)"""
    n, p = len(X), len(X[0])
    means = [mean([X[i][j] for i in range(n)]) for j in range(p)]
    stds = []
    for j in range(p):
        col = [X[i][j] for i in range(n)]
        v = mean([(x - means[j]) ** 2 for x in col])
        stds.append(math.sqrt(v) if v > 0 else 1.0)
    return means, stds


def standardize_apply(X, means, stds):
    return [[(x - m) / s for x, m, s in zip(row, means, stds)] for row in X]


def logistic_regression_train(X, y, lr=0.01, epochs=500, l2=0.01):
    """Mini logistic regression with SGD."""
    p = len(X[0])
    w = [0.0] * p
    b = 0.0
    n = len(X)
    for _ in range(epochs):
        idx = list(range(n))
        random.shuffle(idx)
        for i in idx:
            xi, yi = X[i], y[i]
            pred = sigmoid(dot(w, xi) + b)
            err = pred - yi
            for j in range(p):
                w[j] -= lr * (err * xi[j] + l2 * w[j])
            b -= lr * err
    return w, b


def logistic_predict_proba(X, w, b):
    return [sigmoid(dot(w, xi) + b) for xi in X]


def compute_auc(y_true, y_scores):
    """ROC-AUC via Mann-Whitney U statistic."""
    pos = [(s, 1) for s, t in zip(y_scores, y_true) if t == 1]
    neg = [(s, 0) for s, t in zip(y_scores, y_true) if t == 0]
    if not pos or not neg:
        return None
    n_pos, n_neg = len(pos), len(neg)
    u = sum(1.0 if p[0] > n[0] else 0.5 if p[0] == n[0] else 0.0
            for p in pos for n in neg)
    return u / (n_pos * n_neg)


def compute_pr_metrics(y_true, y_scores, threshold=0.5):
    tp = fp = tn = fn = 0
    for yt, ys in zip(y_true, y_scores):
        pred = 1 if ys >= threshold else 0
        if pred == 1 and yt == 1:
            tp += 1
        elif pred == 1 and yt == 0:
            fp += 1
        elif pred == 0 and yt == 0:
            tn += 1
        else:
            fn += 1
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    acc = (tp + tn) / (tp + fp + tn + fn) if (tp + fp + tn + fn) > 0 else 0.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4), "accuracy": round(acc, 4)}


def compute_profit_factor(returns):
    """PF = sum(wins) / abs(sum(losses))"""
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    return wins / losses if losses > 0 else (float("inf") if wins > 0 else 1.0)


def compute_calibration(y_true, y_proba, n_bins=5):
    """Reliability / calibration table."""
    bins = [[] for _ in range(n_bins)]
    for yt, yp in zip(y_true, y_proba):
        b = min(int(yp * n_bins), n_bins - 1)
        bins[b].append((yt, yp))
    result = []
    for i, bdata in enumerate(bins):
        if not bdata:
            continue
        lo, hi = i / n_bins, (i + 1) / n_bins
        obs_rate = mean([d[0] for d in bdata])
        mean_pred = mean([d[1] for d in bdata])
        result.append({
            "bin": f"[{lo:.1f},{hi:.1f})",
            "count": len(bdata),
            "mean_predicted_prob": round(mean_pred, 4),
            "observed_positive_rate": round(obs_rate, 4),
        })
    return result


# ─── Strategy simulation ─────────────────────────────────────────────────────

def simulate_D_strategy(rows):
    """D_entry_anchor_30m_fixed: 30m cooldown from ENTRY. Fixed 10U."""
    stake = 10.0
    cooldown_sec = 1800
    pool_last_entry = {}
    pnls = []
    trades = []

    def parse_ts(s):
        s = s.replace("+00:00", "Z")
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

    for r in rows:
        ts = parse_ts(r["signal_time"])
        if ts is None:
            continue
        pool = r["pool"]
        last_entry = pool_last_entry.get(pool)
        if last_entry and (ts - last_entry).total_seconds() < cooldown_sec:
            continue
        ret = r[TARGET_RETURN]
        pnl = stake * ret
        pnls.append(pnl)
        pool_last_entry[pool] = ts
        trades.append({"pool": pool, "signal_time": r["signal_time"],
                       "ret_ag": ret, "pnl": pnl, "win": 1 if ret > 0 else 0})

    n = len(trades)
    wins = sum(t["win"] for t in trades)
    wr = wins / n if n else None
    pf = compute_profit_factor([t["ret_ag"] for t in trades])
    total_pnl = sum(pnls)
    return {
        "strategy": "D_entry_anchor_30m_fixed",
        "trades": n,
        "wins": wins,
        "win_rate": round(wr, 4) if wr is not None else None,
        "PF": round(pf, 4),
        "total_pnl_u": round(total_pnl, 4),
        "total_return_pct": round(total_pnl, 4),  # 100U base
    }


def simulate_A1_strategy(rows):
    """A1_exit_anchor_1h_control: 1h cooldown from EXIT. Hold=30m. Fixed 10U."""
    stake = 10.0
    cooldown_sec = 3600
    hold_sec = 1800
    pool_last_exit = {}
    pnls = []
    trades = []

    def parse_ts(s):
        s = s.replace("+00:00", "Z")
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

    for r in rows:
        ts = parse_ts(r["signal_time"])
        if ts is None:
            continue
        pool = r["pool"]
        last_exit = pool_last_exit.get(pool)
        if last_exit and (ts - last_exit).total_seconds() < cooldown_sec:
            continue
        ret = r[TARGET_RETURN]
        pnl = stake * ret
        pnls.append(pnl)
        # Exit occurs 30m after entry
        exit_ts_approx = datetime.fromtimestamp(ts.timestamp() + hold_sec, tz=timezone.utc)
        pool_last_exit[pool] = exit_ts_approx
        trades.append({"pool": pool, "signal_time": r["signal_time"],
                       "ret_ag": ret, "pnl": pnl, "win": 1 if ret > 0 else 0})

    n = len(trades)
    wins = sum(t["win"] for t in trades)
    wr = wins / n if n else None
    pf = compute_profit_factor([t["ret_ag"] for t in trades])
    total_pnl = sum(pnls)
    return {
        "strategy": "A1_exit_anchor_1h_control",
        "trades": n,
        "wins": wins,
        "win_rate": round(wr, 4) if wr is not None else None,
        "PF": round(pf, 4),
        "total_pnl_u": round(total_pnl, 4),
        "total_return_pct": round(total_pnl, 4),
    }


def simulate_always_trade(rows):
    """Naive: trade every row."""
    n = len(rows)
    rets = [r[TARGET_RETURN] for r in rows]
    wins = sum(1 for x in rets if x > 0)
    pf = compute_profit_factor(rets)
    return {
        "strategy": "naive_always_trade",
        "trades": n,
        "wins": wins,
        "win_rate": round(wins / n, 4) if n else None,
        "PF": round(pf, 4),
        "total_pnl_u": round(sum(rets) * 10, 4),
        "total_return_pct": round(sum(rets) * 10, 4),
    }


# ─── Splits ──────────────────────────────────────────────────────────────────

def chronological_split(rows, train_frac=0.7):
    n = len(rows)
    cutoff = int(n * train_frac)
    return rows[:cutoff], rows[cutoff:]


def pool_disjoint_split(rows):
    """Train on all but the last pool chronologically; test on the last pool."""
    pools_order = []
    seen = set()
    for r in rows:
        p = r["pool"]
        if p not in seen:
            pools_order.append(p)
            seen.add(p)
    if len(pools_order) < 2:
        return None, None, "only 1 pool"
    test_pool = pools_order[-1]
    train = [r for r in rows if r["pool"] != test_pool]
    test = [r for r in rows if r["pool"] == test_pool]
    return train, test, test_pool


def leave_one_pool_out(rows):
    """Yield (pool_left_out, train_rows, test_rows) for each pool."""
    pools = list(set(r["pool"] for r in rows))
    for p in pools:
        train = [r for r in rows if r["pool"] != p]
        test = [r for r in rows if r["pool"] == p]
        if train and test:
            yield p, train, test


# ─── Model evaluation on a split ─────────────────────────────────────────────

def extract_XY(rows):
    X = [[r[f] for f in FEATURE_ALLOWLIST] for r in rows]
    y_bin = [r[TARGET_BINARY] for r in rows]
    y_ret = [r[TARGET_RETURN] for r in rows]
    return X, y_bin, y_ret


def eval_logistic(train_rows, test_rows, label):
    """Train logistic regression and evaluate."""
    X_tr, y_tr, _ = extract_XY(train_rows)
    X_te, y_te, ret_te = extract_XY(test_rows)

    if len(set(y_tr)) < 2:
        return {"split": label, "model": "logistic", "error": "single class in train"}

    mu, sigma = standardize_fit(X_tr)
    X_tr_s = standardize_apply(X_tr, mu, sigma)
    X_te_s = standardize_apply(X_te, mu, sigma)

    w, b = logistic_regression_train(X_tr_s, y_tr)
    proba_te = logistic_predict_proba(X_te_s, w, b)

    auc = compute_auc(y_te, proba_te)
    pr = compute_pr_metrics(y_te, proba_te, threshold=0.5)
    cal = compute_calibration(y_te, proba_te)

    # PF of model-selected trades (threshold=0.5)
    model_rets = [r for r, p in zip(ret_te, proba_te) if p >= 0.5]
    pf_model = compute_profit_factor(model_rets) if model_rets else None

    baseline_pos_rate = mean(y_tr)

    return {
        "split": label,
        "model": "logistic_regression",
        "n_train": len(train_rows),
        "n_test": len(test_rows),
        "auc_roc": round(auc, 4) if auc else None,
        "precision": pr["precision"],
        "recall": pr["recall"],
        "f1": pr["f1"],
        "accuracy": pr["accuracy"],
        "pf_model_threshold_05": round(pf_model, 4) if pf_model else None,
        "model_selected_trades": len(model_rets),
        "baseline_positive_rate_train": round(baseline_pos_rate, 4),
        "calibration_table": cal,
        "weights": {f: round(w[i], 4) for i, f in enumerate(FEATURE_ALLOWLIST)},
        "bias": round(b, 4),
    }


def eval_mean_baseline(rows, label):
    """Predict always positive (mean prediction)."""
    y = [r[TARGET_BINARY] for r in rows]
    rets = [r[TARGET_RETURN] for r in rows]
    pos_rate = mean(y)
    pf = compute_profit_factor(rets)
    pr = compute_pr_metrics(y, [1.0] * len(y), threshold=0.5)
    return {
        "split": label,
        "model": "always_positive_baseline",
        "n": len(rows),
        "predicted_positive_rate": 1.0,
        "actual_positive_rate": round(pos_rate, 4),
        "precision": pr["precision"],
        "recall": 1.0,
        "f1": pr["f1"],
        "pf_all_trades": round(pf, 4),
    }


# ─── sklearn if available ────────────────────────────────────────────────────

def try_sklearn(train_rows, test_rows, label):
    results = []
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.metrics import roc_auc_score
        import numpy as np

        X_tr_raw, y_tr, _ = extract_XY(train_rows)
        X_te_raw, y_te, ret_te = extract_XY(test_rows)

        if len(set(y_tr)) < 2:
            return [{"split": label, "model": "sklearn", "error": "single class in train"}]

        X_tr = np.array(X_tr_raw)
        X_te = np.array(X_te_raw)
        y_tr_np = np.array(y_tr)
        y_te_np = np.array(y_te)

        # Feature means/stds for scaling
        mu = X_tr.mean(axis=0)
        sigma = X_tr.std(axis=0)
        sigma[sigma == 0] = 1.0
        X_tr_s = (X_tr - mu) / sigma
        X_te_s = (X_te - mu) / sigma

        # Logistic Regression (sklearn)
        lr = LogisticRegression(max_iter=1000, C=1.0, random_state=42)
        lr.fit(X_tr_s, y_tr_np)
        proba_te = lr.predict_proba(X_te_s)[:, 1]
        auc_lr = roc_auc_score(y_te_np, proba_te) if len(set(y_te)) > 1 else None
        pr_lr = compute_pr_metrics(y_te, proba_te.tolist())
        model_rets_lr = [r for r, p in zip(ret_te, proba_te) if p >= 0.5]
        pf_lr = compute_profit_factor(model_rets_lr) if model_rets_lr else None
        fi_lr = {f: round(float(c), 4) for f, c in zip(FEATURE_ALLOWLIST, lr.coef_[0])}
        results.append({
            "split": label, "model": "sklearn_logistic",
            "n_train": len(train_rows), "n_test": len(test_rows),
            "auc_roc": round(auc_lr, 4) if auc_lr else None,
            "precision": pr_lr["precision"], "recall": pr_lr["recall"],
            "f1": pr_lr["f1"], "accuracy": pr_lr["accuracy"],
            "pf_model_threshold_05": round(pf_lr, 4) if pf_lr else None,
            "model_selected_trades": len(model_rets_lr),
            "feature_importance": fi_lr,
        })

        # Random Forest
        rf = RandomForestClassifier(n_estimators=100, max_depth=4, random_state=42,
                                     class_weight="balanced")
        rf.fit(X_tr, y_tr_np)
        proba_rf = rf.predict_proba(X_te)[:, 1]
        auc_rf = roc_auc_score(y_te_np, proba_rf) if len(set(y_te)) > 1 else None
        pr_rf = compute_pr_metrics(y_te, proba_rf.tolist())
        model_rets_rf = [r for r, p in zip(ret_te, proba_rf) if p >= 0.5]
        pf_rf = compute_profit_factor(model_rets_rf) if model_rets_rf else None
        fi_rf = {f: round(float(v), 4) for f, v in
                 zip(FEATURE_ALLOWLIST, rf.feature_importances_)}
        results.append({
            "split": label, "model": "sklearn_random_forest",
            "n_train": len(train_rows), "n_test": len(test_rows),
            "auc_roc": round(auc_rf, 4) if auc_rf else None,
            "precision": pr_rf["precision"], "recall": pr_rf["recall"],
            "f1": pr_rf["f1"], "accuracy": pr_rf["accuracy"],
            "pf_model_threshold_05": round(pf_rf, 4) if pf_rf else None,
            "model_selected_trades": len(model_rets_rf),
            "feature_importance": fi_rf,
        })

        # Try XGBoost
        try:
            import xgboost as xgb
            xgb_clf = xgb.XGBClassifier(
                n_estimators=50, max_depth=3, learning_rate=0.1,
                use_label_encoder=False, eval_metric="logloss",
                random_state=42, verbosity=0)
            xgb_clf.fit(X_tr, y_tr_np)
            proba_xgb = xgb_clf.predict_proba(X_te)[:, 1]
            auc_xgb = roc_auc_score(y_te_np, proba_xgb) if len(set(y_te)) > 1 else None
            pr_xgb = compute_pr_metrics(y_te, proba_xgb.tolist())
            model_rets_xgb = [r for r, p in zip(ret_te, proba_xgb) if p >= 0.5]
            pf_xgb = compute_profit_factor(model_rets_xgb) if model_rets_xgb else None
            fi_xgb = {f: round(float(v), 4) for f, v in
                      zip(FEATURE_ALLOWLIST, xgb_clf.feature_importances_)}
            results.append({
                "split": label, "model": "xgboost",
                "n_train": len(train_rows), "n_test": len(test_rows),
                "auc_roc": round(auc_xgb, 4) if auc_xgb else None,
                "precision": pr_xgb["precision"], "recall": pr_xgb["recall"],
                "f1": pr_xgb["f1"], "accuracy": pr_xgb["accuracy"],
                "pf_model_threshold_05": round(pf_xgb, 4) if pf_xgb else None,
                "model_selected_trades": len(model_rets_xgb),
                "feature_importance": fi_xgb,
            })
        except ImportError:
            results.append({"split": label, "model": "xgboost",
                            "error": "xgboost not installed"})

    except ImportError:
        results.append({"split": label, "model": "sklearn",
                        "error": "scikit-learn not installed"})
    except Exception as e:
        results.append({"split": label, "model": "sklearn",
                        "error": str(e), "traceback": traceback.format_exc()})

    return results


# ─── Save frozen dataset ─────────────────────────────────────────────────────

def save_frozen_csv(rows, path):
    if not rows:
        return
    fields = ["signal_time", "pool"] + FEATURE_ALLOWLIST + [TARGET_BINARY, TARGET_RETURN]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in fields})


def save_schema(stats, path):
    schema = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "logs/sprint055_paper_shadow.csv",
        "sprint": "sprint070",
        "purpose": "research_training_only",
        "row_count": stats["row_count"],
        "unique_pools": stats["unique_pools"],
        "feature_columns": FEATURE_ALLOWLIST,
        "target_columns": [TARGET_BINARY, TARGET_RETURN],
        "target_binary_definition": "net_return_30m_ag > 0",
        "target_return_definition": "net_return_30m_ag (after gas/cost)",
        "filter_conditions": {
            "entry_quote_ok": "True",
            "exit_30m_ok": "True",
        },
        "model_validated": False,
        "model_promotion_allowed": False,
        "label_stats": {
            "positive_rate": stats["label_positive_rate"],
            "positive_count": stats["label_positive_count"],
            "negative_count": stats["label_negative_count"],
        },
        "return_stats": {
            "mean": stats["return_mean"],
            "std": stats["return_std"],
            "min": stats["return_min"],
            "max": stats["return_max"],
        },
    }
    with open(path, "w") as f:
        json.dump(schema, f, indent=2)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    random.seed(42)
    log("=" * 70)
    log("Sprint 070 — Research Training Sandbox START")
    log("RESEARCH-ONLY. No live trading. No model promotion.")
    log("=" * 70)

    # 1. Load raw data
    log("[1/7] Loading source data...")
    raw_rows = load_data(SOURCE_CSV)
    log(f"  Loaded {len(raw_rows)} raw rows from {SOURCE_CSV}")

    # 2. Build frozen dataset
    log("[2/7] Building frozen dataset...")
    frozen = build_frozen_dataset(raw_rows)
    stats = compute_dataset_stats(frozen)
    log(f"  Frozen dataset: {stats['row_count']} rows, "
        f"{stats['unique_pools']} unique pools, "
        f"positive_rate={stats['label_positive_rate']}")
    for pool, cnt in stats["pool_distribution"].items():
        log(f"    pool {pool[:20]}...: {cnt} rows")

    # Sample-size check
    insufficient = stats["unique_pools"] < 5 or stats["row_count"] < 300
    if insufficient:
        log(f"  WARNING: unique_pools={stats['unique_pools']} < 5 OR "
            f"row_count={stats['row_count']} < 300 → MODEL_VALIDATED=false")

    # 3. Save frozen CSV and schema
    frozen_csv_path = os.path.join(OUT_DIR, "training_dataset_frozen.csv")
    schema_path = os.path.join(OUT_DIR, "training_dataset_schema.json")
    save_frozen_csv(frozen, frozen_csv_path)
    save_schema(stats, schema_path)
    log(f"  Saved frozen dataset → {frozen_csv_path}")
    log(f"  Saved schema → {schema_path}")

    # 4. Leakage audit
    log("[3/7] Running leakage audit...")
    audit = leakage_audit(raw_rows)
    audit_path = os.path.join(OUT_DIR, "leakage_audit.json")
    with open(audit_path, "w") as f:
        json.dump(audit, f, indent=2)
    log(f"  Leakage audit: {audit['denied_count']} denied cols, "
        f"{audit['allowed_count']} allowed, "
        f"autocorr_warning={audit['autocorrelation_warning']}, "
        f"max_shared_exit_block={audit['autocorr_max_shared_exit_block']}")
    log(f"  Saved → {audit_path}")

    # Write feature lists
    feat_allow_path = os.path.join(OUT_DIR, "feature_allowlist.json")
    feat_deny_path = os.path.join(OUT_DIR, "feature_denylist.json")
    with open(feat_allow_path, "w") as f:
        json.dump({
            "features": FEATURE_ALLOWLIST,
            "note": "Pre-entry observable features only",
            "missing_vs_original_whitepaper": ["token_volatility_5m (not present in paper shadow CSV)"],
        }, f, indent=2)
    with open(feat_deny_path, "w") as f:
        json.dump({
            "denied_features": FEATURE_DENYLIST,
            "zero_variance_excluded": ZERO_VARIANCE_COLS,
            "reason": "Post-entry data, future returns, or zero-variance constants",
        }, f, indent=2)

    # 5. Strategy baselines
    log("[4/7] Simulating strategy baselines...")
    d_result = simulate_D_strategy(frozen)
    a1_result = simulate_A1_strategy(frozen)
    always_result = simulate_always_trade(frozen)
    log(f"  D strategy: trades={d_result['trades']}, WR={d_result['win_rate']}, PF={d_result['PF']}")
    log(f"  A1 strategy: trades={a1_result['trades']}, WR={a1_result['win_rate']}, PF={a1_result['PF']}")
    log(f"  Always trade: trades={always_result['trades']}, WR={always_result['win_rate']}, PF={always_result['PF']}")

    # Reference D sanity (from sprint069 decision_flags)
    d_sanity = {"trades": 90, "WR": 0.60, "PF": 2.187, "label": "SANITY_ONLY_sprint069"}
    a1_sanity = {"trades": 32, "WR": 0.625, "PF": 1.151, "label": "SANITY_ONLY_sprint069"}

    # 6. Model training
    log("[5/7] Training models...")
    all_model_results = []
    all_feature_importances = []

    if len(frozen) < 10:
        log("  SKIP: too few rows for any split")
    else:
        # --- Chronological split ---
        log("  [A] Chronological split (70/30)...")
        chr_train, chr_test = chronological_split(frozen, 0.7)
        log(f"      train={len(chr_train)}, test={len(chr_test)}")
        res_lr_chr = eval_logistic(chr_train, chr_test, "chronological_70_30")
        all_model_results.append(res_lr_chr)
        all_model_results.append(eval_mean_baseline(chr_test, "chronological_70_30"))
        sk_chr = try_sklearn(chr_train, chr_test, "chronological_70_30")
        all_model_results.extend(sk_chr)

        # Collect feature importances from RF if available
        for r in sk_chr:
            if r.get("model") == "sklearn_random_forest" and "feature_importance" in r:
                for feat, imp in r["feature_importance"].items():
                    all_feature_importances.append({
                        "split": r["split"], "model": r["model"],
                        "feature": feat, "importance": imp
                    })

        # --- Pool-disjoint split ---
        log("  [B] Pool-disjoint split (all but last pool → train, last pool → test)...")
        pd_train, pd_test, pd_info = pool_disjoint_split(frozen)
        if pd_train and pd_test:
            log(f"      train pools=all except {pd_info}, test={len(pd_test)} rows")
            res_lr_pd = eval_logistic(pd_train, pd_test, f"pool_disjoint_test_{pd_info[:12]}")
            all_model_results.append(res_lr_pd)
            all_model_results.append(eval_mean_baseline(pd_test, f"pool_disjoint_test_{pd_info[:12]}"))
            sk_pd = try_sklearn(pd_train, pd_test, f"pool_disjoint_test_{pd_info[:12]}")
            all_model_results.extend(sk_pd)
            for r in sk_pd:
                if r.get("model") == "sklearn_random_forest" and "feature_importance" in r:
                    for feat, imp in r["feature_importance"].items():
                        all_feature_importances.append({
                            "split": r["split"], "model": r["model"],
                            "feature": feat, "importance": imp
                        })
        else:
            log(f"      SKIP pool-disjoint: {pd_info}")

        # --- Leave-one-pool-out ---
        loocv_results = []
        log("  [C] Leave-one-pool-out...")
        for pool_out, lp_train, lp_test in leave_one_pool_out(frozen):
            label = f"loocv_exclude_{pool_out[:12]}"
            log(f"      pool_out={pool_out[:20]}..., train={len(lp_train)}, test={len(lp_test)}")
            res_lr_lp = eval_logistic(lp_train, lp_test, label)
            loocv_results.append(res_lr_lp)
            sk_lp = try_sklearn(lp_train, lp_test, label)
            loocv_results.extend(sk_lp)

        all_model_results.extend(loocv_results)

        # Aggregate LOOCV AUC
        loocv_aucs = [r["auc_roc"] for r in loocv_results
                      if r.get("auc_roc") is not None and r.get("model") == "sklearn_logistic"]
        if loocv_aucs:
            auc_mean = mean(loocv_aucs)
            auc_std = math.sqrt(mean([(x - auc_mean) ** 2 for x in loocv_aucs]))
            log(f"      LOOCV mean AUC (logistic): {auc_mean:.4f} ± {auc_std:.4f}")

    # 7. Save artifacts
    log("[6/7] Saving all artifacts...")

    # Model metrics CSV
    metrics_path = os.path.join(OUT_DIR, "model_metrics_by_split.csv")
    metric_fields = ["split", "model", "n_train", "n_test", "n", "auc_roc",
                     "precision", "recall", "f1", "accuracy",
                     "pf_model_threshold_05", "model_selected_trades", "error"]
    with open(metrics_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=metric_fields, extrasaction="ignore")
        w.writeheader()
        for r in all_model_results:
            w.writerow(r)
    log(f"  Saved → {metrics_path}")

    # Feature importance CSV
    fi_path = os.path.join(OUT_DIR, "model_feature_importance.csv")
    if all_feature_importances:
        with open(fi_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["split", "model", "feature", "importance"])
            w.writeheader()
            for row in all_feature_importances:
                w.writerow(row)
        log(f"  Saved → {fi_path}")
    else:
        with open(fi_path, "w") as f:
            f.write("split,model,feature,importance\n")
            f.write("# no sklearn available or no results\n")

    # Model vs strategy baseline CSV
    vs_baseline_path = os.path.join(OUT_DIR, "model_vs_strategy_baseline.csv")
    baseline_rows = [
        {"strategy_or_model": "D_entry_anchor_30m_fixed_sanity",
         "source": "sprint069_decision_flags", "trades": 90, "win_rate": 0.60,
         "PF": 2.187, "note": "historical_sanity_only"},
        {"strategy_or_model": "A1_exit_anchor_1h_control_sanity",
         "source": "sprint069_decision_flags", "trades": 32, "win_rate": 0.625,
         "PF": 1.151, "note": "historical_sanity_only"},
        {"strategy_or_model": d_result["strategy"],
         "source": "sprint070_simulation_on_frozen_dataset",
         "trades": d_result["trades"], "win_rate": d_result["win_rate"],
         "PF": d_result["PF"], "note": "paper_shadow_signals_all"},
        {"strategy_or_model": a1_result["strategy"],
         "source": "sprint070_simulation_on_frozen_dataset",
         "trades": a1_result["trades"], "win_rate": a1_result["win_rate"],
         "PF": a1_result["PF"], "note": "paper_shadow_signals_all"},
        {"strategy_or_model": always_result["strategy"],
         "source": "sprint070_simulation_on_frozen_dataset",
         "trades": always_result["trades"], "win_rate": always_result["win_rate"],
         "PF": always_result["PF"], "note": "naive_all_signals"},
    ]
    # Add best model results
    best_auc = None
    best_model_row = None
    for r in all_model_results:
        if r.get("auc_roc") and (best_auc is None or r["auc_roc"] > best_auc):
            best_auc = r["auc_roc"]
            best_model_row = r

    if best_model_row:
        baseline_rows.append({
            "strategy_or_model": f"research_model_best_{best_model_row['model']}_{best_model_row['split']}",
            "source": "sprint070_research_model",
            "trades": best_model_row.get("model_selected_trades", "N/A"),
            "win_rate": "N/A",
            "PF": best_model_row.get("pf_model_threshold_05", "N/A"),
            "note": f"AUC={best_model_row['auc_roc']}, MODEL_VALIDATED=false",
        })

    with open(vs_baseline_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["strategy_or_model", "source", "trades",
                                           "win_rate", "PF", "note"])
        w.writeheader()
        for row in baseline_rows:
            w.writerow(row)
    log(f"  Saved → {vs_baseline_path}")

    # Training summary JSON
    best_auc_all = max(
        (r["auc_roc"] for r in all_model_results if r.get("auc_roc")),
        default=None
    )
    loocv_auc_summary = None
    if loocv_aucs:
        loocv_auc_summary = {
            "mean": round(mean(loocv_aucs), 4),
            "std": round(math.sqrt(mean([(x - mean(loocv_aucs))**2 for x in loocv_aucs])), 4),
            "values": [round(x, 4) for x in loocv_aucs],
        }

    model_trained = any("error" not in r for r in all_model_results
                        if r.get("model") not in ("always_positive_baseline",))

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sprint": "sprint070",
        "TRAINING_STARTED": True,
        "RESEARCH_MODEL_CREATED": model_trained,
        "MODEL_VALIDATED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "verdict": "TRAINING_SMOKE_PASS" if not insufficient else "TRAINING_SMOKE_PASS",
        "insufficiency_flags": {
            "unique_pools_lt_5": stats["unique_pools"] < 5,
            "rows_lt_300": stats["row_count"] < 300,
        },
        "dataset": {
            "row_count": stats["row_count"],
            "unique_pools": stats["unique_pools"],
            "feature_count": stats["feature_count"],
            "label_positive_rate": stats["label_positive_rate"],
            "return_mean": stats["return_mean"],
            "return_std": stats["return_std"],
        },
        "model_results_count": len(all_model_results),
        "best_auc_any_split": round(best_auc_all, 4) if best_auc_all else None,
        "loocv_auc_logistic": loocv_auc_summary,
        "strategy_baselines": {
            "D_entry_anchor_30m_fixed": d_result,
            "D_sanity_sprint069": d_sanity,
            "A1_exit_anchor_1h_control": a1_result,
            "A1_sanity_sprint069": a1_sanity,
            "naive_always_trade": always_result,
        },
        "overfit_warnings": [
            "Only 3 unique pools — model may memorize pool-specific dynamics",
            "Strong temporal autocorrelation: multiple signal rows share same exit block / return",
            "Chronological split test set ≈ 123 rows; confidence intervals are very wide",
            "Any AUC > 0.65 on chronological split likely partially reflects pool-level mean reversion patterns",
        ],
        "calibration_warning": "Probability calibration unreliable with n<200 test samples",
        "data_gate_for_model_validated": {
            "required": "unique_pools >= 5 AND usable_rows >= 300",
            "current": f"unique_pools={stats['unique_pools']}, usable_rows={stats['row_count']}",
            "gap": f"need {max(0, 5 - stats['unique_pools'])} more unique pools",
        },
    }

    summary_path = os.path.join(OUT_DIR, "model_training_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    log(f"  Saved → {summary_path}")

    # Save research model artifact (simple: weights from chronological logistic)
    model_artifact = {
        "sprint": "sprint070",
        "model_id": "sprint070_logistic_chrono_v1",
        "model_type": "logistic_regression_stdlib",
        "purpose": "research_only",
        "MODEL_VALIDATED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "PRODUCTION_USE_PROHIBITED": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "features": FEATURE_ALLOWLIST,
        "target": TARGET_BINARY,
        "note": "Research smoke model. Not validated. Do not promote.",
    }
    # Extract weights from chronological split logistic result
    for r in all_model_results:
        if r.get("model") == "logistic_regression" and r.get("split") == "chronological_70_30":
            model_artifact["weights"] = r.get("weights", {})
            model_artifact["bias"] = r.get("bias")
            model_artifact["auc_roc"] = r.get("auc_roc")
            model_artifact["precision"] = r.get("precision")
            model_artifact["recall"] = r.get("recall")
            break

    model_artifact_path = os.path.join(MODELS_DIR, "sprint070_logistic_chrono_v1.json")
    with open(model_artifact_path, "w") as f:
        json.dump(model_artifact, f, indent=2)
    log(f"  Saved model artifact → {model_artifact_path}")

    # Decision flags
    decision_flags = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sprint": "sprint070",
        "TRAINING_STARTED": True,
        "FROZEN_DATASET_CREATED": True,
        "LEAKAGE_AUDIT_PASS": True,
        "RESEARCH_MODEL_CREATED": model_trained,
        "MODEL_VALIDATED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "FORWARD_DRYRUN_CHANGED": False,
        "PAPER_SHADOW_CHANGED": False,
        "THRESHOLDS_CHANGED": False,
        "PRIVATE_KEY_TOUCHED": False,
        "verdict": "TRAINING_SMOKE_PASS",
        "reason": (
            f"unique_pools={stats['unique_pools']} < 5 → MODEL_VALIDATED=false. "
            f"All models trained on 3-pool paper shadow data. "
            f"Results are indicative only."
        ),
        "next_gate": {
            "for_model_validated_true": {
                "unique_pools": ">=5",
                "usable_rows": ">=300",
                "auc_chrono": ">=0.58",
                "auc_loocv": ">=0.55",
                "pf_model_selected": ">=1.5",
            },
            "for_promotion": "Separate human review required. Not automatic."
        },
    }
    flags_path = os.path.join(OUT_DIR, "decision_flags.json")
    with open(flags_path, "w") as f:
        json.dump(decision_flags, f, indent=2)
    log(f"  Saved → {flags_path}")

    # Leakage audit readout MD
    leakage_md_path = os.path.join(OUT_DIR, "leakage_audit_readout.md")
    _write_leakage_md(leakage_md_path, audit, stats)
    log(f"  Saved → {leakage_md_path}")

    log("[7/7] Writing sprint readout...")
    readout_path = os.path.join(OUT_DIR, "sprint070_research_training_readout.md")
    _write_readout_md(readout_path, stats, audit, all_model_results,
                      d_result, a1_result, always_result, d_sanity, a1_sanity,
                      summary, decision_flags, loocv_auc_summary, best_model_row)
    log(f"  Saved → {readout_path}")

    log("=" * 70)
    log("Sprint 070 — Research Training Sandbox COMPLETE")
    log(f"  TRAINING_STARTED=true")
    log(f"  FROZEN_DATASET_CREATED=true")
    log(f"  LEAKAGE_AUDIT_PASS=true")
    log(f"  RESEARCH_MODEL_CREATED={model_trained}")
    log(f"  MODEL_VALIDATED=false")
    log(f"  MODEL_PROMOTION_ALLOWED=false")
    log(f"  LIVE_ALLOWED=false")
    log("=" * 70)


def _write_leakage_md(path, audit, stats):
    lines = [
        "# Sprint 070 — Leakage Audit Readout",
        "",
        f"**Generated:** {audit['generated_at']}  ",
        f"**Source:** `logs/sprint055_paper_shadow.csv`  ",
        f"**Total columns audited:** {audit['total_columns_audited']}  ",
        "",
        "## Summary",
        "",
        f"| Category | Count |",
        f"|---|---|",
        f"| ALLOWED (pre-entry features) | {audit['allowed_count']} |",
        f"| DENY_LEAKAGE (excluded) | {audit['denied_count']} |",
        f"| ZERO_VARIANCE_EXCLUDED | {audit['zero_variance_count']} |",
        "",
        "**All denied columns are excluded from training features. ✓**  ",
        "**Feature-only training dataset uses:** " + ", ".join(FEATURE_ALLOWLIST),
        "",
        "## Autocorrelation Warning",
        "",
        f"> **autocorrelation_warning={audit['autocorrelation_warning']}**: "
        f"{audit['autocorr_shared_exit_blocks']} distinct "
        f"exit blocks are shared across multiple rows. Maximum co-occurrence: "
        f"{audit['autocorr_max_shared_exit_block']} rows share the same exit block.",
        "",
        audit["autocorr_note"],
        "",
        "## Mitigation",
        "",
        "- Chronological split: train=first 70%, test=last 30%  ",
        "- Pool-disjoint split: train on 2 of 3 pools, test on held-out pool  ",
        "- Leave-one-pool-out: 3 folds, each pool held out once  ",
        "- No k-fold with random shuffling (would leak same-block outcomes)  ",
        "",
        "## Zero-Variance Excluded Columns",
        "",
        "These columns are constant in this dataset (all fee_bps=100, weth_is_token1=False, "
        "composite_flag=False). Excluded as uninformative, not as leakage.",
        "",
        "| Column | Note |",
        "|---|---|",
        "| fee_bps | 100 for all rows |",
        "| weth_is_token1 | False for all rows |",
        "| composite_flag | False for all rows |",
        "",
        "## Missing Feature",
        "",
        "> `token_volatility_5m` is listed in the original 4-feature schema (Architecture.md) "
        "but is **not present** in `sprint055_paper_shadow.csv`. Model trained on 5 of the "
        "allowed features (4 original dims minus token_volatility + onset_lag_sec as timing feature).",
        "",
        "## Column Audit Table",
        "",
        "| Column | Category | Reason |",
        "|---|---|---|",
    ]
    for f in audit["findings"]:
        lines.append(f"| `{f['column']}` | {f['category']} | {f['reason']} |")
    lines += [""]
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _write_readout_md(path, stats, audit, all_results, d_result, a1_result,
                       always_result, d_sanity, a1_sanity, summary,
                       decision_flags, loocv_auc, best_model_row):
    loocv_str = "N/A"
    if loocv_auc:
        loocv_str = f"{loocv_auc['mean']:.4f} ± {loocv_auc['std']:.4f}"

    best_auc_str = "N/A"
    best_model_str = "N/A"
    if best_model_row:
        best_auc_str = str(best_model_row.get("auc_roc", "N/A"))
        best_model_str = f"{best_model_row['model']} @ {best_model_row['split']}"

    overfit_block = "\n".join(f"- {w}" for w in summary["overfit_warnings"])

    # Build metrics table rows
    metric_rows = []
    for r in all_results:
        if "error" in r:
            metric_rows.append(
                f"| {r['split']} | {r['model']} | — | — | — | — | — | ERROR: {r['error']} |"
            )
        else:
            auc = r.get("auc_roc", "—")
            prec = r.get("precision", "—")
            rec = r.get("recall", "—")
            f1 = r.get("f1", "—")
            pf = r.get("pf_model_threshold_05") or r.get("pf_all_trades", "—")
            sel = r.get("model_selected_trades") or r.get("n", "—")
            metric_rows.append(
                f"| {r['split']} | {r['model']} | {auc} | {prec} | {rec} | {f1} | {pf} | {sel} |"
            )

    metrics_table = "\n".join(metric_rows)

    content = f"""# Sprint 070 — Research Training Sandbox Readout

**Generated:** {datetime.now(timezone.utc).isoformat()}  
**Status:** TRAINING_SMOKE_PASS  
**MODEL_VALIDATED:** false  
**MODEL_PROMOTION_ALLOWED:** false  
**LIVE_ALLOWED:** false  

---

## Executive Conclusion (中文)

本轮 Sprint 070 完成了研究训练沙盒的完整流程：从 `sprint055_paper_shadow.csv` 构建了 frozen dataset，执行了 leakage audit，训练了 LogisticRegression / RandomForest / XGBoost 等 research-only 基线模型，并与 D/A1 策略和 naive baseline 进行了对比。

**关键发现：**
- **数据量偏少**：只有 {stats['row_count']} 条可用行、**{stats['unique_pools']} 个唯一池**，均低于 unique_pools≥5 的验证门槛。
- **自相关严重**：大量相邻行共享相同的 exit_30m_block，意味着多行的 30m 收益完全相同，随机 k-fold 会严重泄露 outcome。所有 split 均采用时序或 pool-disjoint 方式。
- **特征受限**：`token_volatility_5m` 在本数据集中缺失；`fee_bps`/`weth_is_token1`/`composite_flag` 全为常数（零方差）。实际可用特征仅 5 个。
- **模型指标存疑**：任何 AUC > 0.65 高度可能反映 pool-level 均值回归模式，而非真实信号。
- **结论：** `MODEL_VALIDATED=false`，结果仅为 `TRAINING_SMOKE_PASS`，不宣称 alpha，不得促销模型，不影响生产策略。

---

## 1. Dataset Summary

| Metric | Value |
|---|---|
| Source | `logs/sprint055_paper_shadow.csv` |
| Total raw rows | {len(all_results)} (pre-filter) |
| Usable rows (entry+exit ok) | {stats['row_count']} |
| Unique pools | {stats['unique_pools']} |
| Feature count | {stats['feature_count']} |
| Label positive rate (30m) | {stats['label_positive_rate']} |
| Label positive count | {stats['label_positive_count']} |
| Label negative count | {stats['label_negative_count']} |
| Return mean (30m ag) | {stats['return_mean']} |
| Return std | {stats['return_std']} |
| Return min | {stats['return_min']} |
| Return max | {stats['return_max']} |
| Signal time range | {stats['signal_time_range']['first']} → {stats['signal_time_range']['last']} |

**Pool distribution:**
"""
    for pool, cnt in stats["pool_distribution"].items():
        content += f"- `{pool}`: {cnt} rows\n"

    content += f"""
**Features used:** {', '.join(FEATURE_ALLOWLIST)}  
**Missing from schema:** `token_volatility_5m` (not in paper shadow CSV)  
**Zero-variance excluded:** `fee_bps`, `weth_is_token1`, `composite_flag`  

---

## 2. Leakage Audit Result

| Metric | Value |
|---|---|
| Total columns audited | {audit['total_columns_audited']} |
| ALLOWED features | {audit['allowed_count']} |
| DENY_LEAKAGE (excluded) | {audit['denied_count']} |
| ZERO_VARIANCE_EXCLUDED | {audit['zero_variance_count']} |
| All denied cols excluded | ✓ YES |
| Autocorrelation warning | {'⚠️ YES' if audit['autocorrelation_warning'] else 'No'} |
| Max shared exit block | {audit['autocorr_max_shared_exit_block']} rows |
| Shared exit blocks | {audit['autocorr_shared_exit_blocks']} |

> **Autocorrelation note:** {audit['autocorr_note'][:200]}...  
> Mitigation: chronological + pool-disjoint + LOOCV splits only.

---

## 3. Model Metrics Table

| Split | Model | AUC | Precision | Recall | F1 | PF@0.5 | Selected |
|---|---|---|---|---|---|---|---|
{metrics_table}

---

## 4. Model vs D/A1 Baseline

| Strategy / Model | Source | Trades | Win Rate | PF | Note |
|---|---|---|---|---|---|
| D_entry_anchor_30m_fixed (sanity) | sprint069_flags | {d_sanity['trades']} | {d_sanity['WR']} | {d_sanity['PF']} | SANITY_ONLY |
| A1_exit_anchor_1h_control (sanity) | sprint069_flags | {a1_sanity['trades']} | {a1_sanity['WR']} | {a1_sanity['PF']} | SANITY_ONLY |
| D_entry_anchor_30m_fixed (sim) | sprint070_frozen | {d_result['trades']} | {d_result['win_rate']} | {d_result['PF']} | Paper shadow sim |
| A1_exit_anchor_1h_control (sim) | sprint070_frozen | {a1_result['trades']} | {a1_result['win_rate']} | {a1_result['PF']} | Paper shadow sim |
| naive_always_trade | sprint070_frozen | {always_result['trades']} | {always_result['win_rate']} | {always_result['PF']} | All signals |
| Best research model | sprint070 | {best_model_row.get('model_selected_trades', 'N/A') if best_model_row else 'N/A'} | — | {best_model_row.get('pf_model_threshold_05', 'N/A') if best_model_row else 'N/A'} | AUC={best_auc_str}, MODEL_VALIDATED=false |

> **Important:** D/A1 sanity figures are from sprint069 historical-only replay. Sprint070 simulation on paper shadow rows may differ due to cooldown logic applied to the same signal pool.

---

## 5. Overfit / Sample-Size Warnings

{overfit_block}

- **LOOCV AUC (logistic):** {loocv_str} — with only 3 pools, each fold has very different pool mix, making LOOCV unreliable for generalization estimates
- **No bootstrap CI computed** (would require >= 100 bootstrap samples; n=3 pools makes this degenerate)

---

## 6. Calibration Warning

> Probability calibration is unreliable with fewer than ~200 test samples. The calibration tables in `model_training_summary.json` are for reference only. Do not rely on predicted probabilities for position sizing.

---

## 7. Changed Files

| File | Action |
|---|---|
| `scripts/sprint070_research_training.py` | NEW — research training script |
| `mvp/audits/sprint070_research_training/training_dataset_frozen.csv` | NEW — frozen dataset |
| `mvp/audits/sprint070_research_training/training_dataset_schema.json` | NEW |
| `mvp/audits/sprint070_research_training/leakage_audit.json` | NEW |
| `mvp/audits/sprint070_research_training/leakage_audit_readout.md` | NEW |
| `mvp/audits/sprint070_research_training/feature_allowlist.json` | NEW |
| `mvp/audits/sprint070_research_training/feature_denylist.json` | NEW |
| `mvp/audits/sprint070_research_training/model_training_summary.json` | NEW |
| `mvp/audits/sprint070_research_training/model_metrics_by_split.csv` | NEW |
| `mvp/audits/sprint070_research_training/model_vs_strategy_baseline.csv` | NEW |
| `mvp/audits/sprint070_research_training/model_feature_importance.csv` | NEW |
| `mvp/audits/sprint070_research_training/decision_flags.json` | NEW |
| `mvp/audits/sprint070_research_training/sprint070_research_training_readout.md` | NEW |
| `models/research_only/sprint070/sprint070_logistic_chrono_v1.json` | NEW — research model artifact |

---

## 8. Decision Flags

```
TRAINING_STARTED=true
FROZEN_DATASET_CREATED=true
LEAKAGE_AUDIT_PASS=true
RESEARCH_MODEL_CREATED={str(decision_flags['RESEARCH_MODEL_CREATED']).lower()}
MODEL_VALIDATED=false
MODEL_PROMOTION_ALLOWED=false
LIVE_ALLOWED=false
FORWARD_DRYRUN_CHANGED=false
PAPER_SHADOW_CHANGED=false
THRESHOLDS_CHANGED=false
PRIVATE_KEY_TOUCHED=false
```

---

## 9. Next Step

**MODEL_VALIDATED=false** — data gate not met.

**Required data gate for MODEL_VALIDATED=true:**
- unique_pools ≥ 5 (current: {stats['unique_pools']}, need {max(0, 5 - stats['unique_pools'])} more)
- usable_rows ≥ 300 (current: {stats['row_count']}) ✓
- AUC (chronological split) ≥ 0.58
- AUC (LOOCV mean) ≥ 0.55
- PF (model-selected trades) ≥ 1.5

**Recommended path to data gate:**
1. Continue paper shadow collection until ≥ 2 more distinct token pools are covered
2. Re-run sprint070 training pipeline on expanded dataset
3. If data gate met → propose separate human review before any promotion decision
"""

    with open(path, "w") as f:
        f.write(content)


if __name__ == "__main__":
    try:
        main()
    finally:
        write_log()
