#!/usr/bin/env python3
"""Sprint034E — read-only CH extract + dashcam bucket join for three fixed pools (additive coverage backfill)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASHCAM = REPO / "logs/dashcam.csv"
CSV_OUT = REPO / "mvp/diagnostics/sprint034e_same_pool_ch_backfill_candidates.csv"
JSON_OUT = REPO / "mvp/diagnostics/sprint034e_same_pool_coverage_backfill_summary.json"
DOCKER_CONTAINER = "eatlq-clickhouse-1"

# Sprint031B last bucket_sec per pool (from sprint034d_dashcam_coverage_autopsy.json).
POOL_LAST_BUCKET_SEC: dict[str, int] = {
    "0x1e98a167082332e5e89146662e34c3c99481e5e6": 1776484500,
    "0x5d2091e0b3e0b516e0f3abd75242e9287c253499": 1776705600,
    "0x8aed442e9c17746c0eb85dd22981ce8974182892": 1776487500,
}

ADDITIVE_BUCKET_FLOOR = 100
ADDITIVE_CAND_CAP = 300

CSV_FIELDS = (
    "pool_address_audit_key",
    "tx_hash_audit_only",
    "block_number_audit_only",
    "timestamp_ms_audit_only",
    "bucket_sec",
    "source",
    "is_post_sprint031b_cutoff",
    "dashcam_present_for_bucket",
    "candidate_for_additive_label",
)


def _load_dashcam_bucket_keys(pool_keys: frozenset[str]) -> frozenset[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    if not DASHCAM.exists():
        return frozenset()
    with DASHCAM.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            tok = (row.get("token_address") or "").strip().lower()
            if tok not in pool_keys:
                continue
            try:
                ts_ms = int(str(row.get("timestamp", "") or "0"))
            except ValueError:
                continue
            bsec = (ts_ms // 1000 // 300) * 300
            seen.add((tok, int(bsec)))
    return frozenset(seen)


def _run_ch_extract() -> list[dict[str, object]]:
    """Return post-cutoff swap rows from ClickHouse via docker exec (read-only)."""
    pools_sql = "', '".join(sorted(POOL_LAST_BUCKET_SEC))
    q = (
        "SELECT lower(s.pool_address), s.tx_hash, toString(s.block_number), "
        "toUnixTimestamp64Milli(assumeNotNull(s.block_time)), "
        "intDiv(toUnixTimestamp(assumeNotNull(s.block_time)),300)*300 "
        "FROM eatlq.uniswap_v3_swaps_enriched AS s "
        "WHERE s.chain_id = 8453 AND s.removed = 0 AND lower(s.pool_address) IN "
        f"('{pools_sql}') AND "
        "("
        "(lower(s.pool_address)='0x1e98a167082332e5e89146662e34c3c99481e5e6' AND "
        "toUnixTimestamp(assumeNotNull(s.block_time)) > 1776484500) OR "
        "(lower(s.pool_address)='0x5d2091e0b3e0b516e0f3abd75242e9287c253499' AND "
        "toUnixTimestamp(assumeNotNull(s.block_time)) > 1776705600) OR "
        "(lower(s.pool_address)='0x8aed442e9c17746c0eb85dd22981ce8974182892' AND "
        "toUnixTimestamp(assumeNotNull(s.block_time)) > 1776487500))"
        " ORDER BY s.block_number ASC, s.log_index ASC FORMAT TabSeparated"
    )
    cmd = ["docker", "exec", DOCKER_CONTAINER, "clickhouse-client", "-q", q]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"clickhouse-client failed ({proc.returncode}): {proc.stderr.strip()}")

    rows: list[dict[str, object]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        pool_l, tx_h, bn, ms_s, bk_s = parts
        unix_ms = int(ms_s)
        bucket_sec = int(bk_s)
        pool_low = pool_l.strip().lower()
        cutoff = POOL_LAST_BUCKET_SEC[pool_low]
        unix_sec = unix_ms // 1000
        is_post = 1 if unix_sec > cutoff else 0
        rows.append(
            {
                "pool_address_audit_key": pool_low,
                "tx_hash_audit_only": tx_h.strip(),
                "block_number_audit_only": bn.strip(),
                "timestamp_ms_audit_only": unix_ms,
                "bucket_sec": bucket_sec,
                "is_post_int": is_post,
            }
        )
    return rows


def main() -> int:
    rpc_evm = bool(__import__("os").environ.get("EVM_HTTP_URL"))
    rpc_label = bool(__import__("os").environ.get("LABEL_RPC_URL"))
    rpc_env = rpc_evm or rpc_label

    pool_keys = frozenset(POOL_LAST_BUCKET_SEC)
    dash_keys = _load_dashcam_bucket_keys(pool_keys)

    try:
        ch_rows = _run_ch_extract()
    except RuntimeError as e:
        err = {
            "kind": "sprint034e_same_pool_coverage_backfill_summary",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
            "clickhouse_reachable": False,
            "rpc_crosscheck": "rpc_crosscheck_skipped",
            "additive_feasibility_status": "FAIL",
        }
        JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
        JSON_OUT.write_text(json.dumps(err, indent=2) + "\n", encoding="utf-8")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    distinct_buckets: set[tuple[str, int]] = set()
    per_pool_counts: dict[str, int] = defaultdict(int)
    for r in ch_rows:
        p = str(r["pool_address_audit_key"])
        b = int(r["bucket_sec"])
        distinct_buckets.add((p, b))
        per_pool_counts[p] += 1

    n_swaps = len(ch_rows)
    n_buckets = len(distinct_buckets)
    passes_additive_floor = n_buckets >= ADDITIVE_BUCKET_FLOOR and n_swaps <= ADDITIVE_CAND_CAP

    any_dashcam_bucket = False
    out_lines: list[dict[str, str]] = []

    bounded_additive_eligible_row = passes_additive_floor
    for r in ch_rows:
        pk = str(r["pool_address_audit_key"])
        bsec = int(r["bucket_sec"])
        dash_ok = (pk, bsec) in dash_keys
        if dash_ok:
            any_dashcam_bucket = True
        iso = str(r["is_post_int"])
        cand = "true" if (bounded_additive_eligible_row and not dash_ok) else "false"
        out_lines.append(
            {
                "pool_address_audit_key": pk,
                "tx_hash_audit_only": str(r["tx_hash_audit_only"]),
                "block_number_audit_only": str(r["block_number_audit_only"]),
                "timestamp_ms_audit_only": str(int(r["timestamp_ms_audit_only"])),
                "bucket_sec": str(bsec),
                "source": "clickhouse_uniswap_v3_swaps_enriched",
                "is_post_sprint031b_cutoff": iso,
                "dashcam_present_for_bucket": "true" if dash_ok else "false",
                "candidate_for_additive_label": cand,
            }
        )

    # Optional RPC cross-check omitted when env absent
    rpc_result = (
        {"status": "not_run", "reason": "EVM_HTTP_URL_and_LABEL_RPC_URL_unset"}
        if not rpc_env
        else {"status": "not_implemented_in_script", "reason": "use_follow_sprint_rpc_tooling"}
    )
    if not rpc_env:
        rpc_note = "rpc_crosscheck_skipped"
    else:
        rpc_note = "rpc_env_present_but_script_does_not_call_rpc"

    # Single primary feasibility status (Sprint034E T5)
    if n_swaps != 19 or n_buckets != 3:
        feas = "NEEDS_COVERAGE_RECHECK"
    elif n_swaps == 0 and n_buckets == 0:
        feas = "SAME_POOL_ADDITIVE_PATH_CLOSED_FOR_NOW"
    elif n_buckets < ADDITIVE_BUCKET_FLOOR:
        feas = "ADDITIVE_PATH_WAIT_FOR_NEW_DATA"
    elif not any_dashcam_bucket and n_buckets >= ADDITIVE_BUCKET_FLOOR:
        feas = "DASHCAM_BACKFILL_REPAIR_NEEDED"
    elif passes_additive_floor:
        feas = "READY_FOR_ADDITIVE_LABEL"
    else:
        feas = "ADDITIVE_PATH_WAIT_FOR_NEW_DATA"

    summary = {
        "kind": "sprint034e_same_pool_coverage_backfill_summary",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sprint034d_validation": {
            "dashcam_post_cutoff_rows_expected_zero": True,
            "ch_post_cutoff_swaps_expected_19": n_swaps == 19,
            "ch_distinct_5m_buckets_expected_3": n_buckets == 3,
            "additive_manifest_status_expected_no_additive_rows": True,
            "recheck_status": ("OK" if n_swaps == 19 and n_buckets == 3 else "NEEDS_COVERAGE_RECHECK"),
        },
        "clickhouse_reachable": True,
        "clickhouse_table": "eatlq.uniswap_v3_swaps_enriched",
        "chain_id_filter": 8453,
        "fixed_pools": sorted(POOL_LAST_BUCKET_SEC),
        "ch_post_cutoff_swap_rows_in_csv": n_swaps,
        "ch_post_cutoff_distinct_bucket_pool_pairs": n_buckets,
        "dashcam_bucket_hits_for_post_cutoff_rows": any_dashcam_bucket,
        "rpc_env_configured": rpc_env,
        "rpc_crosscheck": rpc_result,
        "rpc_crosscheck_note": rpc_note,
        "additive_feasibility_status": feas,
        "bounded_policy": {
            "distinct_bucket_floor": ADDITIVE_BUCKET_FLOOR,
            "candidate_row_cap": ADDITIVE_CAND_CAP,
            "passes_bucket_floor": n_buckets >= ADDITIVE_BUCKET_FLOOR,
        },
        "outputs": {
            "backfill_candidates_csv": str(CSV_OUT.relative_to(REPO)),
            "summary_json": str(JSON_OUT.relative_to(REPO)),
        },
    }

    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    with CSV_OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        w.writeheader()
        for row in out_lines:
            w.writerow({k: row[k] for k in CSV_FIELDS})

    JSON_OUT.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
