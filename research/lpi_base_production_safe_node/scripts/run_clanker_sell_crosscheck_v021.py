#!/usr/bin/env python3
"""Reproduce observed Clanker sell-side Swaps within the final 25-call budget."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_intelligence.clanker_alpha.v02_quote import PoolKey
from launchpad_intelligence.clanker_alpha.v021_audit import (
    TRANSPORT_ERRORS,
    CalibrationRpcClient,
    RpcBudgetExhausted,
    audited_quote,
    strict_json_dumps,
)

CALIBRATION = ROOT / "evidence" / "clanker_quote_calibration_v021_corrected"
SWAPS = ROOT / "evidence" / "clanker_alpha_v02" / "swaps_by_pool.json"
EVIDENCE = ROOT / "evidence" / "clanker_sell_crosscheck_v021"
PRIOR_ATTEMPTS = 175
MAX_CALLS = 25


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, value: Any) -> str:
    payload = strict_json_dumps(value)
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def actual_sell_amounts(swap: dict[str, Any], pool: PoolKey, token: str) -> Optional[tuple[int, int, bool]]:
    """Return token input, paired output, and zeroForOne for a real token sale."""
    amount0, amount1 = int(swap["amount0"]), int(swap["amount1"])
    if token.lower() == pool.currency0.lower() and amount0 < 0 and amount1 > 0:
        return -amount0, amount1, True
    if token.lower() == pool.currency1.lower() and amount1 < 0 and amount0 > 0:
        return -amount1, amount0, False
    return None


def round_robin(candidates: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Prefer pool diversity before taking additional events from one pool."""
    out: list[dict[str, Any]] = []
    depth = 0
    keys = sorted(candidates)
    while len(out) < limit:
        added = False
        for key in keys:
            if depth < len(candidates[key]):
                out.append(candidates[key][depth])
                added = True
                if len(out) == limit:
                    break
        if not added:
            break
        depth += 1
    return out


def load_candidates() -> list[dict[str, Any]]:
    pools = json.loads((CALIBRATION / "pool_results.json").read_text())["rows"]
    swaps = json.loads(SWAPS.read_text())
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pools:
        key = row["pool_key_verification"].get("pool_key")
        if not key:
            continue
        pool = PoolKey(
            key["currency0"], key["currency1"], int(key["fee"]),
            int(key["tick_spacing"]), key["hooks"],
        )
        for swap in swaps.get(row["pool_id"], []):
            if int(swap["block_number"]) <= int(row["launch_block"]):
                continue
            amounts = actual_sell_amounts(swap, pool, row["token"])
            if amounts is None:
                continue
            by_pool[row["pool_id"]].append(
                {
                    "pool_id": row["pool_id"],
                    "token": row["token"],
                    "pool": pool,
                    "swap": swap,
                    "amounts": amounts,
                }
            )
    for rows in by_pool.values():
        rows.sort(key=lambda x: (x["swap"]["block_number"], x["swap"]["log_index"]))
    return round_robin(by_pool, 8)


def main() -> int:
    if git_output("status", "--porcelain"):
        raise SystemExit("refusing_network_run_uncommitted_or_dirty_source")
    source_commit = git_output("rev-parse", "HEAD")
    if EVIDENCE.exists():
        raise SystemExit(f"refusing_to_overwrite:{EVIDENCE}")
    EVIDENCE.mkdir(parents=True)
    candidates = load_candidates()
    write_json(
        EVIDENCE / "selection.json",
        {
            "n": len(candidates),
            "pool_count": len({row["pool_id"] for row in candidates}),
            "rows": [
                {
                    "pool_id": row["pool_id"],
                    "token": row["token"],
                    "transaction_hash": row["swap"]["transaction_hash"],
                    "log_index": row["swap"]["log_index"],
                    "swap_block": row["swap"]["block_number"],
                    "actual_token_in": row["amounts"][0],
                    "actual_paired_out": row["amounts"][1],
                }
                for row in candidates
            ],
        },
    )
    rpc = CalibrationRpcClient(max_calls=MAX_CALLS)
    results: list[dict[str, Any]] = []
    budget_exhausted = False
    stopped_reason = None
    try:
        for candidate in candidates:
            token_in, paired_out, zero_for_one = candidate["amounts"]
            swap = candidate["swap"]
            quote_block = int(swap["block_number"]) - 1
            quote = audited_quote(
                rpc,
                candidate["pool"],
                pool_id=candidate["pool_id"],
                token=candidate["token"],
                side="REAL_SELL_PREBLOCK_CROSSCHECK",
                zero_for_one=zero_for_one,
                amount_in=token_in,
                block=quote_block,
            )
            error = None
            passed = False
            if quote["ok"] and paired_out > 0:
                error = abs(int(quote["amount_out"]) - paired_out) / paired_out
                passed = error <= 0.25
            results.append(
                {
                    "pool_id": candidate["pool_id"],
                    "token": candidate["token"],
                    "transaction_hash": swap["transaction_hash"],
                    "log_index": swap["log_index"],
                    "swap_block": swap["block_number"],
                    "quote_block": quote_block,
                    "actual_token_in": token_in,
                    "actual_paired_out": paired_out,
                    "quote": quote,
                    "relative_output_error": error,
                    "pass_threshold": "relative_output_error <= 0.25",
                    "passed": passed,
                    "limitation": "prior-block state approximation; not transaction-index replay",
                }
            )
    except RpcBudgetExhausted as exc:
        budget_exhausted = True
        stopped_reason = str(exc)

    errors = Counter(row.error_class for row in rpc.journal if not row.ok)
    transport = sum(n for cls, n in errors.items() if cls in TRANSPORT_ERRORS)
    transport_ratio = transport / rpc.calls if rpc.calls else 1.0
    passes = sum(row["passed"] for row in results)
    passing_pools = len({row["pool_id"] for row in results if row["passed"]})
    if budget_exhausted or transport_ratio > 0.20:
        verdict = "TRANSPORT_INSUFFICIENT"
    elif passes >= 5 and passing_pools >= 3:
        verdict = "SELL_PATH_OBSERVED"
    else:
        verdict = "SELL_PATH_NOT_REPRODUCED"
    summary = {
        "verdict": verdict,
        "economic_edge_conclusion": "NOT_AUTHORIZED",
        "attempted_sell_crosschecks": len(results),
        "passing_sell_crosschecks": passes,
        "passing_pool_count": passing_pools,
        "network_attempts": rpc.calls,
        "transport_failures": transport,
        "transport_failure_ratio": transport_ratio,
        "error_class_counts": dict(sorted((str(k), v) for k, v in errors.items())),
        "budget_exhausted": budget_exhausted,
        "stopped_reason": stopped_reason,
        "prior_calibration_attempts": PRIOR_ATTEMPTS,
        "combined_network_attempts": PRIOR_ATTEMPTS + rpc.calls,
        "combined_network_attempt_cap": 200,
    }
    write_json(EVIDENCE / "sell_crosschecks.json", {"n": len(results), "rows": results})
    write_json(EVIDENCE / "rpc_attempt_journal.json", rpc.journal_dict())
    write_json(EVIDENCE / "verdict.json", summary)
    write_json(
        EVIDENCE / "run_metadata.json",
        {
            "run_id": "clanker_sell_crosscheck_v021",
            "created_at_utc": utc_now(),
            "source_commit": source_commit,
            "source_calibration_evidence_commit": "1df2268",
            "source_pool_results_sha256": sha256_file(CALIBRATION / "pool_results.json"),
            "source_swaps_sha256": sha256_file(SWAPS),
            "network_boundary": "PUBLIC_BASE_RPC_ONLY",
            "production_clickhouse_accessed": False,
            "production_redis_accessed": False,
            "docker_accessed": False,
            "wallet_or_signing_accessed": False,
            "transaction_submission_capability": False,
            "database_probe_gate_changed": False,
            "backfill_gate_changed": False,
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
