#!/usr/bin/env python3
"""One-shot ClickHouse system.* metadata inventory under fail-closed gates.

Does NOT open business-table queries, EXPLAIN, RPC, HTTP, or backfill.
Does NOT flip config/defaults.json access switches.
Creates only an ephemeral capability token for this process (deleted on exit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/root/lpi_base_production_safe_node")
sys.path.insert(0, str(ROOT))

from launchpad_intelligence.errors import AccessDeniedError, PreflightError  # noqa: E402
from launchpad_intelligence.evidence.bundle import RemoteEvidenceBundleExporter  # noqa: E402
from launchpad_intelligence.evidence.run_metadata import write_run_metadata  # noqa: E402
from launchpad_intelligence.evidence.verify import verify_bundle_archive  # noqa: E402
from launchpad_intelligence.health.metadata_gate import (  # noqa: E402
    MetadataHealthSnapshot,
    evaluate_metadata_health,
    read_host_basics,
)
from launchpad_intelligence.query.metadata_inventory import (  # noqa: E402
    MetadataInventoryPlan,
    MetadataPlanRegistry,
    assert_sql_matches_plan,
    build_default_schema_inventory_plans,
    build_health_system_plans,
    require_execution_authorized,
)

TABLES_WANTED = (
    "database",
    "name",
    "engine",
    "partition_key",
    "sorting_key",
    "primary_key",
    "sampling_key",
    "storage_policy",
)
COLUMNS_WANTED = (
    "database",
    "table",
    "name",
    "type",
    "position",
    "default_kind",
    "default_expression",
    "is_in_partition_key",
    "is_in_sorting_key",
    "is_in_primary_key",
    "is_in_sampling_key",
)
from launchpad_intelligence.query.metadata_ranking import rank_tables  # noqa: E402
from launchpad_intelligence.query.runner_status import (  # noqa: E402
    RunnerExitCode,
    derive_round_status,
)
from launchpad_intelligence.health.command_executor import ExternalCommandExecutor  # noqa: E402
from launchpad_intelligence.security.auth_gate import (  # noqa: E402
    CAP_METADATA_INVENTORY,
    capability_token_path,
    require_capability,
)
from launchpad_intelligence.security.execution_seal import (  # noqa: E402
    assert_seal_unchanged,
    register_run_owned_outputs,
    require_clean_sealed_worktree,
)

CLICKHOUSE_CONTAINER = "eatlq-clickhouse-1"
REDIS_CONTAINER = "eatlq-redis-1"
PRODUCTION_CONTAINERS = (
    "eatlq-executor-1",
    "eatlq-signal-bridge-1",
    "eatlq-archiver-1",
    "eatlq-ingestor-1",
    "eatlq-clickhouse-1",
    "eatlq-postgres-1",
    "eatlq-redis-1",
    "eatlq-swap-poller-1",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_cmd(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def docker_inspect_containers(
    names: tuple[str, ...],
    executor: ExternalCommandExecutor,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        argv = ["docker", "inspect", "--format", "{{.State.Status}} {{.RestartCount}}", name]
        cp = executor.run(
            argv,
            command_id=f"docker_inspect_{name}",
            command_class="docker_inspect",
            timeout_sec=5,
            argv_sanitized=argv,
        )
        if cp.returncode != 0:
            out[name] = {
                "state": None,
                "restart_count": None,
                "restart_growth": False,
                "error": (cp.stderr or "").strip(),
            }
            continue
        parts = (cp.stdout or "").strip().split()
        state = parts[0] if parts else None
        try:
            rc = int(parts[1]) if len(parts) > 1 else None
        except ValueError:
            rc = None
        out[name] = {"state": state, "restart_count": rc, "restart_growth": False}
    return out



def redis_executor_pending(executor: ExternalCommandExecutor) -> int | None:
    """Single read-only XINFO CONSUMERS; no redundant redis-cli --raw probe."""
    argv = [
        "docker",
        "exec",
        REDIS_CONTAINER,
        "redis-cli",
        "XINFO",
        "CONSUMERS",
        "signals",
        "execution_group",
    ]
    cp = executor.run(
        argv,
        command_id="redis_xinfo_consumers",
        command_class="redis_health",
        timeout_sec=5,
        argv_sanitized=argv,
    )
    if cp.returncode != 0:
        return None
    text = cp.stdout
    # Sum pending across consumers if present
    pending_total = 0
    found = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "pending" and i + 1 < len(lines):
            try:
                pending_total += int(lines[i + 1].strip())
                found = True
            except ValueError:
                return None
    return pending_total if found else None


def clickhouse_query(
    plan: MetadataInventoryPlan,
    *,
    self_query_marker: str,
    executor: ExternalCommandExecutor,
) -> dict[str, Any]:
    require_execution_authorized(plan)
    sql = plan.render_sql()
    assert_sql_matches_plan(plan, sql)
    settings = plan.settings_dict()
    query_id = f"lpi_meta_{plan.query_id}_{int(time.time())}"
    argv = [
        "timeout",
        "5",
        "docker",
        "exec",
        CLICKHOUSE_CONTAINER,
        "clickhouse-client",
        "--readonly",
        str(settings["readonly"]),
        "--max_threads",
        str(settings["max_threads"]),
        "--max_execution_time",
        str(settings["max_execution_time"]),
        "--max_result_rows",
        str(settings["max_result_rows"]),
        "--max_result_bytes",
        str(settings["max_result_bytes"]),
        "--max_bytes_to_read",
        str(settings["max_bytes_to_read"]),
        "--format",
        "TSVWithNames",
        "--query_id",
        query_id,
        "--query",
        sql,
    ]
    sanitized_argv = [
        "timeout",
        "5",
        "docker",
        "exec",
        CLICKHOUSE_CONTAINER,
        "clickhouse-client",
        f"--readonly={settings['readonly']}",
        f"--max_threads={settings['max_threads']}",
        f"--max_execution_time={settings['max_execution_time']}",
        f"--max_result_rows={settings['max_result_rows']}",
        f"--max_result_bytes={settings['max_result_bytes']}",
        f"--max_bytes_to_read={settings['max_bytes_to_read']}",
        "--format=TSVWithNames",
        f"--query_id={query_id}",
        f"--query=<hash:{plan.query_hash()}>",
    ]
    started = utc_now()
    t0 = time.monotonic()
    cp = executor.run(
        argv,
        command_id=f"ch_{plan.query_id}_{self_query_marker}",
        command_class="clickhouse_system",
        timeout_sec=6,
        argv_sanitized=sanitized_argv,
    )
    elapsed = time.monotonic() - t0
    ended = utc_now()
    stdout = cp.stdout or ""
    stderr = cp.stderr or ""
    timed_out = bool(executor.records and executor.records[-1].timeout)
    out_bytes = len(stdout.encode("utf-8", errors="replace"))
    rows = 0
    if stdout.strip():
        rows = max(0, len(stdout.splitlines()) - 1)
    hit_row_cap = rows >= plan.max_result_rows
    hit_byte_cap = out_bytes >= plan.max_result_bytes
    return {
        "started_utc": started,
        "ended_utc": ended,
        "elapsed_sec": round(elapsed, 4),
        "exit_code": cp.returncode,
        "timed_out": timed_out,
        "row_count": rows,
        "output_bytes": out_bytes,
        "hit_row_cap": hit_row_cap,
        "hit_byte_cap": hit_byte_cap,
        "stderr": stderr[-4000:],
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8", errors="replace")).hexdigest(),
        "command_sanitized": {
            "argv_sanitized": sanitized_argv,
            "sql": sql,
            "query_hash": plan.query_hash(),
            "settings": settings,
            "self_query_marker": self_query_marker,
            "query_id": query_id,
        },
        "stdout_text": stdout,
        "query_hash": plan.query_hash(),
        "query_id_runtime": query_id,
    }



def parse_tsv_with_names(text: str) -> list[dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        return []
    headers = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for ln in lines[1:]:
        parts = ln.split("\t")
        row = {headers[i]: (parts[i] if i < len(parts) else "") for i in range(len(headers))}
        rows.append(row)
    return rows


def count_active_queries(result: dict[str, Any], self_query_id: str) -> int | None:
    if result.get("exit_code") != 0:
        return None
    rows = parse_tsv_with_names(result.get("stdout_text", ""))
    n = 0
    for row in rows:
        qid = row.get("query_id", "")
        if qid == self_query_id:
            continue
        # exclude other lpi_meta health probes in same second if any
        n += 1
    return n


def write_json(path: Path, obj: Any, *, allow_replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refuse symlink {path}")
    if path.exists() and not allow_replace:
        raise RuntimeError(f"refuse overwrite {path}")
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_ephemeral_token() -> Path:
    token = capability_token_path(CAP_METADATA_INVENTORY)
    token.parent.mkdir(parents=True, exist_ok=True)
    if token.exists() or token.is_symlink():
        raise RuntimeError(f"token already exists; refuse long-lived reuse without cleanup: {token}")
    token.write_text(f"AUTHORIZED:{CAP_METADATA_INVENTORY}\n", encoding="utf-8")
    os.chmod(token, 0o600)
    return token


def remove_token(token: Path) -> None:
    try:
        if token.is_file() and not token.is_symlink():
            token.unlink()
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorize-metadata-schema-inventory", action="store_true")
    parser.add_argument("--app-database", default="eatlq")
    parser.add_argument("--dry-run-plans-only", action="store_true", help="offline: write plans, no docker/CH")
    args = parser.parse_args()

    token_path = None
    execution_log: list[dict[str, Any]] = []
    ch_calls = {"count": 0}
    seal = None
    executor = ExternalCommandExecutor()
    round_exit = int(RunnerExitCode.INTERNAL_ERROR)

    try:
        if not args.dry_run_plans_only:
            # Seal BEFORE any production-adjacent command or token install side effects that query.
            try:
                seal = require_clean_sealed_worktree(ROOT)
            except AccessDeniedError as exc:
                print(f"EXECUTION_SOURCE_UNSEALED: {exc}", file=sys.stderr)
                return int(RunnerExitCode.EXECUTION_SOURCE_UNSEALED)
            executor.source_commit = seal.head_commit
            executor.source_tree_hash = seal.head_tree
            executor.authorized_capability = CAP_METADATA_INVENTORY
            token_path = install_ephemeral_token()
            require_capability(CAP_METADATA_INVENTORY, explicit_cli_flag=args.authorize_metadata_schema_inventory)

        stamp = utc_stamp()
        evidence_dir = ROOT / "evidence" / f"base_metadata_inventory_{stamp}"
        if evidence_dir.exists():
            raise RuntimeError(f"evidence dir exists: {evidence_dir}")
        evidence_dir.mkdir(parents=True)
        results_dir = evidence_dir / "query_results"
        results_dir.mkdir()

        bundle_path_planned = ROOT / "evidence" / f"base_metadata_inventory_{stamp}.tar.gz"
        staging_planned = ROOT / "var" / "tmp" / f"bundle_stage_{stamp}"
        if seal is not None:
            seal = register_run_owned_outputs(
                seal,
                evidence_dir=evidence_dir,
                staging_dir=staging_planned,
                bundle_tar=bundle_path_planned,
                token_path=token_path,
            )

        if seal is not None:
            source_commit = seal.head_commit
            source_tree = seal.head_tree
        else:
            source_commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
            source_tree = subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}"], text=True
            ).strip()
        write_run_metadata(
            evidence_dir,
            {
                "round_id": "metadata_schema_inventory",
                "artifact_class": "BASE_CLICKHOUSE_METADATA_INVENTORY",
                "source_commit": source_commit,
                "source_tree_hash": source_tree,
                "execution_source_state": "SEALED_HEAD" if seal is not None else "DRY_RUN",
                "created_at": utc_now(),
            },
        )

        plans = build_default_schema_inventory_plans(
            app_database=args.app_database,
            execution_authorized=False,
        )
        health_plans = build_health_system_plans(execution_authorized=False)
        registry = MetadataPlanRegistry.from_plans(plans + health_plans)
        write_json(evidence_dir / "query_plans.json", {"plans": registry.to_list(), "execution_performed": False})

        if args.dry_run_plans_only:
            write_json(
                evidence_dir / "health_before.json",
                {"status": "SKIPPED_DRY_RUN", "execution_performed": False},
            )
            write_json(evidence_dir / "command_log_sanitized.json", {"entries": [], "ch_calls": 0})
            _st, code, st = derive_round_status(dry_run=True)
            write_json(evidence_dir / "runner_state.json", st.to_dict())
            _write_reports(
                evidence_dir=evidence_dir,
                ranked=[],
                health_before={"status": "SKIPPED_DRY_RUN"},
                health_after=None,
                execution_performed=False,
                blocked_reason="dry_run_plans_only",
                query_exec=[],
                source_commit=source_commit,
            )
            return int(code)

        assert seal is not None
        assert_seal_unchanged(seal)

        # --- live health ---
        host = read_host_basics()
        containers = docker_inspect_containers(PRODUCTION_CONTAINERS, executor)
        pending = redis_executor_pending(executor)

        # authorize health plans only
        h_proc = MetadataInventoryPlan(**{**health_plans[0].__dict__, "execution_authorized": True})
        h_merge = MetadataInventoryPlan(**{**health_plans[1].__dict__, "execution_authorized": True})
        ch_calls["count"] += 1
        proc_res = clickhouse_query(h_proc, self_query_marker="health_processes", executor=executor)
        execution_log.append({k: v for k, v in proc_res.items() if k != "stdout_text"})
        (results_dir / "health_before_processes.tsv").write_text(proc_res.get("stdout_text", ""), encoding="utf-8")
        active_q = count_active_queries(proc_res, proc_res.get("query_id_runtime", ""))

        ch_calls["count"] += 1
        merge_res = clickhouse_query(h_merge, self_query_marker="health_merges", executor=executor)
        execution_log.append({k: v for k, v in merge_res.items() if k != "stdout_text"})
        (results_dir / "health_before_merges.tsv").write_text(merge_res.get("stdout_text", ""), encoding="utf-8")
        merges = None
        if merge_res.get("exit_code") == 0:
            merges = max(0, merge_res.get("row_count", 0))

        snap = MetadataHealthSnapshot(
            timestamp_utc=utc_now(),
            load_1m=host["load_1m"],
            nproc=host["nproc"],
            mem_available_kb=host["mem_available_kb"],
            swap_used_kb=host["swap_used_kb"],
            disk_free_bytes=host["disk_free_bytes"],
            containers=containers,
            executor_pending=pending,
            clickhouse_active_queries_excl_self=active_q,
            clickhouse_active_merges=merges,
            notes=tuple(host.get("notes") or []),
            unknown_fields=tuple(host.get("unknown_fields") or ()),
        )
        # If health CH queries failed, force unknown
        unknown = list(snap.unknown_fields)
        if proc_res.get("exit_code") != 0 or proc_res.get("timed_out"):
            unknown.append("clickhouse_active_queries")
            active_q = None
        if merge_res.get("exit_code") != 0 or merge_res.get("timed_out"):
            unknown.append("clickhouse_active_merges")
            merges = None
        snap = MetadataHealthSnapshot(
            timestamp_utc=snap.timestamp_utc,
            load_1m=snap.load_1m,
            nproc=snap.nproc,
            mem_available_kb=snap.mem_available_kb,
            swap_used_kb=snap.swap_used_kb,
            disk_free_bytes=snap.disk_free_bytes,
            containers=snap.containers,
            executor_pending=snap.executor_pending,
            clickhouse_active_queries_excl_self=active_q,
            clickhouse_active_merges=merges,
            notes=snap.notes,
            unknown_fields=tuple(dict.fromkeys(unknown)),
        )
        decision = evaluate_metadata_health(snap)
        write_json(evidence_dir / "health_before.json", {"decision": decision.to_dict(), "snapshot": snap.to_dict()})

        execution_performed = False
        ranked: list[Any] = []
        query_exec: list[dict[str, Any]] = []
        blocked_reason = None

        if decision.status != "SAFE_TO_PROCEED":
            blocked_reason = decision.status
            write_json(
                evidence_dir / "query_plans.json",
                {"plans": registry.to_list(), "execution_performed": False, "blocked": decision.to_dict()},
                allow_replace=True,
            )
        else:
            # Execute introspection first, then seal app plans from confirmed columns.
            stop = False
            tables_rows: list[dict[str, str]] = []
            columns_rows: list[dict[str, str]] = []
            intro_plans = [
                MetadataInventoryPlan(**{**p.__dict__, "execution_authorized": True})
                for p in plans
                if p.query_id in {"q_sys_tables_columns", "q_sys_columns_columns", "q_app_databases"}
            ]
            available_table_cols: set[str] = set()
            available_column_cols: set[str] = set()
            sealed: list[MetadataInventoryPlan] = list(intro_plans)

            for p in intro_plans:
                ch_calls["count"] += 1
                res = clickhouse_query(p, self_query_marker=p.query_id, executor=executor)
                meta = {k: v for k, v in res.items() if k != "stdout_text"}
                query_exec.append(meta)
                execution_log.append(meta)
                (results_dir / f"{p.query_id}.tsv").write_text(res.get("stdout_text", ""), encoding="utf-8")
                if res.get("timed_out") or res.get("exit_code") != 0 or res.get("hit_byte_cap"):
                    stop = True
                    blocked_reason = f"query_failed:{p.query_id}"
                    break
                rows = parse_tsv_with_names(res.get("stdout_text", ""))
                if p.query_id == "q_sys_tables_columns":
                    available_table_cols = {r.get("name", "") for r in rows}
                if p.query_id == "q_sys_columns_columns":
                    available_column_cols = {r.get("name", "") for r in rows}
                if p.query_id == "q_app_databases" and not rows:
                    stop = True
                    blocked_reason = "app_database_missing"
                    break
                execution_performed = True

            if not stop:
                tcols = tuple(c for c in TABLES_WANTED if c in available_table_cols)
                ccols = tuple(c for c in COLUMNS_WANTED if c in available_column_cols)
                if len(tcols) < 4 or len(ccols) < 4:
                    stop = True
                    blocked_reason = "insufficient_system_catalog_columns"
                else:
                    app_plans = [
                        MetadataInventoryPlan(
                            query_id="q_app_tables",
                            purpose="list_app_table_schema_metadata",
                            system_table="system.tables",
                            explicit_columns=tcols,
                            database_filter=args.app_database,
                            limit=5000,
                            execution_authorized=True,
                        ),
                        MetadataInventoryPlan(
                            query_id="q_app_columns",
                            purpose="list_app_column_schema_metadata",
                            system_table="system.columns",
                            explicit_columns=ccols,
                            database_filter=args.app_database,
                            limit=5000,
                            execution_authorized=True,
                        ),
                    ]
                    for p in app_plans:
                        p.validate()
                    sealed.extend(app_plans)
                    write_json(
                        evidence_dir / "query_plans.json",
                        {
                            "plans": MetadataPlanRegistry.from_plans(sealed).to_list(),
                            "execution_performed": True,
                            "available_table_cols": sorted(available_table_cols),
                            "available_column_cols": sorted(available_column_cols),
                        },
                        allow_replace=True,
                    )
                    for p in app_plans:
                        ch_calls["count"] += 1
                        res = clickhouse_query(p, self_query_marker=p.query_id, executor=executor)
                        meta = {k: v for k, v in res.items() if k != "stdout_text"}
                        query_exec.append(meta)
                        execution_log.append(meta)
                        (results_dir / f"{p.query_id}.tsv").write_text(res.get("stdout_text", ""), encoding="utf-8")
                        if res.get("timed_out") or res.get("exit_code") != 0 or res.get("hit_byte_cap"):
                            stop = True
                            blocked_reason = f"query_failed:{p.query_id}"
                            break
                        rows = parse_tsv_with_names(res.get("stdout_text", ""))
                        if rows:
                            missing_cols = [c for c in p.explicit_columns if c not in rows[0]]
                            if missing_cols:
                                stop = True
                                blocked_reason = f"schema_mismatch:{p.query_id}:{missing_cols}"
                                break
                        if p.query_id == "q_app_tables":
                            tables_rows = rows
                        if p.query_id == "q_app_columns":
                            columns_rows = rows
                        execution_performed = True

            if execution_performed and tables_rows and not stop:
                ranked_objs = rank_tables(
                    tables_rows=tables_rows,
                    columns_rows=columns_rows,
                    evidence_refs=[str(evidence_dir.relative_to(ROOT))],
                )
                ranked = [c.to_dict() for c in ranked_objs]
                write_json(evidence_dir / "candidates_ranked.json", {"candidates": ranked})

            # post health
            host2 = read_host_basics()
            containers2 = docker_inspect_containers(PRODUCTION_CONTAINERS, executor)
            # mark restart growth vs before
            for name, meta in containers2.items():
                before_rc = (containers.get(name) or {}).get("restart_count")
                after_rc = meta.get("restart_count")
                if isinstance(before_rc, int) and isinstance(after_rc, int) and after_rc > before_rc:
                    meta["restart_growth"] = True
            pending2 = redis_executor_pending(executor)
            ch_calls["count"] += 1
            proc2 = clickhouse_query(h_proc, self_query_marker="health_processes_after", executor=executor)
            execution_log.append({k: v for k, v in proc2.items() if k != "stdout_text"})
            (results_dir / "health_after_processes.tsv").write_text(proc2.get("stdout_text", ""), encoding="utf-8")
            ch_calls["count"] += 1
            merge2 = clickhouse_query(h_merge, self_query_marker="health_merges_after", executor=executor)
            execution_log.append({k: v for k, v in merge2.items() if k != "stdout_text"})
            (results_dir / "health_after_merges.tsv").write_text(merge2.get("stdout_text", ""), encoding="utf-8")
            active2 = count_active_queries(proc2, proc2.get("query_id_runtime", ""))
            merges2 = merge2.get("row_count") if merge2.get("exit_code") == 0 else None
            snap2 = MetadataHealthSnapshot(
                timestamp_utc=utc_now(),
                load_1m=host2["load_1m"],
                nproc=host2["nproc"],
                mem_available_kb=host2["mem_available_kb"],
                swap_used_kb=host2["swap_used_kb"],
                disk_free_bytes=host2["disk_free_bytes"],
                containers=containers2,
                executor_pending=pending2,
                clickhouse_active_queries_excl_self=active2,
                clickhouse_active_merges=merges2,
                notes=tuple(host2.get("notes") or []),
                unknown_fields=tuple(host2.get("unknown_fields") or ()),
            )
            decision2 = evaluate_metadata_health(snap2)
            write_json(evidence_dir / "health_after.json", {"decision": decision2.to_dict(), "snapshot": snap2.to_dict()})
            if decision2.status != "SAFE_TO_PROCEED":
                blocked_reason = blocked_reason or decision2.status

        # Derive authoritative runner status / exit code (do not treat post-block as success).
        pre_status = decision.status
        post_status = None
        post_collected = (evidence_dir / "health_after.json").exists()
        if post_collected:
            post_status = json.loads((evidence_dir / "health_after.json").read_text())["decision"]["status"]
        schema_planned = 5 if pre_status == "SAFE_TO_PROCEED" else 0
        schema_succeeded = sum(1 for q in query_exec if q.get("exit_code") == 0)
        # schema_complete means all intended schema plans succeeded; post-health is separate.
        schema_complete = bool(
            execution_performed
            and (results_dir / "q_app_tables.tsv").exists()
            and (results_dir / "q_app_columns.tsv").exists()
            and bool(query_exec)
            and all(q.get("exit_code") == 0 for q in query_exec)
        )
        _st, code, st = derive_round_status(
            pre_health_status=pre_status,
            schema_planned=schema_planned if pre_status == "SAFE_TO_PROCEED" else 0,
            schema_succeeded=schema_succeeded,
            schema_complete=schema_complete,
            post_health_status=post_status,
            post_collected=post_collected,
            seal_ok=True,
        )
        st.any_health_query_executed = ch_calls["count"] > 0
        st.any_schema_query_executed = bool(query_exec)
        write_json(evidence_dir / "runner_state.json", st.to_dict())
        round_exit = int(code)

        write_json(
            evidence_dir / "command_log_sanitized.json",
            {
                "entries": execution_log,
                "ch_calls": ch_calls["count"],
                "unified_executor_records": [r.to_dict() for r in executor.records],
                "runner_state": st.to_dict(),
                "exit_code": round_exit,
            },
        )

        # sha256sums
        sum_lines = []
        for path in sorted(evidence_dir.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.name != "sha256sums.tsv":
                rel = path.relative_to(evidence_dir)
                sum_lines.append(f"{sha256_file(path)}\t{rel}")
        (evidence_dir / "sha256sums.tsv").write_text("\n".join(sum_lines) + "\n", encoding="utf-8")

        _write_reports(
            evidence_dir=evidence_dir,
            ranked=ranked,
            health_before=json.loads((evidence_dir / "health_before.json").read_text()),
            health_after=(
                json.loads((evidence_dir / "health_after.json").read_text())
                if (evidence_dir / "health_after.json").exists()
                else None
            ),
            execution_performed=execution_performed,
            blocked_reason=blocked_reason,
            query_exec=query_exec,
            source_commit=source_commit,
        )

        # evidence bundle
        staging = staging_planned
        if staging.exists():
            raise RuntimeError("staging exists")
        staging.mkdir(parents=True)
        # copy evidence files into staging (regular files only)
        for path in evidence_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                rel = path.relative_to(evidence_dir)
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
        bundle_path = bundle_path_planned
        tree = source_tree
        exporter = RemoteEvidenceBundleExporter(ROOT)
        exporter.export(
            staging_dir=staging,
            output_tar=bundle_path,
            bundle_id=f"base_metadata_inventory_{stamp}",
            source_code_commit=source_commit,
            source_tree_hash=tree,
            unknown_count=sum(1 for c in ranked if c.get("candidate_role") == "unknown"),
            golden_results={"on_chain_golden": "NONE", "status": "no_chain_golden"},
        )
        verify_bundle_archive(archive_path=bundle_path, workspace_root=ROOT)

        if seal is not None:
            assert_seal_unchanged(seal)
        print(f"evidence_dir={evidence_dir}")
        print(f"execution_performed={execution_performed}")
        print(f"blocked_reason={blocked_reason}")
        print(f"ch_calls={ch_calls['count']}")
        print(f"bundle={bundle_path}")
        print(f"round_status={st.round_status}")
        print(f"exit_code={round_exit}")
        return round_exit
    except AccessDeniedError as exc:
        print(f"ACCESS_DENIED: {exc}", file=sys.stderr)
        return int(RunnerExitCode.ACCESS_DENIED)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return int(RunnerExitCode.INTERNAL_ERROR)
    finally:
        if token_path is not None:
            remove_token(token_path)


def _write_reports(
    *,
    evidence_dir: Path,
    ranked: list[dict[str, Any]],
    health_before: dict[str, Any],
    health_after: dict[str, Any] | None,
    execution_performed: bool,
    blocked_reason: str | None,
    query_exec: list[dict[str, Any]],
    source_commit: str,
) -> None:
    reports = ROOT / "reports"
    best = ranked[0] if ranked else None
    best_logs = next((c for c in ranked if c.get("candidate_role") == "logs"), None)
    best_elig = next(
        (
            c
            for c in ranked
            if c.get("bounded_probe_safety_status")
            in {"CANDIDATE_UNVERIFIED_DATA", "WEAK_CANDIDATE_UNVERIFIED_DATA"}
        ),
        None,
    )
    candidate_binding_available = bool(best_elig)
    ready_for_review = bool(
        execution_performed
        and best_logs
        and best_logs.get("block_pruning_status") == "VERIFIED_METADATA_PRUNABLE"
        and blocked_reason is None
    )

    # candidate config — never runtime enabled
    cand_path = ROOT / "config" / "base_table_bindings.candidate.json"
    if cand_path.is_symlink():
        raise RuntimeError("candidate path is symlink")
    payload = {
        "status": (
            best_elig.get("bounded_probe_safety_status")
            if best_elig
            else "NO_SAFE_CANDIDATE"
        ),
        "runtime_enabled": False,
        "business_query_authorized": False,
        "source": "clickhouse_system_metadata",
        "review_required": True,
        "primary_logs_candidate": best_logs,
        "best_eligible_candidate": best_elig,
        "clanker_presence_status": "UNKNOWN",
        "factory_address_status": "UNKNOWN",
        "activity_window_status": "UNKNOWN",
        "data_content_status": "UNKNOWN",
        "evidence_dir": str(evidence_dir.relative_to(ROOT)),
    }
    if best_elig:
        payload.update(
            {
                "schema_fingerprint": best_elig.get("schema_fingerprint"),
                "database": best_elig.get("database"),
                "table": best_elig.get("table"),
                "engine": best_elig.get("engine"),
                "partition_key": best_elig.get("partition_key"),
                "sorting_key": best_elig.get("sorting_key"),
                "primary_key": best_elig.get("primary_key"),
                "candidate_role": best_elig.get("candidate_role"),
                "block_pruning_status": best_elig.get("block_pruning_status"),
                "bounded_probe_safety_status": best_elig.get("bounded_probe_safety_status"),
            }
        )
    cand_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    inv = reports / "BASE_CLICKHOUSE_METADATA_INVENTORY_RESULT.md"
    inv.write_text(
        "\n".join(
            [
                "# Base ClickHouse Metadata Inventory Result",
                "",
                f"implementation_parent_commit = {source_commit}",
                f"evidence_dir = {evidence_dir.relative_to(ROOT)}",
                f"execution_performed = {str(execution_performed).lower()}",
                f"blocked_reason = {blocked_reason}",
                f"business_table_access = false",
                f"explain_executed = false",
                f"rpc_http = false",
                f"metadata_inventory_completed = {str(execution_performed).lower()}",
                f"candidate_binding_available = {str(candidate_binding_available).lower()}",
                f"ready_for_codex_business_probe_review = {str(ready_for_review).lower()}",
                "",
                "```text",
                "ready_for_bounded_database_probe = false",
                "ready_for_micro_batch_backfill = false",
                "```",
                "",
                "## Health before",
                "",
                "```json",
                json.dumps(health_before, indent=2, sort_keys=True)[:8000],
                "```",
                "",
                "## Queries",
                "",
                "```json",
                json.dumps(query_exec, indent=2, sort_keys=True)[:12000],
                "```",
                "",
                "## Top candidate",
                "",
                "```json",
                json.dumps(best, indent=2, sort_keys=True)[:8000] if best else "null",
                "```",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "BASE_METADATA_QUERY_HEALTH_COMPARISON.md").write_text(
        "\n".join(
            [
                "# Base Metadata Query Health Comparison",
                "",
                "## Before",
                "```json",
                json.dumps(health_before, indent=2, sort_keys=True)[:8000],
                "```",
                "",
                "## After",
                "```json",
                json.dumps(health_after, indent=2, sort_keys=True)[:8000] if health_after else "null",
                "```",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (reports / "BASE_TABLE_BINDING_CANDIDATES.md").write_text(
        "\n".join(
            [
                "# Base Table Binding Candidates",
                "",
                "All candidates remain UNVERIFIED_DATA_SOURCE. Metadata cannot prove Clanker content.",
                "",
                f"candidate_count = {len(ranked)}",
                "",
                "```json",
                json.dumps(ranked[:20], indent=2, sort_keys=True)[:20000],
                "```",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    recommend = "no"
    if ready_for_review and best and best.get("block_pruning_status") == "VERIFIED_METADATA_PRUNABLE":
        recommend = "review_only_single_readonly_clanker_1000_block_probe_candidate_unverified_data"
    elif execution_performed:
        recommend = "hold_business_probe_until_stronger_prunable_logs_candidate"

    (reports / "CLANKER_FIRST_BOUNDED_PROBE_AUTHORIZATION_DOSSIER.md").write_text(
        "\n".join(
            [
                "# Clanker First Bounded Probe Authorization Dossier",
                "",
                "This dossier does **not** authorize execution. Gates remain closed.",
                "",
                "```text",
                "ready_for_bounded_database_probe = false",
                "ready_for_micro_batch_backfill = false",
                f"execution_performed_metadata_only = {str(execution_performed).lower()}",
                f"recommend_next_codex_action = {recommend}",
                "clanker_data_presence = UNKNOWN",
                "factory_address = UNKNOWN",
                "activity_block_window = UNKNOWN",
                "golden_on_chain = NONE",
                "```",
                "",
                "## Best metadata candidate (unverified data)",
                "",
                "```json",
                json.dumps(best, indent=2, sort_keys=True)[:8000] if best else "null",
                "```",
                "",
                "## Counterexamples / rejects",
                "",
                "```json",
                json.dumps(
                    [c for c in ranked if c.get("bounded_probe_safety_status") == "REJECT_OR_HOLD"][:10],
                    indent=2,
                    sort_keys=True,
                )[:8000],
                "```",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
