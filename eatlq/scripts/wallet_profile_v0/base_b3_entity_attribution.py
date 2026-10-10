#!/usr/bin/env python3
"""Bounded Base B3 account/entity attribution feasibility spike."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from base_attribution import EXCLUSION_ROWS, normalize_address
from base_transfer_net_attribution import RpcClient
from ch_helper import ch_query

CHAIN_ID = 8453
START = 46389458
END = 46417105
WETH = "0x4200000000000000000000000000000000000006"
USER_OPERATION_EVENT = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
ENTRYPOINTS = {
    "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789": "entrypoint_v06",
    "0x0000000071727de22e5e9d8baf0edac6f37da032": "entrypoint_v07",
    "0x4337084d9e255ff0702461cf8895ce9e3b5ff108": "entrypoint_v08",
    "0x433709009b8330fda32311df1c2afa402ed8d009": "entrypoint_v09",
}
UNIVERSAL_ROUTERS = {
    "0x6ff5693b99212da76ad316178a184ab56d299b43",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
    "0xfdf682f51fe81aa4898f0ae2163d8a55c127fbc7",
}
UNISWAP_ROUTERS = {
    "0x2626664c2603336e57b271c5c0b26f421741e481",
    "0xe592427a0aece92de3edee1f18e0157c05861564",
}
AERODROME_ROUTERS = {
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1bebe2438d",
    "0xbe6d8da2d4f922f359b1da84fed3c60b269ce8a0",
}
SOLVER_SETTLEMENT = {
    "0x9008d19f58abd9ed0d60971565aa8510560ab41",
    "0x6a000f20005980200259b80c5102003040001068",
}
AGGREGATORS = {
    row["address"] for row in EXCLUSION_ROWS if row["category"] == "aggregator"
} - SOLVER_SETTLEMENT
AGGREGATORS.add("0x0000000000001ff3684f28c67538d4d072c22734")
DEX_ROUTERS = {"0xc8f6b8ba0dc0f175b568b99440b0867f69a29265"}


def load_rpc_url(path: Path, key: str) -> str:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    value = values.get(key, "")
    if not value:
        raise ValueError(f"missing RPC key {key}")
    return value


def load_universe() -> list[dict[str, Any]]:
    return ch_query(f"""
        WITH p AS
        (
          SELECT lower(pool_address) AS pool,min(created_block) AS created_block
          FROM eatlq.discovered_pools FINAL WHERE chain_id={CHAIN_ID} GROUP BY pool
        ), s AS (SELECT * FROM eatlq.base_cha_wallet_swaps FINAL)
        SELECT s.tx_hash,any(s.block_number) AS block_number,any(s.protocol) AS protocol,
               any(s.venue_address) AS venue_address,any(s.side) AS side,
               any(s.target_token_address) AS target_token_address,
               min(s.log_index) AS first_swap_log_index,
               groupArray(s.log_index) AS swap_log_indices
        FROM s INNER JOIN p ON s.venue_address=p.pool
        WHERE s.chain_id={CHAIN_ID} AND s.block_number BETWEEN {START} AND {END}
          AND s.protocol IN ('uniswap_v3','aero_cl')
          AND s.quote_token_address='{WETH}'
          AND s.block_number>=p.created_block AND s.block_number-p.created_block<=43200
        GROUP BY s.tx_hash
        ORDER BY block_number,s.tx_hash
    """, timeout=900)


def even_sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["protocol"], row["venue_address"], row["side"])].append(row)
    selected: dict[str, dict[str, Any]] = {}
    total = len(rows)
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda row: (int(row["block_number"]), row["tx_hash"]))
        quota = max(1, round(limit * len(group) / total))
        quota = min(quota, len(group))
        for index in range(quota):
            position = 0 if quota == 1 else round(index * (len(group) - 1) / (quota - 1))
            selected[group[position]["tx_hash"]] = group[position]
    if len(selected) > limit:
        ordered = sorted(selected.values(), key=lambda row: hashlib.sha256(row["tx_hash"].encode()).hexdigest())
        selected = {row["tx_hash"]: row for row in ordered[:limit]}
    elif len(selected) < limit:
        remaining = [row for row in rows if row["tx_hash"] not in selected]
        remaining.sort(key=lambda row: hashlib.sha256(row["tx_hash"].encode()).hexdigest())
        for row in remaining[: limit - len(selected)]:
            selected[row["tx_hash"]] = row
    return sorted(selected.values(), key=lambda row: (int(row["block_number"]), row["tx_hash"]))


def route_family(tx_to: str, pools: set[str], has_userops: bool) -> str:
    if has_userops or tx_to in ENTRYPOINTS:
        return "erc4337_entrypoint"
    if tx_to in UNIVERSAL_ROUTERS:
        return "universal_router"
    if tx_to in UNISWAP_ROUTERS:
        return "uniswap_router"
    if tx_to in AERODROME_ROUTERS:
        return "aerodrome_router"
    if tx_to in SOLVER_SETTLEMENT:
        return "solver_settlement"
    if tx_to in AGGREGATORS:
        return "aggregator"
    if tx_to in DEX_ROUTERS:
        return "dex_router"
    if tx_to in pools:
        return "direct_pool"
    return "unknown_contract_or_route"


def parse_userops(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for log in receipt.get("logs") or []:
        topics = [str(topic).lower() for topic in (log.get("topics") or [])]
        if len(topics) >= 3 and topics[0] == USER_OPERATION_EVENT:
            events.append({
                "log_index": int(log["logIndex"], 16),
                "entrypoint": normalize_address(log.get("address")),
                "userop_hash": topics[1],
                "sender": "0x" + topics[2][-40:],
                "paymaster": "0x" + topics[3][-40:] if len(topics) >= 4 else "",
            })
    return sorted(events, key=lambda row: row["log_index"])


def map_swap_to_userop(swap_log_index: int, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    previous = -1
    for event in events:
        if previous < swap_log_index < event["log_index"]:
            return event
        previous = event["log_index"]
    return None


def throttled_batch(
    rpc: RpcClient, calls: list[tuple[str, list[Any]]], *, timeout: int = 180
) -> list[Any]:
    for attempt in range(6):
        try:
            result = rpc.batch(calls, timeout=timeout)
            time.sleep(1.05)
            return result
        except RuntimeError as exc:
            if "request limit" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=1000)
    parser.add_argument("--rpc-env-file", type=Path, required=True)
    parser.add_argument("--rpc-env-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 500 <= args.sample_size <= 1000:
        raise ValueError("B3 sample size must be 500..1000")
    universe = load_universe()
    sample = even_sample(universe, args.sample_size)
    rpc = RpcClient(load_rpc_url(args.rpc_env_file, args.rpc_env_key))
    tx_hashes = [row["tx_hash"] for row in sample]
    transactions: dict[str, dict[str, Any]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    for offset in range(0, len(tx_hashes), 20):
        batch = tx_hashes[offset:offset + 20]
        tx_results = throttled_batch(
            rpc, [("eth_getTransactionByHash", [tx_hash]) for tx_hash in batch]
        )
        receipt_results = throttled_batch(
            rpc, [("eth_getTransactionReceipt", [tx_hash]) for tx_hash in batch]
        )
        for tx_hash, tx, receipt in zip(batch, tx_results, receipt_results):
            transactions[tx_hash] = tx or {}
            receipts[tx_hash] = receipt or {}
        print(f"[b3 tx/receipt] {min(offset+len(batch),len(tx_hashes))}/{len(tx_hashes)}", flush=True)
    code_calls = []
    code_keys = []
    for row in sample:
        tx = transactions[row["tx_hash"]]
        account = normalize_address(tx.get("from"))
        code_keys.append((row["tx_hash"], account))
        code_calls.append(("eth_getCode", [account, hex(int(row["block_number"]))]))
    codes: dict[str, str] = {}
    for offset in range(0, len(code_calls), 40):
        results = throttled_batch(rpc, code_calls[offset:offset + 40])
        for (tx_hash, _), code in zip(code_keys[offset:offset + 40], results):
            codes[tx_hash] = str(code or "0x").lower()

    pools = {row["venue_address"] for row in sample}
    output_rows = []
    for row in sample:
        tx_hash = row["tx_hash"]
        tx = transactions[tx_hash]
        receipt = receipts[tx_hash]
        tx_from = normalize_address(tx.get("from"))
        tx_to = normalize_address(tx.get("to"))
        events = parse_userops(receipt)
        mapped = map_swap_to_userop(int(row["first_swap_log_index"]), events)
        family = route_family(tx_to, pools, bool(events))
        code = codes[tx_hash]
        signer_class = "eoa" if code in ("0x", "0x0") else "eip7702" if code.startswith("0xef0100") else "unexpected_code_sender"
        if mapped:
            resolved = mapped["sender"]
            confidence = "ACCOUNT_HIGH_CONFIDENCE"
            method = "erc4337_userop_sender_log_boundary"
        elif events:
            resolved = ""
            confidence = "AMBIGUOUS"
            method = "erc4337_userop_boundary_unmapped"
        elif family == "solver_settlement":
            resolved = ""
            confidence = "SHARED_INFRA"
            method = "known_solver_settlement"
        elif tx_from and signer_class in ("eoa", "eip7702") and family != "unknown_contract_or_route":
            resolved = tx_from
            confidence = "ACCOUNT_HIGH_CONFIDENCE"
            method = "direct_transaction_signer_protocol_adapter"
        elif tx_from and signer_class in ("eoa", "eip7702"):
            resolved = tx_from
            confidence = "ENTITY_MEDIUM_CONFIDENCE"
            method = "transaction_signer_unknown_route"
        else:
            resolved = ""
            confidence = "UNATTRIBUTABLE"
            method = "no_supported_account_boundary"
        output_rows.append({
            **row,
            "tx_from": tx_from,
            "tx_to": tx_to,
            "tx_input_selector": str(tx.get("input") or "")[:10].lower(),
            "tx_signer_code_class_at_swap_block": signer_class,
            "route_family": family,
            "userop_count": len(events),
            "mapped_userop_hash": mapped["userop_hash"] if mapped else "",
            "resolved_account_or_entity": resolved,
            "resolution_confidence": confidence,
            "resolution_method": method,
        })
    counts = Counter(row["resolution_confidence"] for row in output_rows)
    routes = Counter(row["route_family"] for row in output_rows)
    methods = Counter(row["resolution_method"] for row in output_rows)
    high = counts["ACCOUNT_HIGH_CONFIDENCE"]
    result = {
        "schema_version": 1,
        "research_only": True,
        "no_go": True,
        "chain_id": CHAIN_ID,
        "window": {"start_block": START, "end_block": END},
        "target_universe": {
            "definition": "WETH V3/CL swaps in pools aged <=43200 blocks",
            "unique_transactions": len(universe),
            "unique_pools": len({row["venue_address"] for row in universe}),
        },
        "sample": {"size": len(sample), "method": "protocol/pool/side proportional + block-even deterministic"},
        "resolution_counts": dict(sorted(counts.items())),
        "route_family_counts": dict(sorted(routes.items())),
        "resolution_method_counts": dict(sorted(methods.items())),
        "high_confidence_coverage_pct": round(100 * high / len(output_rows), 6),
        "coverage_gate_65": high / len(output_rows) >= 0.65,
        "precision_gate_95": "PENDING_INDEPENDENT_TRACE_GOLDEN",
        "pnl_reconstructable_gate_80": "PENDING_ACCOUNT_INVENTORY_GOLDEN",
        "shared_contract_error_gate_1": "PENDING_CLASSIFIER_GOLDEN",
        "rpc_batch_calls": rpc.calls,
        "rpc_logical_calls": 3 * len(sample),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rows": output_rows,
        "external_cross_validation": {
            "nansen": "NOT_RUN_MISSING_AUTHORITY_AND_API_KEY",
            "gmgn": "PUBLIC_DOCS_ONLY_NOT_GOLDEN",
            "blockscout": "PUBLIC_CAPABILITY_VERIFIED; LOCAL_EVENT_DECODER_USED",
        },
        "verdict": "PENDING_GOLDEN_GATES",
        "rh1_merit": "UNTESTED",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "target_universe", "sample", "resolution_counts", "route_family_counts",
        "high_confidence_coverage_pct", "coverage_gate_65", "elapsed_seconds"
    )}, indent=2))


if __name__ == "__main__":
    main()
