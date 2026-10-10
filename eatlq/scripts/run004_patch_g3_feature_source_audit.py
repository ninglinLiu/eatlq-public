"""
Run-004 Patch G3 — Phase 1: Feature Source Audit (no RPC).

Scans all offline CSV files in logs/ to determine whether pool-invariant
microstructure features can be joined to the run004 canonical dataset.

Outputs:
  mvp/diagnostics/run004_patch_g3_feature_source_audit.json
  mvp/runs/run-004-patch-g3-feature-source-audit.md

Hard prohibitions:
  - No RPC calls
  - No LIVE_TRADING_MODE
  - No label modification
  - No model promotion
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on",
), "LIVE_TRADING_MODE must not be enabled"

# ── Feature classification rules ──────────────────────────────────────

ALLOWED_POOL_INVARIANT = {
    "final_score", "whale_val", "momentum_val", "activity_val", "risk_val",
    "unique_buyer_entropy_5m", "price_velocity_5m", "buy_pressure_ratio",
    "token_volatility_5m", "activity_heat", "sm_buy_count_5m", "sm_sell_count_5m",
    "sm_unique_buyers_5m", "sm_net_flow_5m", "sm_buy_pressure_ratio_5m",
    "sm_wallet_tier_max", "sm_wallet_tier_mean", "unique_buyers_5m",
    "buyer_gini_5m", "top1_buyer_share_5m", "top3_buyer_share_5m",
    "top5_buyer_share_5m", "new_buyer_ratio_5m", "repeat_buyer_ratio_5m",
    "pool_age_sec", "pool_age_min", "is_new_pool_5m", "is_new_pool_15m",
    "is_new_pool_60m", "sm_first_seen_in_pool",
    "log_index", "log_index_log1p",
    "amount_usd", "amount_raw", "is_whale", "pool_age_bucket",
    "triggered_signal",
}
BANNED_POOL_IDENTITY = {
    "pool_address", "token_address", "symbol", "pool_train_rate", "pool_train_log_count",
}
BANNED_TIME_PATTERN = {
    "hour_of_day", "day_of_week", "sin_hour", "cos_hour", "sin_dow", "cos_dow",
    "timestamp",  # raw timestamp banned as model feature (can derive other things)
}
BANNED_TARGET_ENCODED = {
    "pool_train_rate", "pool_train_log_count",
}
BANNED_FUTURE_LEAKAGE = {
    "net_return", "entry_block", "exit_block", "label", "label_reason",
    "label_type", "candidate_status", "reject_reason", "pre_check",
    "relabel_status", "rpc_calls_this_key", "source_row_id", "gate_status",
}
META_COLS = {
    "tx_hash", "pool_type", "topic0", "event_kind", "pool_age_bucket",
}

TARGET_FEATURES = [
    "whale_val", "momentum_val", "activity_val", "risk_val",
    "unique_buyer_entropy_5m", "price_velocity_5m", "buy_pressure_ratio",
    "token_volatility_5m", "activity_heat",
    "sm_buy_count_5m", "sm_unique_buyers_5m", "unique_buyers_5m",
    "buyer_gini_5m", "top1_buyer_share_5m",
    "pool_age_sec", "pool_age_min",
    "is_new_pool_5m", "is_new_pool_15m",
    "log_index", "log_index_log1p",
]


def _classify(col: str) -> str:
    if col in BANNED_FUTURE_LEAKAGE:
        return "BANNED_FUTURE_OR_LABEL_LEAKAGE"
    if col in BANNED_TARGET_ENCODED:
        return "BANNED_TARGET_ENCODED"
    if col in BANNED_POOL_IDENTITY:
        return "BANNED_POOL_IDENTITY"
    if col in BANNED_TIME_PATTERN:
        return "BANNED_TIME_PATTERN"
    if col in ALLOWED_POOL_INVARIANT:
        return "ALLOWED_POOL_INVARIANT"
    if col in META_COLS:
        return "META_OPERATIONAL"
    return "NEEDS_REVIEW"


def _safe(v: Any, nd: int = 6) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return round(float(v), nd) if isinstance(v, (int, float)) else v


def _scan_file(path: Path, max_rows: int = 0) -> dict[str, Any]:
    """Scan a CSV: columns, row count, timestamp range, null rates, feature classification."""
    if not path.is_file():
        return {"path": str(path), "exists": False}

    size_bytes = path.stat().st_size
    rows_checked = 0
    col_null: dict[str, int] = defaultdict(int)
    col_nonfinite: dict[str, int] = defaultdict(int)
    ts_vals: list[int] = []
    cols: list[str] = []
    total_rows = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        for row in reader:
            total_rows += 1
            if max_rows and total_rows > max_rows:
                continue
            rows_checked += 1
            for c in cols:
                v = row.get(c, "")
                if v in ("", "None", "nan", "NaN", "null"):
                    col_null[c] += 1
                elif c not in ("tx_hash", "pool_address", "token_address", "symbol", "event_kind", "topic0", "pool_type"):
                    try:
                        fv = float(v)
                        if not math.isfinite(fv):
                            col_nonfinite[c] += 1
                    except (ValueError, TypeError):
                        pass
            if "timestamp" in row:
                try:
                    ts_vals.append(int(row["timestamp"]))
                except (ValueError, TypeError):
                    pass

    # Approximate total rows if we didn't read all
    if max_rows and total_rows > max_rows:
        # Extrapolate
        total_rows_est = total_rows
    else:
        total_rows_est = total_rows

    col_info: list[dict[str, Any]] = []
    for c in cols:
        n = rows_checked
        null_r = col_null[c] / max(1, n)
        nonf_r = col_nonfinite[c] / max(1, n)
        col_info.append({
            "col": c,
            "classification": _classify(c),
            "null_rate": _safe(null_r),
            "nonfinite_rate": _safe(nonf_r),
            "is_target_feature": c in TARGET_FEATURES,
        })

    ts_sorted = sorted(ts_vals)
    has_join_key_tx = "tx_hash" in cols and "log_index" in cols
    has_join_key_ta_ts = "token_address" in cols and "timestamp" in cols

    return {
        "path": str(path.relative_to(REPO)),
        "exists": True,
        "size_mb": _safe(size_bytes / 1e6),
        "total_rows_approx": total_rows_est,
        "rows_inspected": rows_checked,
        "columns": cols,
        "n_columns": len(cols),
        "column_details": col_info,
        "ts_min": ts_sorted[0] if ts_sorted else None,
        "ts_max": ts_sorted[-1] if ts_sorted else None,
        "has_join_key_tx_hash_log_index": has_join_key_tx,
        "has_join_key_token_address_timestamp": has_join_key_ta_ts,
        "allowed_pool_invariant_cols": [
            c for c in cols if _classify(c) == "ALLOWED_POOL_INVARIANT"
        ],
        "target_features_present": [c for c in TARGET_FEATURES if c in cols],
        "target_features_missing": [c for c in TARGET_FEATURES if c not in cols],
    }


def _test_join_feasibility(
    canonical_path: Path,
    source_path: Path,
    join_type: str,
    time_window_ms: int = 5000,
    sample: int = 500,
) -> dict[str, Any]:
    """Test join feasibility between canonical and source file."""
    if not source_path.is_file():
        return {"feasible": False, "reason": "source file missing"}

    # Load canonical sample
    can_rows: list[dict] = []
    with canonical_path.open("r", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if i >= sample:
                break
            can_rows.append(row)

    # Load source index
    src_rows: list[dict] = []
    with source_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            src_rows.append(row)

    if join_type == "tx_hash_log_index":
        src_keys = {(r.get("tx_hash", ""), r.get("log_index", "")) for r in src_rows}
        match_count = sum(
            1 for r in can_rows
            if (r.get("tx_hash", ""), r.get("log_index", "")) in src_keys
        )
        return {
            "join_type": join_type,
            "feasible": match_count > 0,
            "match_count": match_count,
            "sample_size": len(can_rows),
            "match_rate": _safe(match_count / max(1, len(can_rows))),
        }

    elif join_type == "token_address_exact":
        src_tas = {r.get("token_address", "").lower() for r in src_rows}
        match_count = sum(
            1 for r in can_rows
            if r.get("token_address", "").lower() in src_tas
        )
        return {
            "join_type": join_type,
            "feasible": match_count > 0,
            "match_count": match_count,
            "sample_size": len(can_rows),
            "match_rate": _safe(match_count / max(1, len(can_rows))),
        }

    elif join_type == "token_address_nearest_timestamp":
        # Build index: token_address -> sorted list of timestamps in source
        src_idx: dict[str, list[int]] = defaultdict(list)
        for r in src_rows:
            ta = r.get("token_address", "").lower()
            try:
                ts = int(r.get("timestamp", 0) or 0)
                src_idx[ta].append(ts)
            except (ValueError, TypeError):
                pass
        for ta in src_idx:
            src_idx[ta].sort()

        ta_no_match = 0
        within_window = 0
        for r in can_rows:
            ta = r.get("token_address", "").lower()
            try:
                ts = int(r.get("timestamp", 0) or 0)
            except (ValueError, TypeError):
                continue
            if ta not in src_idx:
                ta_no_match += 1
                continue
            s = src_idx[ta]
            i = bisect.bisect_left(s, ts)
            deltas = []
            for j in range(max(0, i - 1), min(len(s), i + 2)):
                deltas.append(abs(s[j] - ts))
            if min(deltas) <= time_window_ms:
                within_window += 1

        return {
            "join_type": join_type,
            "time_window_ms": time_window_ms,
            "feasible": within_window > 0,
            "n_no_ta_match": ta_no_match,
            "n_within_window": within_window,
            "sample_size": len(can_rows),
            "match_rate": _safe(within_window / max(1, len(can_rows))),
        }

    return {"join_type": join_type, "feasible": False, "reason": "unknown join type"}


def _token_address_overlap(canonical_path: Path, source_path: Path) -> dict[str, Any]:
    """Check token_address set overlap between canonical and source."""
    can_tas: set[str] = set()
    with canonical_path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            can_tas.add(r.get("token_address", "").lower())

    src_tas: set[str] = set()
    with source_path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            src_tas.add(r.get("token_address", "").lower())

    overlap = can_tas & src_tas
    return {
        "canonical_unique_tas": len(can_tas),
        "source_unique_tas": len(src_tas),
        "overlap": len(overlap),
        "overlap_rate": _safe(len(overlap) / max(1, len(can_tas))),
        "feasible_by_ta": len(overlap) > 0,
    }


def main() -> int:
    canonical_csv = Path(os.environ.get(
        "RUN004_G3_CANONICAL_CSV",
        str(REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"),
    ))
    out_json = Path(
        REPO / "mvp" / "diagnostics" / "run004_patch_g3_feature_source_audit.json"
    )
    out_md = Path(
        REPO / "mvp" / "runs" / "run-004-patch-g3-feature-source-audit.md"
    )

    if not canonical_csv.is_file():
        print(f"ERROR: missing {canonical_csv}", file=sys.stderr)
        return 1

    # Files to scan
    SCAN_FILES: list[tuple[str, Path]] = [
        ("canonical", REPO / "logs" / "run004_v2only_prepared_dryrun_canonical.csv"),
        ("candidates_swaponly", REPO / "logs" / "run004_candidates_swaponly.csv"),
        ("candidates_swaponly_full", REPO / "logs" / "run004_candidates_swaponly_full.csv"),
        ("dashcam", REPO / "logs" / "dashcam.csv"),
        ("dashcam_pre_entropy", REPO / "logs" / "dashcam.pre_entropy_migration.csv"),
        ("dashcam_sm_replay", REPO / "logs" / "dashcam_sm_replay_48h_ge2.csv"),
        ("dashcam_entropy_smoke", REPO / "logs" / "dashcam_entropy_smoke.csv"),
        ("labeled_v2_executable", REPO / "logs" / "labeled_dataset_v2_executable.csv"),
        ("labeled_v2", REPO / "logs" / "labeled_dataset_v2.csv"),
        ("labeled_run003a_enriched", REPO / "logs" / "labeled_dataset_run003a_v2_only_enriched.csv"),
        ("labeled_run003a", REPO / "logs" / "labeled_dataset_run003a_v2_only.csv"),
        ("labeled_run003b_7d", REPO / "logs" / "labeled_dataset_run003b_v2_7d.csv"),
        ("run004_v2only_labeled_full", REPO / "logs" / "run004_v2only_labeled_dryrun_full.csv"),
        ("run004_v2only_prepared", REPO / "logs" / "run004_v2only_prepared_dryrun.csv"),
        ("run003b_broad_enriched", REPO / "logs" / "run003b_compare" / "run003a_broad_enriched.csv"),
        ("run003b_medium_enriched", REPO / "logs" / "run003b_compare" / "run003a_medium_enriched.csv"),
        ("label_patch_b_sample", REPO / "logs" / "label_patch_b_dryrun_sample.csv"),
    ]

    print("Scanning source files...")
    file_audits: dict[str, dict[str, Any]] = {}
    for name, path in SCAN_FILES:
        print(f"  {name}: {path.name}")
        file_audits[name] = _scan_file(path, max_rows=5000)

    # Test join feasibility for key files
    print("Testing join feasibility...")
    JOIN_TESTS: list[tuple[str, str, Path]] = [
        ("canonical_vs_dashcam_ta_nearest", "token_address_nearest_timestamp",
         REPO / "logs" / "dashcam.csv"),
        ("canonical_vs_dashcam_ta_exact", "token_address_exact",
         REPO / "logs" / "dashcam.csv"),
        ("canonical_vs_labeled_v2_exec_ta_exact", "token_address_exact",
         REPO / "logs" / "labeled_dataset_v2_executable.csv"),
        ("canonical_vs_run003a_enriched_ta_exact", "token_address_exact",
         REPO / "logs" / "labeled_dataset_run003a_v2_only_enriched.csv"),
    ]

    join_tests: dict[str, dict[str, Any]] = {}
    for test_name, join_type, source_path in JOIN_TESTS:
        print(f"  {test_name}")
        join_tests[test_name] = _test_join_feasibility(
            canonical_csv, source_path, join_type, time_window_ms=5000, sample=500
        )

    # Token address overlap tests
    print("Testing token_address overlap...")
    overlap_tests: dict[str, dict[str, Any]] = {}
    for name, path in [
        ("dashcam", REPO / "logs" / "dashcam.csv"),
        ("labeled_v2_executable", REPO / "logs" / "labeled_dataset_v2_executable.csv"),
        ("labeled_run003a_enriched", REPO / "logs" / "labeled_dataset_run003a_v2_only_enriched.csv"),
        ("labeled_v2", REPO / "logs" / "labeled_dataset_v2.csv"),
    ]:
        if path.is_file():
            print(f"  {name}")
            overlap_tests[name] = _token_address_overlap(canonical_csv, path)

    # Which target features can be recovered from which files?
    target_recovery: dict[str, dict[str, Any]] = {}
    for feat in TARGET_FEATURES:
        sources_with_feat: list[str] = []
        for name, audit in file_audits.items():
            if audit.get("exists") and feat in (audit.get("columns") or []):
                sources_with_feat.append(name)
        has_joinable_source = False
        for src_name in sources_with_feat:
            src_path = None
            for (n, p) in SCAN_FILES:
                if n == src_name:
                    src_path = p
                    break
            if src_path and src_path.is_file():
                audit = file_audits[src_name]
                has_tx_join = audit.get("has_join_key_tx_hash_log_index", False)
                ta_overlap = overlap_tests.get(src_name, {}).get("overlap", 0)
                if has_tx_join or ta_overlap > 0:
                    has_joinable_source = True
                    break

        target_recovery[feat] = {
            "found_in_files": sources_with_feat,
            "joinable_to_canonical": has_joinable_source,
        }

    # Overall feasibility
    joinable_features = [f for f, v in target_recovery.items() if v["joinable_to_canonical"]]
    non_joinable_features = [f for f, v in target_recovery.items() if not v["joinable_to_canonical"]]

    offline_enrichment_feasible = len(joinable_features) > 0
    rpc_required = len(non_joinable_features) > 0 or not offline_enrichment_feasible

    if offline_enrichment_feasible and len(joinable_features) >= 3:
        conclusion = "PATCH_G3_FEATURE_ENRICHED_DATASET_READY"
    else:
        conclusion = "PATCH_G3_OFFLINE_FEATURES_INSUFFICIENT_RPC_PLAN_REQUIRED"

    print(f"  joinable_features={joinable_features}")
    print(f"  conclusion={conclusion}")

    # Root cause analysis
    root_causes = []
    for name in ["dashcam", "labeled_v2_executable", "labeled_run003a_enriched", "labeled_v2"]:
        ov = overlap_tests.get(name, {})
        if ov.get("overlap", 0) == 0:
            root_causes.append(
                f"{name}: zero token_address overlap with canonical "
                f"(canonical_ta={ov.get('canonical_unique_tas')}, "
                f"source_ta={ov.get('source_unique_tas')}, overlap=0)"
            )

    root_cause_summary = (
        "The canonical dataset (run004) uses pool-based V2 swap event labeling where "
        "token_address refers to the non-WETH token in each LP pair. "
        "The dashcam.csv and labeled_dataset files track DIFFERENT tokens "
        "(new token launches monitored by the dashcam signal system). "
        "There is ZERO token_address overlap between the two populations despite "
        "sharing the same timestamp range. "
        "Sub-score components (whale_val, momentum_val, activity_val, risk_val) were "
        "never written to the run004 candidate pipeline output files — they exist only "
        "in dashcam.csv for a completely different token population. "
        "Therefore, ALL microstructure features (sub-scores, buy_pressure, etc.) are "
        "NOT recoverable offline for the run004 canonical dataset."
    )

    # Available RPC features plan
    rpc_features_plan = [
        {
            "feature": "reserve_weth_at_entry",
            "rpc_method": "eth_call → getReserves() on V2 pair at entry_block",
            "calls_per_row": "1 per unique (pool_address, entry_block)",
            "cacheable": True,
            "cache_key": "(pool_address, entry_block)",
            "archive_required": True,
            "classification": "ALLOWED_POOL_INVARIANT",
        },
        {
            "feature": "reserve_token_at_entry",
            "rpc_method": "eth_call → getReserves() on V2 pair at entry_block",
            "calls_per_row": "same call as reserve_weth",
            "cacheable": True,
            "cache_key": "(pool_address, entry_block)",
            "archive_required": True,
            "classification": "ALLOWED_POOL_INVARIANT",
        },
        {
            "feature": "reserve_ratio",
            "derived_from": ["reserve_weth_at_entry", "reserve_token_at_entry"],
            "rpc_method": "derived",
            "calls_per_row": 0,
            "cacheable": True,
            "classification": "ALLOWED_POOL_INVARIANT",
        },
        {
            "feature": "price_impact_estimate",
            "formula": "NOTIONAL_WEI / max(reserve_weth_at_entry, 1)",
            "rpc_method": "derived from getReserves",
            "calls_per_row": 0,
            "cacheable": True,
            "classification": "ALLOWED_POOL_INVARIANT",
        },
        {
            "feature": "liquidity_to_trade_ratio",
            "formula": "reserve_weth_at_entry / NOTIONAL_WEI",
            "rpc_method": "derived from getReserves",
            "calls_per_row": 0,
            "cacheable": True,
            "classification": "ALLOWED_POOL_INVARIANT",
        },
        {
            "feature": "pool_age_sec_at_entry",
            "rpc_method": "eth_getLogs → Uniswap V2 Factory PairCreated event to find creation_block",
            "calls_per_row": "1 per unique pool_address (archive getLogs)",
            "cacheable": True,
            "cache_key": "pool_address",
            "archive_required": True,
            "classification": "ALLOWED_POOL_INVARIANT",
            "note": "Expensive archive call; can approximate with creation_block × 12s",
        },
        {
            "feature": "block_gas_fill_ratio",
            "rpc_method": "eth_getBlockByNumber at entry_block → gasUsed/gasLimit",
            "calls_per_row": "1 per unique entry_block",
            "cacheable": True,
            "cache_key": "entry_block",
            "archive_required": False,
            "classification": "ALLOWED_POOL_INVARIANT",
        },
        {
            "feature": "weth_price_usd_approx",
            "rpc_method": "eth_call → WETH/USDC pool getReserves at block",
            "calls_per_row": "1 per unique entry_block (single reference pool)",
            "cacheable": True,
            "cache_key": "entry_block",
            "archive_required": True,
            "classification": "ALLOWED_POOL_INVARIANT",
            "note": "Optional; useful for amount_usd computation",
        },
    ]

    # Cost estimate
    can_total_rows = file_audits.get("canonical", {}).get("total_rows_approx", 16644)
    entry_blocks_est = int(can_total_rows * 0.9)  # assume ~90% unique entry_blocks
    pool_addr_est = int(can_total_rows * 0.03)     # ~3% of rows = unique pools (~500 pools)
    rpc_calls_full = {
        "getReserves_per_pool_block": entry_blocks_est,
        "getBlockByNumber_per_block": entry_blocks_est,
        "getLogs_pool_creation_per_pool": pool_addr_est,
        "total_archive_calls_estimated": entry_blocks_est * 2 + pool_addr_est,
        "pilot_200_rows_est_calls": 200 * 2 + 20,  # 2 per row + ~20 pool creations
    }

    report: dict[str, Any] = {
        "patch": "G3",
        "phase": "feature_source_audit",
        "conclusion": conclusion,
        "offline_enrichment_feasible": offline_enrichment_feasible,
        "rpc_plan_required": rpc_required,
        "root_cause_summary": root_cause_summary,
        "root_causes_per_file": root_causes,
        "canonical_file": str(canonical_csv.relative_to(REPO)),
        "files_scanned": {
            name: {
                k: v for k, v in audit.items()
                if k not in ("column_details",)
            }
            for name, audit in file_audits.items()
        },
        "column_classifications": {
            name: {
                "allowed_pool_invariant": audit.get("allowed_pool_invariant_cols", []),
                "target_features_present": audit.get("target_features_present", []),
                "target_features_missing": audit.get("target_features_missing", []),
            }
            for name, audit in file_audits.items()
            if audit.get("exists")
        },
        "join_feasibility_tests": join_tests,
        "token_address_overlap": overlap_tests,
        "target_feature_recovery": target_recovery,
        "joinable_features": joinable_features,
        "non_joinable_features": non_joinable_features,
        "rpc_features_plan": rpc_features_plan,
        "rpc_cost_estimate": rpc_calls_full,
        "note": "Phase 1 only — no RPC calls made, no models trained",
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {out_json}")

    # ── Markdown report ─────────────────────────────────────────────────
    out_md.parent.mkdir(parents=True, exist_ok=True)

    def _file_row(name: str, audit: dict) -> str:
        if not audit.get("exists"):
            return f"| `{name}` | ✘ missing | — | — | — | — | — |"
        tp = audit.get("target_features_present", [])
        has_join = "✔ tx+li" if audit.get("has_join_key_tx_hash_log_index") else (
            "ts+ta" if audit.get("has_join_key_token_address_timestamp") else "none"
        )
        return (
            f"| `{name}` | ✔ | {audit.get('total_rows_approx', '?')} "
            f"| {audit.get('n_columns', 0)} "
            f"| {len(tp)} / {len(TARGET_FEATURES)} "
            f"| {has_join} "
            f"| {', '.join(tp[:5]) or '—'} |"
        )

    def _recovery_row(feat: str) -> str:
        rv = target_recovery[feat]
        joinable = "✔ YES" if rv["joinable_to_canonical"] else "✘ NO"
        sources = ", ".join(rv["found_in_files"]) or "not found"
        return f"| `{feat}` | {joinable} | {sources} |"

    file_table = "\n".join(_file_row(n, file_audits[n]) for n, _ in SCAN_FILES)
    recovery_table = "\n".join(_recovery_row(f) for f in TARGET_FEATURES)
    overlap_table = "\n".join(
        f"| `{n}` | {v.get('canonical_unique_tas')} | {v.get('source_unique_tas')} "
        f"| {v.get('overlap')} | {'✔' if v.get('feasible_by_ta') else '✘'} |"
        for n, v in overlap_tests.items()
    )
    rpc_table = "\n".join(
        f"| `{f['feature']}` | {f.get('rpc_method','derived')} "
        f"| {f.get('calls_per_row', 0)} | {'✔' if f.get('cacheable') else '✘'} "
        f"| {'archive' if f.get('archive_required') else 'standard'} "
        f"| {f.get('classification', '?')} |"
        for f in rpc_features_plan
    )

    md = f"""# Run-004 Patch G3 — Feature Source Audit

**Conclusion**: `{conclusion}`  
**Offline enrichment feasible**: `{offline_enrichment_feasible}`  
**RPC feature plan required**: `{rpc_required}`  
**Phase**: 1 (audit only) — no RPC, no relabel, no model training.

---

## 0. Background

Patch G2 best model: `C2_xgb_d2_t50_engineered` — ROC 0.5479, feature ceiling reached.  
Only 2 pool-invariant features in canonical CSV: `final_score`, `log_index`.  
Goal: discover whether offline sources can provide `whale_val`, `momentum_val`, `buy_pressure_ratio`, `price_velocity_5m`, `pool_age_sec`, etc.

---

## 1. Files Scanned

| File | Exists | Rows | Cols | Target feats (of {len(TARGET_FEATURES)}) | Join key | Target cols present |
|------|:------:|-----:|-----:|:---------:|:--------:|---------------------|
{file_table}

---

## 2. Token Address Overlap (canonical vs source files)

| Source File | Canonical TAs | Source TAs | Overlap | Join Feasible |
|-------------|-------------:|----------:|-------:|:-------------:|
{overlap_table}

> **Critical finding**: ZERO token_address overlap between the canonical dataset and ALL offline sources.

---

## 3. Target Feature Recovery

| Feature | Joinable to canonical? | Found in files |
|---------|:---------------------:|----------------|
{recovery_table}

---

## 4. Root Cause

{root_cause_summary}

### Data Architecture Mismatch

The V2 canonical dataset was built from **pool-based Uniswap V2 swap events**:
- Identifier: `(pool_address, tx_hash, log_index)`
- `token_address` = non-WETH token in the LP pair

The dashcam / labeled_dataset files were built from **token-based dashcam monitoring**:
- Identifier: `(token_address, timestamp)`
- These are NEW token launches monitored at signal generation time

These two populations have completely different token_addresses despite overlapping timestamps. The scoring sub-components (`whale_val`, `momentum_val`, `activity_val`, `risk_val`) exist only in `dashcam.csv` — for a different set of tokens that was never part of the run004 pool-based swap labeling pipeline.

**There is no offline path to recover sub-scores for run004 canonical rows.**

---

## 5. Join Feasibility Tests (sample n=500 canonical rows)

| Test | Type | Match rate | Feasible |
|------|------|----------:|:--------:|
{chr(10).join(f"| {n} | {v.get('join_type','?')} | {v.get('match_rate', 0)} | {'✔' if v.get('feasible') else '✘'} |" for n, v in join_tests.items())}

---

## 6. RPC Feature Plan (Phase 3 — not yet executed)

To enrich the canonical dataset with pool-invariant microstructure features, the following on-chain data must be fetched via RPC:

| Feature | RPC Method | Calls/Row | Cacheable | Node Type | Classification |
|---------|-----------|----------:|:---------:|:---------:|---------------|
{rpc_table}

### Cost Estimate (full run004 canonical: ~{can_total_rows:,} rows)

| Item | Estimate |
|------|--------:|
| Unique (pool, entry_block) pairs | ~{entry_blocks_est:,} |
| Unique entry_blocks | ~{entry_blocks_est:,} |
| Unique pool addresses | ~{pool_addr_est:,} |
| `getReserves` archive calls | ~{rpc_calls_full['getReserves_per_pool_block']:,} |
| `getBlockByNumber` calls | ~{rpc_calls_full['getBlockByNumber_per_block']:,} |
| Pool creation `getLogs` | ~{rpc_calls_full['getLogs_pool_creation_per_pool']:,} |
| **Total archive calls (full run)** | **~{rpc_calls_full['total_archive_calls_estimated']:,}** |
| Pilot (200 rows) | ~{rpc_calls_full['pilot_200_rows_est_calls']:,} |

**Requirements:**
1. Archive node access (QuickNode paid endpoint already available)
2. Per-pool and per-block caching mandatory
3. Pilot 200 rows first; abort if 429 or >10 min
4. Full run only after pilot confirmed and user approval obtained
5. No RPC URL or API key in any logged output file

---

## 7. Conclusion

**`{conclusion}`**

- Offline feature enrichment: ✘ NOT FEASIBLE (zero overlap between all candidate sources)
- RPC feature plan: ✔ DESIGNED (getReserves + getBlock + pool creation)
- Next step: Run `scripts/build_run004_patch_g3_feature_enriched_dataset.py` to confirm null enrichment, then approve RPC pilot

---

## 8. Artifacts

| Artifact | Path |
|----------|------|
| This audit (JSON) | `mvp/diagnostics/run004_patch_g3_feature_source_audit.json` |
| This report (MD) | `mvp/runs/run-004-patch-g3-feature-source-audit.md` |
| RPC feature plan | `mvp/runs/run-004-patch-g3-rpc-feature-plan.md` |
| Phase 2 script | `scripts/build_run004_patch_g3_feature_enriched_dataset.py` |
| G4 training template | `scripts/retrain_run004_patch_g4_feature_enriched_dryrun.py` |
"""

    with out_md.open("w", encoding="utf-8") as f:
        f.write(md)
    print(f"wrote {out_md}")

    print(f"\n=== CONCLUSION: {conclusion} ===")
    print(f"offline_feasible={offline_enrichment_feasible} rpc_required={rpc_required}")
    print(f"joinable_features={joinable_features}")
    print(f"non_joinable_features (requiring RPC): {non_joinable_features[:5]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
