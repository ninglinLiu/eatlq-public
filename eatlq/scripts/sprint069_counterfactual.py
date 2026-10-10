#!/usr/bin/env python3
"""Sprint069 — 100U Capital-Capped Counterfactual for shortlisted candidates.

Uses alpha_001's historical CSV as the reference dataset for labeling.
For other candidates (no QuoterV2 exits yet), computes synthetic counterfactuals
using the overlap with alpha_001's labeled rows.

Run: python3 scripts/sprint069_counterfactual.py
"""
from __future__ import annotations

import csv, json, statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_CSV = ROOT / "logs/sprint055_paper_shadow.csv"
DIAG069 = ROOT / "mvp/diagnostics/sprint069"
AUDIT069 = ROOT / "mvp/audits/sprint069"

CAPITAL_U = 100.0
TRADE_SIZE_U = 10.0
MAX_CONCURRENT = 3
GAS_PCT = 0.002  # 0.2% per side


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_labeled_rows() -> list[dict]:
    rows = []
    with SRC_CSV.open() as f:
        for r in csv.DictReader(f):
            if str(r.get("exit_30m_ok")) == "True" and r.get("net_return_30m"):
                try:
                    r["net_return_30m"] = float(r["net_return_30m"])
                    r["net_return_5m"] = float(r.get("net_return_5m", 0) or 0)
                    rows.append(r)
                except (ValueError, TypeError):
                    pass
    return rows


def compute_counterfactual(rows: list[dict], strategy: str,
                            capital: float = CAPITAL_U,
                            trade_size: float = TRADE_SIZE_U,
                            gas_pct: float = GAS_PCT,
                            max_concurrent: int = MAX_CONCURRENT,
                            cost_per_trade: float = 0.0) -> dict:
    """Compute trading metrics for a given strategy on a set of labeled rows."""
    if not rows:
        return {"strategy": strategy, "n_trades": 0, "pnl": 0, "wr": None, "pf": None}

    # Sort by signal_time
    sorted_rows = sorted(rows, key=lambda r: r.get("signal_time", ""))

    deployed = capital  # current capital
    active_positions: list[dict] = []  # {pool, entry_time, exit_time, trade_size, ret}
    trades = []
    max_capital = capital
    min_capital = capital
    current_time = None

    for row in sorted_rows:
        t_str = row.get("signal_time", "")
        pool = row.get("pool", "")
        ret = row["net_return_30m"]  # net return as fraction

        if strategy == "observation_equal_weight":
            # Each signal = 1 unit weight, no capital constraint
            pnl_u = ret * trade_size - cost_per_trade - gas_pct * trade_size * 2
            trades.append({"ret": ret, "pnl_u": pnl_u, "pool": pool})

        elif strategy == "pool_hour_dedup":
            # One trade per pool per hour window
            hour_key = t_str[:13] if t_str else ""  # YYYY-MM-DD HH
            key = (pool, hour_key)
            if key not in {t.get("key") for t in trades}:
                pnl_u = ret * trade_size - cost_per_trade - gas_pct * trade_size * 2
                trades.append({"ret": ret, "pnl_u": pnl_u, "pool": pool, "key": key})

        elif strategy == "first_signal_per_pool":
            # Only first signal per pool ever
            if pool not in {t.get("pool") for t in trades}:
                pnl_u = ret * trade_size - cost_per_trade - gas_pct * trade_size * 2
                trades.append({"ret": ret, "pnl_u": pnl_u, "pool": pool})

        elif strategy == "one_position_per_pool_30m_cooldown":
            # 30m cooldown per pool
            last_trade_time_dict = {t["pool"]: t["t"] for t in trades if "t" in t}
            if t_str and pool in last_trade_time_dict:
                try:
                    # Parse ISO 8601 format: "2026-05-11T23:55:00+00:00"
                    last_t_str = last_trade_time_dict[pool]
                    last_t = datetime.fromisoformat(last_t_str.replace("+00:00", "+00:00"))
                    curr_t = datetime.fromisoformat(t_str.replace("+00:00", "+00:00"))
                    if (curr_t - last_t).total_seconds() < 1800:
                        continue
                except Exception:
                    pass
            pnl_u = ret * trade_size - cost_per_trade - gas_pct * trade_size * 2
            trades.append({"ret": ret, "pnl_u": pnl_u, "pool": pool, "t": t_str})

        elif strategy == "capital_capped_100u_fixed_10u":
            # 10U per trade, max 100U capital, wait if not enough capital
            if deployed < trade_size:
                continue  # Skip — insufficient capital
            actual_size = min(trade_size, deployed)
            pnl_u = ret * actual_size - cost_per_trade - gas_pct * actual_size * 2
            deployed = max(0, deployed + pnl_u)
            min_capital = min(min_capital, deployed)
            max_capital = max(max_capital, deployed)
            trades.append({"ret": ret, "pnl_u": pnl_u, "pool": pool, "deployed": deployed})

        elif strategy == "capital_capped_100u_max_3_concurrent":
            # max 3 concurrent positions, 10U each
            # Simplified: 3 trades per time bucket, FIFO queue
            active_count = len(active_positions)
            if active_count >= max_concurrent:
                # Close earliest position if it has expired (simplified: always close first)
                closed = active_positions.pop(0)
                deployed += closed["trade_size"] + closed["pnl_u"]
            if deployed < trade_size:
                continue
            actual_size = min(trade_size, deployed / max_concurrent)
            pnl_u = ret * actual_size - cost_per_trade - gas_pct * actual_size * 2
            deployed -= actual_size
            active_positions.append({"pnl_u": pnl_u, "trade_size": actual_size})
            trades.append({"ret": ret, "pnl_u": pnl_u, "pool": pool})

    if not trades:
        return {"strategy": strategy, "n_trades": 0, "pnl": 0, "wr": None, "pf": None,
                "max_drawdown": 0, "concentration": {}}

    pnls = [t["pnl_u"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    wr = len(wins) / len(pnls)
    mean_ret = statistics.mean([t["ret"] for t in trades])
    median_ret = statistics.median([t["ret"] for t in trades])
    max_loss = min(pnls) if pnls else 0

    # Drawdown (cumulative PnL basis)
    cum_pnl = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum_pnl += p
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    # Pool concentration
    pool_counts = defaultdict(int)
    for t in trades:
        pool_counts[t["pool"]] += 1
    top_pool_share = max(pool_counts.values()) / len(trades) if trades else 0

    final_capital = capital + total_pnl if "capital_capped" in strategy else capital + total_pnl
    roi_pct = round((final_capital - capital) / capital * 100, 2)

    return {
        "strategy": strategy,
        "n_signals_available": len(rows),
        "n_trades": len(trades),
        "total_pnl_u": round(total_pnl, 4),
        "wr": round(wr, 4),
        "pf": round(pf, 4),
        "mean_ret": round(mean_ret, 6),
        "median_ret": round(median_ret, 6),
        "max_loss_u": round(max_loss, 4),
        "max_drawdown_u": round(max_dd, 4),
        "top_pool_share": round(top_pool_share, 4),
        "roi_on_100u_pct": roi_pct,
        "final_capital_u": round(capital + total_pnl, 2) if "observation" in strategy or "pool" in strategy
                          else round(final_capital, 2),
    }


def main():
    print(f"[{now_utc()}] Sprint069 counterfactual computation starting...")

    rows = load_labeled_rows()
    print(f"  Loaded {len(rows)} labeled rows from alpha_001")

    strategies = [
        "observation_equal_weight",
        "pool_hour_dedup",
        "first_signal_per_pool",
        "one_position_per_pool_30m_cooldown",
        "capital_capped_100u_fixed_10u",
        "capital_capped_100u_max_3_concurrent",
    ]
    cost_levels = [0.0, 0.02, 0.05, 0.10]

    results = []
    for strat in strategies:
        for cost in cost_levels:
            r = compute_counterfactual(rows, strat, cost_per_trade=cost)
            r["cost_per_trade_u"] = cost
            results.append(r)
            print(f"  {strat} | cost={cost:.2f} | trades={r['n_trades']} | "
                  f"pnl={r.get('total_pnl_u', 0):.2f}U | "
                  f"wr={r.get('wr', 0)*100:.1f}% | pf={r.get('pf', 0):.3f}")

    # Write CSV
    csv_path = AUDIT069 / "sprint069_capital_counterfactual.csv"
    if results:
        fieldnames = list(results[0].keys())
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(results)

    # Write JSON summary
    summary = {
        "kind": "sprint069_counterfactual_summary",
        "generated_at": now_utc(),
        "source_alpha": "alpha_001",
        "n_labeled_rows": len(rows),
        "capital_u": CAPITAL_U,
        "trade_size_u": TRADE_SIZE_U,
        "gas_pct_per_side": GAS_PCT,
        "note": "411 observations != 411 independent trades. Each strategy applies "
                "different dedup/cooldown/capital constraints. "
                "Observation PF (no constraints) overestimates real tradeable PF.",
        "results": results,
    }
    json_path = AUDIT069 / "sprint069_capital_counterfactual.json"
    json_path.write_text(json.dumps(summary, indent=2))

    print(f"\n  CSV: {csv_path}")
    print(f"  JSON: {json_path}")
    print(f"[{now_utc()}] Counterfactual complete.")
    return results


if __name__ == "__main__":
    main()
