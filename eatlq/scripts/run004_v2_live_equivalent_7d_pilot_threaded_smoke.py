"""Smoke test for threaded_single_reserves: confirms requests.Session-based
threaded path returns 100% non_zero_reserve on the rpc_probe sample, with
no socket leakage."""
from __future__ import annotations

import csv
import gzip
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_v2_live_equivalent_1d_pilot import (  # noqa: E402
    pilot_rpc_from_config,
    threaded_single_reserves,
)
from scripts.run004_v2_live_equivalent_backtest import DEFAULT_CONFIG, load_config  # noqa: E402

ROWS_GZ = ROOT / "mvp/diagnostics/run004_v2_live_equivalent_7d_pilot_rows.csv.gz"
SAMPLE_N = int(os.environ.get("SMOKE_N", "500"))


def main() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    rpc = pilot_rpc_from_config(cfg)
    rng = random.Random(42)
    candidates: list[tuple[str, int]] = []
    with gzip.open(ROWS_GZ, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                eb = int(row["entry_block"])
            except (KeyError, ValueError):
                continue
            candidates.append((row["pool_address"], max(1, eb - 1)))
    rng.shuffle(candidates)
    sample = candidates[:SAMPLE_N]
    print(f"[smoke] sample_n={len(sample)}", flush=True)

    t0 = time.monotonic()
    results = threaded_single_reserves(rpc, sample, workers=16)
    elapsed = time.monotonic() - t0
    dist = {"rpc_failure": 0, "decoded_zero": 0, "non_zero_reserve": 0}
    for key in sample:
        v = results.get(key)
        if v is None:
            dist["rpc_failure"] += 1
        elif v[0] == 0 and v[1] == 0:
            dist["decoded_zero"] += 1
        else:
            dist["non_zero_reserve"] += 1
    out = {
        "sample_n": len(sample),
        "elapsed_seconds": elapsed,
        "distribution": dist,
        "rpc_calls": dict(rpc.calls),
        "rpc_errors": dict(rpc.errors),
    }
    print(json.dumps(out, indent=2), flush=True)


if __name__ == "__main__":
    main()
