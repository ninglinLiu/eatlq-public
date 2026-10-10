"""Probe whether the high eth_call error rate observed in 7D pilot is due to
batched RPC behaviour or genuine on-chain reverts/zero reserves.

Reads a sample of pool/entry_block_minus_1 pairs from the 7D rows and queries
getReserves through several batch sizes (50, 20, 10, 5, 1) to find the largest
batch size that is reliable on the configured endpoint. Reports the result
distribution per batch size.

This is a small read-only diagnostic; no state change to any other artifact.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v2_live_equivalent_1d_pilot import (  # noqa: E402
    batch_reserves,
    pilot_rpc_from_config,
)
from scripts.run004_v2_live_equivalent_backtest import (  # noqa: E402
    DEFAULT_CONFIG,
    load_config,
)

ROWS_GZ = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rows.csv.gz"
OUT = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rpc_probe.json"
SAMPLE_N = int(os.environ.get("V2_7D_PROBE_N", "200"))
BATCH_SIZES = [int(x) for x in os.environ.get("V2_7D_PROBE_BATCHES", "50,20,10,5,1").split(",")]


def load_sample() -> list[tuple[str, int]]:
    rng = random.Random(42)
    candidates: list[tuple[str, int]] = []
    with gzip.open(ROWS_GZ, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("entry_block"):
                continue
            try:
                eb = int(row["entry_block"])
            except ValueError:
                continue
            candidates.append((row["pool_address"], max(1, eb - 1)))
    rng.shuffle(candidates)
    return candidates[:SAMPLE_N]


def classify(res):
    if res is None:
        return "rpc_failure"
    r0, r1 = res
    if r0 == 0 and r1 == 0:
        return "decoded_zero"
    return "non_zero_reserve"


def main() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    rpc = pilot_rpc_from_config(cfg)
    sample = load_sample()
    print(f"[probe] sample_n={len(sample)} batch_sizes={BATCH_SIZES}")

    per_batch_results: dict[int, dict[str, int]] = {}
    for size in BATCH_SIZES:
        rpc.calls.clear()
        rpc.errors.clear()
        results = batch_reserves(rpc, sample, chunk_size=size)
        dist = {"rpc_failure": 0, "decoded_zero": 0, "non_zero_reserve": 0}
        for key in sample:
            dist[classify(results.get(key))] += 1
        per_batch_results[size] = {
            **dist,
            "eth_call_invocations": int(rpc.calls.get("eth_call", 0)),
            "eth_call_item_errors": int(rpc.errors.get("eth_call", 0)),
        }
        print(f"[probe] batch_size={size} dist={dist}")

    out = {
        "sample_n": len(sample),
        "batch_sizes_tested": BATCH_SIZES,
        "per_batch_size": per_batch_results,
        "interpretation_note": (
            "non_zero_reserve / sample_n at batch_size=1 is the truth. The largest"
            " batch_size where rpc_failure / sample_n stays below ~0.02 is the"
            " safe batch size for the configured endpoint."
        ),
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
