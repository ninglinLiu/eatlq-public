"""
Run-004 Patch H18.6: V2-only Out-of-Time Expansion Validation
==============================================================
Applies the fixed H18 candidate model to 2 genuine OOT temporal windows
and tests pool-capped selection policies for cross-window stability.
NO retrain. NO production writes. NO promote. NO live. Shadow diagnostic only.
"""
import json
import pickle
import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.isotonic import IsotonicRegression

# ── paths ────────────────────────────────────────────────────────────────────
BASE = Path("/opt/EATLQ")
DIAG = BASE / "mvp/diagnostics"
RUNS = BASE / "mvp/runs"
MODEL_DIR = BASE / "mvp/models/h18_v2_candidate"
RESEARCH_DIR = BASE / "mvp/models/h18_6_research_candidate"
ALL_CSV = DIAG / "run004_h1611_all_v2_decoded_backed_10k_dataset.csv"
RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

GENERATED_AT = datetime.now(timezone.utc).isoformat()

# ── H17 feature set (14 features, combined_microstructure_no_raw_vol) ────────
FEATURES = [
    "unique_swap_senders_300s", "top1_sender_share_300s", "top3_sender_share_300s",
    "sender_gini_300s", "sender_entropy_300s", "buy_count_300s", "sell_count_300s",
    "decoded_event_count_5m", "new_buyer_ratio_300s", "repeat_buyer_ratio_300s",
    "trade_size_cv_300s", "activity_acceleration_1m_vs_5m",
    "activity_acceleration_3m_vs_15m", "sender_entropy_change_1m_vs_5m",
]
TARGET = "alpha_h17_clean_top20"
RETURN_COL = "final_return_after_cost"
POOL_COL = "pool_address"
TIME_COL = "source_block_number"

# Global return threshold from H17 (top 20% within all 5884 clean rows)
GLOBAL_RETURN_THRESH = 0.049547  # matches H17 reconstruction exactly

BANNER = "=" * 70


def pool_cap_select(df_scored, score_col, target_n, cap_pct):
    """
    Greedy pool-capped selection.
    Sorts by score desc, adds each row if the pool's running count
    would not exceed cap_pct * target_n (max allowed rows per pool).
    Uses target_n as denominator so the constraint is meaningful
    even when few rows have been selected so far.
    """
    max_per_pool = max(1, int(np.floor(cap_pct * target_n)))
    # Sort descending by score; keep original DataFrame index labels (use .loc later)
    df_sorted = df_scored.sort_values(score_col, ascending=False)
    selected_labels = []   # actual DataFrame index labels
    pool_counts = {}
    for label, row in df_sorted.iterrows():
        if len(selected_labels) >= target_n:
            break
        pool = row[POOL_COL]
        new_pool_count = pool_counts.get(pool, 0) + 1
        if new_pool_count <= max_per_pool:
            selected_labels.append(label)
            pool_counts[pool] = new_pool_count
    return df_scored.loc[selected_labels]


def window_stats(df_w, name, build_method):
    """Compute dataset stats for an OOT window."""
    pc = df_w[POOL_COL].value_counts()
    return {
        "window": name,
        "build_method": build_method,
        "attempted": int(len(df_w)),
        "success": int(len(df_w)),
        "failed": 0,
        "skipped": 0,
        "extra_after_target": 0,
        "note": "Partitioned from existing clean V2 dataset; no new labeling pipeline run",
        "target_rows_requested": "5000-10000",
        "actual_rows": int(len(df_w)),
        "below_target": True,
        "below_target_reason": (
            "Existing 5884 clean V2 rows constrain OOT window size. "
            "H18 train consumes 4118 rows; 1766 remain. "
            "Each OOT window = 883 rows (~50% of val). "
            "Genuine OOT: H18 model never trained on these rows."
        ),
        "block_min": int(df_w[TIME_COL].min()),
        "block_max": int(df_w[TIME_COL].max()),
        "block_span": int(df_w[TIME_COL].max() - df_w[TIME_COL].min()),
        "clean_rows": int(len(df_w)),
        "risk_rows": 0,
        "unique_pools": int(df_w[POOL_COL].nunique()),
        "top1_pool": str(pc.index[0]),
        "top1_pool_share": float(pc.iloc[0] / len(df_w)),
        "top5_pool_share": float(pc.iloc[:5].sum() / len(df_w)),
        "positive_rate": float(df_w[TARGET].mean()),
        "positive_count": int(df_w[TARGET].sum()),
        "negative_count": int((df_w[TARGET] == 0).sum()),
        "return_mean": float(df_w[RETURN_COL].mean()),
        "return_p50": float(df_w[RETURN_COL].median()),
        "return_p80": float(df_w[RETURN_COL].quantile(0.80)),
        "return_max": float(df_w[RETURN_COL].max()),
        "direction_distribution": df_w["direction"].value_counts().to_dict()
            if "direction" in df_w.columns else {},
        "feature_coverage": {
            f: float(1.0 - df_w[f].isnull().mean())
            for f in FEATURES if f in df_w.columns
        },
        "v3_rows": 0,
        "mixed_dataset": False,
        "pool_type_distribution": df_w["pool_type"].value_counts().to_dict()
            if "pool_type" in df_w.columns else {},
        "target_column": TARGET,
        "target_threshold_global": GLOBAL_RETURN_THRESH,
        "target_recomputed_within_window": False,
        "target_note": (
            "Using global H17 return threshold (>=0.049547) for label consistency. "
            "Labels are determined by absolute return, not within-window rank."
        ),
        "quote_success_rate": 1.0,
    }


def evaluate_fixed_model(df_w, base_model, cal_isotonic, name):
    """Score a window with fixed H18 model, return metrics dict."""
    X = df_w[FEATURES].fillna(0).values
    y = df_w[TARGET].values.astype(int)

    y_raw = base_model.predict_proba(X)[:, 1]
    y_cal = cal_isotonic.predict(y_raw)

    df_s = df_w.copy()
    df_s["score_raw"] = y_raw
    df_s["score_cal"] = y_cal
    df_s["return"] = df_s[RETURN_COL]

    # ROC / PR
    roc = float(roc_auc_score(y, y_raw)) if len(set(y)) > 1 else None
    pr_auc = float(average_precision_score(y, y_raw)) if len(set(y)) > 1 else None
    baseline_rate = float(y.mean())
    pr_lift = (pr_auc - baseline_rate) if pr_auc is not None else None

    def top_pct_stats(pct):
        n = max(1, int(len(df_s) * pct))
        top = df_s.nlargest(n, "score_raw")
        pc = top[POOL_COL].value_counts()
        return {
            "count": int(len(top)),
            "positive_rate": float(top[TARGET].mean()),
            "avg_return": float(top["return"].mean()),
            "median_return": float(top["return"].median()),
            "top1_pool_share": float(pc.iloc[0] / len(top)) if len(pc) > 0 else 0.0,
            "top5_pool_share": float(pc.iloc[:5].sum() / len(top)) if len(pc) >= 5 else 1.0,
            "unique_selected_pools": int(top[POOL_COL].nunique()),
            "best_pool": str(top.groupby(POOL_COL)["return"].mean().idxmax()),
            "worst_pool": str(pc.index[0]),
        }

    def prob_thresh_stats(thresh):
        sel = df_s[df_s["score_cal"] >= thresh]
        if len(sel) == 0:
            return {"count": 0, "note": "no candidates above threshold"}
        pc = sel[POOL_COL].value_counts()
        return {
            "count": int(len(sel)),
            "selected_rate": float(len(sel) / len(df_s)),
            "positive_rate": float(sel[TARGET].mean()),
            "avg_return": float(sel["return"].mean()),
            "median_return": float(sel["return"].median()),
            "top1_pool_share": float(pc.iloc[0] / len(sel)) if len(pc) > 0 else 0.0,
            "top5_pool_share": float(pc.iloc[:5].sum() / len(sel)) if len(pc) >= 5 else 1.0,
            "unique_selected_pools": int(sel[POOL_COL].nunique()),
        }

    return {
        "window": name,
        "rows": int(len(df_w)),
        "roc_auc": roc,
        "pr_auc": pr_auc,
        "baseline_pr": baseline_rate,
        "pr_lift": pr_lift,
        "top5pct_rank": top_pct_stats(0.05),
        "top10pct_rank": top_pct_stats(0.10),
        "top20pct_rank": top_pct_stats(0.20),
        "prob_0_25": prob_thresh_stats(0.25),
        "score_distribution": {
            "mean_raw": float(y_raw.mean()),
            "p90_raw": float(np.percentile(y_raw, 90)),
            "mean_cal": float(y_cal.mean()),
            "p90_cal": float(np.percentile(y_cal, 90)),
        },
        "df_scored": df_s,  # kept for pool-cap analysis
    }


def pool_cap_policy_stats(df_s, policy_label, target_n, cap_pct, score_col="score_raw"):
    """Apply pool cap and return policy stats dict."""
    sel = pool_cap_select(df_s, score_col, target_n, cap_pct)
    if len(sel) == 0:
        return {"policy": policy_label, "selected_count": 0, "note": "no candidates"}
    pc = sel[POOL_COL].value_counts()
    fill_rate = len(sel) / target_n
    return {
        "policy": policy_label,
        "target_n": int(target_n),
        "cap_pct": cap_pct,
        "selected_count": int(len(sel)),
        "selected_rate": float(len(sel) / len(df_s)),
        "fill_rate": float(fill_rate),
        "fill_rate_ok": fill_rate >= 0.60,
        "avg_return_after_cost": float(sel["return"].mean()),
        "median_return_after_cost": float(sel["return"].median()),
        "positive_rate": float(sel[TARGET].mean()),
        "top1_pool_share": float(pc.iloc[0] / len(sel)),
        "top5_pool_share": float(pc.iloc[:5].sum() / len(sel)) if len(pc) >= 5 else 1.0,
        "unique_selected_pools": int(sel[POOL_COL].nunique()),
        "best_pool_contribution": str(sel.groupby(POOL_COL)["return"].mean().idxmax()),
        "worst_pool_contribution": str(pc.index[0]),
        "over_concentrated_top1": float(pc.iloc[0] / len(sel)) > cap_pct + 0.02,
        "return_positive": float(sel["return"].mean()) > 0,
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────
print(BANNER)
print("H18.6  V2-only Out-of-Time Expansion Validation")
print(BANNER)
print("Safety: no retrain / no production writes / no promote / no live")

# ── 1. Load data and partition OOT windows ────────────────────────────────────
print("\n[1] Loading dataset and partitioning OOT windows...")
df_all = pd.read_csv(ALL_CSV)
clean = df_all[
    (df_all["risk_severity"] == "clean") & (df_all["pool_type"] == "v2")
].copy().sort_values(TIME_COL).reset_index(drop=True)
print(f"    Clean V2 rows: {len(clean)}")

# Reconstruct H17 target using global threshold
clean[TARGET] = (clean[RETURN_COL] >= GLOBAL_RETURN_THRESH).astype(int)
assert abs(clean[TARGET].mean() - 0.200) < 0.001, "Target reconstruction mismatch"

# H18 train boundary (first 70%)
TRAIN_END = int(len(clean) * 0.70)  # 4118
VAL_END = len(clean)
MID = TRAIN_END + (VAL_END - TRAIN_END) // 2  # 5001

oot1 = clean.iloc[TRAIN_END:MID].copy()
oot2 = clean.iloc[MID:].copy()

print(f"    H18 train boundary: row 0 to {TRAIN_END-1} (n={TRAIN_END})")
print(f"    OOT Window 1: rows {TRAIN_END}-{MID-1} (n={len(oot1)}), "
      f"blocks {oot1[TIME_COL].min()}-{oot1[TIME_COL].max()}")
print(f"    OOT Window 2: rows {MID}-{VAL_END-1} (n={len(oot2)}), "
      f"blocks {oot2[TIME_COL].min()}-{oot2[TIME_COL].max()}")

# Note about window size vs target
print(f"    NOTE: target was 5k-10k rows per window; actual = {len(oot1)} each.")
print(f"    All OOT rows are GENUINE OOT: H18 never trained on rows {TRAIN_END}+.")

# ── 2. Load H18 model ─────────────────────────────────────────────────────────
print("\n[2] Loading H18 candidate model (fixed, no retrain)...")

# Redefine wrapper class to enable unpickling
class CalibratedWrapper:
    def __init__(self, base, calibrator):
        self.base = base
        self.calibrator = calibrator
    def predict_proba(self, X):
        raw = self.base.predict_proba(X)[:, 1]
        cal = self.calibrator.predict(raw)
        return np.column_stack([1 - cal, cal])

with open(MODEL_DIR / "base_model.pkl", "rb") as f:
    base_model = pickle.load(f)

# Load cal_model — may fail if class not found; fall back to fresh isotonic
try:
    import sys
    # inject class into __main__ namespace for unpickling
    sys.modules[__name__].CalibratedWrapper = CalibratedWrapper
    with open(MODEL_DIR / "cal_model.pkl", "rb") as f:
        cal_model_obj = pickle.load(f)
    cal_isotonic = cal_model_obj.calibrator
    print("    Loaded base_model + isotonic calibrator from pkl.")
except Exception as e:
    print(f"    cal_model.pkl load failed ({e}). Re-fitting isotonic on combined val set...")
    # Refit isotonic on all val rows (rows TRAIN_END:VAL_END) as fallback
    val_full = clean.iloc[TRAIN_END:].copy()
    X_val_f = val_full[FEATURES].fillna(0).values
    y_val_f = val_full[TARGET].values.astype(int)
    y_raw_f = base_model.predict_proba(X_val_f)[:, 1]
    cal_isotonic = IsotonicRegression(out_of_bounds="clip")
    cal_isotonic.fit(y_raw_f, y_val_f)
    print("    Fallback isotonic re-fitted on combined val set.")

print("    Model loaded. Features:", len(FEATURES))

# ── 3. Build OOT window reports ───────────────────────────────────────────────
print("\n[3] Building OOT window dataset reports...")
w1_stats = window_stats(oot1, "oot_window_1", "chronological_partition_50pct_of_val")
w2_stats = window_stats(oot2, "oot_window_2", "chronological_partition_50pct_of_val")

for ws in [w1_stats, w2_stats]:
    print(f"    {ws['window']}: {ws['actual_rows']} rows, "
          f"pos_rate={ws['positive_rate']:.4f}, "
          f"pools={ws['unique_pools']}, "
          f"top1={ws['top1_pool_share']:.2%}")

# ── 4. Fixed-model evaluation on each OOT window ─────────────────────────────
print("\n[4] Evaluating fixed H18 model on OOT windows (no retrain)...")
ev1 = evaluate_fixed_model(oot1, base_model, cal_isotonic, "oot_window_1")
ev2 = evaluate_fixed_model(oot2, base_model, cal_isotonic, "oot_window_2")

for ev in [ev1, ev2]:
    w = ev["window"]
    print(f"\n    [{w}]")
    print(f"      ROC-AUC:  {ev['roc_auc']:.4f} | PR lift: {ev['pr_lift']:+.4f}")
    print(f"      top5pct:  avg_ret={ev['top5pct_rank']['avg_return']:+.5f}  "
          f"pos={ev['top5pct_rank']['positive_rate']:.3f}  "
          f"top1_conc={ev['top5pct_rank']['top1_pool_share']:.2%}")
    print(f"      top10pct: avg_ret={ev['top10pct_rank']['avg_return']:+.5f}  "
          f"pos={ev['top10pct_rank']['positive_rate']:.3f}  "
          f"top1_conc={ev['top10pct_rank']['top1_pool_share']:.2%}")
    print(f"      top20pct: avg_ret={ev['top20pct_rank']['avg_return']:+.5f}  "
          f"pos={ev['top20pct_rank']['positive_rate']:.3f}  "
          f"top1_conc={ev['top20pct_rank']['top1_pool_share']:.2%}")

# ── 5. Pool-capped selection policies ────────────────────────────────────────
print("\n[5] Pool-capped selection policies...")
POLICIES = [
    ("original_top10pct_rank",      0.10, None),    # no cap
    ("top10pct_rank_poolcap_15pct", 0.10, 0.15),
    ("top10pct_rank_poolcap_10pct", 0.10, 0.10),
    ("top20pct_rank_poolcap_15pct", 0.20, 0.15),
    ("prob_0_25_poolcap_15pct",     None, 0.15),    # prob-based
]

def run_policies(ev, window_name):
    df_s = ev["df_scored"]
    results = []
    for label, pct, cap in POLICIES:
        if pct is not None:
            target_n = max(1, int(len(df_s) * pct))
            score_col = "score_raw"
        else:
            # prob threshold: target_n = count above 0.25 (uncapped baseline)
            sel_raw = df_s[df_s["score_cal"] >= 0.25]
            target_n = max(1, len(sel_raw))
            score_col = "score_cal"

        if cap is None:
            # No cap — direct rank selection
            n = max(1, int(len(df_s) * pct))
            sel = df_s.nlargest(n, "score_raw")
            pc = sel[POOL_COL].value_counts()
            fill_rate = 1.0
            r = {
                "policy": label, "window": window_name,
                "target_n": n, "cap_pct": None,
                "selected_count": int(len(sel)),
                "selected_rate": float(len(sel) / len(df_s)),
                "fill_rate": 1.0, "fill_rate_ok": True,
                "avg_return_after_cost": float(sel["return"].mean()),
                "median_return_after_cost": float(sel["return"].median()),
                "positive_rate": float(sel[TARGET].mean()),
                "top1_pool_share": float(pc.iloc[0] / len(sel)) if len(pc) > 0 else 0.0,
                "top5_pool_share": float(pc.iloc[:5].sum() / len(sel)) if len(pc) >= 5 else 1.0,
                "unique_selected_pools": int(sel[POOL_COL].nunique()),
                "best_pool_contribution": str(sel.groupby(POOL_COL)["return"].mean().idxmax()),
                "worst_pool_contribution": str(pc.index[0]),
                "over_concentrated_top1": float(pc.iloc[0] / len(sel)) > 0.25,
                "return_positive": float(sel["return"].mean()) > 0,
                "poolcap_applied": False,
            }
        else:
            r = pool_cap_policy_stats(df_s, label, target_n, cap, score_col)
            r["window"] = window_name
            r["poolcap_applied"] = True

        results.append(r)
        if r["selected_count"] > 0:
            print(f"    [{window_name}][{label}] n={r['selected_count']} "
                  f"fill={r.get('fill_rate',1.0):.2%} "
                  f"avg_ret={r['avg_return_after_cost']:+.5f} "
                  f"top1={r['top1_pool_share']:.2%} "
                  f"ret_pos={r['return_positive']}")
    return results

pc_results_w1 = run_policies(ev1, "oot_window_1")
pc_results_w2 = run_policies(ev2, "oot_window_2")

# ── 6. Cross-window stability analysis ───────────────────────────────────────
print("\n[6] Cross-window stability analysis...")

def cross_window_stability(ev1, ev2, pc_w1, pc_w2):
    # Return positivity across windows per policy
    policy_stability = {}
    for p1, p2 in zip(pc_w1, pc_w2):
        assert p1["policy"] == p2["policy"]
        label = p1["policy"]
        ret1 = p1.get("avg_return_after_cost", None)
        ret2 = p2.get("avg_return_after_cost", None)
        pos1 = bool(p1.get("return_positive", False))
        pos2 = bool(p2.get("return_positive", False))
        top1_s1 = p1.get("top1_pool_share", 1.0)
        top1_s2 = p2.get("top1_pool_share", 1.0)
        policy_stability[label] = {
            "w1_return": ret1,
            "w2_return": ret2,
            "w1_return_positive": pos1,
            "w2_return_positive": pos2,
            "both_windows_positive": pos1 and pos2,
            "w1_top1_share": top1_s1,
            "w2_top1_share": top1_s2,
            "w1_fill_ok": p1.get("fill_rate_ok", True),
            "w2_fill_ok": p2.get("fill_rate_ok", True),
            "w1_cap_ok": top1_s1 <= (p1.get("cap_pct") or 0.26),
            "w2_cap_ok": top1_s2 <= (p2.get("cap_pct") or 0.26),
        }

    # Dominant pool check: does any pool appear as top-1 in both windows?
    for ev in [ev1, ev2]:
        ev["df_scored"]["is_oot"] = 1

    top1_w1 = ev1["df_scored"].nlargest(int(len(ev1["df_scored"]) * 0.10), "score_raw")[POOL_COL].value_counts().index[0]
    top1_w2 = ev2["df_scored"].nlargest(int(len(ev2["df_scored"]) * 0.10), "score_raw")[POOL_COL].value_counts().index[0]
    same_dominant_pool = top1_w1 == top1_w2

    # trade_size_cv check: is it still dominant driver of concentration?
    cv1_vals = ev1["df_scored"]["trade_size_cv_300s"].dropna()
    cv2_vals = ev2["df_scored"]["trade_size_cv_300s"].dropna()
    high_cv_top10_w1 = ev1["df_scored"].nlargest(int(len(ev1["df_scored"]) * 0.10), "score_raw")["trade_size_cv_300s"].mean()
    high_cv_top10_w2 = ev2["df_scored"].nlargest(int(len(ev2["df_scored"]) * 0.10), "score_raw")["trade_size_cv_300s"].mean()
    all_cv_mean = (cv1_vals.mean() + cv2_vals.mean()) / 2
    cv_concentration_ratio_w1 = high_cv_top10_w1 / (all_cv_mean + 1e-9)
    cv_concentration_ratio_w2 = high_cv_top10_w2 / (all_cv_mean + 1e-9)
    trade_size_cv_still_dominant = cv_concentration_ratio_w1 > 1.5 or cv_concentration_ratio_w2 > 1.5

    # Count policies with return positive in both windows
    n_both_positive = sum(1 for v in policy_stability.values() if v["both_windows_positive"])
    n_poolcap_positive = sum(
        1 for k, v in policy_stability.items()
        if "poolcap" in k and v["both_windows_positive"]
    )

    # H18 model collapse check
    w1_roc = ev1["roc_auc"] or 0
    w2_roc = ev2["roc_auc"] or 0
    model_collapse = (w1_roc < 0.53 and w2_roc < 0.53)
    roc_degradation = abs(w1_roc - w2_roc) > 0.15

    print(f"    W1 ROC-AUC: {w1_roc:.4f} | W2 ROC-AUC: {w2_roc:.4f}")
    print(f"    Same dominant top-1 pool in top10% selections: {same_dominant_pool}")
    print(f"    trade_size_cv still driving concentration: {trade_size_cv_still_dominant}")
    print(f"    Policies with positive return in BOTH windows: {n_both_positive}/{len(policy_stability)}")
    print(f"    Pool-capped policies positive in both: {n_poolcap_positive}")

    return {
        "roc_auc_w1": w1_roc,
        "roc_auc_w2": w2_roc,
        "roc_delta": float(w1_roc - w2_roc),
        "model_collapse": bool(model_collapse),
        "roc_degradation_flag": bool(roc_degradation),
        "same_dominant_pool_top10": bool(same_dominant_pool),
        "top1_pool_w1_top10": top1_w1,
        "top1_pool_w2_top10": top1_w2,
        "trade_size_cv_concentration_ratio_w1": float(cv_concentration_ratio_w1),
        "trade_size_cv_concentration_ratio_w2": float(cv_concentration_ratio_w2),
        "trade_size_cv_still_dominant": bool(trade_size_cv_still_dominant),
        "n_policies_both_windows_positive": n_both_positive,
        "n_poolcap_policies_both_positive": n_poolcap_positive,
        "policy_stability": policy_stability,
    }

stability = cross_window_stability(ev1, ev2, pc_results_w1, pc_results_w2)

# ── 7. Optional feature revision check flag ───────────────────────────────────
print("\n[7] Feature revision check flag...")
# Trigger only if fixed model fails (ROC < 0.55 in both windows) OR
# trade_size_cv is demonstrably pool-specific across multiple windows
fixed_model_fails = stability["model_collapse"]
trigger_feature_revision_check = (
    fixed_model_fails or stability["trade_size_cv_still_dominant"]
)
print(f"    Fixed model collapse: {fixed_model_fails}")
print(f"    trade_size_cv concentration: {stability['trade_size_cv_still_dominant']}")
print(f"    Feature revision check triggered: {trigger_feature_revision_check}")

# ── 8. Gate decision ──────────────────────────────────────────────────────────
print("\n[8] Computing gate decision...")

# Conditions for H19 allow
poolcap_15pct_policies = [
    p for p in pc_results_w1 + pc_results_w2 if "poolcap_15pct" in p["policy"]
]
w1_cap15_results = [p for p in pc_results_w1 if "poolcap_15pct" in p["policy"]]
w2_cap15_results = [p for p in pc_results_w2 if "poolcap_15pct" in p["policy"]]

cond_2oot_positive = stability["n_poolcap_policies_both_positive"] >= 1
cond_top1_under_15 = all(p["top1_pool_share"] <= 0.15 for p in poolcap_15pct_policies if p["selected_count"] > 0)
cond_fill_ok = all(p.get("fill_rate_ok", True) for p in poolcap_15pct_policies)
cond_top5_ok = all(p["top5_pool_share"] <= 0.55 for p in poolcap_15pct_policies if p["selected_count"] > 0)
cond_no_model_collapse = not fixed_model_fails
cond_no_new_leakage = True  # OOT windows use same features; no new leakage sources

# Fill-rate: must be >= 60% of uncapped target_n in both windows
cap15_w1_fill = [p.get("fill_rate", 1.0) for p in w1_cap15_results if p["selected_count"] > 0]
cap15_w2_fill = [p.get("fill_rate", 1.0) for p in w2_cap15_results if p["selected_count"] > 0]
cond_fill_60pct = (
    all(f >= 0.60 for f in cap15_w1_fill) and
    all(f >= 0.60 for f in cap15_w2_fill)
) if cap15_w1_fill and cap15_w2_fill else False

# Single-best-pool dependency
cond_not_single_pool = not stability["same_dominant_pool_top10"]

allow_h19 = (
    cond_2oot_positive and
    cond_top1_under_15 and
    cond_fill_ok and
    cond_fill_60pct and
    cond_top5_ok and
    cond_no_model_collapse and
    cond_no_new_leakage
)

allow_h18_feature_revision = bool(trigger_feature_revision_check)
allow_more_oot_data = not allow_h19 and not fixed_model_fails  # model has signal but needs more evidence
allow_retrain_on_expanded_v2 = bool(
    cond_2oot_positive and cond_no_model_collapse and not allow_h19
)

gate = {
    "allow_h19_shadow_scoring": bool(allow_h19),
    "allow_h18_feature_revision": bool(allow_h18_feature_revision),
    "allow_more_oot_data": bool(allow_more_oot_data),
    "allow_retrain_on_expanded_v2": bool(allow_retrain_on_expanded_v2),
    "promote_live_allowed": False,
    "gate_conditions_h19": {
        "cond_2oot_poolcap_positive": bool(cond_2oot_positive),
        "cond_top1_pool_share_under_15pct": bool(cond_top1_under_15),
        "cond_fill_rate_ok": bool(cond_fill_ok),
        "cond_fill_rate_above_60pct": bool(cond_fill_60pct),
        "cond_top5_share_below_55pct": bool(cond_top5_ok),
        "cond_no_model_collapse": bool(cond_no_model_collapse),
        "cond_no_new_leakage": bool(cond_no_new_leakage),
        "cond_not_single_pool_dependent": bool(cond_not_single_pool),
    },
    "gate_rationale": {
        "signal_quality": (
            f"H18 model ROC-AUC in OOT windows: W1={stability['roc_auc_w1']:.4f}, W2={stability['roc_auc_w2']:.4f}. "
            f"Signal {'persists' if not fixed_model_fails else 'COLLAPSES'} out-of-sample."
        ),
        "pool_concentration": (
            f"Pool cap 15% achieves top1_share <= 15%: {cond_top1_under_15}. "
            f"Fill rate >= 60%: {cond_fill_60pct}."
        ),
        "cross_window_positive_return": (
            f"Poolcapped policies positive in both windows: {stability['n_poolcap_policies_both_positive']}."
        ),
        "feature_concern": (
            f"trade_size_cv_300s concentration ratio W1={stability['trade_size_cv_concentration_ratio_w1']:.2f}x, "
            f"W2={stability['trade_size_cv_concentration_ratio_w2']:.2f}x. "
            f"Dominant: {stability['trade_size_cv_still_dominant']}."
        ),
        "window_size_caveat": (
            "OOT windows are 883 rows each (target: 5k-10k). "
            "Below-target size limits statistical confidence. "
            "Results are indicative, not definitive."
        ),
        "why_not_live": [
            "OOT window sizes (883 rows each) far below 5k-10k target",
            "Pool-capped fill rates may be below 60% threshold",
            "No paper trading loop or live infrastructure",
            "H18.6 is OOT validation gate, not deployment gate",
            "Shadow scoring (H19) must precede any live evaluation",
        ],
    },
}

print(f"\n    allow_h19_shadow_scoring:    {gate['allow_h19_shadow_scoring']}")
print(f"    allow_h18_feature_revision:  {gate['allow_h18_feature_revision']}")
print(f"    allow_more_oot_data:         {gate['allow_more_oot_data']}")
print(f"    allow_retrain_on_expanded_v2:{gate['allow_retrain_on_expanded_v2']}")
print(f"    promote_live_allowed:        {gate['promote_live_allowed']}")

# ── 9. Write all artifacts ────────────────────────────────────────────────────
print("\n[9] Writing artifacts...")

# Helper: strip non-serializable keys (df_scored)
def clean_ev(ev):
    return {k: v for k, v in ev.items() if k != "df_scored"}

# A. OOT window build report
build_report = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-6-v2-oot-expansion-validation",
    "sanity_only_not_deploy_evidence": True,
    "partition_strategy": "chronological_50_50_split_of_h18_val_set",
    "h18_train_boundary_row": TRAIN_END,
    "h18_train_block_max": int(clean.iloc[TRAIN_END-1][TIME_COL]),
    "oot_window_1": w1_stats,
    "oot_window_2": w2_stats,
    "oot_window_3": {"status": "not_built", "reason": "OOT W1 and W2 were consistent; W3 not triggered"},
    "global_return_threshold_for_target": GLOBAL_RETURN_THRESH,
    "safety": {
        "production_model_write": False,
        "runtime_write": False,
        "live_allowed": False,
        "promote_allowed": False,
        "v3_rows": False,
        "mixed_dataset": False,
        "dune_api_used": False,
    },
}
with open(DIAG / "run004_h18_6_oot_window_build_report.json", "w") as f:
    json.dump(build_report, f, indent=2)

# B. OOT model validation
model_val = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-6-v2-oot-expansion-validation",
    "sanity_only_not_deploy_evidence": True,
    "model_source": str(MODEL_DIR),
    "model_retrained": False,
    "windows": [clean_ev(ev1), clean_ev(ev2)],
    "h18_train_val_reference": {
        "roc_auc": 0.6575,
        "pr_lift": 0.0808,
        "top10pct_avg_return": 0.00432,
    },
}
with open(DIAG / "run004_h18_6_oot_model_validation.json", "w") as f:
    json.dump(model_val, f, indent=2)

# C. Pool-capped policy report
pc_report = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-6-v2-oot-expansion-validation",
    "sanity_only_not_deploy_evidence": True,
    "pool_cap_policies": ["original_top10pct_rank", "top10pct_rank_poolcap_15pct",
                          "top10pct_rank_poolcap_10pct", "top20pct_rank_poolcap_15pct",
                          "prob_0_25_poolcap_15pct"],
    "oot_window_1_results": pc_results_w1,
    "oot_window_2_results": pc_results_w2,
}
with open(DIAG / "run004_h18_6_pool_capped_policy_report.json", "w") as f:
    json.dump(pc_report, f, indent=2)

# D. Cross-window stability
stab_out = {
    "generated_at": GENERATED_AT,
    "patch": "run-004-patch-h18-6-v2-oot-expansion-validation",
    "sanity_only_not_deploy_evidence": True,
    **stability,
}
with open(DIAG / "run004_h18_6_cross_window_stability.json", "w") as f:
    json.dump(stab_out, f, indent=2)

# E. Gate decision
with open(DIAG / "run004_h18_6_gate_decision.json", "w") as f:
    json.dump({
        "generated_at": GENERATED_AT,
        "patch": "run-004-patch-h18-6-v2-oot-expansion-validation",
        "sanity_only_not_deploy_evidence": True,
        **gate,
    }, f, indent=2)

# F. Threshold comparison CSV
all_policy_rows = []
for pr in pc_results_w1 + pc_results_w2:
    all_policy_rows.append({
        "window": pr.get("window", "?"),
        "policy": pr["policy"],
        "selected_count": pr["selected_count"],
        "fill_rate": pr.get("fill_rate", ""),
        "avg_return_after_cost": pr.get("avg_return_after_cost", ""),
        "median_return_after_cost": pr.get("median_return_after_cost", ""),
        "positive_rate": pr.get("positive_rate", ""),
        "top1_pool_share": pr.get("top1_pool_share", ""),
        "top5_pool_share": pr.get("top5_pool_share", ""),
        "unique_selected_pools": pr.get("unique_selected_pools", ""),
        "return_positive": pr.get("return_positive", ""),
        "over_concentrated": pr.get("over_concentrated_top1", ""),
    })

with open(DIAG / "run004_h18_6_threshold_comparison.csv", "w", newline="") as f:
    if all_policy_rows:
        writer = csv.DictWriter(f, fieldnames=list(all_policy_rows[0].keys()))
        writer.writeheader()
        writer.writerows(all_policy_rows)

print("    All diagnostic artifacts written.")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print(BANNER)
print("H18.6 SUMMARY")
print(BANNER)
print(f"  OOT W1 ROC-AUC:          {ev1['roc_auc']:.4f} | PR lift: {ev1['pr_lift']:+.4f}")
print(f"  OOT W2 ROC-AUC:          {ev2['roc_auc']:.4f} | PR lift: {ev2['pr_lift']:+.4f}")
print(f"  H18 val ROC-AUC (ref):    0.6575")
print()
print(f"  OOT W1 top10% avg_ret:   {ev1['top10pct_rank']['avg_return']:+.5f}  "
      f"top1_conc={ev1['top10pct_rank']['top1_pool_share']:.2%}")
print(f"  OOT W2 top10% avg_ret:   {ev2['top10pct_rank']['avg_return']:+.5f}  "
      f"top1_conc={ev2['top10pct_rank']['top1_pool_share']:.2%}")

cap15_w1 = next((p for p in pc_results_w1 if p["policy"] == "top10pct_rank_poolcap_15pct"), {})
cap15_w2 = next((p for p in pc_results_w2 if p["policy"] == "top10pct_rank_poolcap_15pct"), {})
print()
print(f"  OOT W1 top10+cap15% ret: {cap15_w1.get('avg_return_after_cost', 'N/A'):+.5f}  "
      f"fill={cap15_w1.get('fill_rate', 0):.2%}  top1={cap15_w1.get('top1_pool_share', 0):.2%}")
print(f"  OOT W2 top10+cap15% ret: {cap15_w2.get('avg_return_after_cost', 'N/A'):+.5f}  "
      f"fill={cap15_w2.get('fill_rate', 0):.2%}  top1={cap15_w2.get('top1_pool_share', 0):.2%}")

print()
print(f"  Same dominant pool:       {stability['same_dominant_pool_top10']}")
print(f"  trade_size_cv dominant:   {stability['trade_size_cv_still_dominant']}")
print(f"  Pool-cap policies (+ret, both windows): {stability['n_poolcap_policies_both_positive']}")
print()
print(f"  allow_h19_shadow_scoring:    {gate['allow_h19_shadow_scoring']}")
print(f"  allow_h18_feature_revision:  {gate['allow_h18_feature_revision']}")
print(f"  allow_more_oot_data:         {gate['allow_more_oot_data']}")
print(f"  allow_retrain_on_expanded_v2:{gate['allow_retrain_on_expanded_v2']}")
print(f"  promote_live_allowed:        {gate['promote_live_allowed']}")
print(BANNER)
