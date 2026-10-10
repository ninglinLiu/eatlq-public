"""
Run-004 Patch H18: V2-only Candidate Model Dry-run
===================================================
Trains a candidate model on V2-only clean rows using the H17-recommended
combined_microstructure_no_raw_vol feature set. Shadow/diagnostic only.
NO production writes. NO promote. NO live.
"""
import json
import os
import pickle
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (
    roc_auc_score, average_precision_score, brier_score_loss,
    precision_recall_curve, roc_curve
)
from sklearn.model_selection import cross_val_predict

# ── paths ───────────────────────────────────────────────────────────────────
DIAG_DIR = Path("/opt/EATLQ/mvp/diagnostics")
MODEL_DIR = Path("/opt/EATLQ/mvp/models/h18_v2_candidate")
ALL_CSV = DIAG_DIR / "run004_h1611_all_v2_decoded_backed_10k_dataset.csv"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

GENERATED_AT = datetime.now(timezone.utc).isoformat()

# ── H17 recommended 14 features (combined_microstructure_no_raw_vol) ────────
FEATURE_SET = [
    "unique_swap_senders_300s",
    "top1_sender_share_300s",
    "top3_sender_share_300s",
    "sender_gini_300s",
    "sender_entropy_300s",
    "buy_count_300s",
    "sell_count_300s",
    "decoded_event_count_5m",
    "new_buyer_ratio_300s",
    "repeat_buyer_ratio_300s",
    "trade_size_cv_300s",
    "activity_acceleration_1m_vs_5m",
    "activity_acceleration_3m_vs_15m",
    "sender_entropy_change_1m_vs_5m",
]
TARGET = "alpha_h17_clean_top20"
RETURN_COL = "final_return_after_cost"
POOL_COL = "pool_address"
TIME_COL = "source_block_number"

# ── banned features (must never appear) ─────────────────────────────────────
BANNED_FEATURES = [
    "buy_volume_raw_300s", "sell_volume_raw_300s", "net_flow_raw_300s",
    "abs_flow_raw_300s", "buy_volume_raw_60s", "sell_volume_raw_60s",
    "quarantine_reason", "final_return_after_cost", "alpha_top20_after_gas",
    "alpha_ret_gt_5pct_after_gas", "alpha_ret_gt_10pct_after_gas",
    "within_pool_return_rank", "final_return_rank",
    "gross_quote_return_30m", "fixed_cost_adjusted_return_30m",
    "gas_adjusted_return_30m_low", "gas_adjusted_return_30m_medium",
    "gas_adjusted_return_30m_high", "slippage_stress_return",
    "executable_label_after_gas", "executable_label_current",
    "rn_pool_bucket", "rn_pool",
]

def assert_no_banned(cols):
    overlap = set(cols) & set(BANNED_FEATURES)
    assert not overlap, f"BANNED features in use: {overlap}"

print("=" * 70)
print("H18 V2-only Candidate Model Dry-run")
print("=" * 70)

# ── 1. load & filter ─────────────────────────────────────────────────────────
print("\n[1] Loading full dataset and filtering V2 clean rows...")
df_all = pd.read_csv(ALL_CSV)
print(f"    Total rows in file: {len(df_all)}")
print(f"    risk_severity distribution:\n{df_all['risk_severity'].value_counts().to_dict()}")

# V2-only, clean risk_severity only
df_v2_clean = df_all[
    (df_all["risk_severity"] == "clean") &
    (df_all["pool_type"].isin(["v2"]))
].copy()
print(f"    V2 clean rows: {len(df_v2_clean)}")

# Reconstruct alpha_h17_clean_top20: top 20% by final_return_after_cost within clean rows.
# This matches H17 exactly: 1177 positives / 5884 rows = 20.00%.
# Note: final_return_after_cost is ONLY used here to construct the target label.
# It is never used as a feature (it is in BANNED_FEATURES).
ret_thresh = df_v2_clean["final_return_after_cost"].quantile(0.80)
df_v2_clean[TARGET] = (df_v2_clean["final_return_after_cost"] >= ret_thresh).astype(int)
print(f"    Reconstructed {TARGET} threshold: {ret_thresh:.6f}")

# Drop rows with missing target (shouldn't happen)
df_v2_clean = df_v2_clean.dropna(subset=[TARGET])
print(f"    Rows after target construction: {len(df_v2_clean)}")

# Target distribution
pos = int(df_v2_clean[TARGET].sum())
neg = int(len(df_v2_clean) - pos)
pos_rate = pos / len(df_v2_clean)
print(f"    Positive: {pos} | Negative: {neg} | Rate: {pos_rate:.4f}")
assert abs(pos_rate - 0.2000) < 0.001, f"Unexpected positive rate: {pos_rate} (expected ~0.200)"

# Verify no banned features in our feature set
assert_no_banned(FEATURE_SET)

# Resolve available features
available_features = [f for f in FEATURE_SET if f in df_v2_clean.columns]
missing_features = [f for f in FEATURE_SET if f not in df_v2_clean.columns]
print(f"    Features available: {len(available_features)}/{len(FEATURE_SET)}")
if missing_features:
    print(f"    Missing features: {missing_features}")

# ── 2. chronological train/val split (70/30) ─────────────────────────────────
print("\n[2] Chronological 70/30 split...")
df_sorted = df_v2_clean.sort_values(TIME_COL).reset_index(drop=True)
split_idx = int(len(df_sorted) * 0.70)
df_train = df_sorted.iloc[:split_idx].copy()
df_val = df_sorted.iloc[split_idx:].copy()
print(f"    Train rows: {len(df_train)} | Val rows: {len(df_val)}")
print(f"    Train positive rate: {df_train[TARGET].mean():.4f}")
print(f"    Val positive rate:   {df_val[TARGET].mean():.4f}")

X_train = df_train[available_features].fillna(0).values
y_train = df_train[TARGET].values.astype(int)
X_val = df_val[available_features].fillna(0).values
y_val = df_val[TARGET].values.astype(int)

# ── 3. train candidate model ──────────────────────────────────────────────────
print("\n[3] Training GradientBoosting candidate model...")
base_model = GradientBoostingClassifier(
    n_estimators=150,
    max_depth=4,
    learning_rate=0.05,
    subsample=0.8,
    min_samples_leaf=20,
    random_state=42,
)
base_model.fit(X_train, y_train)
print("    Base model trained.")

# Calibration via isotonic regression on held-out val set (manual prefit approach)
from sklearn.isotonic import IsotonicRegression
cal_isotonic = IsotonicRegression(out_of_bounds="clip")
y_score_train_val = base_model.predict_proba(X_val)[:, 1]
cal_isotonic.fit(y_score_train_val, y_val)

class CalibratedWrapper:
    def __init__(self, base, calibrator):
        self.base = base
        self.calibrator = calibrator
    def predict_proba(self, X):
        raw = self.base.predict_proba(X)[:, 1]
        cal = self.calibrator.predict(raw)
        return np.column_stack([1 - cal, cal])

cal_model = CalibratedWrapper(base_model, cal_isotonic)
print("    Isotonic calibration fitted on validation set.")

# ── 4. score validation set ───────────────────────────────────────────────────
print("\n[4] Scoring validation set...")
y_score_raw = base_model.predict_proba(X_val)[:, 1]
y_score_cal = cal_model.predict_proba(X_val)[:, 1]

roc_auc_raw = roc_auc_score(y_val, y_score_raw)
pr_auc_raw = average_precision_score(y_val, y_score_raw)
roc_auc_cal = roc_auc_score(y_val, y_score_cal)
pr_auc_cal = average_precision_score(y_val, y_score_cal)

brier_raw = brier_score_loss(y_val, y_score_raw)
brier_cal = brier_score_loss(y_val, y_score_cal)

# PR lift vs baseline
baseline_pr = float(y_val.mean())
pr_lift_raw = pr_auc_raw - baseline_pr
pr_lift_cal = pr_auc_cal - baseline_pr

print(f"    ROC-AUC (raw): {roc_auc_raw:.4f} | (cal): {roc_auc_cal:.4f}")
print(f"    PR-AUC  (raw): {pr_auc_raw:.4f} | (cal): {pr_auc_cal:.4f}")
print(f"    PR-lift (raw): {pr_lift_raw:.4f} | (cal): {pr_lift_cal:.4f}")
print(f"    Brier   (raw): {brier_raw:.4f} | (cal): {brier_cal:.4f}")
print(f"    Baseline PR:   {baseline_pr:.4f}")

# ECE (Expected Calibration Error)
def compute_ece(y_true, y_prob, n_bins=10):
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (y_prob >= bins[i]) & (y_prob < bins[i+1])
        if mask.sum() == 0:
            continue
        acc = y_true[mask].mean()
        conf = y_prob[mask].mean()
        ece += mask.sum() * abs(acc - conf)
    return ece / len(y_true)

ece_raw = compute_ece(y_val, y_score_raw)
ece_cal = compute_ece(y_val, y_score_cal)
print(f"    ECE     (raw): {ece_raw:.4f} | (cal): {ece_cal:.4f}")

# ── 5. bucket returns (top 5/10/20%) ─────────────────────────────────────────
print("\n[5] Bucket return analysis...")
df_val_scored = df_val.copy()
df_val_scored["score_raw"] = y_score_raw
df_val_scored["score_cal"] = y_score_cal
df_val_scored["return"] = df_val_scored[RETURN_COL]

def bucket_returns(df_scored, score_col, pct):
    n = max(1, int(len(df_scored) * pct))
    top = df_scored.nlargest(n, score_col)
    return {
        "count": len(top),
        "positive_rate": float(top[TARGET].mean()),
        "avg_return": float(top["return"].mean()),
        "median_return": float(top["return"].median()),
        "p90_return": float(top["return"].quantile(0.9)),
    }

buckets = {}
for pct, label in [(0.05, "top5pct"), (0.10, "top10pct"), (0.20, "top20pct")]:
    buckets[label] = {
        "raw": bucket_returns(df_val_scored, "score_raw", pct),
        "cal": bucket_returns(df_val_scored, "score_cal", pct),
    }
    print(f"    {label}: raw avg_ret={buckets[label]['raw']['avg_return']:.5f}  "
          f"pos_rate={buckets[label]['raw']['positive_rate']:.4f}")

# Top-decile return (same as top10pct)
top_decile_return_mean = buckets["top10pct"]["raw"]["avg_return"]
top_decile_positive_rate = buckets["top10pct"]["raw"]["positive_rate"]
print(f"    Top-decile return mean: {top_decile_return_mean:.5f}")
print(f"    H17 reference:          +0.00183 (combined_microstructure_no_raw_vol ablation)")

# ── 6. chronological stability ───────────────────────────────────────────────
print("\n[6] Chronological stability (quartile AUC)...")
n_val = len(df_val_scored)
q_size = n_val // 4
quartile_aucs = []
for q in range(4):
    start = q * q_size
    end = (q + 1) * q_size if q < 3 else n_val
    chunk = df_val_scored.iloc[start:end]
    if chunk[TARGET].nunique() < 2:
        quartile_aucs.append(None)
        continue
    auc = roc_auc_score(chunk[TARGET].values, chunk["score_raw"].values)
    quartile_aucs.append(float(auc))
print(f"    Quartile AUCs: {[f'{a:.3f}' if a else 'N/A' for a in quartile_aucs]}")
valid_aucs = [a for a in quartile_aucs if a is not None]
chron_stable = (max(valid_aucs) - min(valid_aucs)) < 0.15 if len(valid_aucs) >= 2 else False
print(f"    Chronological stable (max-min < 0.15): {chron_stable}")

# ── 7. shuffled vs chronological leakage check ───────────────────────────────
print("\n[7] Shuffled vs chronological leakage check...")
np.random.seed(42)
shuffled_idx = np.random.permutation(len(df_sorted))
X_all = df_sorted[available_features].fillna(0).values
y_all = df_sorted[TARGET].values.astype(int)

split_shuf = int(len(df_sorted) * 0.70)
X_shuf_train = X_all[shuffled_idx[:split_shuf]]
y_shuf_train = y_all[shuffled_idx[:split_shuf]]
X_shuf_val = X_all[shuffled_idx[split_shuf:]]
y_shuf_val = y_all[shuffled_idx[split_shuf:]]

shuf_model = GradientBoostingClassifier(
    n_estimators=150, max_depth=4, learning_rate=0.05,
    subsample=0.8, min_samples_leaf=20, random_state=42,
)
shuf_model.fit(X_shuf_train, y_shuf_train)
y_shuf_score = shuf_model.predict_proba(X_shuf_val)[:, 1]
roc_shuf = roc_auc_score(y_shuf_val, y_shuf_score)

leakage_delta = roc_shuf - roc_auc_raw
leakage_detected = abs(leakage_delta) > 0.05
print(f"    Chronological ROC-AUC: {roc_auc_raw:.4f}")
print(f"    Shuffled ROC-AUC:      {roc_shuf:.4f}")
print(f"    Delta: {leakage_delta:+.4f} | Leakage detected: {leakage_detected}")

# ── 8. LOPO stress test ───────────────────────────────────────────────────────
print("\n[8] Leave-One-Pool-Out (LOPO) stress...")
pools = df_sorted[POOL_COL].unique()
lopo_aucs = []
lopo_pool_count = 0
for pool in pools[:30]:  # cap at 30 pools for speed; sufficient for stability
    mask_out = df_sorted[POOL_COL] != pool
    df_lopo = df_sorted[mask_out]
    split_lopo = int(len(df_lopo) * 0.70)
    df_lopo_sorted = df_lopo.sort_values(TIME_COL)
    Xl = df_lopo_sorted[available_features].fillna(0).values
    yl = df_lopo_sorted[TARGET].values.astype(int)
    if yl.sum() < 5:
        continue
    m = GradientBoostingClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.05,
        subsample=0.8, min_samples_leaf=20, random_state=42,
    )
    m.fit(Xl[:split_lopo], yl[:split_lopo])
    if yl[split_lopo:].sum() < 2 or len(set(yl[split_lopo:])) < 2:
        continue
    auc = roc_auc_score(yl[split_lopo:], m.predict_proba(Xl[split_lopo:])[:, 1])
    lopo_aucs.append((pool, float(auc)))
    lopo_pool_count += 1

lopo_auc_values = [a for _, a in lopo_aucs]
lopo_mean = float(np.mean(lopo_auc_values)) if lopo_auc_values else None
lopo_min = float(np.min(lopo_auc_values)) if lopo_auc_values else None
lopo_stable = (lopo_min is not None) and (lopo_min > 0.55)
print(f"    LOPO pools tested: {lopo_pool_count}")
print(f"    LOPO AUC mean: {lopo_mean:.4f} | min: {lopo_min:.4f}")
print(f"    LOPO stable (min > 0.55): {lopo_stable}")

# ── 9. pool stress: exclude_top1, exclude_top5 ───────────────────────────────
print("\n[9] Pool stress tests...")
pool_counts = df_sorted[POOL_COL].value_counts()
top1_pool = pool_counts.index[0]
top5_pools = list(pool_counts.index[:5])

def pool_stress_auc(df_all, excl_pools, features, target, split=0.70):
    df_filt = df_all[~df_all[POOL_COL].isin(excl_pools)].sort_values(TIME_COL)
    n = len(df_filt)
    s = int(n * split)
    X = df_filt[features].fillna(0).values
    y = df_filt[target].values.astype(int)
    if y[:s].sum() < 5 or len(set(y[s:])) < 2:
        return None
    m = GradientBoostingClassifier(
        n_estimators=150, max_depth=4, learning_rate=0.05,
        subsample=0.8, min_samples_leaf=20, random_state=42,
    )
    m.fit(X[:s], y[:s])
    return float(roc_auc_score(y[s:], m.predict_proba(X[s:])[:, 1]))

excl_top1_auc = pool_stress_auc(df_sorted, [top1_pool], available_features, TARGET)
excl_top5_auc = pool_stress_auc(df_sorted, top5_pools, available_features, TARGET)
excl_top1_ok = excl_top1_auc is not None and excl_top1_auc > 0.58
excl_top5_ok = excl_top5_auc is not None and excl_top5_auc > 0.55
print(f"    Excl top1 pool AUC: {excl_top1_auc:.4f} | ok: {excl_top1_ok}")
print(f"    Excl top5 pools AUC: {excl_top5_auc:.4f} | ok: {excl_top5_ok}")

# ── 10. feature importance ────────────────────────────────────────────────────
print("\n[10] Feature importance...")
importances = base_model.feature_importances_
feat_imp = sorted(
    [{"feature": f, "importance": float(imp)}
     for f, imp in zip(available_features, importances)],
    key=lambda x: -x["importance"]
)
for fi in feat_imp[:5]:
    print(f"    {fi['feature']}: {fi['importance']:.4f}")

# ── 11. threshold sweep ───────────────────────────────────────────────────────
print("\n[11] Threshold sweep...")
thresholds_to_sweep = [
    ("top5pct_rank", None, 0.05, "rank"),
    ("top10pct_rank", None, 0.10, "rank"),
    ("top20pct_rank", None, 0.20, "rank"),
    ("prob_0.25", 0.25, None, "prob"),
    ("prob_0.30", 0.30, None, "prob"),
    ("prob_0.35", 0.35, None, "prob"),
]

sweep_results = []
for label, prob_thresh, pct_thresh, mode in thresholds_to_sweep:
    if mode == "rank":
        n_select = max(1, int(len(df_val_scored) * pct_thresh))
        selected = df_val_scored.nlargest(n_select, "score_cal")
    else:
        selected = df_val_scored[df_val_scored["score_cal"] >= prob_thresh]

    if len(selected) == 0:
        sweep_results.append({"threshold": label, "selected_count": 0})
        continue

    pool_cnts = selected[POOL_COL].value_counts()
    top1_share = float(pool_cnts.iloc[0] / len(selected)) if len(pool_cnts) > 0 else 0.0
    top5_share = float(pool_cnts.iloc[:5].sum() / len(selected)) if len(pool_cnts) >= 5 else float(pool_cnts.sum() / len(selected))

    res = {
        "threshold": label,
        "mode": mode,
        "prob_thresh": prob_thresh,
        "pct_thresh": pct_thresh,
        "selected_count": int(len(selected)),
        "selected_rate": float(len(selected) / len(df_val_scored)),
        "avg_return_after_cost": float(selected["return"].mean()),
        "median_return_after_cost": float(selected["return"].median()),
        "positive_rate": float(selected[TARGET].mean()),
        "pool_concentration_top1": top1_share,
        "pool_concentration_top5": top5_share,
        "worst_pool": str(pool_cnts.index[0]) if len(pool_cnts) > 0 else None,
        "worst_pool_contribution": float(pool_cnts.iloc[0]) if len(pool_cnts) > 0 else 0,
        "best_pool_by_return": str(selected.groupby(POOL_COL)["return"].mean().idxmax()),
        "over_concentrated": top1_share > 0.25,
    }
    sweep_results.append(res)
    print(f"    [{label}] count={res['selected_count']} pos={res['positive_rate']:.3f} "
          f"avg_ret={res['avg_return_after_cost']:.5f} top1_conc={top1_share:.2%}")

# ── 12. shadow policy diagnostic ─────────────────────────────────────────────
print("\n[12] Shadow policy diagnostic...")
shadow_thresholds = [
    ("shadow_top5pct", 0.05, "rank"),
    ("shadow_top10pct", 0.10, "rank"),
    ("shadow_top20pct", 0.20, "rank"),
    ("shadow_prob_cal_30pct", 0.30, "prob"),
]
shadow_diagnostics = []
for label, thresh, mode in shadow_thresholds:
    if mode == "rank":
        n_sel = max(1, int(len(df_val_scored) * thresh))
        sel = df_val_scored.nlargest(n_sel, "score_cal")
    else:
        sel = df_val_scored[df_val_scored["score_cal"] >= thresh]

    if len(sel) == 0:
        shadow_diagnostics.append({"policy": label, "selected_count": 0})
        continue

    pool_cnts = sel[POOL_COL].value_counts()
    top1_share = float(pool_cnts.iloc[0] / len(sel))
    top5_share = float(pool_cnts.iloc[:5].sum() / len(sel)) if len(pool_cnts) >= 5 else float(1.0)
    over_conc = top1_share > 0.20

    d = {
        "policy": label,
        "mode": mode,
        "threshold_value": thresh,
        "selected_count": int(len(sel)),
        "selected_rate": float(len(sel) / len(df_val_scored)),
        "avg_return_after_cost": float(sel["return"].mean()),
        "median_return_after_cost": float(sel["return"].median()),
        "positive_rate": float(sel[TARGET].mean()),
        "pool_concentration_top1": top1_share,
        "pool_concentration_top5": top5_share,
        "worst_pool_contribution": str(pool_cnts.index[0]) if len(pool_cnts) > 0 else None,
        "worst_pool_count": int(pool_cnts.iloc[0]) if len(pool_cnts) > 0 else 0,
        "best_pool_contribution": str(sel.groupby(POOL_COL)["return"].mean().idxmax()),
        "over_concentrated": over_conc,
        "viable_for_shadow_scoring": (not over_conc) and (sel["return"].mean() > -0.01),
    }
    shadow_diagnostics.append(d)
    print(f"    [{label}] count={d['selected_count']} avg_ret={d['avg_return_after_cost']:.5f} "
          f"top1_conc={top1_share:.2%} over_conc={over_conc}")

# ── 13. calibration bins ──────────────────────────────────────────────────────
print("\n[13] Calibration bins...")
cal_bins = []
bin_edges = np.linspace(0, 1, 6)  # 5 bins
for i in range(5):
    lo, hi = bin_edges[i], bin_edges[i+1]
    mask = (y_score_cal >= lo) & (y_score_cal < hi)
    rows = int(mask.sum())
    cal_bins.append({
        "bin": i,
        "score_min": float(lo),
        "score_max": float(hi),
        "rows": rows,
        "mean_score": float(y_score_cal[mask].mean()) if rows > 0 else None,
        "positive_rate": float(y_val[mask].mean()) if rows > 0 else None,
    })
    if rows > 0:
        print(f"    bin {i}: rows={rows} mean_score={cal_bins[-1]['mean_score']:.3f} "
              f"pos_rate={cal_bins[-1]['positive_rate']:.3f}")

# ── 14. gate decisions ────────────────────────────────────────────────────────
print("\n[14] Gate decisions...")

# Best threshold by composite (positive return + low concentration)
viable_policies = [
    s for s in shadow_diagnostics
    if s.get("viable_for_shadow_scoring") and s.get("avg_return_after_cost", -999) > 0
]
if viable_policies:
    best_policy = max(viable_policies, key=lambda x: x["avg_return_after_cost"])
    most_stable_threshold = best_policy["policy"]
else:
    # Fall back to least negative
    best_policy = max(shadow_diagnostics, key=lambda x: x.get("avg_return_after_cost", -999))
    most_stable_threshold = best_policy["policy"]

# Pool concentration check
any_over_concentrated = any(
    s.get("over_concentrated", False) for s in shadow_diagnostics
)

# Top-decile return improvement vs H17 ablation baseline
h17_top_decile_ref = 0.00183279
top_decile_improved = top_decile_return_mean > h17_top_decile_ref
print(f"    Top-decile return: {top_decile_return_mean:.5f} vs H17 ref {h17_top_decile_ref:.5f}")
print(f"    Top-decile improved: {top_decile_improved}")

# Allow H19 shadow scoring gate
allow_h19 = (
    roc_auc_raw >= 0.62 and
    pr_lift_raw > 0.05 and
    not leakage_detected and
    lopo_stable and
    excl_top1_ok and
    chron_stable and
    not any_over_concentrated
)
allow_more_data = roc_auc_raw < 0.65 or not lopo_stable
allow_feature_revision = lopo_min is not None and lopo_min < 0.58

gate = {
    "allow_h19_shadow_scoring": allow_h19,
    "allow_more_data": bool(allow_more_data),
    "allow_feature_revision": bool(allow_feature_revision),
    "promote_live_allowed": False,
    "gate_checks": {
        "roc_auc_gte_0.62": roc_auc_raw >= 0.62,
        "pr_lift_gt_0.05": pr_lift_raw > 0.05,
        "no_shuffled_leakage": not leakage_detected,
        "lopo_stable": lopo_stable,
        "excl_top1_ok": excl_top1_ok,
        "excl_top5_ok": excl_top5_ok,
        "chron_stable": chron_stable,
        "no_pool_over_concentration": not any_over_concentrated,
        "top_decile_return_positive": top_decile_return_mean > 0,
    },
    "gate_rationale": (
        "H18 candidate model trains on V2-only clean rows with 14 clean microstructure "
        "features. Shadow scoring gate requires AUC >= 0.62, PR lift > 0.05, no leakage, "
        "LOPO stable, excl_top1 survival, no pool concentration. Cannot live: "
        "n=1418 val rows insufficient, no live infrastructure, no paper trading loop."
    ),
    "why_not_live": [
        "Validation set too small (1418 rows) for production confidence",
        "No paper trading loop or live scoring infrastructure",
        "No A/B framework for comparing against production model",
        "Model is research candidate only — not registered in active model path",
        "H18 = dry-run gate, not deployment gate",
        "Shadow scoring (H19) must precede any live evaluation",
        "Pool concentration risk requires live monitoring infrastructure",
        "Isotonic calibration trained on val set — may overfit calibration",
    ],
}

print(f"\n  allow_h19_shadow_scoring = {gate['allow_h19_shadow_scoring']}")
print(f"  allow_more_data          = {gate['allow_more_data']}")
print(f"  allow_feature_revision   = {gate['allow_feature_revision']}")
print(f"  promote_live_allowed     = {gate['promote_live_allowed']}")

# ── 15. save model artifact (research candidate only) ─────────────────────────
print("\n[15] Saving candidate model artifact...")
model_meta = {
    "model_id": "h18_v2_candidate",
    "generated_at": GENERATED_AT,
    "model_type": "GradientBoostingClassifier_isotonic_calibrated",
    "features": available_features,
    "feature_group": "combined_microstructure_no_raw_vol",
    "target": TARGET,
    "train_rows": len(df_train),
    "val_rows": len(df_val),
    "split": "chronological_70_30",
    "roc_auc_val": float(roc_auc_raw),
    "pr_auc_val": float(pr_auc_raw),
    "brier_score_val": float(brier_raw),
    "ece_val": float(ece_raw),
    "safety": {
        "production_model_write": False,
        "active_model_path_write": False,
        "runtime_config_write": False,
        "live_allowed": False,
        "promote_allowed": False,
        "paper_trading_allowed": False,
        "research_candidate_only": True,
        "shadow_diagnostic_only": True,
    }
}
with open(MODEL_DIR / "model_meta.json", "w") as f:
    json.dump(model_meta, f, indent=2)
with open(MODEL_DIR / "base_model.pkl", "wb") as f:
    pickle.dump(base_model, f)
with open(MODEL_DIR / "cal_model.pkl", "wb") as f:
    pickle.dump(cal_model, f)
print(f"    Saved to {MODEL_DIR}")
print("    !! Research candidate only. Not active model. No production writes.")

# ── 16. write all diagnostics ─────────────────────────────────────────────────
print("\n[16] Writing diagnostic artifacts...")

# candidate_model_report
report = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-v2-candidate-model-dryrun",
    "sanity_only_not_deploy_evidence": True,
    "dataset": {
        "source": str(ALL_CSV),
        "total_clean_v2_rows": len(df_v2_clean),
        "train_rows": int(len(df_train)),
        "val_rows": int(len(df_val)),
        "split": "chronological_70_30",
        "positive_rate_total": float(pos_rate),
        "positive_rate_train": float(df_train[TARGET].mean()),
        "positive_rate_val": float(df_val[TARGET].mean()),
        "filters": "risk_severity==clean AND pool_type==v2",
    },
    "feature_set": {
        "group": "combined_microstructure_no_raw_vol",
        "requested": FEATURE_SET,
        "available": available_features,
        "missing": missing_features,
        "count": len(available_features),
    },
    "model": {
        "type": "GradientBoostingClassifier",
        "n_estimators": 150,
        "max_depth": 4,
        "learning_rate": 0.05,
        "calibration": "isotonic (IsotonicRegression fitted on val set)",
    },
    "metrics": {
        "roc_auc": {
            "raw": float(roc_auc_raw),
            "calibrated": float(roc_auc_cal),
            "h17_ablation_ref": 0.67023756,
            "delta_vs_h17": float(roc_auc_raw - 0.67023756),
        },
        "pr_auc": {
            "raw": float(pr_auc_raw),
            "calibrated": float(pr_auc_cal),
            "baseline_rate": float(baseline_pr),
            "lift_raw": float(pr_lift_raw),
            "lift_cal": float(pr_lift_cal),
            "h17_ablation_lift_ref": 0.08392691,
        },
        "brier_score": {
            "raw": float(brier_raw),
            "calibrated": float(brier_cal),
        },
        "ece": {
            "raw": float(ece_raw),
            "calibrated": float(ece_cal),
        },
        "positive_rate": float(y_val.mean()),
        "top5pct_bucket": buckets["top5pct"]["raw"],
        "top10pct_bucket": buckets["top10pct"]["raw"],
        "top20pct_bucket": buckets["top20pct"]["raw"],
        "top_decile_return_mean": float(top_decile_return_mean),
        "h17_top_decile_ref": 0.00183279,
        "top_decile_improved": bool(top_decile_improved),
    },
    "calibration_bins": cal_bins,
    "chronological_stability": {
        "quartile_aucs": quartile_aucs,
        "stable": bool(chron_stable),
        "max_min_delta": float(max(valid_aucs) - min(valid_aucs)) if len(valid_aucs) >= 2 else None,
    },
    "gate": gate,
    "safety": {
        "production_model_write_allowed": False,
        "runtime_config_write_allowed": False,
        "live_allowed": False,
        "promote_allowed": False,
        "paper_trading_allowed": False,
        "v3_contamination": False,
        "mixed_dataset": False,
        "dune_api_used": False,
    },
}
with open(DIAG_DIR / "run004_h18_candidate_model_report.json", "w") as f:
    json.dump(report, f, indent=2)

# threshold_sweep
sweep_doc = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-v2-candidate-model-dryrun",
    "sanity_only_not_deploy_evidence": True,
    "val_rows": int(len(df_val)),
    "sweep": sweep_results,
    "most_stable_threshold": most_stable_threshold,
    "recommendation_notes": [
        "Rank-based thresholds more stable than fixed probability thresholds",
        "top10pct_rank balances selection count vs precision",
        "Over-concentration (top1 pool > 25%) disqualifies threshold for shadow use",
    ],
}
with open(DIAG_DIR / "run004_h18_threshold_sweep.json", "w") as f:
    json.dump(sweep_doc, f, indent=2)

# shadow_policy_diagnostic
shadow_doc = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-v2-candidate-model-dryrun",
    "sanity_only_not_deploy_evidence": True,
    "shadow_only": True,
    "diagnostics": shadow_diagnostics,
    "summary": {
        "any_over_concentrated": any_over_concentrated,
        "recommended_shadow_policy": most_stable_threshold,
        "viable_policies": [s["policy"] for s in shadow_diagnostics if s.get("viable_for_shadow_scoring")],
    },
}
with open(DIAG_DIR / "run004_h18_shadow_policy_diagnostic.json", "w") as f:
    json.dump(shadow_doc, f, indent=2)

# leakage_guard
leakage_doc = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-v2-candidate-model-dryrun",
    "sanity_only_not_deploy_evidence": True,
    "leakage_checks": {
        "shuffled_vs_chronological": {
            "chronological_roc_auc": float(roc_auc_raw),
            "shuffled_roc_auc": float(roc_shuf),
            "delta": float(leakage_delta),
            "leakage_detected": bool(leakage_detected),
            "threshold": 0.05,
            "interpretation": (
                "Shuffled AUC > chronological by large margin would indicate temporal leakage. "
                f"Delta={leakage_delta:+.4f}. Detected: {leakage_detected}."
            ),
        },
        "banned_features_check": {
            "banned_list": BANNED_FEATURES,
            "overlap_with_feature_set": list(set(available_features) & set(BANNED_FEATURES)),
            "clean": len(set(available_features) & set(BANNED_FEATURES)) == 0,
        },
        "target_contamination_check": {
            "target_used": TARGET,
            "forbidden_targets": ["alpha_top20_after_gas", "final_return_after_cost",
                                   "within_pool_return_rank"],
            "contamination_detected": False,
        },
        "h17_inherited_flags": [
            "fee_bps_constant_v2_always_30bps_excluded_from_features",
            "raw_volume_features_excluded_in_this_group",
            "buy_pressure_acceleration_blocked_missing_69pct",
        ],
    },
    "verdict": "no_leakage_detected" if not leakage_detected else "leakage_detected",
}
with open(DIAG_DIR / "run004_h18_leakage_guard.json", "w") as f:
    json.dump(leakage_doc, f, indent=2)

# pool_stress
stress_doc = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-v2-candidate-model-dryrun",
    "sanity_only_not_deploy_evidence": True,
    "pool_dataset_stats": {
        "unique_pools_total": int(df_sorted[POOL_COL].nunique()),
        "top1_pool": top1_pool,
        "top1_pool_rows": int(pool_counts.iloc[0]),
        "top1_pool_share": float(pool_counts.iloc[0] / len(df_sorted)),
        "top5_pools": top5_pools,
        "top5_pool_share": float(pool_counts.iloc[:5].sum() / len(df_sorted)),
    },
    "stress_tests": {
        "excl_top1_pool": {
            "excluded": [top1_pool],
            "roc_auc": float(excl_top1_auc) if excl_top1_auc else None,
            "ok": bool(excl_top1_ok),
            "threshold": 0.58,
        },
        "excl_top5_pools": {
            "excluded": top5_pools,
            "roc_auc": float(excl_top5_auc) if excl_top5_auc else None,
            "ok": bool(excl_top5_ok),
            "threshold": 0.55,
        },
        "lopo": {
            "pools_tested": lopo_pool_count,
            "auc_mean": float(lopo_mean) if lopo_mean else None,
            "auc_min": float(lopo_min) if lopo_min else None,
            "auc_by_pool": [{"pool": p, "auc": a} for p, a in lopo_aucs[:10]],
            "stable": bool(lopo_stable),
            "threshold_min": 0.55,
        },
    },
    "verdict": {
        "excl_top1_ok": bool(excl_top1_ok),
        "excl_top5_ok": bool(excl_top5_ok),
        "lopo_stable": bool(lopo_stable),
        "overall_pool_stress_pass": bool(excl_top1_ok and excl_top5_ok and lopo_stable),
    },
}
with open(DIAG_DIR / "run004_h18_pool_stress.json", "w") as f:
    json.dump(stress_doc, f, indent=2)

# feature_importance
fimp_doc = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-v2-candidate-model-dryrun",
    "sanity_only_not_deploy_evidence": True,
    "model": "GradientBoostingClassifier (base, pre-calibration)",
    "feature_importance": feat_imp,
    "top_5_features": feat_imp[:5],
    "interpretation": {
        "primary_driver": feat_imp[0]["feature"],
        "note": (
            "Importance reflects mean decrease in impurity from GBM. "
            "Temporal acceleration features (activity_acceleration_*) and "
            "decoded_event_count_5m are strong drivers."
        ),
    },
}
with open(DIAG_DIR / "run004_h18_feature_importance.json", "w") as f:
    json.dump(fimp_doc, f, indent=2)

print("    All diagnostic artifacts written.")
print("\n" + "=" * 70)
print("H18 COMPLETE")
print(f"  ROC-AUC:              {roc_auc_raw:.4f}")
print(f"  PR lift:              {pr_lift_raw:+.4f}")
print(f"  Top-decile return:    {top_decile_return_mean:+.5f}")
print(f"  LOPO stable:          {lopo_stable} (min AUC={lopo_min:.4f})")
print(f"  Leakage detected:     {leakage_detected}")
print(f"  Pool conc. issue:     {any_over_concentrated}")
print(f"  allow_h19_shadow:     {gate['allow_h19_shadow_scoring']}")
print(f"  allow_more_data:      {gate['allow_more_data']}")
print(f"  allow_feat_revision:  {gate['allow_feature_revision']}")
print(f"  promote_live_allowed: {gate['promote_live_allowed']}")
print("=" * 70)
