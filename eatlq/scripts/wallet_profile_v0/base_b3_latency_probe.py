#!/usr/bin/env python3
"""Bounded historical receipt+UserOp parsing latency probe for B3."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from base_b3_entity_attribution import load_rpc_url, parse_userops
from base_transfer_net_attribution import RpcClient


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.999999)))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--rpc-env-file", type=Path, required=True)
    parser.add_argument("--rpc-env-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    rows = sorted(source["rows"], key=lambda row: (int(row["block_number"]), row["tx_hash"]))
    step = (len(rows) - 1) / (args.sample_size - 1)
    sample = [rows[round(index * step)] for index in range(args.sample_size)]
    rpc = RpcClient(load_rpc_url(args.rpc_env_file, args.rpc_env_key))
    timings = []
    parsed = []
    for row in sample:
        started = time.perf_counter()
        receipt = rpc.call("eth_getTransactionReceipt", [row["tx_hash"]], timeout=30)
        events = parse_userops(receipt or {})
        elapsed = time.perf_counter() - started
        timings.append(elapsed)
        parsed.append({"tx_hash": row["tx_hash"], "seconds": round(elapsed, 6), "userop_count": len(events)})
    p50 = statistics.median(timings)
    p95 = percentile(timings, 0.95)
    result = {
        "schema_version": 1,
        "research_only": True,
        "sample_count": len(sample),
        "operation": "historical eth_getTransactionReceipt + local UserOperationEvent parse",
        "p50_seconds": round(p50, 6),
        "p95_seconds": round(p95, 6),
        "max_seconds": round(max(timings), 6),
        "threshold_seconds": 2.0,
        "latency_gate": p95 <= 2.0,
        "rows": parsed,
        "no_go": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "sample_count", "p50_seconds", "p95_seconds", "max_seconds", "threshold_seconds", "latency_gate"
    )}, indent=2))


if __name__ == "__main__":
    main()
