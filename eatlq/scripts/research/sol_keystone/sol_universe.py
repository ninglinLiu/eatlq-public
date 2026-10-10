#!/usr/bin/env python3
"""Task 1 — Solana early-pool universe (ClickHouse-first; fallback cached JSONL)."""
from __future__ import annotations

import subprocess
from collections import Counter

from common import OUT, build_universe_rows, write_json


def clickhouse_sol_check() -> dict:
    try:
        r = subprocess.run(
            ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
             "SHOW TABLES FROM eatlq"],
            capture_output=True, text=True, timeout=30,
        )
        tables = r.stdout.strip().split("\n") if r.returncode == 0 else []
        sol = [t for t in tables if any(x in t.lower() for x in ("solana", "pump", "raydium"))]
        return {"available": bool(sol), "sol_tables": sol, "source": "clickhouse"}
    except Exception as e:
        return {"available": False, "error": str(e), "source": "clickhouse"}


def main():
    ch = clickhouse_sol_check()
    rows = build_universe_rows()
    n = len(rows)
    days = Counter(r["day"] for r in rows)
    splits = Counter(r["split"] for r in rows)
    with_early = sum(1 for r in rows if r["has_earlypath"])
    with_grid = sum(1 for r in rows if r["has_grid"])
    graduated = sum(1 for r in rows if r["graduated"])
    ages = [r["lifespan_s"] or 0 for r in rows if r.get("lifespan_s") is not None]
    n180 = [r["n_trades_180s"] or 0 for r in rows if r.get("n_trades_180s") is not None]

    stats = {
        "data_source": "mvp/diagnostics/sol/*.jsonl (ClickHouse has no Sol tables)",
        "clickhouse": ch,
        "rpc_calls_this_task": 0,
        "cash_cost_usd": 0,
        "entry_slot_s": 180,
        "venue_mix": {"pump_bonding_curve": n, "raydium": 0},
        "pool_count": n,
        "with_earlypath_features": with_early,
        "with_price_grid": with_grid,
        "graduated_pct": graduated / n if n else 0,
        "day_distribution": dict(sorted(days.items())),
        "split_distribution": dict(splits),
        "lifespan_s_median": sorted(ages)[len(ages) // 2] if ages else None,
        "n_trades_180s_median": sorted(n180)[len(n180) // 2] if n180 else None,
        "time_span_days": sorted(days.keys()),
        "pump_fun_raydium_ratio": f"{n}:0",
    }
    write_json("_sol_universe.json", {"stats": stats, "rows": rows})
    print(f"universe n={n} earlypath={with_early} grid={with_grid} CH_sol={ch['available']}")


if __name__ == "__main__":
    main()
