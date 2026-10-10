"""
Run-004 Patch G3 — Merge: Feature-Enriched Canonical Dataset Builder.

Merges on-chain microstructure features from the full RPC fetch into the
canonical V2-only dataset, then adds G2-style engineered features.

Inputs:
  logs/run004_v2only_prepared_dryrun_canonical.csv   (16,644 rows, immutable)
  logs/run004_patch_g3_rpc_features_full.csv         (from full RPC fetch)

Outputs:
  logs/run004_v2only_prepared_dryrun_feature_enriched.csv
  mvp/diagnostics/run004_patch_g3_feature_enriched_dataset_report.json
  mvp/runs/run-004-patch-g3-feature-enriched-dataset.md

Hard prohibitions:
  - No RPC calls
  - No LIVE_TRADING_MODE
  - No label modification
  - No pool_address/token_address in model feature list
  - No overwrite of labeled_dataset.csv or canonical.csv
  - No target leakage (exit_block, label, net_return, relabel_status excluded from features)
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on",
), "LIVE_TRADING_MODE must not be enabled"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CANONICAL_CSV = REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"
RPC_FULL_CSV = REPO / "logs" / "run004_patch_g3_rpc_features_full.csv"
OUT_ENRICHED_CSV = REPO / "logs" / "run004_v2only_prepared_dryrun_feature_enriched.csv"
OUT_REPORT_JSON = REPO / "mvp" / "diagnostics" / "run004_patch_g3_feature_enriched_dataset_report.json"
OUT_MD = REPO / "mvp" / "runs" / "run-004-patch-g3-feature-enriched-dataset.md"

# Guard: never overwrite the canonical dataset
assert str(OUT_ENRICHED_CSV) != str(CANONICAL_CSV), "Output must differ from canonical"

HOLDOUT_LO_MS = int(os.environ.get("RUN004_G3_HOLDOUT_LO_MS", "1776520584003"))

# ---------------------------------------------------------------------------
# Feature classification
# ---------------------------------------------------------------------------

# Raw features coming from the RPC fetch CSV
RPC_FEATURES_ALLOWED: list[str] = [
    "reserve_weth_at_entry",
    "reserve_token_at_entry",
    "log_reserve_weth",
    "reserve_ratio",
    "swap_size_vs_reserve",
    "liquidity_to_trade_ratio",
    "price_impact_estimate_at_notional",
    "block_gas_used",
    "block_gas_limit",
    "block_gas_fill_ratio",
    "block_tx_count",
    "is_new_pool_7d",
    "is_new_pool_30d",
    "pair_created_block",        # diagnostics only (often null, not a model feature)
    "pool_age_blocks_at_entry",  # only non-null when pair_created_block available
    "pool_age_sec_at_entry",
]

# Fetch status columns (diagnostics only, not model features)
RPC_DIAGNOSTICS_COLS: list[str] = [
    "weth_is_token0", "pool_has_weth",
    "fetch_ok_group_a", "fetch_ok_group_b", "fetch_ok_group_c", "fetch_error",
]

# G2-style engineered features derived from canonical columns only
ENGINEERED_FEATURES: list[str] = [
    "log_index_log1p",
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

# Columns passed through as metadata (not model features)
METADATA_COLS: list[str] = [
    "timestamp", "pool_address", "token_address", "pool_type",
    "topic0", "event_kind", "tx_hash", "log_index",
    "source_row_id", "candidate_status", "reject_reason", "gate_status",
    "label", "net_return", "label_reason",
    "entry_block",   # metadata for feature provenance; banned as model feature
    "exit_block",    # future leakage — metadata only
    "pre_check", "relabel_status", "rpc_calls_this_key",
    "final_score",   # base allowed invariant feature
]

# Complete ordered output columns
OUTPUT_COLS: list[str] = (
    METADATA_COLS
    + RPC_FEATURES_ALLOWED
    + ENGINEERED_FEATURES
    + RPC_DIAGNOSTICS_COLS
)

# Model-usable features (allowed pool-invariant)
MODEL_FEATURES: list[str] = (
    ["final_score"]
    + RPC_FEATURES_ALLOWED[:-3]   # exclude pair_created_block, pool_age_blocks, pool_age_sec_at_entry (partial)
    + ["pool_age_sec_at_entry"]    # include if non-null; use carefully
    + ENGINEERED_FEATURES
)

# Leakage-banned features (must NOT appear in MODEL_FEATURES)
LEAKAGE_BANNED: list[str] = [
    "exit_block", "label", "net_return", "label_reason", "relabel_status",
    "entry_block", "pool_address", "token_address", "source_row_id",
    "timestamp",
]


# ---------------------------------------------------------------------------
# Engineering helpers
# ---------------------------------------------------------------------------
def _safe_float(v: str) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _logit(p: float) -> float:
    p = max(1e-9, min(1 - 1e-9, p))
    return math.log(p / (1 - p))


def _compute_engineered(row: dict, fs_ranks: dict, li_ranks: dict) -> dict:
    """Compute G2-style engineered features for a single row."""
    fs = _safe_float(row.get("final_score", ""))
    li = _safe_float(row.get("log_index", ""))

    eng: dict = {k: "" for k in ENGINEERED_FEATURES}
    if fs is None or li is None:
        return eng

    li_log1p = math.log1p(li)
    fs_logit = _logit(fs)
    fs_rank = fs_ranks.get(id(row), 0.5)
    li_rank = li_ranks.get(id(row), 0.5)
    fs_sq = fs ** 2
    fs_neg = 1.0 - fs
    li_sq_log1p = math.log1p(li ** 2)

    eng["log_index_log1p"] = str(round(li_log1p, 8))
    eng["final_score_logit"] = str(round(fs_logit, 8))
    eng["final_score_rank_pct"] = str(round(fs_rank, 6))
    eng["final_score_sq"] = str(round(fs_sq, 8))
    eng["fs_neg"] = str(round(fs_neg, 8))
    eng["is_low_fs_p10"] = str(int(fs_rank < 0.10))
    eng["is_low_fs_p25"] = str(int(fs_rank < 0.25))
    eng["log_index_rank_pct"] = str(round(li_rank, 6))
    eng["log_index_sq_log1p"] = str(round(li_sq_log1p, 8))
    eng["is_low_li_p25"] = str(int(li_rank < 0.25))
    eng["fs_x_li_log1p"] = str(round(fs * li_log1p, 8))
    eng["fs_logit_x_li_log1p"] = str(round(fs_logit * li_log1p, 8))
    eng["fs_neg_x_li_log1p"] = str(round(fs_neg * li_log1p, 8))
    return eng


def _build_rank_maps(rows: list) -> tuple[dict, dict]:
    """Build percentile rank maps for final_score and log_index."""
    fs_vals = [(float(r.get("final_score", "0") or "0"), i) for i, r in enumerate(rows)]
    li_vals = [(float(r.get("log_index", "0") or "0"), i) for i, r in enumerate(rows)]

    n = len(rows)
    fs_sorted = sorted(range(n), key=lambda i: fs_vals[i][0])
    li_sorted = sorted(range(n), key=lambda i: li_vals[i][0])

    fs_ranks = {}
    li_ranks = {}
    for rank, idx in enumerate(fs_sorted):
        fs_ranks[id(rows[idx])] = rank / max(n - 1, 1)
    for rank, idx in enumerate(li_sorted):
        li_ranks[id(rows[idx])] = rank / max(n - 1, 1)

    return fs_ranks, li_ranks


# ---------------------------------------------------------------------------
# Null rate helpers
# ---------------------------------------------------------------------------
def _null_rate(rows: list, col: str) -> float:
    if not rows:
        return 1.0
    nulls = sum(1 for r in rows if not r.get(col, "").strip())
    return round(nulls / len(rows), 4)


# ---------------------------------------------------------------------------
# Main merge
# ---------------------------------------------------------------------------
def run_merge() -> dict:
    # --- Load canonical ---
    print(f"[INFO] Loading canonical: {CANONICAL_CSV}")
    can_rows: list = []
    with open(CANONICAL_CSV) as f:
        for r in csv.DictReader(f):
            can_rows.append(r)
    print(f"[INFO] Canonical rows: {len(can_rows)}")

    # --- Load RPC features ---
    if not RPC_FULL_CSV.exists():
        return {
            "conclusion": "HOLD_DUE_TO_MERGE_FAILURE",
            "error": f"RPC full fetch CSV not found: {RPC_FULL_CSV}",
        }
    print(f"[INFO] Loading RPC features: {RPC_FULL_CSV}")
    rpc_rows: list = []
    with open(RPC_FULL_CSV) as f:
        for r in csv.DictReader(f):
            rpc_rows.append(r)
    print(f"[INFO] RPC rows: {len(rpc_rows)}")

    # --- Build join index: (tx_hash.lower(), log_index) → rpc_row ---
    rpc_index: dict = {}
    rpc_pool_block_index: dict = {}
    dup_rpc_keys = 0
    for r in rpc_rows:
        tx = r.get("tx_hash", "").lower().strip()
        li = r.get("log_index", "").strip()
        if tx and li:
            key = (tx, li)
            if key in rpc_index:
                dup_rpc_keys += 1
            rpc_index[key] = r
        # Fallback index by (pool_address, entry_block)
        pool = r.get("pool_address", "").lower().strip()
        blk = r.get("entry_block", "").strip()
        if pool and blk:
            rpc_pool_block_index[(pool, blk)] = r

    print(f"[INFO] RPC index built: {len(rpc_index)} tx+logindex keys, "
          f"{dup_rpc_keys} duplicates in RPC CSV")

    # --- Build percentile ranks for engineering ---
    fs_ranks, li_ranks = _build_rank_maps(can_rows)

    # --- Merge ---
    output_rows: list = []
    join_hits_primary = 0
    join_hits_fallback = 0
    join_misses = 0
    label_mutations = 0

    for row in can_rows:
        tx = row.get("tx_hash", "").lower().strip()
        li = row.get("log_index", "").strip()
        pool = row.get("pool_address", "").lower().strip()
        blk = row.get("entry_block", "").strip()

        # Try primary join: (tx_hash, log_index)
        rpc = rpc_index.get((tx, li))
        if rpc:
            join_hits_primary += 1
        else:
            # Fallback: (pool_address, entry_block)
            rpc = rpc_pool_block_index.get((pool, blk))
            if rpc:
                join_hits_fallback += 1
            else:
                join_misses += 1

        # Build output row
        out: dict = {col: row.get(col, "") for col in METADATA_COLS}

        # RPC features (null if no join)
        for col in RPC_FEATURES_ALLOWED:
            out[col] = rpc.get(col, "") if rpc else ""

        # RPC diagnostics
        for col in RPC_DIAGNOSTICS_COLS:
            out[col] = rpc.get(col, "") if rpc else ""

        # Engineered features
        eng = _compute_engineered(row, fs_ranks, li_ranks)
        out.update(eng)

        # Label mutation check
        can_label = row.get("label", "").strip()
        out_label = out.get("label", "").strip()
        if can_label != out_label:
            label_mutations += 1

        output_rows.append(out)

    print(f"[INFO] Join: primary={join_hits_primary} fallback={join_hits_fallback} "
          f"miss={join_misses}")

    # --- Leakage check ---
    leakage_violations = []
    for feat in MODEL_FEATURES:
        if feat in LEAKAGE_BANNED:
            leakage_violations.append(feat)

    # --- Write output ---
    OUT_ENRICHED_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_ENRICHED_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"[INFO] Wrote enriched CSV: {OUT_ENRICHED_CSV} ({len(output_rows)} rows)")

    # --- Null rate analysis ---
    null_rates_rpc = {col: _null_rate(output_rows, col) for col in RPC_FEATURES_ALLOWED}
    null_rates_eng = {col: _null_rate(output_rows, col) for col in ENGINEERED_FEATURES}

    # --- Gates ---
    gate_group_a = null_rates_rpc.get("reserve_weth_at_entry", 1.0) <= 0.03
    gate_group_b = null_rates_rpc.get("block_gas_fill_ratio", 1.0) <= 0.03
    gate_group_c = null_rates_rpc.get("is_new_pool_7d", 1.0) <= 0.03
    gate_row_count = len(output_rows) == len(can_rows)
    gate_no_dup = len(set(
        (r.get("tx_hash", "").lower(), r.get("log_index", ""))
        for r in output_rows
    )) == len(output_rows)
    gate_no_label_mutation = label_mutations == 0
    gate_no_leakage = len(leakage_violations) == 0
    gate_join_rate = (join_hits_primary + join_hits_fallback) / max(len(can_rows), 1) >= 0.95

    all_gates_pass = all([
        gate_group_a, gate_group_b, gate_group_c,
        gate_row_count, gate_no_dup, gate_no_label_mutation,
        gate_no_leakage, gate_join_rate,
    ])

    if leakage_violations:
        conclusion = "HOLD_DUE_TO_FEATURE_LEAKAGE_RISK"
    elif not all_gates_pass:
        failing = []
        if not gate_group_a: failing.append("GROUP_A_NULL")
        if not gate_group_b: failing.append("GROUP_B_NULL")
        if not gate_group_c: failing.append("GROUP_C_NULL")
        if not gate_row_count: failing.append("ROW_COUNT_MISMATCH")
        if not gate_no_dup: failing.append("DUPLICATE_KEYS")
        if not gate_no_label_mutation: failing.append("LABEL_MUTATION")
        if not gate_join_rate: failing.append("LOW_JOIN_RATE")
        conclusion = "HOLD_DUE_TO_MERGE_FAILURE"
    else:
        conclusion = "PATCH_G3_FEATURE_ENRICHED_DATASET_READY"

    report = {
        "patch": "G3-Merge",
        "conclusion": conclusion,
        "merge": {
            "canonical_rows": len(can_rows),
            "rpc_rows": len(rpc_rows),
            "output_rows": len(output_rows),
            "join_hits_primary": join_hits_primary,
            "join_hits_fallback": join_hits_fallback,
            "join_misses": join_misses,
            "join_rate": round((join_hits_primary + join_hits_fallback) / max(len(can_rows), 1), 4),
            "dup_rpc_keys": dup_rpc_keys,
            "label_mutations": label_mutations,
        },
        "gates": {
            "group_a_null_le_3pct": gate_group_a,
            "group_b_null_le_3pct": gate_group_b,
            "group_c_null_le_3pct": gate_group_c,
            "row_count_exact": gate_row_count,
            "no_duplicate_keys": gate_no_dup,
            "no_label_mutation": gate_no_label_mutation,
            "no_leakage_in_model_features": gate_no_leakage,
            "join_rate_ge_95pct": gate_join_rate,
            "all_pass": all_gates_pass,
        },
        "null_rates_rpc": null_rates_rpc,
        "null_rates_engineered": null_rates_eng,
        "feature_classification": {
            "allowed_pool_invariant": MODEL_FEATURES,
            "banned_identity_metadata": ["pool_address", "token_address"],
            "banned_label_future": LEAKAGE_BANNED,
            "diagnostics_only": (
                RPC_DIAGNOSTICS_COLS
                + ["pair_created_block", "pool_age_blocks_at_entry", "entry_block", "exit_block"]
            ),
        },
        "leakage_violations": leakage_violations,
        "g4_retrain_allowed": conclusion == "PATCH_G3_FEATURE_ENRICHED_DATASET_READY",
    }

    return report


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def _write_md(report: dict):
    m = report["merge"]
    g = report["gates"]
    c = report["conclusion"]
    go = c == "PATCH_G3_FEATURE_ENRICHED_DATASET_READY"

    nr_rpc = "\n".join(
        f"| `{k}` | {v:.1%} |"
        for k, v in sorted(report["null_rates_rpc"].items(), key=lambda x: x[1])
    )
    gate_rows = "\n".join(
        f"| {k} | {'PASS' if v else 'FAIL'} |"
        for k, v in g.items() if k != "all_pass"
    )
    model_feats = ", ".join(f"`{f}`" for f in report["feature_classification"]["allowed_pool_invariant"])

    md = f"""# Run-004 Patch G3: Feature-Enriched Dataset Build Report

**Conclusion**: `{c}`  
**G4 Retrain Allowed**: {"YES — feature-enriched dataset is ready" if go else "NO — fix issues first"}

---

## Merge Summary

| Metric | Value |
|--------|-------|
| Canonical rows | {m['canonical_rows']:,} |
| RPC rows loaded | {m['rpc_rows']:,} |
| Output rows | {m['output_rows']:,} |
| Primary join (tx+logindex) | {m['join_hits_primary']:,} |
| Fallback join (pool+block) | {m['join_hits_fallback']:,} |
| Join misses | {m['join_misses']:,} |
| Join rate | {m['join_rate']:.1%} |
| Label mutations | {m['label_mutations']} |
| Duplicate RPC keys | {m['dup_rpc_keys']} |

---

## Gate Results

| Gate | Result |
|------|--------|
{gate_rows}

---

## Null Rates — RPC Features

| Feature | Null Rate |
|---------|-----------|
{nr_rpc}

---

## Feature Classification

**allowed_pool_invariant (model features)**:  
{model_feats}

**banned_identity_metadata**: `pool_address`, `token_address`

**banned_label/future**: `exit_block`, `label`, `net_return`, `label_reason`, `relabel_status`, `entry_block`, `timestamp`

**diagnostics_only**: `pair_created_block`, `pool_age_blocks_at_entry`, `weth_is_token0`, `fetch_ok_*`, `fetch_error`

---

## Leakage Assessment

{"No leakage violations detected." if not report.get('leakage_violations') else "⚠️ Leakage violations: " + ", ".join(report.get('leakage_violations', []))}

---

## Conclusion

**`{c}`**

{"Dataset ready. G4 retrain dry-run is ALLOWED (requires explicit request)." if go else "Dataset not ready. Investigate gate failures before G4."}

### Artifacts
- Enriched CSV: `logs/run004_v2only_prepared_dryrun_feature_enriched.csv`
- Report: `mvp/diagnostics/run004_patch_g3_feature_enriched_dataset_report.json`

### Next Step
{"- G4 retrain dry-run: `scripts/retrain_run004_patch_g4_feature_enriched_dryrun.py` (requires explicit user request)" if go else ""}
"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_MD, "w") as f:
        f.write(md.strip() + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("Run-004 Patch G3 Merge: Feature-Enriched Dataset Builder")
    print("=" * 70)

    if not RPC_FULL_CSV.exists():
        print(f"ERROR: RPC full fetch CSV not found: {RPC_FULL_CSV}")
        print("Run fetch_run004_patch_g3_rpc_features_full.py first.")
        sys.exit(1)

    report = run_merge()

    OUT_REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)

    _write_md(report)

    print()
    print("=" * 70)
    print(f"CONCLUSION: {report['conclusion']}")
    print(f"G4 retrain allowed: {report.get('g4_retrain_allowed', False)}")
    m = report["merge"]
    print(f"Rows: canonical={m['canonical_rows']:,} → output={m['output_rows']:,} | "
          f"join_rate={m['join_rate']:.1%} | misses={m['join_misses']}")
    print(f"Group A null: {report['null_rates_rpc'].get('reserve_weth_at_entry', '?'):.1%} | "
          f"Group B null: {report['null_rates_rpc'].get('block_gas_fill_ratio', '?'):.1%} | "
          f"Group C null: {report['null_rates_rpc'].get('is_new_pool_7d', '?'):.1%}")
    print()
    print("Artifacts:")
    print(f"  {OUT_ENRICHED_CSV}")
    print(f"  {OUT_REPORT_JSON}")
    print(f"  {OUT_MD}")
    print("=" * 70)


if __name__ == "__main__":
    main()
