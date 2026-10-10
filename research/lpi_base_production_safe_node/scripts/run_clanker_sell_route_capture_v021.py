#!/usr/bin/env python3
"""Capture public calldata for the eight observed Clanker sell transactions."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eth_abi import decode

from launchpad_intelligence.clanker_alpha.v021_audit import (
    CalibrationRpcClient,
    RpcBudgetExhausted,
    strict_json_dumps,
)

SOURCE = ROOT / "evidence" / "clanker_quote_calibration_v021_corrected" / "real_swap_crosschecks.json"
EVIDENCE = ROOT / "evidence" / "clanker_actual_sell_route_capture_v021"
UNIVERSAL_ROUTER_EXECUTE = "0x3593564c"
PRIOR_ATTEMPTS = 191
MAX_CALLS = 9


def sha256_hex(value: str) -> str:
    return hashlib.sha256(bytes.fromhex(value.removeprefix("0x"))).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> str:
    payload = strict_json_dumps(value)
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode()).hexdigest()


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def decode_top_level_input(input_hex: str) -> dict[str, Any]:
    selector = input_hex[:10].lower() if len(input_hex) >= 10 else input_hex.lower()
    row: dict[str, Any] = {
        "selector": selector,
        "calldata_bytes": max(0, (len(input_hex) - 2) // 2),
        "calldata_sha256": sha256_hex(input_hex),
        "universal_router_execute_decoded": False,
    }
    if selector != UNIVERSAL_ROUTER_EXECUTE:
        return row
    try:
        commands, inputs, deadline = decode(
            ["bytes", "bytes[]", "uint256"], bytes.fromhex(input_hex[10:])
        )
    except Exception as exc:  # noqa: BLE001
        row["decode_error"] = f"{type(exc).__name__}:{exc}"[:500]
        return row
    row.update(
        universal_router_execute_decoded=True,
        commands="0x" + commands.hex(),
        command_rows=[
            {
                "raw_command": command,
                "command_type_low6": command & 0x3F,
                "allow_revert": bool(command & 0x80),
                "reserved_flag": bool(command & 0x40),
                "input_bytes": len(command_input),
                "input_sha256": hashlib.sha256(command_input).hexdigest(),
                "input": "0x" + command_input.hex(),
            }
            for command, command_input in zip(commands, inputs)
        ],
        command_count=len(commands),
        input_count=len(inputs),
        deadline=int(deadline),
    )
    return row


def main() -> int:
    if git_output("status", "--porcelain"):
        raise SystemExit("refusing_network_run_uncommitted_or_dirty_source")
    source_commit = git_output("rev-parse", "HEAD")
    if EVIDENCE.exists():
        raise SystemExit(f"refusing_to_overwrite:{EVIDENCE}")
    EVIDENCE.mkdir(parents=True)
    sells = json.loads(SOURCE.read_text())["rows"]
    tx_hashes = list(dict.fromkeys(row["transaction_hash"] for row in sells))
    if len(tx_hashes) > 8:
        raise SystemExit("unexpected_transaction_count")
    rpc = CalibrationRpcClient(max_calls=MAX_CALLS)
    rows: list[dict[str, Any]] = []
    budget_exhausted = False
    stopped_reason = None
    try:
        for tx_hash in tx_hashes:
            outcome = rpc.call(
                "eth_getTransactionByHash",
                [tx_hash],
                purpose="capture_observed_sell_transaction_calldata",
            )
            row: dict[str, Any] = {"transaction_hash": tx_hash, "rpc": outcome.to_dict()}
            tx = outcome.result if outcome.ok else None
            if isinstance(tx, dict):
                input_hex = tx.get("input") or "0x"
                row.update(
                    ok=True,
                    from_address=(tx.get("from") or "").lower(),
                    to_address=(tx.get("to") or "").lower(),
                    block_number=int(tx["blockNumber"], 16) if tx.get("blockNumber") else None,
                    value=int(tx.get("value") or "0x0", 16),
                    input=input_hex,
                    top_level=decode_top_level_input(input_hex),
                )
                row["rpc"].pop("result", None)
            else:
                row["ok"] = False
            rows.append(row)
    except RpcBudgetExhausted as exc:
        budget_exhausted = True
        stopped_reason = str(exc)

    selectors = {}
    destinations = {}
    command_types = {}
    for row in rows:
        selector = row.get("top_level", {}).get("selector")
        destination = row.get("to_address")
        if selector:
            selectors[selector] = selectors.get(selector, 0) + 1
        if destination:
            destinations[destination] = destinations.get(destination, 0) + 1
        for command in row.get("top_level", {}).get("command_rows", []):
            key = str(command["command_type_low6"])
            command_types[key] = command_types.get(key, 0) + 1
    summary = {
        "captured_transactions": sum(bool(row.get("ok")) for row in rows),
        "requested_transactions": len(tx_hashes),
        "top_level_selectors": dict(sorted(selectors.items())),
        "destination_counts": dict(sorted(destinations.items())),
        "universal_router_command_type_counts": dict(sorted(command_types.items())),
        "network_attempts": rpc.calls,
        "prior_calibration_attempts": PRIOR_ATTEMPTS,
        "combined_network_attempts": PRIOR_ATTEMPTS + rpc.calls,
        "combined_network_attempt_cap": 200,
        "budget_exhausted": budget_exhausted,
        "stopped_reason": stopped_reason,
        "economic_edge_conclusion": "NOT_AUTHORIZED",
    }
    write_json(EVIDENCE / "transactions.json", {"n": len(rows), "rows": rows})
    write_json(EVIDENCE / "rpc_attempt_journal.json", rpc.journal_dict())
    write_json(EVIDENCE / "summary.json", summary)
    write_json(
        EVIDENCE / "run_metadata.json",
        {
            "run_id": "clanker_actual_sell_route_capture_v021",
            "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_commit": source_commit,
            "source_crosscheck_evidence_commit": "1df2268",
            "source_direction_corrected_commit": "e883ba0",
            "source_sell_crosschecks_sha256": sha256_file(SOURCE),
            "network_boundary": "PUBLIC_BASE_RPC_ONLY",
            "production_clickhouse_accessed": False,
            "production_redis_accessed": False,
            "docker_accessed": False,
            "wallet_or_signing_accessed": False,
            "transaction_submission_capability": False,
        },
    )
    hashes = {
        path.name: sha256_file(path)
        for path in sorted(EVIDENCE.glob("*.json"))
        if path.name != "SHA256SUMS.json"
    }
    write_json(EVIDENCE / "SHA256SUMS.json", hashes)
    print(strict_json_dumps(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
