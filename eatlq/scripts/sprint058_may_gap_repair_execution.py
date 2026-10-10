#!/usr/bin/env python3
"""Sprint058 executor: may-gap repair + root-cause fix readout + watcher rerun."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "mvp" / "audits" / "sprint058"

RPC_KEYS = [
    "LABEL_RPC_URL",
    "EVM_HTTP_URL",
    "LIVE_BASE_RPC_URL",
    "LIVE_BASE_RPC_URL_FALLBACK",
    "ALCHEMY_HTTP_URL",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ch(query: str) -> str:
    cmd = [
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--database",
        "eatlq",
        "--query",
        query,
    ]
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=120).strip()


def tsv_rows(query: str) -> list[dict[str, str]]:
    out = ch(query + " FORMAT TSVWithNames")
    lines = [x for x in out.splitlines() if x.strip()]
    if len(lines) < 2:
        return []
    cols = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        vals = line.split("\t")
        rows.append({cols[i]: vals[i] if i < len(vals) else "" for i in range(len(cols))})
    return rows


def bool_env_presence() -> dict[str, bool]:
    return {k: bool(os.getenv(k)) for k in RPC_KEYS}


def run_cmd(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc-url", required=True, help="Ephemeral RPC URL from chat input.")
    args = ap.parse_args()

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    rpc_value = args.rpc_url.strip().strip('"').strip("'")

    pre_presence = bool_env_presence()
    child_env = os.environ.copy()
    child_env["LABEL_RPC_URL"] = rpc_value
    child_env["EVM_HTTP_URL"] = rpc_value

    rpc_presence = {
        "generated_at": now_iso(),
        "pre_injection_env_presence": pre_presence,
        "post_injection_env_presence": {
            **pre_presence,
            "LABEL_RPC_URL": True,
            "EVM_HTTP_URL": True,
        },
        "rpc_provided": True,
        "rpc_source": "chat_input",
        "rpc_key_used": "LABEL_RPC_URL",
        "rpc_value_masked": "https://***redacted***",
        "rpc_value_sha256_prefix": hashlib.sha256(rpc_value.encode("utf-8")).hexdigest()[:8],
    }
    (AUDIT_DIR / "rpc_env_presence.json").write_text(json.dumps(rpc_presence, indent=2), encoding="utf-8")

    rc_57, out_57, err_57 = run_cmd(
        ["python3", "scripts/sprint057_base_v3_backfill_foundation.py", "--execute-may-repair"],
        env=child_env,
    )
    may_exec_summary = {
        "generated_at": now_iso(),
        "sprint057_cmd_rc": rc_57,
        "sprint057_stdout": out_57[-1500:],
        "sprint057_stderr_tail": err_57[-800:],
    }
    (AUDIT_DIR / "may_repair_execution_summary.json").write_text(
        json.dumps(may_exec_summary, indent=2),
        encoding="utf-8",
    )

    # Post-repair inventory.
    post_cov = {
        "generated_at": now_iso(),
        "coverage": {
            "uniswap_v3_swaps": tsv_rows(
                "SELECT max(block_time) AS max_block_time, max(received_at_ms) AS max_received_at, "
                "countIf(block_time >= toDateTime64('2026-05-01 00:00:00',3,'UTC')) AS may_rows_block_time, "
                "countIf(received_at_ms >= toDateTime64('2026-05-01 00:00:00',3,'UTC')) AS may_rows_received, "
                "countIf(block_time IS NULL) AS null_block_time_rows "
                "FROM uniswap_v3_swaps WHERE chain_id=8453"
            )[0],
            "raw_evm_logs": tsv_rows(
                "SELECT max(block_time) AS max_block_time, max(received_at_ms) AS max_received_at, "
                "countIf(block_time IS NULL) AS null_block_time_rows "
                "FROM raw_evm_logs WHERE chain_id=8453"
            )[0],
            "uniswap_swaps_clean_mat": tsv_rows(
                "SELECT max(block_time) AS max_block_time, count() AS rows_all "
                "FROM uniswap_swaps_clean_mat WHERE chain_id=8453"
            )[0],
        },
        "null_block_time_generation": tsv_rows(
            "SELECT source, countIf(block_time IS NULL) AS null_rows, "
            "countIf(block_time IS NULL AND received_at_ms >= now() - INTERVAL 24 HOUR) AS null_rows_24h "
            "FROM uniswap_v3_swaps WHERE chain_id=8453 GROUP BY source ORDER BY null_rows DESC LIMIT 20"
        ),
    }
    (AUDIT_DIR / "post_repair_coverage_inventory.json").write_text(json.dumps(post_cov, indent=2), encoding="utf-8")

    # Rerun watcher after repair.
    rc_56, out_56, err_56 = run_cmd(
        ["python3", "scripts/sprint056_0x501e_fresh_paper_watcher.py", "--max-stream-scan", "30000"],
        env=child_env,
    )
    watcher_flags = {}
    flags_path = ROOT / "mvp" / "audits" / "sprint056" / "decision_flags.json"
    if flags_path.exists():
        watcher_flags = json.loads(flags_path.read_text(encoding="utf-8"))
    watcher_md = [
        "# Sprint058 — Sprint056 watcher rerun summary",
        "",
        f"- rerun_rc: `{rc_56}`",
        f"- rerun_stdout_tail: `{out_56[-500:]}`",
        f"- rerun_stderr_tail: `{err_56[-300:]}`",
        f"- fresh_sample_count: `{watcher_flags.get('fresh_sample_count', '')}`",
        f"- quoteable_share: `{watcher_flags.get('quoteable_share', '')}`",
        f"- sprint056_verdict: `{watcher_flags.get('sprint056_verdict', '')}`",
        f"- ch_v3_max_block_time: `{watcher_flags.get('ch_v3_max_block_time', '')}`",
        f"- ch_all_max_block_time: `{watcher_flags.get('ch_all_max_block_time', '')}`",
    ]
    (AUDIT_DIR / "sprint056_watcher_rerun_summary.md").write_text("\n".join(watcher_md), encoding="utf-8")

    # Root-cause fix note references code patches.
    root_fix_md = """# Sprint058 — block_time root-cause fix

- `services/pool_discovery/src/swap_poller.py` now resolves per-block timestamp (`eth_getBlockByNumber`) and embeds `payload_json.params.result.blockTimestamp`.
- When timestamp resolution fails, poller skips the record with warning (instead of silently publishing rows that land as `block_time IS NULL`).
- This prevents new `discovery_poller` rows from silently extending `received_at_ms` while freezing `max(block_time)`.
- Post-run sanity check uses `uniswap_v3_swaps` null counters by source (see `post_repair_coverage_inventory.json`).
"""
    (AUDIT_DIR / "block_time_root_cause_fix.md").write_text(root_fix_md, encoding="utf-8")

    weekly_md = """# Sprint058 — weekly incremental runner plan

## Runner contract

1. Read `mvp/state/base_v3_backfill_watermark.json`.
2. Resolve latest head block, apply reorg buffer (`64` blocks), compute `end_block`.
3. Use `start_block = watermark.last_backfill_end_block + 1`.
4. Chunk execution in `100000`-block windows using `backfill_block_timestamps.py`.
5. Emit per-run fields:
   - `start_block`
   - `end_block`
   - `inserted_rows` (proxy: null-row delta)
   - `skipped_duplicates` (idempotent key: `chain_id+tx_hash+log_index`)
   - `failed_chunks`
   - `max_block_time_before`
   - `max_block_time_after`
   - `null_block_time_rows`

## Scheduler examples (not auto-installed)

- Cron: `15 * * * 0 cd /opt/EATLQ && python3 scripts/sprint057_base_v3_backfill_foundation.py --execute-may-repair`
- Windows Task Scheduler:
  - Program: `python`
  - Args: `scripts\\sprint057_base_v3_backfill_foundation.py --execute-may-repair`
  - Start in: `C:\\path\\to\\EATLQ`
"""
    (AUDIT_DIR / "weekly_incremental_runner_plan.md").write_text(weekly_md, encoding="utf-8")

    decision = {
        "generated_at": now_iso(),
        "rpc_provided": True,
        "rpc_source": "chat_input",
        "rpc_key_used": "LABEL_RPC_URL",
        "rpc_value_masked": "https://***redacted***",
        "may_repair_command_rc": rc_57,
        "may_repair_executed": rc_57 == 0,
        "watcher_rerun_rc": rc_56,
        "live_allowed": False,
        "training_allowed": False,
        "model_promotion_allowed": False,
        "production_signals_written": False,
    }
    (AUDIT_DIR / "decision_flags.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")

    exec_md = [
        "# Sprint058 — Executive Readout",
        "",
        f"- generated_at: `{now_iso()}`",
        "- rpc_provided: `true` (masked)",
        f"- may_repair_command_rc: `{rc_57}`",
        f"- watcher_rerun_rc: `{rc_56}`",
        f"- ch_max_block_time_uniswap_v3_swaps: `{post_cov['coverage']['uniswap_v3_swaps'].get('max_block_time', '')}`",
        f"- ch_null_block_time_uniswap_v3_swaps: `{post_cov['coverage']['uniswap_v3_swaps'].get('null_block_time_rows', '')}`",
        "",
        "See sibling JSON/MD artifacts for detailed breakdown.",
    ]
    (AUDIT_DIR / "executive_readout.md").write_text("\n".join(exec_md), encoding="utf-8")

    print(
        json.dumps(
            {
                "sprint058_audit_dir": str(AUDIT_DIR),
                "may_repair_rc": rc_57,
                "watcher_rerun_rc": rc_56,
                "rpc_key_used": "LABEL_RPC_URL",
                "rpc_value_masked": "https://***redacted***",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
