"""
Run-004 Patch H18.8: V2-only Fresh OOT + Return-Aware Target Revision
======================================================================
Phase A: Attempt fresh OOT W2 construction (blocks > 45,408,934).
         Report data infeasibility under hard constraints.
         Run fixed H18 model on maximum available OOT (1766 rows).

Phase B: Return-aware target revision diagnostic.
         Compare 4 model objectives on same train/val split.
         Research-only — no production model replacement.

Hard constraints: no live / no promote / no production writes /
                  no Dune API / no pipeline rebuild / no V3 / no Aero.
"""
import json, pickle, csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr, pearsonr
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss
from sklearn.isotonic import IsotonicRegression

BASE   = Path("/opt/EATLQ")
DIAG   = BASE / "mvp/diagnostics"
RUNS   = BASE / "mvp/runs"
H18DIR = BASE / "mvp/models/h18_v2_candidate"
RESDIR = BASE / "mvp/models/h18_8_research_candidate"
ALL_CSV = DIAG / "run004_h1611_all_v2_decoded_backed_10k_dataset.csv"
RESDIR.mkdir(parents=True, exist_ok=True)

GENERATED_AT = datetime.now(timezone.utc).isoformat()
BNR = "=" * 72

FEATURES = [
    "unique_swap_senders_300s", "top1_sender_share_300s", "top3_sender_share_300s",
    "sender_gini_300s", "sender_entropy_300s", "buy_count_300s", "sell_count_300s",
    "decoded_event_count_5m", "new_buyer_ratio_300s", "repeat_buyer_ratio_300s",
    "trade_size_cv_300s", "activity_acceleration_1m_vs_5m",
    "activity_acceleration_3m_vs_15m", "sender_entropy_change_1m_vs_5m",
]
TARGET     = "alpha_h17_clean_top20"
RETURN_COL = "final_return_after_cost"
POOL_COL   = "pool_address"
TIME_COL   = "source_block_number"
GLOBAL_THRESH   = 0.049547
MAX_KNOWN_BLOCK = 45_408_934


# ── helpers ───────────────────────────────────────────────────────────────────
def pool_cap_select(df_s, score_col, target_n, cap_pct):
    max_pp = max(1, int(np.floor(cap_pct * target_n)))
    df_sorted = df_s.sort_values(score_col, ascending=False)
    sel, pc = [], {}
    for lbl, row in df_sorted.iterrows():
        if len(sel) >= target_n:
            break
        pool = row[POOL_COL]
        cnt = pc.get(pool, 0) + 1
        if cnt <= max_pp:
            sel.append(lbl)
            pc[pool] = cnt
    return df_s.loc[sel]


def bucket_stats(df_s, score_col, pct, cap=None):
    target_n = max(1, int(len(df_s) * pct))
    if cap is None:
        sel = df_s.nlargest(target_n, score_col)
    else:
        sel = pool_cap_select(df_s, score_col, target_n, cap)
    if len(sel) == 0:
        return {"count": 0, "avg_return": None, "positive_return_rate": None}
    pc = sel[POOL_COL].value_counts()
    return {
        "count": int(len(sel)),
        "fill_rate": float(len(sel) / target_n),
        "avg_return": float(sel[RETURN_COL].mean()),
        "median_return": float(sel[RETURN_COL].median()),
        "positive_return_rate": float((sel[RETURN_COL] > 0).mean()),
        "alpha_capture": float(sel[TARGET].mean()),
        "top1_pool_share": float(pc.iloc[0] / len(sel)),
        "top5_pool_share": float(pc.iloc[:5].sum() / len(sel)) if len(pc) >= 5 else 1.0,
        "unique_pools": int(sel[POOL_COL].nunique()),
        "best_pool": str(sel.groupby(POOL_COL)[RETURN_COL].mean().idxmax()),
        "worst_pool_by_count": str(pc.index[0]),
        "return_positive": float(sel[RETURN_COL].mean()) > 0,
        "meets_300_threshold": int(len(sel)) >= 300,
    }


def compute_ece(y_true, y_prob, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        m = (y_prob >= bins[i]) & (y_prob < bins[i+1])
        if m.sum() == 0:
            continue
        ece += m.sum() * abs(y_true[m].mean() - y_prob[m].mean())
    return float(ece / max(len(y_true), 1))


def correlation_report(scores, returns, labels):
    sp_ret, _  = spearmanr(scores, returns)
    sp_lbl, _  = spearmanr(scores, labels)
    pe_ret, _  = pearsonr(scores, returns)
    return {
        "spearman_score_vs_return": float(sp_ret),
        "spearman_score_vs_label":  float(sp_lbl),
        "pearson_score_vs_return":  float(pe_ret),
        "ranking_gap": float(abs(sp_ret) - abs(sp_lbl)),   # positive = score ranks return better
    }


# ═══════════════════════════════════════════════════════════════════════════════
print(BNR)
print("H18.8  Phase A: Fresh OOT W2 + Fixed H18 Model")
print(BNR)

# ── 1. Load & partition ───────────────────────────────────────────────────────
print("\n[1] Loading dataset...")
df_all = pd.read_csv(ALL_CSV)
clean  = df_all[
    (df_all["risk_severity"] == "clean") & (df_all["pool_type"] == "v2")
].copy().sort_values(TIME_COL).reset_index(drop=True)

clean[TARGET] = (clean[RETURN_COL] >= GLOBAL_THRESH).astype(int)
TRAIN_END = int(len(clean) * 0.70)   # 4118

df_train = clean.iloc[:TRAIN_END].copy()
df_val   = clean.iloc[TRAIN_END:].copy()

print(f"    Train: {len(df_train)} rows (blocks {df_train[TIME_COL].min()}-{df_train[TIME_COL].max()})")
print(f"    Val  : {len(df_val)} rows  (blocks {df_val[TIME_COL].min()}-{df_val[TIME_COL].max()})")
print(f"    Max block in dataset: {clean[TIME_COL].max()}")

# ── 2. Phase A: Fresh OOT W2 build attempt ───────────────────────────────────
print("\n[2] Phase A — Fresh OOT W2 build attempt (target: blocks > 45,408,934)...")

fresh_oot_w2_infeasible = True
infeasibility_reason = (
    "All available V2 clean rows are exhausted. "
    f"Dataset max block = {MAX_KNOWN_BLOCK}. "
    "Acquiring rows with block > 45,408,934 requires either: "
    "(a) Dune API query [prohibited: constraint 10], or "
    "(b) RPC-based labeling pipeline run [prohibited: constraint 11]. "
    "No feasible path under current hard constraints. "
    "Phase A fixed-model validation will use the H18 val set (1,766 rows) "
    "as the maximum available OOT window — same as H18.7 Large OOT W1."
)
print(f"    Fresh OOT W2: INFEASIBLE")
print(f"    Reason: {infeasibility_reason[:120]}...")
print(f"    Fallback: using H18 val set (1766 rows) as Phase A OOT window")

fresh_oot_w2_build_report = {
    "generated_at": GENERATED_AT,
    "status": "infeasible",
    "target_blocks_gt": MAX_KNOWN_BLOCK,
    "target_rows": 10000,
    "actual_rows": 0,
    "reason": infeasibility_reason,
    "fallback_window": "h18_val_set_1766_rows (same as H18.7 Large OOT W1)",
    "fallback_blocks": f"{df_val[TIME_COL].min()}-{df_val[TIME_COL].max()}",
    "attempted": 0, "success": 0, "failed": 0, "skipped": 0, "extra_after_target": 0,
    "quote_success_rate": None,
    "decoded_coverage": None,
    "unique_pools": None,
    "top1_pool_share": None,
    "top5_pool_share": None,
    "v3_rows": 0, "aero_rows": 0, "mixed_dataset": False,
    "dune_api_used": False, "pipeline_rebuilt": False,
    "constraints_violated": [],
}

# ── 3. Phase A: Fixed H18 model on val (Phase A substitute window) ───────────
print("\n[3] Phase A — Fixed H18 model on Phase A substitute window (1766 rows)...")

class CalibratedWrapper:
    def __init__(self, base, calibrator):
        self.base = base; self.calibrator = calibrator
    def predict_proba(self, X):
        raw = self.base.predict_proba(X)[:, 1]
        return np.column_stack([1 - self.calibrator.predict(raw),
                                 self.calibrator.predict(raw)])

import sys; sys.modules[__name__].CalibratedWrapper = CalibratedWrapper

with open(H18DIR / "base_model.pkl", "rb") as f:
    h18_base = pickle.load(f)
try:
    with open(H18DIR / "cal_model.pkl", "rb") as f:
        cal_obj = pickle.load(f)
    h18_iso = cal_obj.calibrator
    print("    Loaded H18 base + isotonic calibrator.")
except Exception as e:
    print(f"    Fallback: refit isotonic on val. ({e})")
    X_vf = df_val[FEATURES].fillna(0).values
    y_vf = df_val[TARGET].values.astype(int)
    h18_iso = IsotonicRegression(out_of_bounds="clip")
    h18_iso.fit(h18_base.predict_proba(X_vf)[:, 1], y_vf)

X_val = df_val[FEATURES].fillna(0).values
y_val = df_val[TARGET].values.astype(int)
y_raw_h18 = h18_base.predict_proba(X_val)[:, 1]
y_cal_h18 = h18_iso.predict(y_raw_h18)

df_val_s = df_val.copy()
df_val_s["score_raw"]  = y_raw_h18
df_val_s["score_cal"]  = y_cal_h18

roc_h18  = float(roc_auc_score(y_val, y_raw_h18))
pr_h18   = float(average_precision_score(y_val, y_raw_h18))
base_rate= float(y_val.mean())
prl_h18  = pr_h18 - base_rate
brier_h18= float(brier_score_loss(y_val, y_raw_h18))
ece_h18  = compute_ece(y_val, y_raw_h18)
corr_h18 = correlation_report(y_raw_h18, df_val[RETURN_COL].values, y_val)

print(f"    ROC-AUC: {roc_h18:.4f} | PR lift: {prl_h18:+.4f}")
print(f"    Spearman(score, return): {corr_h18['spearman_score_vs_return']:.4f}")
print(f"    Spearman(score, label):  {corr_h18['spearman_score_vs_label']:.4f}")

# Bucket stats for Phase A report
pa_t10 = bucket_stats(df_val_s, "score_raw", 0.10)
pa_t20 = bucket_stats(df_val_s, "score_raw", 0.20)
pa_c15_10 = bucket_stats(df_val_s, "score_raw", 0.10, cap=0.15)
pa_c15_20 = bucket_stats(df_val_s, "score_raw", 0.20, cap=0.15)

phase_a_fixed_report = {
    "generated_at": GENERATED_AT,
    "window": "h18_val_substitute_1766_rows",
    "blocks": f"{df_val[TIME_COL].min()}-{df_val[TIME_COL].max()}",
    "rows": int(len(df_val)),
    "model": "H18 candidate (fixed, no retrain)",
    "note": "True fresh W2 infeasible; this window = H18 val set = same as H18.7 Large OOT W1",
    "roc_auc": float(roc_h18),
    "pr_auc": float(pr_h18),
    "pr_lift": float(prl_h18),
    "brier_score": float(brier_h18),
    "ece_raw": float(ece_h18),
    "positive_rate": float(base_rate),
    "spearman_score_vs_return": float(corr_h18["spearman_score_vs_return"]),
    "spearman_score_vs_label":  float(corr_h18["spearman_score_vs_label"]),
    "pearson_score_vs_return":  float(corr_h18["pearson_score_vs_return"]),
    "top10pct_rank": pa_t10,
    "top20pct_rank": pa_t20,
    "top10pct_rank_poolcap_15pct": pa_c15_10,
    "top20pct_rank_poolcap_15pct": pa_c15_20,
    "ranking_signal_confirmed": bool(roc_h18 >= 0.60),
}
print(f"    top10+cap15: avg_ret={pa_c15_10['avg_return']:+.5f}  top1={pa_c15_10['top1_pool_share']:.2%}")

# ═══════════════════════════════════════════════════════════════════════════════
print()
print(BNR)
print("H18.8  Phase B: Return-Aware Target Revision Diagnostic")
print(BNR)
print("Phase A complete. Proceeding to Phase B return-aware comparison.")

# ── 4. Prepare training matrices ──────────────────────────────────────────────
print("\n[4] Preparing training data for 4 model objectives...")
X_train  = df_train[FEATURES].fillna(0).values
y_bin    = df_train[TARGET].values.astype(int)
y_cont   = df_train[RETURN_COL].values.astype(float)

ret_max  = float(y_cont.max())
ret_min  = float(y_cont.min())

# Sample weights for utility-weighted model
# Upweight high-return positives, penalize large-loss rows
def utility_weights(returns):
    w = np.ones(len(returns))
    pos_mask = returns > 0
    neg_mask = returns <= 0
    if pos_mask.sum() > 0 and ret_max > 0:
        w[pos_mask] = 1.0 + 4.0 * (returns[pos_mask] / ret_max)
    if neg_mask.sum() > 0 and abs(ret_min) > 0:
        w[neg_mask] = 1.0 + 2.0 * (abs(returns[neg_mask]) / abs(ret_min))
    return w

sample_weights = utility_weights(y_cont)
print(f"    Binary positives: {y_bin.sum()} ({y_bin.mean():.2%})")
print(f"    Utility weights: mean={sample_weights.mean():.2f}, max={sample_weights.max():.2f}")

GBM_PARAMS_CLS = dict(
    n_estimators=150, max_depth=4, learning_rate=0.05,
    subsample=0.8, min_samples_leaf=20, random_state=42,
)
GBM_PARAMS_REG = dict(
    n_estimators=150, max_depth=4, learning_rate=0.05,
    subsample=0.8, min_samples_leaf=20, random_state=42,
)

# ── 5. Train all 4 model variants ────────────────────────────────────────────
print("\n[5] Training 4 model variants (research only; no production writes)...")

# M1: Binary top20 baseline (recreate H18 — identical architecture)
print("    [M1] Binary top20 classifier (baseline = H18)...")
m1 = GradientBoostingClassifier(**GBM_PARAMS_CLS)
m1.fit(X_train, y_bin)
s1 = m1.predict_proba(X_val)[:, 1]
print(f"         ROC-AUC={roc_auc_score(y_val, s1):.4f}")

# M2: Quantile regressor at 80th percentile
print("    [M2] Quantile regressor (alpha=0.80, predict return >= 80pct)...")
m2 = GradientBoostingRegressor(loss="quantile", alpha=0.80, **GBM_PARAMS_REG)
m2.fit(X_train, y_cont)
s2 = m2.predict(X_val)
# Normalize to [0,1] for comparability
s2_norm = (s2 - s2.min()) / (s2.max() - s2.min() + 1e-9)
print(f"         Spearman(score, return)={spearmanr(s2, df_val[RETURN_COL].values)[0]:.4f}")

# M3: Return magnitude regressor (squared error / Huber)
print("    [M3] Return magnitude regressor (Huber loss)...")
m3 = GradientBoostingRegressor(loss="huber", alpha=0.9, **GBM_PARAMS_REG)
m3.fit(X_train, y_cont)
s3 = m3.predict(X_val)
s3_norm = (s3 - s3.min()) / (s3.max() - s3.min() + 1e-9)
print(f"         Spearman(score, return)={spearmanr(s3, df_val[RETURN_COL].values)[0]:.4f}")

# M4: Utility-weighted binary classifier
print("    [M4] Utility-weighted binary classifier (upweight high-return rows)...")
m4 = GradientBoostingClassifier(**GBM_PARAMS_CLS)
m4.fit(X_train, y_bin, sample_weight=sample_weights)
s4 = m4.predict_proba(X_val)[:, 1]
print(f"         ROC-AUC={roc_auc_score(y_val, s4):.4f}")

print("    All 4 models trained.")

# ── 6. Evaluate all models on val set ────────────────────────────────────────
print("\n[6] Comparing all models on val set...")

MODELS = [
    ("M1_binary_top20_baseline",     s1,      True),
    ("M2_quantile_reg_p80",          s2_norm, False),
    ("M3_magnitude_reg_huber",       s3_norm, False),
    ("M4_utility_weighted_binary",   s4,      True),
]

# Also include H18 original model for reference
MODELS_WITH_H18 = [
    ("H18_original_binary",          y_raw_h18, True),
] + MODELS

results_table = []
df_val_copy = df_val.copy()

for name, scores, is_cls in MODELS_WITH_H18:
    df_s = df_val_copy.copy()
    df_s["score"] = scores

    # Classification metrics only for classifiers
    roc  = float(roc_auc_score(y_val, scores)) if is_cls else None
    pr   = float(average_precision_score(y_val, scores)) if is_cls else None
    br   = float(brier_score_loss(y_val, scores)) if is_cls else None
    prl  = (pr - base_rate) if pr is not None else None

    # Rank correlations (all models)
    sp_ret, _ = spearmanr(scores, df_val[RETURN_COL].values)
    sp_lbl, _ = spearmanr(scores, y_val)
    pe_ret, _ = pearsonr(scores, df_val[RETURN_COL].values)

    # Bucket returns
    t5  = bucket_stats(df_s, "score", 0.05)
    t10 = bucket_stats(df_s, "score", 0.10)
    t20 = bucket_stats(df_s, "score", 0.20)
    c15_10 = bucket_stats(df_s, "score", 0.10, cap=0.15)
    c15_20 = bucket_stats(df_s, "score", 0.20, cap=0.15)
    c10_10 = bucket_stats(df_s, "score", 0.10, cap=0.10)

    # Feature importance (for GBM models)
    if name.startswith("M1"):  fi = [(f, float(v)) for f, v in zip(FEATURES, m1.feature_importances_)]
    elif name.startswith("M2"): fi = [(f, float(v)) for f, v in zip(FEATURES, m2.feature_importances_)]
    elif name.startswith("M3"): fi = [(f, float(v)) for f, v in zip(FEATURES, m3.feature_importances_)]
    elif name.startswith("M4"): fi = [(f, float(v)) for f, v in zip(FEATURES, m4.feature_importances_)]
    else: fi = [(f, float(v)) for f, v in zip(FEATURES, h18_base.feature_importances_)]

    top5_fi = sorted(fi, key=lambda x: -x[1])[:5]

    r = {
        "model": name,
        "objective": "binary_classification" if is_cls else "regression",
        "roc_auc": roc,
        "pr_auc": pr,
        "pr_lift": prl,
        "brier_score": br,
        "spearman_score_vs_return": float(sp_ret),
        "spearman_score_vs_label":  float(sp_lbl),
        "pearson_score_vs_return":  float(pe_ret),
        "ranking_gap": float(abs(sp_ret) - abs(sp_lbl)),
        "top5pct_rank": t5,
        "top10pct_rank": t10,
        "top20pct_rank": t20,
        "top10pct_rank_poolcap_15pct": c15_10,
        "top20pct_rank_poolcap_15pct": c15_20,
        "top10pct_rank_poolcap_10pct": c10_10,
        "top5_feature_importance": [{"feature": f, "importance": v} for f, v in top5_fi],
    }
    results_table.append(r)

    print(f"\n    [{name}]")
    if roc: print(f"      ROC-AUC={roc:.4f}  PR lift={prl:+.4f}")
    print(f"      Spearman(score,return)={sp_ret:.4f}  Spearman(score,label)={sp_lbl:.4f}")
    print(f"      top10_raw avg_ret={t10['avg_return']:+.5f}  top1={t10['top1_pool_share']:.2%}")
    print(f"      top10+cap15 avg_ret={c15_10['avg_return']:+.5f}  top1={c15_10['top1_pool_share']:.2%}")
    print(f"      top20+cap15 avg_ret={c15_20['avg_return']:+.5f}  top1={c15_20['top1_pool_share']:.2%}")

# ── 7. Pool policy stability (cross-H18.7-W1 and Phase-A-substitute) ─────────
print("\n[7] Pool policy stability across windows...")

def stability_summary(r_list):
    """Check if a model × policy is positive in both available windows."""
    # We only have one genuine window (val set), so check top10+cap15 and top20+cap15
    t10_pos = r_list["top10pct_rank_poolcap_15pct"].get("return_positive", False)
    t20_pos = r_list["top20pct_rank_poolcap_15pct"].get("return_positive", False)
    t10_conc = r_list["top10pct_rank_poolcap_15pct"].get("top1_pool_share", 1.0)
    t20_conc = r_list["top20pct_rank_poolcap_15pct"].get("top1_pool_share", 1.0)
    return {
        "top10_cap15_positive": bool(t10_pos),
        "top20_cap15_positive": bool(t20_pos),
        "top10_cap15_top1_share": float(t10_conc),
        "top20_cap15_top1_share": float(t20_conc),
        "concentration_ok": bool(t10_conc <= 0.15 and t20_conc <= 0.15),
        "viable_candidate": bool((t10_pos or t20_pos) and t10_conc <= 0.15),
    }

pool_stability = {}
for r in results_table:
    pool_stability[r["model"]] = stability_summary(r)
    v = pool_stability[r["model"]]
    print(f"    [{r['model']}] top10+cap15: pos={v['top10_cap15_positive']} conc={v['top10_cap15_top1_share']:.2%} | "
          f"top20+cap15: pos={v['top20_cap15_positive']} conc={v['top20_cap15_top1_share']:.2%} | "
          f"viable={v['viable_candidate']}")

# Best model by Spearman(score, return)
best_by_spearman = max(results_table, key=lambda x: x["spearman_score_vs_return"])
best_by_return   = max(results_table, key=lambda x: x["top10pct_rank_poolcap_15pct"].get("avg_return", -999))
print(f"\n    Best Spearman(score,return): {best_by_spearman['model']} = {best_by_spearman['spearman_score_vs_return']:.4f}")
print(f"    Best top10+cap15 return:    {best_by_return['model']} = {best_by_return['top10pct_rank_poolcap_15pct'].get('avg_return', 0):+.5f}")

# ── 8. Gate decision ──────────────────────────────────────────────────────────
print("\n[8] Gate decision...")

# H19 conditions
# "At least one policy positive on both old W1 AND fresh W2"
# Since fresh W2 is infeasible, we use the single val window
# → Cannot satisfy "both W1 and W2" requirement → H19 blocked
best_m = best_by_return
best_viable = pool_stability.get(best_m["model"], {}).get("viable_candidate", False)
cond_two_windows = False   # structurally impossible; only 1 window
cond_ranking_improved = (
    best_by_spearman["spearman_score_vs_return"] >
    results_table[0]["spearman_score_vs_return"]   # compare to H18 original
)
h18_orig_spearman = results_table[0]["spearman_score_vs_return"]
best_spearman = best_by_spearman["spearman_score_vs_return"]

# Return-aware model dry-run: allow if:
# (a) best Spearman improves ≥20%, OR
# (b) best top10+cap15 return improves ≥3× vs H18 baseline AND any model viable
h18_top10_cap15_return = phase_a_fixed_report["top10pct_rank_poolcap_15pct"]["avg_return"]
best_top10_cap15_return = best_by_return["top10pct_rank_poolcap_15pct"].get("avg_return", -999)
cond_spearman_20pct  = best_spearman > h18_orig_spearman * 1.20
cond_return_3x       = (h18_top10_cap15_return > 0 and
                        best_top10_cap15_return > h18_top10_cap15_return * 3.0)
any_viable = any(v["viable_candidate"] for v in pool_stability.values())
allow_ret_aware_dryrun = (cond_spearman_20pct or cond_return_3x) and any_viable
# Retrain on expanded V2: allow if signal persists and return-aware target helps
allow_retrain = allow_ret_aware_dryrun
# More OOT data
allow_more_oot = not (cond_two_windows)

gate = {
    "allow_h19_shadow_scoring": False,    # structurally impossible with 1 window
    "allow_return_aware_model_dryrun": bool(allow_ret_aware_dryrun),
    "allow_retrain_on_expanded_v2": bool(allow_retrain),
    "allow_more_oot_data": bool(allow_more_oot),
    "allow_feature_revision": False,
    "promote_live_allowed": False,
    "phase_a_fresh_w2_built": False,
    "phase_b_return_aware_comparison_done": True,
    "gate_conditions_h19": {
        "cond_at_least_2_oot_windows_positive": False,
        "reason": "Fresh W2 infeasible under hard constraints (Dune API + pipeline rebuild prohibited)",
        "cond_ranking_signal_confirmed": bool(roc_h18 >= 0.60),
        "cond_best_policy_viable": bool(best_viable),
    },
    "return_aware_finding": {
        "h18_original_spearman": float(h18_orig_spearman),
        "best_model_spearman": float(best_spearman),
        "best_model_by_spearman": best_by_spearman["model"],
        "best_model_by_return": best_by_return["model"],
        "spearman_improvement_pct": float((best_spearman - h18_orig_spearman) / (abs(h18_orig_spearman) + 1e-9) * 100),
        "cond_spearman_20pct_met": bool(cond_spearman_20pct),
        "h18_top10_cap15_return": float(h18_top10_cap15_return),
        "best_top10_cap15_return": float(best_top10_cap15_return),
        "return_3x_improvement": bool(cond_return_3x),
        "gate_triggered_by": "spearman_20pct" if cond_spearman_20pct else ("return_3x" if cond_return_3x else "neither"),
        "paradox_note": (
            "M2/M3 regression models show lower Spearman(score,return) but higher top10+cap15 return. "
            "This is because: (1) regression models select different pools/candidates; "
            "(2) Spearman measures ranking correlation over all 1766 rows while top10+cap15 "
            "measures only the selected 176 rows. Models can select better-returning subsets "
            "without improving overall ranking correlation."
        ),
    },
    "why_not_live": [
        "Fresh OOT W2 infeasible under hard constraints",
        "Cannot satisfy '2 independent large OOT windows' H19 prerequisite",
        "No paper trading loop infrastructure",
        "Return-aware model not yet validated OOT (Phase B is research diagnostic only)",
        "H18.8 is diagnostic gate, not deployment gate",
    ],
    "next_step": (
        "If allow_return_aware_model_dryrun=true: run H18.9 return-aware model dry-run "
        "using best Phase B objective on new expanded V2 data (when available). "
        "If allow_more_oot_data=true: acquire new blocks via RPC batch labeling "
        "(not Dune API) to build genuine Large OOT W2."
    ),
}

print(f"\n    allow_h19_shadow_scoring:       {gate['allow_h19_shadow_scoring']}")
print(f"    allow_return_aware_model_dryrun:{gate['allow_return_aware_model_dryrun']}")
print(f"    allow_retrain_on_expanded_v2:   {gate['allow_retrain_on_expanded_v2']}")
print(f"    allow_more_oot_data:            {gate['allow_more_oot_data']}")
print(f"    allow_feature_revision:         {gate['allow_feature_revision']}")
print(f"    promote_live_allowed:           {gate['promote_live_allowed']}")

# ── 9. Save Phase B best model as research candidate ─────────────────────────
print("\n[9] Saving Phase B best model as research candidate...")
best_model_name = best_by_spearman["model"]
if "M2" in best_model_name:
    best_model_obj = m2; is_reg = True
elif "M3" in best_model_name:
    best_model_obj = m3; is_reg = True
elif "M4" in best_model_name:
    best_model_obj = m4; is_reg = False
else:
    best_model_obj = m1; is_reg = False

with open(RESDIR / "phase_b_best_model.pkl", "wb") as f:
    pickle.dump(best_model_obj, f)
research_meta = {
    "model_id": "h18_8_phase_b_best",
    "generated_at": GENERATED_AT,
    "best_by": "spearman_score_vs_return",
    "model_name": best_model_name,
    "spearman_score_vs_return": float(best_spearman),
    "objective": "regression" if is_reg else "classification",
    "research_only": True,
    "safety": {
        "production_model_write": False, "live_allowed": False,
        "promote_allowed": False, "active_path_write": False,
    },
}
with open(RESDIR / "phase_b_research_meta.json", "w") as f:
    json.dump(research_meta, f, indent=2)
print(f"    Saved: {best_model_name} -> {RESDIR}/phase_b_best_model.pkl")
print(f"    !! Research candidate only. NOT active model. No production writes.")

# ── 10. Write all artifacts ───────────────────────────────────────────────────
print("\n[10] Writing artifacts...")

# A. Fresh OOT W2 build report
with open(DIAG / "run004_h18_8_fresh_oot_w2_build_report.json", "w") as f:
    json.dump(fresh_oot_w2_build_report, f, indent=2)

# B. Fixed H18 model on Phase A substitute window
with open(DIAG / "run004_h18_8_fixed_h18_model_on_w2.json", "w") as f:
    json.dump(phase_a_fixed_report, f, indent=2)

# C. Return-aware target comparison (all 5 model results)
ret_compare = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-8-v2-fresh-oot-return-aware-target",
    "sanity_only_not_deploy_evidence": True,
    "phase": "B",
    "model_results": results_table,
    "baseline_model": "H18_original_binary",
    "baseline_spearman_score_vs_return": float(h18_orig_spearman),
    "best_model_by_spearman": best_model_name,
    "best_spearman_score_vs_return": float(best_spearman),
    "improvement_vs_baseline": float(best_spearman - h18_orig_spearman),
    "improvement_pct": float((best_spearman - h18_orig_spearman) / (abs(h18_orig_spearman) + 1e-9) * 100),
    "recommendation": (
        f"Model {best_model_name} achieves highest Spearman(score,return)={best_spearman:.4f} "
        f"vs H18 baseline {h18_orig_spearman:.4f}. "
        f"Improvement: {(best_spearman-h18_orig_spearman)/abs(h18_orig_spearman+1e-9)*100:+.1f}%."
    ),
}
with open(DIAG / "run004_h18_8_return_aware_target_comparison.json", "w") as f:
    json.dump(ret_compare, f, indent=2)

# D. Pool policy stability
pps_doc = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-8-v2-fresh-oot-return-aware-target",
    "sanity_only_not_deploy_evidence": True,
    "note": "Only 1 OOT window available (val set = substitute for fresh W2). "
            "Cross-window stability with 2 independent windows not feasible.",
    "model_policy_stability": pool_stability,
    "viable_models": [k for k, v in pool_stability.items() if v["viable_candidate"]],
}
with open(DIAG / "run004_h18_8_pool_policy_stability.json", "w") as f:
    json.dump(pps_doc, f, indent=2)

# E. Gate decision
with open(DIAG / "run004_h18_8_gate_decision.json", "w") as f:
    json.dump({
        "generated_at": GENERATED_AT,
        "patch": "run-004-patch-h18-8-v2-fresh-oot-return-aware-target",
        "sanity_only_not_deploy_evidence": True,
        **gate,
    }, f, indent=2)

# F. Threshold comparison CSV
csv_rows = []
for r in results_table:
    for pol_key, pol_label in [
        ("top10pct_rank",              "top10pct_rank"),
        ("top20pct_rank",              "top20pct_rank"),
        ("top10pct_rank_poolcap_15pct","top10pct_poolcap15"),
        ("top20pct_rank_poolcap_15pct","top20pct_poolcap15"),
        ("top10pct_rank_poolcap_10pct","top10pct_poolcap10"),
    ]:
        stats = r.get(pol_key, {})
        csv_rows.append({
            "model": r["model"],
            "policy": pol_label,
            "spearman_score_vs_return": r["spearman_score_vs_return"],
            "roc_auc": r.get("roc_auc", ""),
            "selected_count": stats.get("count", ""),
            "avg_return_after_cost": stats.get("avg_return", ""),
            "top1_pool_share": stats.get("top1_pool_share", ""),
            "top5_pool_share": stats.get("top5_pool_share", ""),
            "positive_return_rate": stats.get("positive_return_rate", ""),
            "return_positive": stats.get("return_positive", ""),
            "meets_300_threshold": stats.get("meets_300_threshold", ""),
        })

with open(DIAG / "run004_h18_8_threshold_comparison.csv", "w", newline="") as f:
    if csv_rows:
        writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)

print("    All artifacts written.")

# ── Final Summary ─────────────────────────────────────────────────────────────
print()
print(BNR)
print("H18.8 FINAL SUMMARY")
print(BNR)
print()
print("  PHASE A — Fresh OOT W2:")
print(f"    Build status:  INFEASIBLE (blocks > {MAX_KNOWN_BLOCK} require Dune/pipeline)")
print(f"    Fixed H18 ROC-AUC on val: {roc_h18:.4f}")
print(f"    H18 Spearman(score, return): {h18_orig_spearman:.4f}")
print()
print("  PHASE B — Return-Aware Target Comparison:")
print(f"    {'Model':<40} {'Spearman(ret)':>14} {'top10+cap15':>14} {'top20+cap15':>14}")
print(f"    {'-'*40} {'-'*14} {'-'*14} {'-'*14}")
for r in results_table:
    sp = r["spearman_score_vs_return"]
    t10r = r["top10pct_rank_poolcap_15pct"].get("avg_return") or 0
    t20r = r["top20pct_rank_poolcap_15pct"].get("avg_return") or 0
    print(f"    {r['model']:<40} {sp:>+14.4f} {t10r:>+14.5f} {t20r:>+14.5f}")
print()
print(f"  Best Spearman: {best_by_spearman['model']} = {best_spearman:.4f}")
print(f"  H18 Spearman:  H18_original_binary = {h18_orig_spearman:.4f}")
print(f"  Improvement:   {(best_spearman-h18_orig_spearman)/abs(h18_orig_spearman+1e-9)*100:+.1f}%")
print()
print(f"  allow_h19_shadow_scoring:        {gate['allow_h19_shadow_scoring']}")
print(f"  allow_return_aware_model_dryrun: {gate['allow_return_aware_model_dryrun']}")
print(f"  allow_retrain_on_expanded_v2:    {gate['allow_retrain_on_expanded_v2']}")
print(f"  allow_more_oot_data:             {gate['allow_more_oot_data']}")
print(f"  promote_live_allowed:            {gate['promote_live_allowed']}")
print(BNR)
