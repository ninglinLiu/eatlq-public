#!/usr/bin/env python3
"""Scan ClickHouse for high-activity V3 LP candidate pools (Path B)."""
from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mvp" / "audits" / "path_b_lp_candidates"
CH_CONTAINER = os.environ.get("CH_CONTAINER", "eatlq-clickhouse-1")
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

SQL = """
SELECT
    lower(s.pool_address) AS pool,
    dp.fee_bps AS fee_bps,
    lower(dp.token0_address) AS token0,
    lower(dp.token1_address) AS token1,
    count() AS swap_count_24h,
    quantile(0.1)(s.tick) AS tick_p10,
    quantile(0.9)(s.tick) AS tick_p90,
    (quantile(0.9)(s.tick) - quantile(0.1)(s.tick)) AS tick_bandwidth
FROM eatlq.uniswap_v3_swaps_decoded AS s
INNER JOIN eatlq.discovered_pools AS dp ON lower(dp.pool_address) = lower(s.pool_address)
WHERE s.block_time >= now() - INTERVAL 24 HOUR
  AND dp.has_weth_leg = 1
  AND (
    lower(dp.token0_address) IN ('{weth}', '{usdc}')
    OR lower(dp.token1_address) IN ('{weth}', '{usdc}')
  )
GROUP BY pool, fee_bps, token0, token1
HAVING swap_count_24h >= 1000
ORDER BY swap_count_24h DESC
LIMIT 50
FORMAT JSONEachRow
""".format(weth=WETH, usdc=USDC)


def ch_query(sql: str) -> list[dict]:
    cmd = ["docker", "exec", CH_CONTAINER, "clickhouse-client", "-q", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"CH query failed: {proc.stderr.strip()}")
    rows = []
    for line in proc.stdout.strip().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    try:
        rows = ch_query(SQL)
    except Exception as e:
        rows = []
        err = str(e)
    else:
        err = None

    csv_path = OUT_DIR / "lp_candidates.csv"
    fields = [
        "pool", "fee_bps", "token0", "token1",
        "swap_count_24h", "tick_p10", "tick_p90", "tick_bandwidth",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    md = OUT_DIR / "READOUT.md"
    lines = [
        "# Path B LP Candidate Scan",
        f"\nGenerated: {generated}\n",
        f"Query: 24h swaps, WETH/USDC leg, swap_count >= 1000\n",
    ]
    if err:
        lines.append(f"**ERROR**: {err}\n")
    else:
        lines.append(f"**Pools found**: {len(rows)}\n\n")
        lines.append("| pool | fee_bps | swaps_24h | tick_bandwidth |\n")
        lines.append("|---|---:|---:|---:|\n")
        for r in rows[:15]:
            lines.append(
                f"| `{r.get('pool','')[:18]}...` | {r.get('fee_bps')} | "
                f"{r.get('swap_count_24h')} | {r.get('tick_bandwidth')} |\n"
            )
        if rows:
            top = rows[0]
            lines.append(
                f"\n**Preliminary pick**: `{top.get('pool')}` "
                f"(fee_bps={top.get('fee_bps')}, swaps={top.get('swap_count_24h')}). "
                "Review IL risk before mint.\n"
            )
        else:
            lines.append(
                "\nNo pools met threshold. Consider lowering swap_count or "
                "widening fee tiers. fee=10000 pools had 0 swaps in prior audit.\n"
            )

    md.write_text("".join(lines))
    summary = {
        "generated": generated,
        "pool_count": len(rows),
        "csv": str(csv_path),
        "readout": str(md),
        "error": err,
    }
    (OUT_DIR / "scan_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
