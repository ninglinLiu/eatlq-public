#!/usr/bin/env python3
"""Independent callTracer golden for B3 account/entity resolutions."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from base_b3_entity_attribution import load_rpc_url
from base_transfer_net_attribution import RpcClient


def walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    output = [node]
    for child in node.get("calls") or []:
        output.extend(walk(child))
    return output


def contains_to(node: dict[str, Any], address: str) -> bool:
    return any(str(item.get("to") or "").lower() == address for item in walk(node))


def choose(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["resolution_confidence"] == "ACCOUNT_HIGH_CONFIDENCE":
            groups[row["route_family"]].append(row)
    chosen: dict[str, dict[str, Any]] = {}
    # Protect the key innovation from being drowned by Universal Router volume.
    for family, group in sorted(groups.items()):
        group.sort(key=lambda row: (int(row["block_number"]), row["tx_hash"]))
        floor = min(len(group), 25 if family == "erc4337_entrypoint" else 5)
        for index in range(floor):
            position = 0 if floor == 1 else round(index * (len(group) - 1) / (floor - 1))
            chosen[group[position]["tx_hash"]] = group[position]
    remaining = [row for row in rows if row["resolution_confidence"] == "ACCOUNT_HIGH_CONFIDENCE" and row["tx_hash"] not in chosen]
    remaining.sort(key=lambda row: hashlib.sha256(row["tx_hash"].encode()).hexdigest())
    for row in remaining[: max(0, size - len(chosen))]:
        chosen[row["tx_hash"]] = row
    return sorted(chosen.values(), key=lambda row: (row["route_family"], int(row["block_number"]), row["tx_hash"]))[:size]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--rpc-env-file", type=Path, required=True)
    parser.add_argument("--rpc-env-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    sample = choose(source["rows"], args.sample_size)
    if len(sample) != args.sample_size:
        raise RuntimeError(f"expected {args.sample_size} golden rows, got {len(sample)}")
    rpc = RpcClient(load_rpc_url(args.rpc_env_file, args.rpc_env_key))
    results = []
    started = time.monotonic()
    for index, row in enumerate(sample, 1):
        trace = rpc.call(
            "debug_traceTransaction",
            [row["tx_hash"], {"tracer": "callTracer", "timeout": "30s"}],
            timeout=90,
        )
        nodes = walk(trace)
        pool_calls = sum(str(node.get("to") or "").lower() == row["venue_address"] for node in nodes)
        if row["resolution_method"] == "erc4337_userop_sender_log_boundary":
            account_nodes = [
                node for node in nodes
                if str(node.get("to") or "").lower() == row["resolved_account_or_entity"]
                and contains_to(node, row["venue_address"])
            ]
            trace_match = len(account_nodes) == 1 and pool_calls >= 1
            criterion = "exactly one UserOp-sender call subtree contains target pool"
        else:
            root_from = str(trace.get("from") or "").lower()
            trace_match = root_from == row["resolved_account_or_entity"] and pool_calls >= 1
            account_nodes = []
            criterion = "trace root signer equals account and trace contains target pool"
        results.append({
            "tx_hash": row["tx_hash"],
            "block_number": row["block_number"],
            "route_family": row["route_family"],
            "resolution_method": row["resolution_method"],
            "resolved_account_or_entity": row["resolved_account_or_entity"],
            "target_pool": row["venue_address"],
            "trace_root_from": str(trace.get("from") or "").lower(),
            "trace_root_to": str(trace.get("to") or "").lower(),
            "pool_call_count": pool_calls,
            "matching_account_subtree_count": len(account_nodes),
            "criterion": criterion,
            "trace_match": trace_match,
        })
        if index % 10 == 0:
            print(f"[b3 trace golden] {index}/{len(sample)}", flush=True)
    matched = sum(row["trace_match"] for row in results)
    output = {
        "schema_version": 1,
        "research_only": True,
        "sample_method": "route floors (ERC4337=25, other=5) then deterministic hash fill",
        "sample_count": len(results),
        "matched": matched,
        "mismatched": len(results) - matched,
        "precision_pct": round(100 * matched / len(results), 6),
        "precision_gate_95": matched / len(results) >= 0.95,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rpc_calls": rpc.calls,
        "rows": results,
        "no_go": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: output[key] for key in (
        "sample_count", "matched", "mismatched", "precision_pct", "precision_gate_95", "elapsed_seconds"
    )}, indent=2))


if __name__ == "__main__":
    main()
