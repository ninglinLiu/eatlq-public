#!/usr/bin/env python3
"""Sprint057 - Base V3 data backfill foundation.

Scope:
1) Coverage inventory for key CH tables + key diagnostics CSV.
2) May gap preflight (no unbounded backfill by default).
3) Weekly incremental backfill design with idempotent keys + watermark plan.
4) Optional bounded May repair only if thresholds and env allow.

Hard guards:
- No live execution path
- No training/model promotion
- No production signal writes
- No .env/secret reads or prints
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "mvp" / "audits" / "sprint057"
DIAG_DIR = ROOT / "mvp" / "diagnostics"
STATE_DIR = ROOT / "mvp" / "state"

CHAIN_ID = 8453
MAY_START = "2026-05-01 00:00:00"
TARGET_TABLES = [
    "raw_evm_logs",
    "uniswap_v3_swaps",
    "uniswap_swaps_clean_mat",
    "uniswap_v3_swaps_deduped",
    "uniswap_v3_swaps_enriched",
    "uniswap_v3_swaps_trade_semantic",
    "swap_trade_events_all",
    "erc20_transfers_semantic",
]
WATCHER_RELIABLE_ORDER = [
    "uniswap_v3_swaps",
    "raw_evm_logs",
    "uniswap_swaps_clean_mat",
]

# Thresholds from user contract
THRESH_MAX_RPC_CALLS = 200_000
THRESH_MAX_RUNTIME_MIN = 20
THRESH_MAX_COST_USD = 5.0
REPAIR_START_BLOCK = 45363676
REPAIR_END_BLOCK = 45799315
REORG_BUFFER_BLOCKS = 64
RPC_PRIORITY_KEYS = [
    "LABEL_RPC_URL",
    "EVM_HTTP_URL",
    "LIVE_BASE_RPC_URL",
    "LIVE_BASE_RPC_URL_FALLBACK",
    "ALCHEMY_HTTP_URL",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sh(command: str, timeout: int = 90) -> str:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {command}\nSTDERR:\n{proc.stderr[:800]}"
        )
    return proc.stdout.strip()


def ch(query: str, timeout: int = 120) -> str:
    q = query.replace('"', '\\"')
    cmd = (
        'docker exec eatlq-clickhouse-1 clickhouse-client --database eatlq '
        f'--query "{q}"'
    )
    return sh(cmd, timeout=timeout)


def parse_tsv_with_names(text: str) -> list[dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    headers = lines[0].split("\t")
    out: list[dict[str, str]] = []
    for ln in lines[1:]:
        vals = ln.split("\t")
        row = {headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))}
        out.append(row)
    return out


def f(v: Any, default: float = 0.0) -> float:
    if v in ("", None):
        return default
    try:
        return float(v)
    except Exception:
        return default


def i(v: Any, default: int = 0) -> int:
    if v in ("", None):
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def resolve_rpc_env() -> dict[str, Any]:
    present = {k: bool(os.getenv(k)) for k in RPC_PRIORITY_KEYS}
    used_key = ""
    used_value = ""
    for key in RPC_PRIORITY_KEYS:
        value = (os.getenv(key) or "").strip()
        if value:
            used_key = key
            used_value = value
            break
    masked = "https://***redacted***" if used_value else ""
    sha_prefix = hashlib.sha256(used_value.encode("utf-8")).hexdigest()[:8] if used_value else ""
    return {
        "rpc_provided": bool(used_value),
        "rpc_source": "env",
        "rpc_key_used": used_key,
        "rpc_value_masked": masked,
        "rpc_value_sha256_prefix": sha_prefix,
        "env_presence": present,
    }


@dataclass
class TableCoverage:
    table: str
    engine: str
    table_type: str
    is_merge_tree: bool
    min_block_time: str
    max_block_time: str
    row_count: int | None
    may_covered: bool | None
    daily_rows: list[dict[str, Any]]
    weekly_rows: list[dict[str, Any]]
    view_oom_risk: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "engine": self.engine,
            "table_type": self.table_type,
            "is_merge_tree": self.is_merge_tree,
            "min_block_time": self.min_block_time,
            "max_block_time": self.max_block_time,
            "row_count": self.row_count,
            "may_covered": self.may_covered,
            "daily_rows": self.daily_rows,
            "weekly_rows": self.weekly_rows,
            "view_oom_risk": self.view_oom_risk,
            "note": self.note,
        }


def get_table_engines() -> dict[str, str]:
    table_list = ",".join(f"'{t}'" for t in TARGET_TABLES)
    rows = parse_tsv_with_names(
        ch(
            f"SELECT name, engine FROM system.tables WHERE database='eatlq' "
            f"AND name IN ({table_list}) ORDER BY name FORMAT TSVWithNames"
        )
    )
    return {r["name"]: r["engine"] for r in rows}


def get_table_columns() -> dict[str, set[str]]:
    table_list = ",".join(f"'{t}'" for t in TARGET_TABLES)
    rows = parse_tsv_with_names(
        ch(
            f"SELECT table, name FROM system.columns WHERE database='eatlq' "
            f"AND table IN ({table_list}) FORMAT TSVWithNames"
        )
    )
    out: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        out[r["table"]].add(r["name"])
    return out


def build_coverage_inventory() -> dict[str, Any]:
    engines = get_table_engines()
    cols = get_table_columns()
    out: list[TableCoverage] = []

    for table in TARGET_TABLES:
        engine = engines.get(table, "UNKNOWN")
        is_view = engine.lower() == "view"
        is_merge_tree = "mergetree" in engine.lower()
        table_type = "view" if is_view else ("mergetree" if is_merge_tree else "other")

        if is_view:
            # Per query-mv-refreshable + query-join-filter-before guidance:
            # avoid full-scan count on heavy semantic views.
            out.append(
                TableCoverage(
                    table=table,
                    engine=engine,
                    table_type=table_type,
                    is_merge_tree=False,
                    min_block_time="",
                    max_block_time="",
                    row_count=None,
                    may_covered=None,
                    daily_rows=[],
                    weekly_rows=[],
                    view_oom_risk="high_if_unbounded_full_scan",
                    note="View evaluation skipped to avoid OOM/full-scan risk; use upstream MergeTree.",
                )
            )
            continue

        agg = parse_tsv_with_names(
            ch(
                f"SELECT min(block_time) AS min_block_time, max(block_time) AS max_block_time, "
                f"count() AS row_count FROM {table} "
                f"WHERE chain_id={CHAIN_ID} FORMAT TSVWithNames"
            )
        )
        row = agg[0] if agg else {}
        max_bt = row.get("max_block_time", "")
        may_covered = bool(max_bt and max_bt >= "2026-05-01 00:00:00.000")

        cset = cols.get(table, set())
        if "received_at_ms" in cset:
            time_expr = "coalesce(block_time, received_at_ms)"
        else:
            time_expr = "block_time"

        daily = parse_tsv_with_names(
            ch(
                f"SELECT toDate({time_expr}) AS d, count() AS c "
                f"FROM {table} WHERE chain_id={CHAIN_ID} "
                f"GROUP BY d ORDER BY d DESC LIMIT 14 FORMAT TSVWithNames"
            )
        )
        weekly = parse_tsv_with_names(
            ch(
                f"SELECT toStartOfWeek({time_expr}) AS wk, count() AS c "
                f"FROM {table} WHERE chain_id={CHAIN_ID} "
                f"GROUP BY wk ORDER BY wk DESC LIMIT 12 FORMAT TSVWithNames"
            )
        )

        out.append(
            TableCoverage(
                table=table,
                engine=engine,
                table_type=table_type,
                is_merge_tree=True,
                min_block_time=row.get("min_block_time", ""),
                max_block_time=max_bt,
                row_count=i(row.get("row_count")),
                may_covered=may_covered,
                daily_rows=daily,
                weekly_rows=weekly,
                view_oom_risk="low",
                note="Direct MergeTree aggregate.",
            )
        )

    # Root-cause diagnostics for Apr30 freeze
    root = parse_tsv_with_names(
        ch(
            "SELECT "
            "max(block_time) AS max_block_time, "
            "max(received_at_ms) AS max_received_at, "
            "count() AS total_rows, "
            "countIf(received_at_ms >= toDateTime64('2026-05-01 00:00:00',3,'UTC')) AS rows_ingested_since_may, "
            "countIf(received_at_ms >= toDateTime64('2026-05-01 00:00:00',3,'UTC') AND block_time IS NULL) AS rows_since_may_null_block_time "
            "FROM uniswap_v3_swaps WHERE chain_id=8453 FORMAT TSVWithNames"
        )
    )[0]
    source_mix = parse_tsv_with_names(
        ch(
            "SELECT source, count() AS n, countIf(block_time IS NULL) AS n_null "
            "FROM uniswap_v3_swaps "
            "WHERE chain_id=8453 AND received_at_ms >= toDateTime64('2026-05-01 00:00:00',3,'UTC') "
            "GROUP BY source ORDER BY n DESC LIMIT 20 FORMAT TSVWithNames"
        )
    )

    watcher_reliable = []
    by_table = {x.table: x for x in out}
    for t in WATCHER_RELIABLE_ORDER:
        tc = by_table.get(t)
        if not tc:
            continue
        watcher_reliable.append(
            {
                "table": t,
                "reason": (
                    "MergeTree, directly queryable, lower OOM risk for bounded filters."
                    if tc.is_merge_tree
                    else "fallback only"
                ),
                "max_block_time": tc.max_block_time,
                "may_covered": tc.may_covered,
            }
        )

    return {
        "generated_at": now_iso(),
        "chain_id": CHAIN_ID,
        "tables": [x.as_dict() for x in out],
        "root_cause_apr30_freeze": {
            "summary": (
                "Ingestion continued after May 1 (received_at_ms advances), but discovery_poller "
                "rows have null block_time; historical bf180 inserts in May carry old block_time. "
                "Therefore max(block_time) remains stuck at 2026-04-30."
            ),
            "uniswap_v3_swaps": root,
            "post_may_source_mix_top20": source_mix,
        },
        "watcher_reliable_tables": watcher_reliable,
    }


def inspect_csv_artifacts() -> list[dict[str, Any]]:
    files = [
        ROOT / "mvp/diagnostics/sprint055_track_a_0x501e_watchlist.csv",
        ROOT / "mvp/diagnostics/sprint056_0x501e_fresh_events.csv",
        ROOT / "mvp/diagnostics/sprint056_0x501e_paper_returns.csv",
        ROOT / "logs/copy_follow_dashcam.csv",
    ]
    out: list[dict[str, Any]] = []
    for p in files:
        rec = {
            "path": str(p.relative_to(ROOT)) if p.exists() else str(p.relative_to(ROOT)),
            "exists": p.exists(),
            "rows": 0,
            "last_timestamp_like": "",
            "mtime_utc": "",
        }
        if p.exists():
            try:
                with p.open(newline="", encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
                rec["rows"] = len(rows)
                rec["mtime_utc"] = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
                if rows:
                    for k in ("observed_at", "timestamp_utc", "block_time_utc", "timestamp"):
                        if k in rows[-1] and rows[-1].get(k):
                            rec["last_timestamp_like"] = str(rows[-1].get(k))
                            break
            except Exception as exc:
                rec["error"] = str(exc)
        out.append(rec)
    return out


def build_may_preflight(inv: dict[str, Any]) -> dict[str, Any]:
    frontier = parse_tsv_with_names(
        ch(
            "WITH (SELECT maxIf(block_number, block_time IS NOT NULL) FROM uniswap_v3_swaps WHERE chain_id=8453) AS f "
            "SELECT f AS block_frontier, "
            "count() AS rows_after_frontier, "
            "countDistinct(block_number) AS distinct_blocks_after_frontier, "
            "min(block_number) AS min_block_after_frontier, "
            "max(block_number) AS max_block_after_frontier "
            "FROM uniswap_v3_swaps WHERE chain_id=8453 AND block_number > f FORMAT TSVWithNames"
        )
    )[0]

    block_frontier = i(frontier.get("block_frontier"))
    rows_after = i(frontier.get("rows_after_frontier"))
    blocks_after = i(frontier.get("distinct_blocks_after_frontier"))
    min_block = i(frontier.get("min_block_after_frontier"))
    max_block = i(frontier.get("max_block_after_frontier"))
    block_span = max(0, max_block - min_block + 1) if max_block and min_block else 0
    span_days = block_span / 43200.0 if block_span else 0.0

    # backfill_block_timestamps.py defaults:
    max_cps = 45.0
    est_block_ts_calls = blocks_after
    est_swap_logs = rows_after
    est_receipt_calls = 0  # block-time repair path does not require receipts
    est_metadata_calls = 0
    est_runtime_fetch_min = est_block_ts_calls / max_cps / 60.0 if est_block_ts_calls else 0.0
    est_runtime_mutation_min = 12.0 if est_block_ts_calls else 0.0
    est_runtime_total_min = est_runtime_fetch_min + est_runtime_mutation_min

    # Conservative price assumption for archive RPC.
    usd_per_1k_calls = 0.10
    est_cost_usd = (est_block_ts_calls / 1000.0) * usd_per_1k_calls

    est_storage_mb = round((est_swap_logs * 0.25) / 1024.0, 2)  # repair metadata + mutation overhead approximation

    rpc_meta = resolve_rpc_env()
    rpc_ready = rpc_meta["rpc_provided"]

    over_rpc = est_block_ts_calls > THRESH_MAX_RPC_CALLS
    over_runtime = est_runtime_total_min > THRESH_MAX_RUNTIME_MIN
    over_cost = est_cost_usd > THRESH_MAX_COST_USD

    can_execute = rpc_ready and not any([over_rpc, over_runtime, over_cost])
    abort_reasons = []
    if not rpc_ready:
        abort_reasons.append("RPC_ENV_MISSING_ALL_SUPPORTED_KEYS")
    if over_rpc:
        abort_reasons.append(f"RPC_CALLS_GT_{THRESH_MAX_RPC_CALLS}")
    if over_runtime:
        abort_reasons.append(f"RUNTIME_GT_{THRESH_MAX_RUNTIME_MIN}MIN")
    if over_cost:
        abort_reasons.append(f"COST_GT_{THRESH_MAX_COST_USD}USD")

    return {
        "generated_at": now_iso(),
        "window_start": MAY_START,
        "window_end_observed_block": max_block,
        "block_frontier_with_non_null_block_time": block_frontier,
        "block_range_missing": {
            "min_block": min_block,
            "max_block": max_block,
            "span_blocks": block_span,
            "span_days_est": round(span_days, 2),
        },
        "estimates": {
            "estimated_swap_logs": est_swap_logs,
            "estimated_tx_receipt_calls": est_receipt_calls,
            "estimated_block_timestamp_calls": est_block_ts_calls,
            "estimated_token_pool_metadata_calls": est_metadata_calls,
            "estimated_storage_mb": est_storage_mb,
            "estimated_runtime_min": round(est_runtime_total_min, 2),
            "estimated_cost_usd": round(est_cost_usd, 2),
            "cost_model_usd_per_1k_calls": usd_per_1k_calls,
        },
        "failure_modes": [
            "RPC 429/rate-limit bursts during eth_getBlockByNumber batches",
            "ALTER TABLE UPDATE mutation backlog / lock contention",
            "View fan-out OOM if validation queries are unbounded",
            "Partial chunk completion without persisted watermark state",
        ],
        "abort_thresholds": {
            "max_rpc_calls": THRESH_MAX_RPC_CALLS,
            "max_runtime_min": THRESH_MAX_RUNTIME_MIN,
            "max_cost_usd": THRESH_MAX_COST_USD,
        },
        "execute_may_repair_allowed": can_execute,
        "abort_reasons": abort_reasons,
        "rpc_env_flags": rpc_meta,
    }


def maybe_execute_may_repair(preflight: dict[str, Any], execute: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "executed": False,
        "reason": "",
        "summary_csv": "",
        "failed_chunks_csv": "",
        "watermark_json": "",
    }
    if not execute:
        result["reason"] = "MAY_REPAIR_NOT_REQUESTED"
        return result
    if not preflight.get("execute_may_repair_allowed"):
        result["reason"] = "MAY_REPAIR_BLOCKED_BY_PREFLIGHT_OR_ENV"
        return result

    rpc_meta = resolve_rpc_env()
    if not rpc_meta["rpc_provided"]:
        result["reason"] = "MAY_REPAIR_BLOCKED_NO_RPC"
        return result

    min_block = max(REPAIR_START_BLOCK, i(preflight["block_range_missing"]["min_block"]))
    max_block = min(REPAIR_END_BLOCK, i(preflight["block_range_missing"]["max_block"]))
    if max_block < min_block:
        result["reason"] = "MAY_REPAIR_EMPTY_RANGE"
        return result

    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    summary_csv = DIAG_DIR / "sprint057_may_gap_backfill_summary.csv"
    failed_csv = DIAG_DIR / "sprint057_may_gap_failed_chunks.csv"
    watermark = STATE_DIR / "base_v3_backfill_watermark.json"

    def snapshot() -> dict[str, Any]:
        row = parse_tsv_with_names(
            ch(
                "SELECT "
                "max(block_time) AS max_block_time, "
                "countIf(block_time IS NULL) AS null_rows, "
                "countIf(block_time IS NULL AND source='discovery_poller') AS discovery_null_rows "
                "FROM uniswap_v3_swaps WHERE chain_id=8453 FORMAT TSVWithNames"
            )
        )[0]
        return {
            "max_block_time": row.get("max_block_time", ""),
            "null_rows": i(row.get("null_rows")),
            "discovery_null_rows": i(row.get("discovery_null_rows")),
        }

    chunk_size = 100_000
    chunks: list[tuple[int, int]] = []
    cur = min_block
    while cur <= max_block:
        end = min(cur + chunk_size - 1, max_block)
        chunks.append((cur, end))
        cur = end + 1

    before = snapshot()
    failed_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    last_success_end = min_block - 1

    for idx, (start_b, end_b) in enumerate(chunks, start=1):
        env = os.environ.copy()
        env["LABEL_RPC_URL"] = os.getenv(rpc_meta["rpc_key_used"], "")
        env["EVM_HTTP_URL"] = env["LABEL_RPC_URL"]
        env["CHAIN_ID"] = str(CHAIN_ID)
        env["START_BLOCK"] = str(start_b)
        env["END_BLOCK"] = str(end_b)
        env["TARGET_TABLES"] = "uniswap_v3_swaps"
        env["RUN_MUTATIONS"] = "1"
        env["WAIT_FOR_MUTATIONS"] = "1"
        env["MAX_CPS"] = "45"

        before_chunk = snapshot()
        proc = subprocess.run(
            ["python3", "scripts/backfill_block_timestamps.py"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        after_chunk = snapshot()
        row = {
            "chunk_id": idx,
            "start_block": start_b,
            "end_block": end_b,
            "status": "ok" if proc.returncode == 0 else "failed",
            "null_rows_before": before_chunk["null_rows"],
            "null_rows_after": after_chunk["null_rows"],
            "null_rows_delta": before_chunk["null_rows"] - after_chunk["null_rows"],
            "max_block_time_before": before_chunk["max_block_time"],
            "max_block_time_after": after_chunk["max_block_time"],
            "stderr_tail": (proc.stderr or "")[-300:],
        }
        summary_rows.append(row)
        if proc.returncode != 0:
            failed_rows.append(
                {
                    "chunk_id": idx,
                    "start_block": start_b,
                    "end_block": end_b,
                    "error": f"RC_{proc.returncode}",
                }
            )
        else:
            last_success_end = end_b

    after = snapshot()

    with summary_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "chunk_id",
                "start_block",
                "end_block",
                "status",
                "null_rows_before",
                "null_rows_after",
                "null_rows_delta",
                "max_block_time_before",
                "max_block_time_after",
                "stderr_tail",
            ],
        )
        w.writeheader()
        w.writerows(summary_rows)

    with failed_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["chunk_id", "start_block", "end_block", "error"])
        w.writeheader()
        w.writerows(failed_rows)

    watermark.write_text(
        json.dumps(
            {
                "chain_id": CHAIN_ID,
                "table": "uniswap_v3_swaps",
                "last_backfill_start_block": min_block,
                "last_backfill_end_block": last_success_end,
                "last_backfill_completed_at": now_iso(),
                "idempotent_key": "chain_id+tx_hash+log_index",
                "reorg_buffer_blocks": REORG_BUFFER_BLOCKS,
                "max_block_time_before": before["max_block_time"],
                "max_block_time_after": after["max_block_time"],
                "null_block_time_rows_before": before["null_rows"],
                "null_block_time_rows_after": after["null_rows"],
                "discovery_null_rows_after": after["discovery_null_rows"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    executed_ok = len(failed_rows) == 0
    result.update(
        {
            "executed": executed_ok,
            "reason": "EXECUTED_OK" if executed_ok else "EXECUTED_PARTIAL_WITH_FAILED_CHUNKS",
            "summary_csv": str(summary_csv.relative_to(ROOT)),
            "failed_chunks_csv": str(failed_csv.relative_to(ROOT)),
            "watermark_json": str(watermark.relative_to(ROOT)),
            "chunk_count": len(chunks),
            "failed_chunk_count": len(failed_rows),
            "max_block_time_before": before["max_block_time"],
            "max_block_time_after": after["max_block_time"],
            "null_rows_before": before["null_rows"],
            "null_rows_after": after["null_rows"],
        }
    )
    return result


def build_incremental_plan_md(preflight: dict[str, Any], execute_result: dict[str, Any]) -> str:
    return f"""# Sprint057 — Incremental Backfill Plan

## Design

- Watermark path: `mvp/state/base_v3_backfill_watermark.json`
- Idempotent key: `chain_id + tx_hash + log_index`
- Runner cadence: weekly (UTC Sunday), plus on-demand catch-up
- Chunking: block-range chunk + retry backoff
- Failed chunk log: `mvp/diagnostics/sprint057_may_gap_failed_chunks.csv`
- Sanity check: per-run row count delta + max(block_time) advancement
- Query safety: avoid unbounded scans on heavy semantic views; query MergeTree base first.

## Weekly runner contract

1. Read watermark and current `maxIf(block_number, block_time IS NOT NULL)` frontier.
2. Compute gap blocks, split into bounded chunks.
3. For each chunk, run timestamp backfill (`eth_getBlockByNumber` batch), then mutate `uniswap_v3_swaps`.
4. Write per-chunk success/failure and update watermark only on chunk success.
5. Emit summary CSV + decision flags.

## Preflight gate

- `max_rpc_calls <= {THRESH_MAX_RPC_CALLS}`
- `estimated_runtime_min <= {THRESH_MAX_RUNTIME_MIN}`
- `estimated_cost_usd <= {THRESH_MAX_COST_USD}`

Current preflight allow-execute: `{preflight.get('execute_may_repair_allowed')}`  
Execution status this sprint: `{execute_result.get('reason')}`
"""


def build_historical_shard_plan(preflight: dict[str, Any]) -> list[dict[str, Any]]:
    # Conservative order-of-magnitude only (no 3-year full run in this sprint).
    calls_per_day_est = max(1, i(preflight["estimates"]["estimated_block_timestamp_calls"]) // max(1, int(preflight["block_range_missing"]["span_days_est"] or 1)))

    shards = [
        ("last_90d", 90, "highest", "needed for current watcher and near-term strategy calibration"),
        ("2026_ytd", 131, "high", "aligns current year regime and fee shifts"),
        ("2025", 365, "medium", "broader regime context, lower immediate wallet-watch value"),
        ("2024", 366, "low", "historical context only; likely diminishing relevance"),
        ("earliest_base_history", 365, "low", "archive completeness / research tail"),
    ]
    out = []
    for name, days, prio, value in shards:
        est_calls = calls_per_day_est * days
        est_cost = (est_calls / 1000.0) * 0.10
        est_runtime_min = (est_calls / 45.0) / 60.0 + 12.0
        out.append(
            {
                "shard": name,
                "days_est": days,
                "priority": prio,
                "data_value": value,
                "estimated_block_ts_calls": est_calls,
                "estimated_runtime_min": round(est_runtime_min, 1),
                "estimated_cost_usd": round(est_cost, 2),
                "stop_condition": "stop when thresholds exceeded, chunk failure ratio >5%, or watcher-useful tables stop improving",
            }
        )
    return out


def write_outputs(
    inventory: dict[str, Any],
    preflight: dict[str, Any],
    execute_result: dict[str, Any],
    shard_plan: list[dict[str, Any]],
) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    # coverage json
    cov_json = {
        **inventory,
        "csv_diagnostics_coverage": inspect_csv_artifacts(),
    }
    (AUDIT_DIR / "coverage_inventory.json").write_text(json.dumps(cov_json, indent=2), encoding="utf-8")

    # coverage md
    lines = [
        "# Sprint057 — Coverage Inventory",
        "",
        f"Generated: `{now_iso()}`",
        "",
        "## Table Coverage",
        "",
        "| table | engine | type | min(block_time) | max(block_time) | row_count | may_covered | view_oom_risk |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for t in inventory["tables"]:
        lines.append(
            f"| {t['table']} | {t['engine']} | {t['table_type']} | {t['min_block_time']} | {t['max_block_time']} | "
            f"{'' if t['row_count'] is None else t['row_count']} | {t['may_covered']} | {t['view_oom_risk']} |"
        )
    lines.extend(
        [
            "",
            "## Root Cause (Why max block_time stuck at 2026-04-30)",
            "",
            inventory["root_cause_apr30_freeze"]["summary"],
            "",
            "## Watcher-reliable tables",
            "",
        ]
    )
    for r in inventory["watcher_reliable_tables"]:
        lines.append(
            f"- `{r['table']}`: {r['reason']} (max_block_time={r['max_block_time']}, may_covered={r['may_covered']})"
        )
    lines.extend(["", "## CSV / diagnostics coverage", ""])
    for c in inspect_csv_artifacts():
        lines.append(
            f"- `{c['path']}` exists={c['exists']} rows={c['rows']} last_ts={c['last_timestamp_like']} mtime={c['mtime_utc']}"
        )
    (AUDIT_DIR / "coverage_inventory.md").write_text("\n".join(lines), encoding="utf-8")

    # may preflight json/md
    (AUDIT_DIR / "may_gap_preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    may_md = [
        "# Sprint057 — May Gap Preflight",
        "",
        f"Generated: `{now_iso()}`",
        "",
        f"- Missing block range: `{preflight['block_range_missing']['min_block']}..{preflight['block_range_missing']['max_block']}`",
        f"- Estimated swap logs: `{preflight['estimates']['estimated_swap_logs']}`",
        f"- Estimated block timestamp calls: `{preflight['estimates']['estimated_block_timestamp_calls']}`",
        f"- Estimated tx receipt calls: `{preflight['estimates']['estimated_tx_receipt_calls']}`",
        f"- Estimated metadata calls: `{preflight['estimates']['estimated_token_pool_metadata_calls']}`",
        f"- Estimated storage: `{preflight['estimates']['estimated_storage_mb']} MB`",
        f"- Estimated runtime: `{preflight['estimates']['estimated_runtime_min']} min`",
        f"- Estimated cost: `${preflight['estimates']['estimated_cost_usd']}`",
        "",
        f"Execute May repair allowed: `{preflight['execute_may_repair_allowed']}`",
        f"Abort reasons: `{', '.join(preflight['abort_reasons']) if preflight['abort_reasons'] else 'none'}`",
    ]
    (AUDIT_DIR / "may_gap_preflight.md").write_text("\n".join(may_md), encoding="utf-8")

    # incremental plan md
    (AUDIT_DIR / "incremental_backfill_plan.md").write_text(
        build_incremental_plan_md(preflight, execute_result),
        encoding="utf-8",
    )

    # decision flags
    flags = {
        "generated_at": now_iso(),
        "sprint057_verdict": "FOUNDATION_BUILT_MAY_REPAIR_NOT_EXECUTED"
        if not execute_result["executed"]
        else "FOUNDATION_AND_MAY_REPAIR_EXECUTED",
        "why_ch_stuck_apr30": inventory["root_cause_apr30_freeze"]["summary"],
        "may_repair_executed": execute_result["executed"],
        "may_repair_reason": execute_result["reason"],
        "may_repair_allowed_by_preflight": preflight["execute_may_repair_allowed"],
        "live_allowed": False,
        "training_allowed": False,
        "model_promotion_allowed": False,
        "production_signals_written": False,
        "shared_tx_to_operator_identity_allowed": False,
        "author_wallet_found": False,
        "watcher_reliable_tables": [x["table"] for x in inventory["watcher_reliable_tables"]],
        "historical_shard_priority": [x["shard"] for x in shard_plan],
    }
    (AUDIT_DIR / "decision_flags.json").write_text(json.dumps(flags, indent=2), encoding="utf-8")

    # attach shard plan into incremental plan appendix
    with (AUDIT_DIR / "incremental_backfill_plan.md").open("a", encoding="utf-8") as fh:
        fh.write("\n\n## Historical shard plan (no full run in Sprint057)\n\n")
        fh.write("| shard | days_est | priority | est_calls | est_runtime_min | est_cost_usd | stop_condition |\n")
        fh.write("| --- | ---: | --- | ---: | ---: | ---: | --- |\n")
        for s in shard_plan:
            fh.write(
                f"| {s['shard']} | {s['days_est']} | {s['priority']} | {s['estimated_block_ts_calls']} | "
                f"{s['estimated_runtime_min']} | {s['estimated_cost_usd']} | {s['stop_condition']} |\n"
            )


def main() -> None:
    execute_may_repair = "--execute-may-repair" in os.sys.argv

    inventory = build_coverage_inventory()
    preflight = build_may_preflight(inventory)
    execute_result = maybe_execute_may_repair(preflight, execute=execute_may_repair)
    shard_plan = build_historical_shard_plan(preflight)
    write_outputs(inventory, preflight, execute_result, shard_plan)

    print(
        json.dumps(
            {
                "coverage_tables": len(inventory["tables"]),
                "may_repair_executed": execute_result["executed"],
                "may_repair_reason": execute_result["reason"],
                "preflight_allowed": preflight["execute_may_repair_allowed"],
                "sprint057_audit_dir": str(AUDIT_DIR),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
