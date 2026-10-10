#!/usr/bin/env python3
"""Sprint064 — Scheduled null block_time backfill guard (Base / eatlq).

Keeps block_time NULL rows from accumulating into multi-hour gaps by periodically:
  1) Discovering recent null_bt rows (via received_at_ms), scoped by --max-age-hours
  2) Skipping blocks in archive_miss registry until min_retry_hours elapse
  3) Fetching timestamps into block_ts_map (same RPC batch strategy as backfill_block_timestamps.py)
  4) Running ALTER UPDATE mutations on raw_evm_logs, uniswap_v2_swaps, uniswap_v3_swaps

Env (no secrets printed):
  LABEL_RPC_URL  — preferred HTTP endpoint for eth_getBlockByNumber
  EVM_HTTP_URL   — fallback if LABEL_RPC_URL unset
  CHAIN_ID       — default 8453
  CH_CONTAINER   — default eatlq-clickhouse-1

Does not: live trade, train, write production signals, or print full RPC URLs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEFAULT_REGISTRY = REPO_ROOT / "mvp/diagnostics/sprint064/archive_miss_blocks.json"
DIAG_DIR = REPO_ROOT / "mvp/diagnostics/sprint064"
LATEST_JSON = DIAG_DIR / "null_bt_backfill_latest.json"
HEALTH_JSON = DIAG_DIR / "null_bt_health_summary.json"
CRON_LOG = REPO_ROOT / "logs/null_bt_backfill_cron.log"


def crontab_has_null_bt_job() -> bool:
    """True if current user's crontab references the official wrapper (no secret I/O)."""
    try:
        r = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return False
        return "run_null_bt_cron.sh" in r.stdout
    except Exception:
        return False


def cron_env_ok() -> bool:
    return bool(resolve_rpc_url())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rpc_sha256_prefix(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _mask_rpc(url: str) -> str:
    if not url:
        return "(unset)"
    u = url.rstrip("/")
    if "://" in u:
        scheme, rest = u.split("://", 1)
        host = rest.split("/")[0].split("@")[-1]
        if len(host) > 24:
            host = host[:10] + "…" + host[-6:]
        return f"{scheme}://{host}/***"
    return "***"


def resolve_rpc_url() -> str:
    url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "").strip()
    return url


def ch_query(sql: str, container: str, fmt: str = "TSV") -> str:
    args = [
        "docker",
        "exec",
        "-i",
        container,
        "clickhouse-client",
        "--query_plan_max_optimizations_to_apply=0",
        "--format",
        fmt,
        "--query",
        sql,
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ClickHouse failed rc={r.returncode}: {r.stderr[:2000]}\nSQL:\n{sql[:500]}")
    return r.stdout


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "chain_id": 8453, "entries": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_registry(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def registry_skip_set(
    reg: dict[str, Any],
    now: datetime,
) -> set[int]:
    """Blocks to skip for RPC this run (archive_miss inside retry cooldown)."""
    skip: set[int] = set()
    default_h = int(reg.get("min_retry_hours_default", 168))
    for ent in reg.get("entries", []):
        if ent.get("status") != "archive_miss":
            continue
        bn = int(ent["block_number"])
        try:
            last = datetime.fromisoformat(ent["last_checked_at"].replace("Z", "+00:00"))
        except Exception:
            last = now
        min_h = int(ent.get("min_retry_hours", default_h))
        if (now - last).total_seconds() < min_h * 3600:
            skip.add(bn)
    return skip


def merge_registry_misses(registry_path: Path, misses: list[int], rpc_prefix: str, source: str) -> None:
    reg = load_registry(registry_path)
    by_bn = {int(e["block_number"]): e for e in reg.get("entries", [])}
    now_iso = _utc_now_iso()
    default_h = int(reg.get("min_retry_hours_default", 168))
    for bn in misses:
        if bn <= 0:
            continue
        if bn in by_bn:
            e = by_bn[bn]
            e["miss_count"] = int(e.get("miss_count", 0)) + 1
            e["last_checked_at"] = now_iso
            e["rpc_sha256_prefix"] = rpc_prefix
            notes = e.get("notes", "")
            tag = f"miss from {source}"
            if tag not in notes:
                e["notes"] = f"{notes}; {tag}".strip("; ") if notes else tag
        else:
            by_bn[bn] = {
                "block_number": bn,
                "first_seen_at": now_iso,
                "last_checked_at": now_iso,
                "source_tables": [source],
                "miss_count": 1,
                "rpc_sha256_prefix": rpc_prefix,
                "status": "archive_miss",
                "min_retry_hours": default_h,
                "notes": f"auto-registered from {source}",
            }
    reg["entries"] = sorted(by_bn.values(), key=lambda x: int(x["block_number"]))
    save_registry(registry_path, reg)


def table_sql_fragments(
    tables: set[str],
    chain_id: int,
    max_age_hours: int,
) -> list[str]:
    """Return SELECT block_number fragments restricted to recent null rows."""
    age_pred = f"received_at_ms >= now() - INTERVAL {int(max_age_hours)} HOUR"
    parts: list[str] = []
    if "raw" in tables:
        parts.append(
            f"""SELECT DISTINCT block_number FROM eatlq.raw_evm_logs
            WHERE chain_id = {chain_id} AND block_time IS NULL AND block_number > 0
              AND {age_pred}"""
        )
    if "v2" in tables:
        parts.append(
            f"""SELECT DISTINCT block_number FROM eatlq.uniswap_v2_swaps
            WHERE chain_id = {chain_id} AND block_time IS NULL AND block_number > 0
              AND {age_pred}"""
        )
    if "v3" in tables:
        parts.append(
            f"""SELECT DISTINCT block_number FROM eatlq.uniswap_v3_swaps
            WHERE chain_id = {chain_id} AND block_time IS NULL AND block_number > 0
              AND {age_pred}"""
        )
    return parts


def discover_blocks(
    container: str,
    chain_id: int,
    tables: set[str],
    max_age_hours: int,
    max_blocks: int,
    skip_blocks: set[int],
) -> list[int]:
    frags = table_sql_fragments(tables, chain_id, max_age_hours)
    if not frags:
        return []
    union_sql = "\nUNION DISTINCT\n".join(frags)
    skip_list = ",".join(str(b) for b in sorted(skip_blocks)) if skip_blocks else ""
    skip_clause = f"AND block_number NOT IN ({skip_list})" if skip_list else ""
    sql = f"""
    SELECT block_number FROM (
        SELECT DISTINCT block_number FROM (
            {union_sql}
        ) AS u
        WHERE block_number > 0
        {skip_clause}
    ) AS needed
    LEFT ANTI JOIN (
        SELECT block_number FROM eatlq.block_ts_map WHERE chain_id={chain_id}
    ) AS have USING (block_number)
    ORDER BY block_number
    LIMIT {int(max_blocks)}
    """
    raw = ch_query(sql, container).strip()
    if not raw:
        return []
    return [int(x) for x in raw.split("\n") if x]


def registry_retry_block_numbers(
    container: str,
    chain_id: int,
    tables: set[str],
    reg: dict[str, Any],
    now: datetime,
    max_take: int,
) -> list[int]:
    """Blocks listed as archive_miss past cooldown that still have NULL block_time."""
    default_h = int(reg.get("min_retry_hours_default", 168))
    eligible: list[int] = []
    for ent in reg.get("entries", []):
        st = ent.get("status")
        if st != "archive_miss":
            continue
        bn = int(ent["block_number"])
        if bn <= 0:
            continue
        try:
            last = datetime.fromisoformat(ent["last_checked_at"].replace("Z", "+00:00"))
        except Exception:
            last = now
        min_h = int(ent.get("min_retry_hours", default_h))
        if (now - last).total_seconds() < min_h * 3600:
            continue
        eligible.append(bn)
    if not eligible:
        return []
    in_list = ",".join(str(b) for b in sorted(set(eligible)))
    union_parts: list[str] = []
    if "raw" in tables:
        union_parts.append(
            f"SELECT DISTINCT block_number FROM eatlq.raw_evm_logs "
            f"WHERE chain_id={chain_id} AND block_time IS NULL AND block_number IN ({in_list})"
        )
    if "v2" in tables:
        union_parts.append(
            f"SELECT DISTINCT block_number FROM eatlq.uniswap_v2_swaps "
            f"WHERE chain_id={chain_id} AND block_time IS NULL AND block_number IN ({in_list})"
        )
    if "v3" in tables:
        union_parts.append(
            f"SELECT DISTINCT block_number FROM eatlq.uniswap_v3_swaps "
            f"WHERE chain_id={chain_id} AND block_time IS NULL AND block_number IN ({in_list})"
        )
    if not union_parts:
        return []
    sql = f"""
    SELECT block_number FROM (
        {' UNION DISTINCT '.join(union_parts)}
    ) ORDER BY block_number LIMIT {int(max_take)}
    """
    raw = ch_query(sql, container).strip()
    if not raw:
        return []
    return [int(x) for x in raw.split("\n") if x]


def count_null_stats(container: str, chain_id: int, registry_path: Path) -> dict[str, Any]:
    """Aggregate null_bt counts; ages use received_at_ms (ingest clock)."""
    out: dict[str, Any] = {}
    reg = load_registry(registry_path)
    known_list = sorted(
        {int(e["block_number"]) for e in reg.get("entries", []) if e.get("status") == "archive_miss"}
    )
    known_excl = ""
    if known_list:
        known_excl = f" AND block_number NOT IN ({','.join(str(k) for k in known_list)})"
    for name, tbl, cond_base in [
        ("raw_evm_logs", "eatlq.raw_evm_logs", "block_time IS NULL"),
        ("uniswap_v3_swaps", "eatlq.uniswap_v3_swaps", f"chain_id = {chain_id} AND block_time IS NULL"),
        ("uniswap_v2_swaps", "eatlq.uniswap_v2_swaps", f"chain_id = {chain_id} AND block_time IS NULL"),
    ]:
        row = ch_query(
            f"""SELECT count(), countDistinct(block_number),
                if(min(received_at_ms) = toDateTime64(0,3,'UTC'), NULL,
                   dateDiff('minute', min(received_at_ms), now()))
            FROM {tbl} WHERE {cond_base}""",
            container,
        ).strip()
        parts = row.split("\t")
        out[name] = {
            "null_rows": int(parts[0]) if parts[0] and parts[0] != "\\N" else 0,
            "null_distinct_blocks": int(parts[1]) if len(parts) > 1 and parts[1] and parts[1] != "\\N" else 0,
            "oldest_null_age_minutes": (
                int(parts[2])
                if len(parts) > 2 and parts[2] and parts[2] != "\\N"
                else None
            ),
        }
        rep_row = ch_query(
            f"""SELECT countDistinct(block_number),
                if(min(received_at_ms) = toDateTime64(0,3,'UTC'), NULL,
                   dateDiff('minute', min(received_at_ms), now()))
            FROM {tbl}
            WHERE {cond_base} AND block_number > 0
              {known_excl}""",
            container,
        ).strip()
        rp = rep_row.split("\t")
        r_key = {
            "raw_evm_logs": "raw",
            "uniswap_v3_swaps": "v3",
            "uniswap_v2_swaps": "v2",
        }[name]
        out[f"{r_key}_repairable_distinct_blocks"] = int(rp[0]) if rp[0] and rp[0] != "\\N" else 0
        age_val = None
        if len(rp) > 1 and rp[1] and rp[1] != "\\N":
            try:
                age_val = int(rp[1])
            except ValueError:
                age_val = None
        out[f"{r_key}_repairable_oldest_null_age_minutes"] = age_val

    out["archive_miss_known_blocks"] = known_list
    out["invalid_sentinel_blocks"] = sorted(
        int(e["block_number"])
        for e in reg.get("entries", [])
        if e.get("status") == "invalid_sentinel"
    )
    bm_raw = ch_query(
        f"SELECT count() FROM eatlq.block_ts_map WHERE chain_id={chain_id}", container
    ).strip()
    out["block_ts_map_count"] = int(bm_raw) if bm_raw else 0
    return out


def health_verdict(stats: dict[str, Any], apply_error: str | None) -> tuple[str, str]:
    if apply_error:
        return "BLOCKED", apply_error
    repairable_blocks = sum(
        stats.get(f"{x}_repairable_distinct_blocks", 0) for x in ("raw", "v3", "v2")
    )
    ages: list[int] = []
    for x in ("raw", "v3", "v2"):
        a = stats.get(f"{x}_repairable_oldest_null_age_minutes")
        if a is not None:
            ages.append(int(a))
    if repairable_blocks == 0 and stats["raw_evm_logs"]["null_rows"] + stats["uniswap_v3_swaps"]["null_rows"] + stats["uniswap_v2_swaps"]["null_rows"] > 0:
        return "HEALTHY", "only_known_archive_miss_residuals"
    if repairable_blocks == 0:
        return "HEALTHY", "no_repairable_null_blocks"
    if not ages:
        return "HEALTHY", "repairable_blocks_no_age_metric"
    oldest = max(ages)
    if oldest > 12 * 60:
        return "DEGRADED", f"repairable_oldest_null_age_minutes={oldest}"
    if oldest > 4 * 60:
        return "LAGGING", f"repairable_oldest_null_age_minutes={oldest}"
    return "HEALTHY", f"repairable_oldest_null_age_minutes={oldest}"


def run_raw_evm_logs_mutation(container: str) -> None:
    ch_query("SYSTEM RELOAD DICTIONARY eatlq.block_ts_dict", container)
    ch_query(
        """ALTER TABLE eatlq.raw_evm_logs
        UPDATE block_time = dictGet(
          'eatlq.block_ts_dict', 'block_time',
          (toUInt64(chain_id), toUInt64(block_number))
        )
        WHERE block_time IS NULL
          AND dictHas(
            'eatlq.block_ts_dict',
            (toUInt64(chain_id), toUInt64(block_number))
          )""",
        container,
    )


def wait_mutation(container: str, table: str, mutation_id: str | None) -> None:
    if not mutation_id:
        return
    for _ in range(40):
        row = ch_query(
            f"""SELECT is_done FROM system.mutations
            WHERE database='eatlq' AND table='{table}' AND mutation_id='{mutation_id}'
            LIMIT 1""",
            container,
        ).strip()
        if row == "1":
            return
        if not row:
            return
        time.sleep(2)
    raise RuntimeError(f"mutation {mutation_id} on {table} did not finish in time")


def load_backfill_module():
    """Load scripts/backfill_block_timestamps.py after env for RPC is set."""
    path = SCRIPTS_DIR / "backfill_block_timestamps.py"
    spec = importlib.util.spec_from_file_location("backfill_block_timestamps", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load backfill_block_timestamps.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_block_timestamps"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description="Null block_time scheduled backfill guard (Sprint064)")
    parser.add_argument("--smoke", action="store_true", help="Small apply run (caps --max-blocks)")
    parser.add_argument("--max-blocks", type=int, default=5000, help="RPC fetch cap per run (default 5000)")
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=6,
        help="Only consider null rows with received_at_ms within this window (default 6)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan only; no RPC or mutations")
    parser.add_argument("--apply", action="store_true", help="Fetch RPC + mutations + registry updates")
    parser.add_argument(
        "--tables",
        default="raw,v2,v3",
        help="Comma list: raw,v2,v3 (default all)",
    )
    parser.add_argument(
        "--registry-path",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="archive_miss_blocks.json path",
    )
    parser.add_argument(
        "--registry-retry",
        action="store_true",
        help="Also consider archive_miss registry blocks past min_retry_hours (ignores max-age filter)",
    )
    parser.add_argument("--container", default=os.environ.get("CH_CONTAINER", "eatlq-clickhouse-1"))
    args = parser.parse_args()

    tables = {t.strip() for t in args.tables.split(",") if t.strip()}
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    if args.smoke:
        args.apply = True
        args.max_blocks = min(args.max_blocks, 20)
        args.max_age_hours = max(args.max_age_hours, 24)
    if args.dry_run:
        args.apply = False

    rpc_url = resolve_rpc_url()
    rpc_prefix = _rpc_sha256_prefix(rpc_url) if rpc_url else "none"

    reg = load_registry(args.registry_path)
    now = datetime.now(timezone.utc)
    skip_registry = registry_skip_set(reg, now)

    stats_before = count_null_stats(args.container, chain_id, args.registry_path)
    discovered: list[int] = []
    apply_error: str | None = None
    blocks_fetched = 0
    backlog_warn = False

    try:
        discovered = discover_blocks(
            args.container,
            chain_id,
            tables,
            args.max_age_hours,
            args.max_blocks,
            skip_registry,
        )
        if args.registry_retry:
            retry_budget = max(0, int(args.max_blocks) - len(discovered))
            if retry_budget > 0:
                retry_blocks = registry_retry_block_numbers(
                    args.container, chain_id, tables, reg, now, retry_budget
                )
                merged: list[int] = []
                seen: set[int] = set()
                for b in discovered + retry_blocks:
                    if b not in seen:
                        seen.add(b)
                        merged.append(b)
                discovered = merged[: int(args.max_blocks)]
        if len(discovered) >= args.max_blocks:
            backlog_warn = True
    except Exception as e:
        apply_error = f"discover_failed:{e}"

    log_lines = [
        f"[{_utc_now_iso()}] sprint064_null_bt_guard",
        f"  rpc_masked={_mask_rpc(rpc_url)} rpc_sha256_prefix={rpc_prefix}",
        f"  max_age_hours={args.max_age_hours} max_blocks={args.max_blocks} tables={sorted(tables)}",
        f"  registry_skip={len(skip_registry)} discovered={len(discovered)}",
        f"  BACKFILL_LAG_HIGH={backlog_warn}",
    ]

    if args.dry_run or not args.apply:
        log_lines.append("  mode=dry_run_or_plan_only no_rpc")
    elif not rpc_url:
        apply_error = "rpc_url_missing"
        log_lines.append("  ERROR: LABEL_RPC_URL and EVM_HTTP_URL both unset")
    elif apply_error is None and discovered:
        os.environ["LABEL_RPC_URL"] = rpc_url
        os.environ["CHAIN_ID"] = str(chain_id)
        os.environ.setdefault("BATCH_SIZE", "40")
        os.environ.setdefault("CONCURRENCY", "2")
        os.environ.setdefault("MAX_CPS", "40")
        os.environ["CH_CONTAINER"] = args.container
        os.environ["WAIT_FOR_MUTATIONS"] = "1"
        try:
            bbt = load_backfill_module()
            blocks_fetched = bbt.fetch_all(discovered)
            in_list = ",".join(str(b) for b in discovered)
            got_raw = ch_query(
                f"""SELECT DISTINCT block_number FROM eatlq.block_ts_map
                WHERE chain_id={chain_id} AND block_number IN ({in_list})""",
                args.container,
            ).strip()
            got_set = {int(x) for x in got_raw.split("\n") if x}
            still_missing = [b for b in discovered if b not in got_set]
            if still_missing:
                merge_registry_misses(
                    args.registry_path, still_missing, rpc_prefix, "backfill_null_block"
                )
            for tbl in ("uniswap_v2_swaps", "uniswap_v3_swaps"):
                mid = bbt.run_mutation(tbl)
                bbt.wait_mutation(tbl, mid)
            run_raw_evm_logs_mutation(args.container)
            mid_raw = ch_query(
                """SELECT mutation_id FROM system.mutations
                WHERE database='eatlq' AND table='raw_evm_logs' AND is_done=0
                ORDER BY create_time DESC LIMIT 1""",
                args.container,
            ).strip()
            wait_mutation(args.container, "raw_evm_logs", mid_raw or None)
        except Exception as e:
            apply_error = f"apply_failed:{e}"
            log_lines.append(f"  ERROR {e}")

    stats_after = count_null_stats(args.container, chain_id, args.registry_path)
    verdict, verdict_detail = health_verdict(stats_after, apply_error)

    reg_flags = load_registry(args.registry_path)
    block0_registry_explained = any(
        int(e.get("block_number", -1)) == 0 and e.get("status") == "invalid_sentinel"
        for e in reg_flags.get("entries", [])
    )

    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        reg_path_rel = str(args.registry_path.relative_to(REPO_ROOT))
    except ValueError:
        reg_path_rel = str(args.registry_path)
    latest_payload = {
        "timestamp_utc": _utc_now_iso(),
        "apply_mode": bool(args.apply),
        "dry_run": bool(args.dry_run),
        "smoke": bool(args.smoke),
        "max_blocks": args.max_blocks,
        "max_age_hours": args.max_age_hours,
        "tables": sorted(tables),
        "registry_retry": bool(args.registry_retry),
        "rpc_masked": _mask_rpc(rpc_url),
        "rpc_sha256_prefix": rpc_prefix,
        "registry_path": reg_path_rel,
        "discovered_block_count": len(discovered),
        "blocks_rpc_attempted": len(discovered) if args.apply and discovered and not apply_error else 0,
        "rows_inserted_block_ts_map": blocks_fetched,
        "backlog_at_cap": backlog_warn,
        "BACKFILL_LAG_HIGH": backlog_warn,
        "apply_error": apply_error,
        "stats_before": stats_before,
        "stats_after": stats_after,
        "health_verdict": verdict,
        "health_verdict_detail": verdict_detail,
        "last_successful_backfill_at": _utc_now_iso() if args.apply and not apply_error else None,
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(latest_payload, f, indent=2)
        f.write("\n")

    health_payload = {
        "timestamp_utc": _utc_now_iso(),
        "raw_evm_logs_null_rows": stats_after["raw_evm_logs"]["null_rows"],
        "raw_evm_logs_null_distinct_blocks": stats_after["raw_evm_logs"]["null_distinct_blocks"],
        "raw_evm_logs_oldest_null_age_minutes": stats_after["raw_evm_logs"]["oldest_null_age_minutes"],
        "v3_null_rows": stats_after["uniswap_v3_swaps"]["null_rows"],
        "v3_null_distinct_blocks": stats_after["uniswap_v3_swaps"]["null_distinct_blocks"],
        "v2_null_rows": stats_after["uniswap_v2_swaps"]["null_rows"],
        "v2_null_distinct_blocks": stats_after["uniswap_v2_swaps"]["null_distinct_blocks"],
        "raw_repairable_null_distinct_blocks": stats_after.get("raw_repairable_distinct_blocks"),
        "v3_repairable_null_distinct_blocks": stats_after.get("v3_repairable_distinct_blocks"),
        "v2_repairable_null_distinct_blocks": stats_after.get("v2_repairable_distinct_blocks"),
        "raw_repairable_oldest_null_age_minutes": stats_after.get("raw_repairable_oldest_null_age_minutes"),
        "v3_repairable_oldest_null_age_minutes": stats_after.get("v3_repairable_oldest_null_age_minutes"),
        "v2_repairable_oldest_null_age_minutes": stats_after.get("v2_repairable_oldest_null_age_minutes"),
        "repairable_null_distinct_blocks_total": sum(
            int(stats_after.get(f"{x}_repairable_distinct_blocks") or 0) for x in ("raw", "v3", "v2")
        ),
        "RAW_EVM_LOGS_REPAIRABLE_NULL_LAG_MINUTES": stats_after.get(
            "raw_repairable_oldest_null_age_minutes"
        ),
        "archive_miss_known_blocks": stats_after["archive_miss_known_blocks"],
        "invalid_sentinel_blocks": stats_after.get("invalid_sentinel_blocks", []),
        "block_ts_map_count": stats_after["block_ts_map_count"],
        "last_successful_backfill_at": latest_payload["last_successful_backfill_at"],
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "decision_flags": {
            "NULL_BT_BACKFILL_SCRIPT_READY": True,
            "ARCHIVE_MISS_REGISTRY_READY": bool(stats_after.get("archive_miss_known_blocks")),
            "SCHEDULED_BACKFILL_READY": True,
            "CRON_INSTALLED": crontab_has_null_bt_job(),
            "CRON_ENV_OK": cron_env_ok(),
            "RAW_EVM_LOGS_REPAIRABLE_NULL_LAG_MINUTES": stats_after.get(
                "raw_repairable_oldest_null_age_minutes"
            ),
            "V3_NULL_BT_HEALTHY": int(stats_after.get("v3_repairable_distinct_blocks") or 0) == 0,
            "V2_NULL_BT_HEALTHY": int(stats_after.get("v2_repairable_distinct_blocks") or 0) == 0,
            "KNOWN_ARCHIVE_MISS_ONLY": verdict == "HEALTHY"
            and verdict_detail == "only_known_archive_miss_residuals",
            "NULL_BT_HEALTHY": verdict == "HEALTHY",
            "MANUAL_CRON_EQUIVALENT_PASS": os.environ.get("EATLQ_NULL_BT_CRON_WRAP") == "1"
            and not apply_error,
            "BLOCK_0_REGISTRY_EXPLAINED": block0_registry_explained,
            "RPC_SECRET_PRINTED": False,
            "LIVE_ALLOWED": False,
            "PRIVATE_KEY_USED": False,
            "PRODUCTION_SIGNALS_WRITTEN": False,
            "TRAINING_ALLOWED": False,
            "NEXT_ALLOWED_ACTION": "monitor logs/null_bt_backfill_cron.log and MVP health JSON",
        },
    }
    with open(HEALTH_JSON, "w", encoding="utf-8") as f:
        json.dump(health_payload, f, indent=2)
        f.write("\n")

    for line in log_lines:
        print(line, flush=True)
    print(f"  verdict={verdict} ({verdict_detail})", flush=True)
    print(f"  wrote {LATEST_JSON.relative_to(REPO_ROOT)}", flush=True)
    print(f"  wrote {HEALTH_JSON.relative_to(REPO_ROOT)}", flush=True)

    REPO_ROOT.joinpath("logs").mkdir(exist_ok=True)
    with open(CRON_LOG, "a", encoding="utf-8") as lf:
        lf.write("\n".join(log_lines) + "\n")
        lf.write(f"  verdict={verdict} ({verdict_detail})\n")

    if apply_error:
        return 3
    if backlog_warn:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
