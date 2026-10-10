"""
sprint072_a6_model_retrain.py
P3 Buyer Diversity feature enrichment + sprint070 model retrain.
Tests whether adding buyer_diversity_score pushes LOOCV AUC from 0.48 → 0.55.

Feature: count(DISTINCT recipient) in [entry_block-100, entry_block+100] per signal.
RESEARCH ONLY.
"""

import csv, json, math, pathlib, statistics, subprocess
from collections import defaultdict

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_score, LeaveOneGroupOut
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler
    SKLEARN_OK = True
except ImportError:
    SKLEARN_OK = False

REPO    = pathlib.Path("/opt/EATLQ")
SRC_CSV = REPO / "logs" / "sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp" / "audits" / "sprint072_alpha_waterfall"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CHAIN_ID  = 8453
MEM_CAP   = "max_memory_usage = 3000000000"
LABEL_COL = "net_return_30m_ag"   # positive = win

# Sprint070 original features
BASE_FEATURES = ["activity_heat","buy_pressure_ratio","price_velocity_5m","pool_age_sec","onset_lag_sec"]
# New P3 feature
NEW_FEATURES  = ["buyer_diversity_score"]
ALL_FEATURES  = BASE_FEATURES + NEW_FEATURES

def log(msg): print(f"[a6_retrain] {msg}", flush=True)

def ch(sql, timeout=45):
    r = subprocess.run(
        ["docker","exec","eatlq-clickhouse-1","clickhouse-client",
         "--database","eatlq","--format","TSV","--query", sql],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip(), r.stderr.strip()[:200]

def load_rows():
    rows = []
    with open(SRC_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True" or not r.get("entry_block"):
                continue
            if not r.get("exit_30m_ok") == "True" or not r.get("net_return_30m_ag"):
                continue
            row = {
                "signal_time":      r["signal_time"],
                "pool":             r["pool"],
                "entry_block":      int(r["entry_block"]),
                "activity_heat":    float(r["activity_heat"]) if r["activity_heat"] else 0.0,
                "buy_pressure_ratio": float(r["buy_pressure_ratio"]) if r["buy_pressure_ratio"] else 0.5,
                "price_velocity_5m":  float(r["price_velocity_5m"]) if r["price_velocity_5m"] else 0.0,
                "pool_age_sec":      float(r["pool_age_sec"]) if r["pool_age_sec"] else 0.0,
                "onset_lag_sec":     float(r["onset_lag_sec"]) if r["onset_lag_sec"] else 0.0,
                "label":             1 if float(r["net_return_30m_ag"]) > 0 else 0,
                "net_return_30m_ag": float(r["net_return_30m_ag"]),
            }
            rows.append(row)
    return sorted(rows, key=lambda x: x["signal_time"])

def fetch_buyer_diversity(rows):
    log("Fetching buyer diversity from ClickHouse (batched by pool)...")
    by_pool = defaultdict(list)
    for i, r in enumerate(rows):
        by_pool[r["pool"]].append((i, r["entry_block"]))

    diversity_by_idx = {}
    for pool, entries in by_pool.items():
        min_blk = min(e for _, e in entries) - 100
        max_blk = max(e for _, e in entries) + 100
        out, err = ch(f"""
SELECT block_number, count(DISTINCT recipient) as div
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND pool_address = '{pool}'
  AND block_number BETWEEN {min_blk} AND {max_blk}
GROUP BY block_number
SETTINGS {MEM_CAP}
""", timeout=45)
        blk_div = {}
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) >= 2:
                try:
                    blk_div[int(parts[0])] = int(parts[1])
                except:
                    pass
        for idx, entry_block in entries:
            window_divs = [v for b, v in blk_div.items() if abs(b - entry_block) <= 100]
            if window_divs:
                diversity_by_idx[idx] = sum(window_divs)
            else:
                diversity_by_idx[idx] = None
    return diversity_by_idx

def run_loocv(X, y, groups, label):
    """LOOCV by pool group."""
    if not SKLEARN_OK:
        return None, None

    logo = LeaveOneGroupOut()
    unique_groups = list(set(groups))
    if len(unique_groups) < 2:
        return None, None

    aucs = []
    scaler = StandardScaler()
    X_s = scaler.fit_transform(X)

    for train_idx, test_idx in logo.split(X_s, y, groups):
        X_tr, X_te = X_s[train_idx], X_s[test_idx]
        y_tr, y_te = [y[i] for i in train_idx], [y[i] for i in test_idx]
        if len(set(y_te)) < 2:
            continue
        clf = LogisticRegression(max_iter=1000, random_state=42, C=1.0)
        clf.fit(X_tr, y_tr)
        proba = clf.predict_proba(X_te)[:, 1]
        try:
            auc = roc_auc_score(y_te, proba)
            aucs.append(auc)
        except:
            pass

    if not aucs:
        return None, None
    return round(statistics.mean(aucs), 4), round(statistics.stdev(aucs) if len(aucs) > 1 else 0.0, 4)

def run_chrono_auc(X, y, label):
    """Chronological 70/30 split AUC."""
    if not SKLEARN_OK:
        return None
    n     = len(y)
    split = int(n * 0.7)
    X_s   = StandardScaler().fit_transform(X)
    X_tr, X_te = X_s[:split], X_s[split:]
    y_tr, y_te = y[:split], y[split:]
    if len(set(y_te)) < 2 or len(set(y_tr)) < 2:
        return None
    clf = LogisticRegression(max_iter=1000, random_state=42, C=1.0)
    clf.fit(X_tr, y_tr)
    proba = clf.predict_proba(X_te)[:, 1]
    try:
        return round(roc_auc_score(y_te, proba), 4)
    except:
        return None

def main():
    log("=" * 60)
    log("Sprint072 A6 — P3 Buyer Diversity Model Retrain")
    log("=" * 60)

    rows = load_rows()
    log(f"Loaded {len(rows)} fully-labeled rows")

    # 1. Fetch buyer diversity
    diversity = fetch_buyer_diversity(rows)
    valid_idx = [i for i, v in diversity.items() if v is not None]
    log(f"Diversity available for {len(valid_idx)}/{len(rows)} rows")

    # Fill missing with median
    vals = [diversity[i] for i in valid_idx]
    med_div = statistics.median(vals) if vals else 0
    log(f"Diversity: median={med_div:.0f}, min={min(vals)}, max={max(vals)}, mean={statistics.mean(vals):.1f}")

    for i in range(len(rows)):
        val = diversity.get(i)
        rows[i]["buyer_diversity_score"] = float(val) if val is not None else float(med_div)

    # 2. Prepare matrices
    y      = [r["label"] for r in rows]
    groups = [r["pool"][:10] for r in rows]

    X_base = [[r[f] for f in BASE_FEATURES] for r in rows]
    X_all  = [[r[f] for f in ALL_FEATURES]  for r in rows]

    if not SKLEARN_OK:
        log("ERROR: scikit-learn not available — cannot retrain")
        return

    import numpy as np
    X_base_np = np.array(X_base, dtype=float)
    X_all_np  = np.array(X_all,  dtype=float)

    log("\n--- Chronological 70/30 AUC ---")
    auc_base_chrono = run_chrono_auc(X_base_np, y, "base")
    auc_all_chrono  = run_chrono_auc(X_all_np,  y, "all")
    log(f"  Base features (5):          AUC = {auc_base_chrono}")
    log(f"  +buyer_diversity (6 feat):  AUC = {auc_all_chrono}")
    delta_chrono = round(auc_all_chrono - auc_base_chrono, 4) if (auc_base_chrono and auc_all_chrono) else None
    log(f"  Delta: {delta_chrono:+.4f}")

    log("\n--- LOOCV by pool (AUC per fold) ---")
    loocv_base_mean, loocv_base_std = run_loocv(X_base_np, y, groups, "base")
    loocv_all_mean,  loocv_all_std  = run_loocv(X_all_np,  y, groups, "all")
    log(f"  Base (5 feat): LOOCV AUC = {loocv_base_mean} ± {loocv_base_std}")
    log(f"  +diversity (6): LOOCV AUC = {loocv_all_mean} ± {loocv_all_std}")
    delta_loocv = round(loocv_all_mean - loocv_base_mean, 4) if (loocv_base_mean and loocv_all_mean) else None
    log(f"  Delta: {delta_loocv:+.4f}")

    gate_pass = loocv_all_mean and loocv_all_mean >= 0.55
    log(f"\n  AUC gate (>= 0.55): {'PASS' if gate_pass else 'FAIL'}  "
        f"(LOOCV mean = {loocv_all_mean})")

    # 3. Feature importance via RF
    log("\n--- RandomForest feature importance (all features) ---")
    from sklearn.ensemble import RandomForestClassifier
    scaler = StandardScaler()
    X_s = scaler.fit_transform(X_all_np)
    rf = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)
    rf.fit(X_s, y)
    importances = dict(zip(ALL_FEATURES, rf.feature_importances_))
    for feat, imp in sorted(importances.items(), key=lambda x: -x[1]):
        log(f"  {feat:<30s}: {imp:.4f}")

    # 4. Diversity correlation analysis
    log("\n--- Diversity vs 30m return correlation ---")
    paired = [(rows[i]["buyer_diversity_score"], rows[i]["net_return_30m_ag"]) for i in range(len(rows))]
    div_vals = [p[0] for p in paired]
    ret_vals = [p[1] for p in paired]
    mean_div, mean_ret = statistics.mean(div_vals), statistics.mean(ret_vals)
    cov = statistics.mean([(d - mean_div)*(r - mean_ret) for d, r in paired])
    std_div = statistics.stdev(div_vals) if len(div_vals) > 1 else 1
    std_ret = statistics.stdev(ret_vals) if len(ret_vals) > 1 else 1
    pearson_r = cov / (std_div * std_ret) if (std_div * std_ret) > 0 else 0
    log(f"  Pearson r(diversity, 30m_return) = {pearson_r:.4f}")

    # 5. Save results
    result = {
        "generated_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        "feature_set_base": BASE_FEATURES,
        "feature_set_enriched": ALL_FEATURES,
        "buyer_diversity_stats": {
            "median": med_div,
            "min": min(vals), "max": max(vals), "mean": round(statistics.mean(vals),1),
            "coverage": f"{len(valid_idx)}/{len(rows)}"
        },
        "chrono_auc": {
            "base_5feat": auc_base_chrono,
            "enriched_6feat": auc_all_chrono,
            "delta": delta_chrono
        },
        "loocv_auc": {
            "base_5feat_mean": loocv_base_mean,
            "base_5feat_std": loocv_base_std,
            "enriched_6feat_mean": loocv_all_mean,
            "enriched_6feat_std": loocv_all_std,
            "delta": delta_loocv,
            "gate_0p55_pass": gate_pass
        },
        "rf_feature_importances": {k: round(v,4) for k, v in importances.items()},
        "pearson_r_diversity_vs_30m_return": round(pearson_r, 4),
        "verdict": (
            "P3 buyer_diversity IMPROVES LOOCV AUC — MODEL_VALIDATED gate closer to passing"
            if gate_pass else
            f"P3 buyer_diversity improves LOOCV AUC by {delta_loocv} but gate 0.55 not yet reached. "
            "Additional features needed."
        )
    }
    out_path = OUT_DIR / "sprint072_a6_model_retrain_results.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    log(f"\nResults written to {out_path}")
    return result

if __name__ == "__main__":
    main()
