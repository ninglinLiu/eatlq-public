#!/usr/bin/env python3
"""Extend the frozen B5 decoded-swap evidence to the B8 observation cutoff."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path("/opt/EATLQ")
OUT = ROOT / "mvp/diagnostics/base_b8_future_swaps_20260714.tsv"
CUTOFF = 46_865_124


def b5_module():
    path = ROOT / "scripts/research/base_b5/b5_offline_reverify.py"
    spec = importlib.util.spec_from_file_location("b5_for_b8_extract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = b5_module()
    df, _, _ = mod.load_base()
    df["regime"] = df.t0.map(mod.regime)
    scoped = df[df.regime.notna()]
    start_exclusive = int(scoped.entry_block.max()) + 11_000
    pools = sorted(set(scoped.pool.str.lower()))
    quoted = ",".join("'" + p + "'" for p in pools)
    sql = f"""SELECT lower(pool_address),block_number,toUnixTimestamp64Milli(block_time),lower(tx_hash),log_index,
toString(amount0),toString(amount1),tick FROM eatlq.uniswap_v3_swaps_decoded FINAL
WHERE chain_id=8453 AND removed=0 AND block_number>{start_exclusive} AND block_number<={CUTOFF}
AND lower(pool_address) IN ({quoted}) ORDER BY pool_address,block_number,log_index FORMAT TabSeparated"""
    proc = subprocess.run(
        ["docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client", "--query", sql],
        text=True,
        capture_output=True,
        timeout=600,
        check=True,
    )
    OUT.write_text(proc.stdout)
    print({"out": str(OUT), "rows": len(proc.stdout.splitlines()), "start_exclusive": start_exclusive, "cutoff": CUTOFF})


if __name__ == "__main__":
    main()
