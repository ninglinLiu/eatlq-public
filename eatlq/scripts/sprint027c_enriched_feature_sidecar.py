#!/usr/bin/env python3
"""Sprint027C — Enriched feature sidecar skeleton.

H10-style diagnostic sidecar. Produces per-row feature stubs for multi-window
(60s / 180s / 300s / 900s / 1800s) enriched features from dashcam CSV rows
that carry pool_address (H18.14 schema). It does NOT train, label, write Redis,
or call any RPC/ClickHouse/Dune endpoint.

Contract (enforced by tests/test_sprint027c_sidecar.py):
- NO Redis writes, reads, or imports of redis-py.
- NO RPC / HTTP calls beyond reading local filesystem files.
- NO training or labeling.
- NO modification to services/, feature_store.py, executor, ingestor, or any
  runtime component.
- Output is diagnostic-only: sidecar CSV + coverage JSON under mvp/diagnostics/.
- IMPLEMENTATION_ALLOWED only for this diagnostic diagnostic script.
- TRAINING_ALLOWED=false  LABELING_ALLOWED=false  LIVE_ALLOWED=false
  PHASE6_ALLOWED=false

Usage:
    python3 scripts/sprint027c_enriched_feature_sidecar.py [--limit N] [--dry]

Env knobs:
    DASHCAM_PATH          default logs/dashcam.csv
    H8_DATASET_PATH       default mvp/diagnostics/run004_h8_executable_dataset.csv
    H108_DATASET_PATH     default mvp/diagnostics/run004_h108_enriched_executable_dataset.csv
    OUTPUT_DIR            default mvp/diagnostics
    SIDECAR_LIMIT         default 1000 (max sample rows; 0 = unlimited, not recommended)

Data source precedence (no RPC at any step):
  1. If H108 enriched dataset exists → use as primary enriched source (already has
     buyer_concentration + flow_quality + temporal_regime multi-window features).
  2. Else if H8 executable dataset exists → produce sidecar from dashcam context only.
  3. Else dashcam H18 rows only → produce stub sidecar with feature_source flags.

Feature families covered per source:
  FAMILY                  H108-available   dashcam-H18-only
  ----------------------- ---------------  ----------------
  flow_direction_proxy    YES (recomputed) PROXY (momentum_val / whale_val)
  buy_pressure_proxy      YES              PROXY (whale_val)
  activity_count          YES              PROXY (activity_val)
  buyer_concentration     YES (from CH)    UNAVAILABLE_FROM_DASHCAM_ONLY
  flow_quality_raw        YES              UNAVAILABLE_FROM_DASHCAM_ONLY
  pool_quality            PARTIAL          UNAVAILABLE_FROM_DASHCAM_ONLY
  multi_window_dynamics   YES              UNAVAILABLE_FROM_DASHCAM_ONLY

Columns matching DENYLIST_COLUMNS or DENYLIST_PREFIXES (mirrors retrain_model.py)
or identity fields are audit-only / must not be used as model features.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── Contract guards ───────────────────────────────────────────────────────────
# Enforced via AST scan in tests/test_sprint027c_sidecar.py
# Forbidden Redis primitives must not appear in this file:
#   xadd, xdel, xtrim, xclaim, set, setex, delete, hset, zadd, publish ...
# Forbidden RPC patterns: no external HTTP, no chain calls, no training scripts.
# This file must never import redis, make chain queries, or invoke retrain logic.
# ─────────────────────────────────────────────────────────────────────────────

WINDOWS_SEC: Tuple[int, ...] = (60, 180, 300, 900, 1800)

# Denylist mirrors retrain_model.py — these are audit-only, NOT model features.
DENYLIST_COLUMNS = frozenset({
    "pool_address", "token_address", "pool_type", "protocol",
    "tx_hash", "log_index", "block_number", "timestamp",
    "approximate_label", "executable_label",
    "executable_net_return", "approximate_net_return",
    "executable_status", "failure_reason",
    "decoded_source_used", "decoded_join_status",
    "final_score", "label", "net_return", "entry_block", "exit_block",
    "canonical_key", "entry_price", "exit_price",
})
DENYLIST_PREFIXES = (
    "future_",
    "exit_",
    "entry_",
    "quote_",
    "executable_",
    "approximate_",
    "label_",
)


def _column_denied(name: str) -> bool:
    if name in DENYLIST_COLUMNS:
        return True
    return any(name.startswith(prefix) for prefix in DENYLIST_PREFIXES)

DECISION_FLAGS = {
    "FAILED_NO_7D_ALPHA_ACTIVE": True,
    "TRAINING_ALLOWED": False,
    "LABELING_ALLOWED": False,
    "LIVE_ALLOWED": False,
    "PHASE6_ALLOWED": False,
    "WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED": False,
    "IMPLEMENTATION_ALLOWED_sidecar_diagnostic": True,
    "redis_write": False,
    "rpc_called": False,
    "dune_called": False,
}


# ── Source-availability map (from H10 diagnostics) ───────────────────────────

FEATURE_SOURCE_STATUS: Dict[str, str] = {
    # From run004_h10_feature_recompute_sources.json and h108 coverage:
    "buyer_concentration": "available_from_h108_clickhouse_decoded",
    "flow_quality_count": "available_from_h108_clickhouse_decoded",
    "flow_quality_raw": "available_from_h108_clickhouse_decoded",
    "flow_quality_usd": "unavailable_no_amount_usd",
    "pool_quality_age": "partial_from_discovered_pools_metadata",
    "pool_quality_reserve": "unavailable_requires_rpc_or_clickhouse",
    # slippage: requires chain-level quote call — not available in offline sidecar
    "pool_quality_slippage": "unavailable_requires_chain_quote",
    "temporal_regime": "available_from_h108_activity_acceleration",
    "protocol_audit": "available_as_audit_split_not_model_feature",
    # dashcam-only proxies:
    "flow_direction_proxy_dashcam": "partial_proxy_from_momentum_val",
    "buy_pressure_proxy_dashcam": "partial_proxy_from_whale_val",
    "activity_count_proxy_dashcam": "partial_proxy_from_activity_val",
    "unique_buyer_entropy_dashcam": "partially_available_pre_h18_missing",
}


def _optional_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _load_csv_bounded(path: Path, limit: int = 0) -> Tuple[List[str], List[Dict[str, str]]]:
    """Stream-read CSV, return (header, rows). Never reads >limit rows if limit>0."""
    rows: List[Dict[str, str]] = []
    header: List[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames[:] if reader.fieldnames else []
        for row in reader:
            rows.append(dict(row))
            if limit > 0 and len(rows) >= limit:
                break
    return header, rows


def _build_dashcam_sidecar(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Build a minimal sidecar from dashcam H18 rows (partial proxies only)."""
    out = []
    for row in rows:
        pa = row.get("pool_address", "")
        ta = row.get("token_address", "")
        if not pa:
            continue  # skip legacy rows

        side: Dict[str, Any] = {
            # identity (audit-only, denylist for model training)
            "_row_pool_address_AUDIT_ONLY": pa,
            "_row_token_address_AUDIT_ONLY": ta,
            "_row_timestamp_ms_AUDIT_ONLY": row.get("timestamp", ""),
            "_row_pool_type_AUDIT_ONLY": row.get("pool_type", ""),
            "_row_dex_AUDIT_ONLY": row.get("dex", ""),
            "_row_tx_hash_AUDIT_ONLY": row.get("tx_hash", ""),
            "_row_log_index_AUDIT_ONLY": row.get("log_index", ""),

            # existing dashcam features (5m window, runtime-computed)
            "price_velocity_5m": _optional_float(row.get("momentum_val")),
            "buy_pressure_ratio": _optional_float(row.get("whale_val")),
            "token_volatility_5m": _optional_float(row.get("risk_val")),
            "activity_heat": _optional_float(row.get("activity_val")),
            "unique_buyer_entropy_5m": _optional_float(row.get("unique_buyer_entropy_5m")),
            "final_score": _optional_float(row.get("final_score")),
            "triggered_signal": row.get("triggered_signal", ""),
            "normalized_side": row.get("normalized_side", ""),

            # multi-window stubs — not computable from dashcam alone
            **{f"activity_count_{w}s": None for w in WINDOWS_SEC},
            **{f"buy_pressure_proxy_{w}s": None for w in WINDOWS_SEC},
            **{f"flow_direction_proxy_{w}s": None for w in WINDOWS_SEC},
            **{f"unique_buyers_{w}s": None for w in WINDOWS_SEC},
            **{f"top1_buyer_share_{w}s": None for w in WINDOWS_SEC},
            **{f"buy_count_{w}s": None for w in WINDOWS_SEC},
            **{f"sell_count_{w}s": None for w in WINDOWS_SEC},
            **{f"buy_volume_raw_{w}s": None for w in WINDOWS_SEC},
            **{f"pool_age_sec": None},

            # source flags
            "_sidecar_source": "dashcam_h18_proxy_only",
            "_buyer_concentration_status": "unavailable_from_dashcam_only",
            "_flow_quality_raw_status": "unavailable_from_dashcam_only",
            "_pool_quality_status": "unavailable_from_dashcam_only",
            "_multi_window_dynamics_status": "unavailable_from_dashcam_only",
            "_sidecar_warning": (
                "proxies from dashcam scalar features are NOT equivalent to "
                "per-window aggregate recompute from swap event history"
            ),
        }
        out.append(side)
    return out


def _build_h108_sidecar_bridge(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Extract sidecar columns from the H108 enriched dataset (already has multi-window)."""
    multi_window_cols = [
        col for col in (rows[0].keys() if rows else [])
        if any(col.endswith(f"_{w}s") for w in WINDOWS_SEC)
    ]
    out = []
    for row in rows:
        side: Dict[str, Any] = {
            # identity audit-only
            "_row_pool_address_AUDIT_ONLY": row.get("pool_address", ""),
            "_row_token_address_AUDIT_ONLY": row.get("token_address", ""),
            "_row_timestamp_ms_AUDIT_ONLY": row.get("timestamp", ""),
            "_row_pool_type_AUDIT_ONLY": row.get("pool_type", ""),
            "_row_tx_hash_AUDIT_ONLY": row.get("tx_hash", ""),
            "_row_log_index_AUDIT_ONLY": row.get("log_index", ""),

            # existing 5m features
            "price_velocity_5m": _optional_float(row.get("price_velocity_5m")),
            "buy_pressure_ratio": _optional_float(row.get("buy_pressure_ratio")),
            "token_volatility_5m": _optional_float(row.get("token_volatility_5m")),
            "activity_heat": _optional_float(row.get("activity_heat")),
            "unique_buyer_entropy_5m": _optional_float(row.get("unique_buyer_entropy_5m")),

            # executable label columns (for join validation only, audit-only)
            "_executable_label_AUDIT_ONLY": row.get("executable_label", ""),
            "_executable_net_return_AUDIT_ONLY": row.get("executable_net_return", ""),

            "_sidecar_source": "h108_enriched_clickhouse_decoded",
            "_buyer_concentration_status": "available_from_h108",
            "_flow_quality_raw_status": "available_from_h108",
            "_pool_quality_status": "partial_pool_type_only",
            "_multi_window_dynamics_status": "available_partial_from_h108",
        }
        # Pass through multi-window columns
        for col in multi_window_cols:
            col_clean = col  # keep as-is; they are not denylist fields
            if not _column_denied(col):
                side[col_clean] = _optional_float(row.get(col))
        out.append(side)
    return out


def _coverage_report(sidecar_rows: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
    if not sidecar_rows:
        return {"rows": 0, "source": source}
    n = len(sidecar_rows)
    # Coverage per column
    col_coverage: Dict[str, float] = {}
    all_keys = sorted({k for r in sidecar_rows for k in r.keys()})
    for col in all_keys:
        non_null = sum(1 for r in sidecar_rows if r.get(col) is not None and r.get(col) != "")
        col_coverage[col] = round(non_null / n, 4)

    # Pool concentration
    pool_c = Counter(str(r.get("_row_pool_address_AUDIT_ONLY", "")) for r in sidecar_rows)
    top5 = pool_c.most_common(5)
    top1_share = top5[0][1] / n if top5 else 0
    top5_share = sum(c for _, c in top5) / n if top5 else 0

    # Feature families coverage summary
    families: Dict[str, Any] = {}
    for fam, key_fragments in [
        ("buyer_concentration", ["unique_buyers_", "top1_buyer_share_"]),
        ("flow_quality", ["buy_count_", "sell_count_", "buy_volume_raw_"]),
        ("temporal_dynamics", ["activity_count_", "activity_acceleration"]),
        ("existing_5m_baseline", ["price_velocity_5m", "buy_pressure_ratio", "token_volatility_5m", "activity_heat"]),
    ]:
        matched = [
            (col, cov) for col, cov in col_coverage.items()
            if any(frag in col for frag in key_fragments)
            and not col.startswith("_")
        ]
        families[fam] = {
            "feature_count": len(matched),
            "avg_coverage": round(sum(c for _, c in matched) / len(matched), 4) if matched else 0.0,
            "features_gte_70pct": [col for col, cov in matched if cov >= 0.70],
        }

    return {
        "source": source,
        "rows": n,
        "pool_concentration": {
            "distinct_pools": len(pool_c),
            "top1_share": round(top1_share, 4),
            "top5_share": round(top5_share, 4),
        },
        "feature_family_coverage": families,
        "column_coverage_sample": {
            col: col_coverage[col] for col in list(all_keys)[:40]
        },
        "decision_flags": DECISION_FLAGS,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("SIDECAR_LIMIT", "1000")))
    parser.add_argument("--dry", action="store_true", help="Print config and exit; no file writes.")
    parser.add_argument(
        "--source",
        choices=["auto", "h108_enriched", "dashcam_h18", "h108_pool_quality_joined"],
        default="auto",
        help=(
            "Override data source. 'h108_pool_quality_joined' reads Sprint027D joined output "
            "from mvp/diagnostics/sprint027d_h108_pool_quality_joined_sample.csv "
            "(or --input path). All sources remain diagnostic-only; no RPC, no Redis writes."
        ),
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Override input CSV path (used with --source h108_pool_quality_joined).",
    )
    args = parser.parse_args(argv)

    dashcam_path = Path(os.environ.get("DASHCAM_PATH", REPO_ROOT / "logs" / "dashcam.csv"))
    h8_path = Path(os.environ.get("H8_DATASET_PATH",
                   REPO_ROOT / "mvp" / "diagnostics" / "run004_h8_executable_dataset.csv"))
    h108_path = Path(os.environ.get("H108_DATASET_PATH",
                     REPO_ROOT / "mvp" / "diagnostics" / "run004_h108_enriched_executable_dataset.csv"))
    out_dir = Path(os.environ.get("OUTPUT_DIR", REPO_ROOT / "mvp" / "diagnostics"))
    out_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "dashcam_path": str(dashcam_path),
        "h8_path": str(h8_path),
        "h108_path": str(h108_path),
        "output_dir": str(out_dir),
        "limit": args.limit,
        "h108_exists": h108_path.exists(),
        "h8_exists": h8_path.exists(),
        "dashcam_exists": dashcam_path.exists(),
        "decision_flags": DECISION_FLAGS,
        "feature_source_status": FEATURE_SOURCE_STATUS,
    }
    if args.dry:
        print(json.dumps(config, indent=2))
        return 0

    print(f"sprint027c_enriched_feature_sidecar: START limit={args.limit}", flush=True)
    print(f"  H108 enriched dataset: {'found' if h108_path.exists() else 'missing'}")
    print(f"  H8 dataset: {'found' if h8_path.exists() else 'missing'}")
    print(f"  Dashcam: {'found' if dashcam_path.exists() else 'missing'}")

    # Determine source
    pq_joined_path = Path(
        args.input
        if args.input
        else REPO_ROOT / "mvp" / "diagnostics" / "sprint027d_h108_pool_quality_joined_sample.csv"
    )
    use_pq_joined = args.source == "h108_pool_quality_joined"
    use_h108 = args.source in ("auto", "h108_enriched")
    use_dashcam = args.source in ("auto", "dashcam_h18")

    if use_pq_joined and pq_joined_path.exists():
        print("  Source: h108_pool_quality_joined (Sprint027D pool_quality sidecar)")
        header, rows = _load_csv_bounded(pq_joined_path, limit=args.limit)
        sidecar = _build_h108_sidecar_bridge(rows)
        source = "h108_pool_quality_joined"
    elif use_pq_joined:
        print(f"BLOCKED_SOURCE_UNAVAILABLE: h108_pool_quality_joined path not found: {pq_joined_path}")
        return 3
    elif use_h108 and h108_path.exists():
        print("  Source: H108 enriched (buyer_concentration + flow_quality + temporal available)")
        header, rows = _load_csv_bounded(h108_path, limit=args.limit)
        sidecar = _build_h108_sidecar_bridge(rows)
        source = "h108_enriched"
    elif use_dashcam and dashcam_path.exists():
        print("  Source: dashcam H18 rows only (proxy stubs; enriched features UNAVAILABLE)")
        # Stream only H18 rows
        header_d, all_rows = _load_csv_bounded(dashcam_path, limit=args.limit * 10)
        h18 = [r for r in all_rows if r.get("pool_address", "")][:args.limit]
        if not h18:
            print("BLOCKED_SCHEMA_INSUFFICIENT: no H18 pool_address rows found in dashcam sample")
            return 3
        sidecar = _build_dashcam_sidecar(h18)
        source = "dashcam_h18_proxy"
    else:
        print("BLOCKED_SOURCE_UNAVAILABLE: no dashcam or H108 dataset found")
        return 3

    # Write sidecar CSV (≤limit rows)
    sidecar_trimmed = sidecar[:args.limit]
    out_csv = out_dir / "sprint027c_enriched_sidecar_sample.csv"
    if sidecar_trimmed:
        fieldnames = sorted(sidecar_trimmed[0].keys())
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sidecar_trimmed)
        print(f"  Wrote sidecar CSV: {out_csv.relative_to(REPO_ROOT)} ({len(sidecar_trimmed)} rows)")

    # Coverage report JSON
    coverage = _coverage_report(sidecar_trimmed, source)
    out_cov = out_dir / "sprint027c_enriched_sidecar_summary.json"
    out_cov.write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    print(f"  Wrote coverage: {out_cov.relative_to(REPO_ROOT)}")

    # Feature source status JSON
    source_status_out = out_dir / "sprint027c_schema_discovery.json"
    if not source_status_out.exists():
        source_status_out.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(f"sprint027c_enriched_feature_sidecar: OK rows={len(sidecar_trimmed)} source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
