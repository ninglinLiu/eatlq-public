#!/usr/bin/env python3
"""Bounded public-RPC calibration of the Clanker v4 historical quote path.

Safety boundary: this runner only talks to one public Base JSON-RPC endpoint. It
has no ClickHouse, Redis, Docker, wallet, transaction-signing, or write-RPC
code path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eth_abi import decode

from launchpad_intelligence.clanker_alpha.v02_quote import (
    GET_LIQUIDITY_SEL,
    GET_SLOT0_SEL,
    STATE_VIEW,
    V4_QUOTER,
    PoolKey,
    notional_to_amount,
)
from launchpad_intelligence.clanker_alpha.v021_audit import (
    TRANSPORT_ERRORS,
    CalibrationRpcClient,
    RpcBudgetExhausted,
    adjudicate,
    audited_quote,
    compute_pool_id,
    strict_json_dumps,
)

SOURCE = ROOT / "evidence" / "clanker_alpha_v02"
EVIDENCE = ROOT / "evidence" / "clanker_quote_calibration_v021_corrected"
PRIOR_RUN_NETWORK_ATTEMPTS = 85
INIT_TOPIC0 = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SELECTED_INDICES = [0, 1, 7, 10, 16, 17, 18, 19, 21, 75, 86, 180]
COHORT_BY_INDEX = {
    0: "PRIOR_ENTRY_SUCCESS_NO_SWAP",
    1: "PRIOR_ENTRY_SUCCESS_NO_SWAP",
    7: "PRIOR_ENTRY_SUCCESS_NO_SWAP",
    10: "PRIOR_ENTRY_SUCCESS_NO_SWAP",
    16: "PRIOR_ENTRY_FAILURE_WITH_SWAP",
    17: "PRIOR_ENTRY_FAILURE_WITH_SWAP",
    18: "PRIOR_ENTRY_FAILURE_WITH_SWAP",
    19: "PRIOR_ENTRY_FAILURE_WITH_SWAP",
    21: "PRIOR_ENTRY_SUCCESS_WITH_SWAP",
    75: "PRIOR_ENTRY_SUCCESS_WITH_SWAP",
    86: "PRIOR_ENTRY_SUCCESS_WITH_SWAP",
    180: "PRIOR_ENTRY_SUCCESS_WITH_SWAP",
}
ETH_USD_FOR_SIZE_ONLY = 1842.6
PRIMARY_NOTIONAL_USD = 250.0


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> str:
    payload = strict_json_dumps(value)
    path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode()).hexdigest()


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def require_clean_committed_source() -> str:
    status = git_output("status", "--porcelain")
    if status:
        raise SystemExit("refusing_network_run_uncommitted_or_dirty_source")
    head = git_output("rev-parse", "HEAD")
    if not head:
        raise SystemExit("refusing_network_run_unknown_source_commit")
    return head


def load_source() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    launches = json.loads((SOURCE / "launches.json").read_text())["launches"]
    labels = json.loads((SOURCE / "labels.json").read_text())["rows"]
    swaps = json.loads((SOURCE / "swaps_by_pool.json").read_text())
    if len(launches) != 200 or len(labels) != 200:
        raise SystemExit("sealed_v02_source_shape_changed")
    return launches, labels, swaps


def rpc_receipt(
    rpc: CalibrationRpcClient,
    *,
    transaction_hash: str,
    pool_id: str,
    token: str,
) -> dict[str, Any]:
    outcome = rpc.call(
        "eth_getTransactionReceipt",
        [transaction_hash],
        purpose="recover_pool_key_from_launch_receipt",
        pool_id=pool_id,
        token=token,
    )
    return outcome.to_dict()


def decode_pool_key(receipt: dict[str, Any], expected_pool_id: str) -> tuple[Optional[PoolKey], dict[str, Any]]:
    if not receipt.get("ok"):
        return None, {"status": "RECEIPT_RPC_FAILED", "rpc": receipt}
    raw_receipt = receipt.get("result")
    if not isinstance(raw_receipt, dict):
        return None, {"status": "RECEIPT_MISSING", "rpc": receipt}
    candidates = []
    for log in raw_receipt.get("logs", []):
        topics = log.get("topics") or []
        if len(topics) < 4 or topics[0].lower() != INIT_TOPIC0:
            continue
        if topics[1].lower() != expected_pool_id.lower():
            continue
        try:
            fee, spacing, hooks, sqrt_price_x96, tick = decode(
                ["uint24", "int24", "address", "uint160", "int24"],
                bytes.fromhex(log["data"][2:]),
            )
        except Exception as exc:  # noqa: BLE001
            return None, {"status": "INITIALIZE_ABI_DECODE_FAILED", "error": f"{type(exc).__name__}:{exc}"}
        currency0 = "0x" + topics[2][-40:]
        currency1 = "0x" + topics[3][-40:]
        pool = PoolKey(currency0.lower(), currency1.lower(), int(fee), int(spacing), hooks.lower())
        candidates.append(
            {
                "pool": pool,
                "initialize_log_index": int(log["logIndex"], 16),
                "initialize_sqrt_price_x96": int(sqrt_price_x96),
                "initialize_tick": int(tick),
            }
        )
    if len(candidates) != 1:
        return None, {"status": "INITIALIZE_MATCH_COUNT_INVALID", "match_count": len(candidates)}
    found = candidates[0]
    computed = compute_pool_id(found["pool"])
    detail = {
        "status": "OK" if computed.lower() == expected_pool_id.lower() else "POOL_ID_MISMATCH",
        "computed_pool_id": computed,
        "expected_pool_id": expected_pool_id,
        "initialize_log_index": found["initialize_log_index"],
        "initialize_sqrt_price_x96": found["initialize_sqrt_price_x96"],
        "initialize_tick": found["initialize_tick"],
        "pool_key": {
            "currency0": found["pool"].currency0,
            "currency1": found["pool"].currency1,
            "fee": found["pool"].fee,
            "tick_spacing": found["pool"].tick_spacing,
            "hooks": found["pool"].hooks,
        },
    }
    return (found["pool"] if detail["status"] == "OK" else None), detail


def contract_code_check(rpc: CalibrationRpcClient, address: str, block: int, purpose: str) -> dict[str, Any]:
    outcome = rpc.call(
        "eth_getCode",
        [address, hex(block)],
        purpose=purpose,
        historical_block=block,
        to_address=address,
    )
    row = outcome.to_dict()
    code = row.get("result")
    row["has_code"] = bool(row.get("ok") and isinstance(code, str) and code not in ("0x", "0x0"))
    if isinstance(code, str):
        row["code_sha256"] = hashlib.sha256(bytes.fromhex(code.removeprefix("0x"))).hexdigest()
        row["code_bytes"] = max(0, (len(code) - 2) // 2)
        row.pop("result", None)
    return row


def state_snapshot(
    rpc: CalibrationRpcClient,
    *,
    pool_id: str,
    token: str,
    block: int,
    phase: str,
) -> dict[str, Any]:
    pid = pool_id[2:].rjust(64, "0")
    slot_data = "0x" + GET_SLOT0_SEL.hex() + pid
    liq_data = "0x" + GET_LIQUIDITY_SEL.hex() + pid
    slot = rpc.call(
        "eth_call",
        [{"to": STATE_VIEW, "data": slot_data}, hex(block)],
        purpose=f"stateview_{phase}_slot0",
        pool_id=pool_id,
        token=token,
        historical_block=block,
        to_address=STATE_VIEW,
        calldata=slot_data,
    )
    liquidity = rpc.call(
        "eth_call",
        [{"to": STATE_VIEW, "data": liq_data}, hex(block)],
        purpose=f"stateview_{phase}_liquidity",
        pool_id=pool_id,
        token=token,
        historical_block=block,
        to_address=STATE_VIEW,
        calldata=liq_data,
    )
    row: dict[str, Any] = {
        "block": block,
        "phase": phase,
        "slot0_rpc": slot.to_dict(),
        "liquidity_rpc": liquidity.to_dict(),
        "sqrt_price_x96": None,
        "tick": None,
        "protocol_fee": None,
        "lp_fee": None,
        "liquidity": None,
        "ok": False,
    }
    try:
        if slot.ok and liquidity.ok:
            sqrt_price, tick, protocol_fee, lp_fee = decode(
                ["uint160", "int24", "uint24", "uint24"], bytes.fromhex(slot.result[2:])
            )
            raw_liquidity = decode(["uint128"], bytes.fromhex(liquidity.result[2:]))[0]
            row.update(
                ok=True,
                sqrt_price_x96=int(sqrt_price),
                tick=int(tick),
                protocol_fee=int(protocol_fee),
                lp_fee=int(lp_fee),
                liquidity=int(raw_liquidity),
            )
    except Exception as exc:  # noqa: BLE001
        row["decode_error"] = f"{type(exc).__name__}:{exc}"[:500]
    row["slot0_rpc"].pop("result", None)
    row["liquidity_rpc"].pop("result", None)
    return row


def quote_directions(pool: PoolKey, token: str, paired: str) -> tuple[bool, bool]:
    c0, c1 = pool.currency0.lower(), pool.currency1.lower()
    token, paired = token.lower(), paired.lower()
    if {c0, c1} != {token, paired}:
        raise ValueError("pool_key_currencies_do_not_match_token_pair")
    paired_to_token = paired == c0
    return paired_to_token, not paired_to_token


def choose_real_swap_crosschecks(
    selected: list[dict[str, Any]],
    swaps_by_pool: dict[str, list[dict[str, Any]]],
    pool_keys: dict[str, PoolKey],
) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for item in selected:
        rec = item["launch"]["record"]
        token = rec["token_address"].lower()
        paired = rec["paired_token"].lower()
        pool = pool_keys.get(rec["pool_id"].lower())
        if pool is None:
            continue
        for swap in swaps_by_pool.get(rec["pool_id"].lower(), []):
            if int(swap["block_number"]) <= int(rec["block_number"]):
                continue
            amounts = actual_buy_amounts(swap, pool, token)
            if amounts is None:
                continue
            choices.append(
                {
                    "index": item["index"],
                    "token": token,
                    "paired": paired,
                    "swap": swap,
                    "actual_buy_amounts": amounts,
                }
            )
    choices.sort(key=lambda x: (x["swap"]["block_number"], x["swap"]["log_index"]))
    # A maximum of eight attempts keeps the entire run comfortably below 200 calls.
    return choices[:8]


def actual_buy_amounts(swap: dict[str, Any], pool: PoolKey, token: str) -> Optional[tuple[int, int, bool]]:
    amount0, amount1 = int(swap["amount0"]), int(swap["amount1"])
    if token.lower() == pool.currency0.lower() and amount0 > 0 and amount1 < 0:
        return -amount1, amount0, False
    if token.lower() == pool.currency1.lower() and amount1 > 0 and amount0 < 0:
        return -amount0, amount1, True
    return None


def main() -> int:
    source_commit = require_clean_committed_source()
    if EVIDENCE.exists():
        raise SystemExit(f"refusing_to_overwrite:{EVIDENCE}")
    EVIDENCE.mkdir(parents=True)
    launches, labels, swaps_by_pool = load_source()

    selected = [
        {
            "index": i,
            "cohort": COHORT_BY_INDEX[i],
            "launch": launches[i],
            "prior_label": labels[i],
            "prior_swap_count": len(swaps_by_pool.get(labels[i]["pool_id"].lower(), [])),
        }
        for i in SELECTED_INDICES
    ]
    selection_rows = [
        {
            "index": row["index"],
            "cohort": row["cohort"],
            "token": row["launch"]["record"]["token_address"],
            "pool_id": row["launch"]["record"]["pool_id"],
            "launch_block": row["launch"]["record"]["block_number"],
            "prior_status": row["prior_label"]["timeline"]["status"],
            "prior_entry_ok": bool(row["prior_label"].get("entry_quote")),
            "prior_swap_count": row["prior_swap_count"],
        }
        for row in selected
    ]
    write_json(EVIDENCE / "selection.json", {"n": len(selection_rows), "rows": selection_rows})

    # Run 1 used 85 attempts. Cap this corrected run at 115 so the combined
    # calibration remains within the preregistered 200-attempt boundary.
    rpc = CalibrationRpcClient(max_calls=200 - PRIOR_RUN_NETWORK_ATTEMPTS)
    earliest_block = min(row["launch"]["record"]["block_number"] for row in selected)
    contracts = {
        "v4_quoter": contract_code_check(rpc, V4_QUOTER, earliest_block, "verify_v4_quoter_code"),
        "state_view": contract_code_check(rpc, STATE_VIEW, earliest_block, "verify_stateview_code"),
    }
    pool_keys: dict[str, PoolKey] = {}
    results: list[dict[str, Any]] = []
    budget_exhausted = False
    stopped_reason: Optional[str] = None

    try:
        for item in selected:
            rec = item["launch"]["record"]
            label = item["prior_label"]
            pool_id = rec["pool_id"].lower()
            token = rec["token_address"].lower()
            paired = rec["paired_token"].lower()
            receipt = rpc_receipt(
                rpc,
                transaction_hash=rec["transaction_hash"],
                pool_id=pool_id,
                token=token,
            )
            pool, key_detail = decode_pool_key(receipt, pool_id)
            receipt.pop("result", None)
            entry_block = int(label["timeline"]["intended_entry_block"])
            exit_block = int(label["timeline"]["intended_exit_block"])
            row: dict[str, Any] = {
                "index": item["index"],
                "cohort": item["cohort"],
                "pool_id": pool_id,
                "token": token,
                "paired": paired,
                "launch_transaction_hash": rec["transaction_hash"],
                "launch_block": int(rec["block_number"]),
                "intended_entry_block": entry_block,
                "intended_exit_block": exit_block,
                "receipt_rpc": receipt,
                "pool_key_verification": key_detail,
                "entry_quote": None,
                "immediate_reverse_quote": None,
                "exit_reverse_quote": None,
                "entry_state": None,
                "exit_state": None,
            }
            if pool is None:
                results.append(row)
                continue
            pool_keys[pool_id] = pool
            amount_in, unit = notional_to_amount(paired, PRIMARY_NOTIONAL_USD, ETH_USD_FOR_SIZE_ONLY)
            row["sizing"] = {
                "notional_usd": PRIMARY_NOTIONAL_USD,
                "amount_in": amount_in,
                "unit": unit,
                "eth_usd_for_size_only": ETH_USD_FOR_SIZE_ONLY if unit == "WETH" else None,
                "economic_inference_authorized": False,
            }
            if amount_in is None:
                results.append(row)
                continue
            paired_to_token, token_to_paired = quote_directions(pool, token, paired)
            row["entry_state"] = state_snapshot(
                rpc, pool_id=pool_id, token=token, block=entry_block, phase="entry"
            )
            row["entry_quote"] = audited_quote(
                rpc,
                pool,
                pool_id=pool_id,
                token=token,
                side="ENTRY_PAIRED_TO_TOKEN",
                zero_for_one=paired_to_token,
                amount_in=amount_in,
                block=entry_block,
            )
            if row["entry_quote"]["ok"]:
                token_amount = int(row["entry_quote"]["amount_out"])
                row["immediate_reverse_quote"] = audited_quote(
                    rpc,
                    pool,
                    pool_id=pool_id,
                    token=token,
                    side="IMMEDIATE_REVERSE_TOKEN_TO_PAIRED",
                    zero_for_one=token_to_paired,
                    amount_in=token_amount,
                    block=entry_block,
                )
                row["exit_state"] = state_snapshot(
                    rpc, pool_id=pool_id, token=token, block=exit_block, phase="exit"
                )
                row["exit_reverse_quote"] = audited_quote(
                    rpc,
                    pool,
                    pool_id=pool_id,
                    token=token,
                    side="EXIT_REVERSE_TOKEN_TO_PAIRED",
                    zero_for_one=token_to_paired,
                    amount_in=token_amount,
                    block=exit_block,
                )
            results.append(row)

        crosschecks: list[dict[str, Any]] = []
        for candidate in choose_real_swap_crosschecks(selected, swaps_by_pool, pool_keys):
            rec = selected[SELECTED_INDICES.index(candidate["index"])]["launch"]["record"]
            pool_id = rec["pool_id"].lower()
            pool = pool_keys.get(pool_id)
            if pool is None:
                continue
            amounts = candidate["actual_buy_amounts"]
            actual_in, actual_out, zero_for_one = amounts
            quote_block = int(candidate["swap"]["block_number"]) - 1
            quote = audited_quote(
                rpc,
                pool,
                pool_id=pool_id,
                token=candidate["token"],
                side="REAL_SWAP_PREBLOCK_CROSSCHECK",
                zero_for_one=zero_for_one,
                amount_in=actual_in,
                block=quote_block,
            )
            relative_error = None
            passed = False
            if quote["ok"] and actual_out > 0:
                relative_error = abs(int(quote["amount_out"]) - actual_out) / actual_out
                passed = relative_error <= 0.25
            crosschecks.append(
                {
                    "pool_id": pool_id,
                    "token": candidate["token"],
                    "transaction_hash": candidate["swap"]["transaction_hash"],
                    "log_index": candidate["swap"]["log_index"],
                    "swap_block": candidate["swap"]["block_number"],
                    "quote_block": quote_block,
                    "actual_amount_in": actual_in,
                    "actual_amount_out": actual_out,
                    "quote": quote,
                    "relative_output_error": relative_error,
                    "pass_threshold": "relative_output_error <= 0.25",
                    "passed": passed,
                    "limitation": "eth_call at prior block is a pre-block approximation, not transaction-index state",
                }
            )
    except RpcBudgetExhausted as exc:
        budget_exhausted = True
        stopped_reason = str(exc)

    # Even a stopped run writes an honest, immutable audit trail.
    entry_successes = sum(bool(row.get("entry_quote") and row["entry_quote"]["ok"]) for row in results)
    immediate_reverse_successes = sum(
        bool(row.get("immediate_reverse_quote") and row["immediate_reverse_quote"]["ok"]) for row in results
    )
    exit_successes = sum(
        bool(row.get("exit_reverse_quote") and row["exit_reverse_quote"]["ok"]) for row in results
    )
    crosschecks = locals().get("crosschecks", [])
    crosscheck_passes = sum(bool(row["passed"]) for row in crosschecks)
    error_counts = Counter(row.error_class for row in rpc.journal if not row.ok)
    transport_failures = sum(count for key, count in error_counts.items() if key in TRANSPORT_ERRORS)
    verdict = adjudicate(
        entry_successes=entry_successes,
        immediate_reverse_successes=immediate_reverse_successes,
        crosscheck_passes=crosscheck_passes,
        transport_failures=transport_failures,
        total_network_attempts=rpc.calls,
        budget_exhausted=budget_exhausted,
    )
    summary = {
        "verdict": verdict,
        "economic_edge_conclusion": "NOT_AUTHORIZED",
        "selected_pools": len(selection_rows),
        "completed_pool_results": len(results),
        "verified_pool_keys": len(pool_keys),
        "entry_successes": entry_successes,
        "immediate_reverse_successes": immediate_reverse_successes,
        "exit_successes": exit_successes,
        "real_swap_crosscheck_attempts": len(crosschecks),
        "real_swap_crosscheck_passes": crosscheck_passes,
        "network_attempts": rpc.calls,
        "prior_run_network_attempts": PRIOR_RUN_NETWORK_ATTEMPTS,
        "combined_network_attempts": PRIOR_RUN_NETWORK_ATTEMPTS + rpc.calls,
        "transport_failures": transport_failures,
        "transport_failure_ratio": (transport_failures / rpc.calls) if rpc.calls else None,
        "error_class_counts": dict(sorted((str(k), v) for k, v in error_counts.items())),
        "budget_exhausted": budget_exhausted,
        "stopped_reason": stopped_reason,
        "validation_thresholds": {
            "entry_successes_gte": 8,
            "immediate_reverse_successes_gte": 8,
            "real_swap_crosscheck_passes_gte": 5,
            "transport_failure_ratio_lte": 0.20,
        },
    }
    write_json(EVIDENCE / "contract_checks.json", contracts)
    write_json(EVIDENCE / "pool_results.json", {"n": len(results), "rows": results})
    write_json(EVIDENCE / "real_swap_crosschecks.json", {"n": len(crosschecks), "rows": crosschecks})
    write_json(EVIDENCE / "rpc_attempt_journal.json", rpc.journal_dict())
    write_json(EVIDENCE / "verdict.json", summary)
    metadata = {
        "run_id": "clanker_quote_calibration_v021",
        "created_at_utc": utc_now(),
        "source_commit": source_commit,
        "source_v02_evidence_commit": "cf65a01",
        "supersedes_incomplete_crosscheck_run": "evidence/clanker_quote_calibration_v021",
        "prior_run_network_attempts": PRIOR_RUN_NETWORK_ATTEMPTS,
        "combined_network_attempt_cap": 200,
        "source_evidence_hashes": {
            name: sha256_file(SOURCE / name)
            for name in ["launches.json", "labels.json", "swaps_by_pool.json", "rpc_journal.json"]
        },
        "rpc_endpoint": rpc.endpoint,
        "rpc_endpoint_class": rpc.endpoint_class,
        "network_boundary": "PUBLIC_BASE_RPC_ONLY",
        "production_clickhouse_accessed": False,
        "production_redis_accessed": False,
        "docker_accessed": False,
        "wallet_or_signing_accessed": False,
        "transaction_submission_capability": False,
        "database_probe_gate_changed": False,
        "backfill_gate_changed": False,
    }
    write_json(EVIDENCE / "run_metadata.json", metadata)
    hashes = {
        path.name: sha256_file(path)
        for path in sorted(EVIDENCE.glob("*.json"))
        if path.name != "SHA256SUMS.json"
    }
    write_json(EVIDENCE / "SHA256SUMS.json", hashes)
    print(strict_json_dumps(summary, indent=2), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
