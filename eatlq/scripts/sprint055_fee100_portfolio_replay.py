#!/usr/bin/env python3
"""Sprint055-A — fee_100bps Portfolio Replay.

Uses Sprint054 QuoterV2-labeled candidates to simulate portfolio-level
economics across multiple position-sizing and concurrency policies.

No RPC, no live, no training. Pure offline simulation on 543 labeled rows.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
S054_LABELS = ROOT / "mvp/diagnostics/sprint054/labeled_candidates.json"
S053_META   = ROOT / "mvp/diagnostics/sprint053/pool_meta.json"
AUDIT_OUT   = ROOT / "mvp/audits/sprint055"
DIAG_OUT    = ROOT / "mvp/diagnostics/sprint055"
AUDIT_OUT.mkdir(parents=True, exist_ok=True)
DIAG_OUT.mkdir(parents=True, exist_ok=True)

GAS_PCT     = 0.002   # 0.2% gas+slippage proxy
WINDOW_SEC  = 300     # 5 min

# USD notional levels (expressed as WETH-equivalent fractions for PnL; actual $ irrelevant for ratios)
NOTIONALS   = [10, 50, 100]     # USD notional
MAX_CONCURRENT = [1, 3, 5]
COOLDOWNS   = [5, 15]           # minutes


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_labeled() -> tuple[list[dict], dict]:
    rows = json.loads(S054_LABELS.read_text())
    meta = json.loads(S053_META.read_text())
    return rows, meta


def _fee_label(f: int | None) -> str:
    if f is None: return "fee_unknown"
    return {100: "fee_100bps", 500: "fee_500bps", 3000: "fee_3000bps", 10000: "fee_10000bps"}.get(f, f"fee_{f}bps")


def _heat_bucket(h: int) -> str:
    if h < 20: return "heat_lt_20"
    if h < 50: return "heat_20_50"
    if h < 100: return "heat_50_100"
    return "heat_ge_100"


# ---------------------------------------------------------------------------
# Portfolio simulation
# ---------------------------------------------------------------------------

def simulate_portfolio(
    signals: list[dict],
    max_concurrent: int,
    cooldown_sec: int,
    notional_usd: float,
) -> dict[str, Any]:
    """Simulate portfolio with fixed notional, concurrency limit, same-pool cooldown.

    Signals must be sorted by wts ascending.
    Returns per-trade list and summary statistics.
    """
    # Track open positions: (open_wts, close_wts, pool, net_ret)
    open_positions: list[dict] = []  # {open_wts, close_wts, pool, net_ret, notional}
    pool_last_close: dict[str, int] = {}  # pool → last close timestamp
    trades: list[dict] = []

    for sig in signals:
        wts  = sig["wts"]
        pool = sig["pool_address"]
        net  = sig["net_return_5m"] - GAS_PCT

        # Close expired positions
        open_positions = [p for p in open_positions if p["close_wts"] > wts]

        # Cooldown check
        last_close = pool_last_close.get(pool, 0)
        if wts < last_close + cooldown_sec:
            continue  # in cooldown for this pool

        # Concurrency check
        if len(open_positions) >= max_concurrent:
            continue

        # Open position
        close_wts = wts + WINDOW_SEC
        open_positions.append({"open_wts": wts, "close_wts": close_wts, "pool": pool, "net_ret": net})
        pool_last_close[pool] = close_wts
        pnl_usd = notional_usd * net
        trades.append({
            "wts": wts,
            "date": datetime.fromtimestamp(wts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "pool": pool,
            "heat": sig.get("activity_heat", 0),
            "net_ret_after_gas": round(net, 6),
            "pnl_usd": round(pnl_usd, 4),
            "notional_usd": notional_usd,
        })

    return _summarise_trades(trades)


def _summarise_trades(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"n_trades": 0, "note": "no_trades"}

    rets = [t["net_ret_after_gas"] for t in trades]
    pnls = [t["pnl_usd"] for t in trades]
    notional = trades[0]["notional_usd"]
    n = len(trades)
    pos = sum(1 for r in rets if r > 0)
    gains = [r for r in rets if r > 0]
    losses = [-r for r in rets if r < 0]
    pf = (sum(gains) / sum(losses)) if losses else (999.0 if gains else None)

    # Equity curve (cumulative PnL)
    equity = [0.0]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    # Loss tail (10th pct)
    loss_tail = sorted(rets)[max(0, int(n * 0.1))]

    # Date span for trades/day
    dates = sorted({t["date"] for t in trades})
    days_active = len(dates)
    full_span = (max(t["wts"] for t in trades) - min(t["wts"] for t in trades)) / 86400 + 1

    # Pool concentration
    pool_cnt = Counter(t["pool"] for t in trades)
    total_p = sum(pool_cnt.values())
    top1 = pool_cnt.most_common(1)[0][1] / total_p if total_p else 0

    total_invested = n * notional
    total_pnl = sum(pnls)
    roi_pct = total_pnl / total_invested if total_invested else 0

    return {
        "n_trades": n,
        "n_pos_trades": pos,
        "n_neg_trades": n - pos,
        "win_rate": round(pos / n, 4),
        "profit_factor": round(pf, 3) if pf and pf < 999 else pf,
        "total_pnl_usd": round(total_pnl, 3),
        "total_invested_usd": round(total_invested, 2),
        "roi_pct": round(roi_pct * 100, 3),
        "mean_ret": round(statistics.mean(rets), 5),
        "median_ret": round(statistics.median(rets), 5),
        "loss_tail_p10": round(loss_tail, 5),
        "max_loss": round(min(rets), 5),
        "max_gain": round(max(rets), 5),
        "max_drawdown_usd": round(max_dd, 3),
        "max_drawdown_pct_of_notional": round(max_dd / notional * 100, 2) if notional else 0,
        "equity_curve_usd": [round(e, 4) for e in equity],
        "days_active": days_active,
        "trades_per_day_active": round(n / days_active, 1) if days_active else 0,
        "pool_top1_share": round(top1, 3),
    }


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def run_replay() -> dict[str, Any]:
    rows, meta = load_labeled()

    # Enrich
    for r in rows:
        m = meta.get(r["pool_address"], {})
        r["fee_bps"] = m.get("fee_bps")
        r["fee_label"] = _fee_label(m.get("fee_bps"))
        r["heat_bucket"] = _heat_bucket(r.get("activity_heat", 0))

    # Universe definitions
    universes = {
        "fee_100_all":   [r for r in rows if r.get("exit_quote_ok") and r["fee_bps"] == 100],
        "fee_100_h2050": [r for r in rows if r.get("exit_quote_ok") and r["fee_bps"] == 100 and r["activity_heat"] >= 20 and r["activity_heat"] < 50],
        "fee_100_h20p":  [r for r in rows if r.get("exit_quote_ok") and r["fee_bps"] == 100 and r["activity_heat"] >= 20],
        "fee_500_all":   [r for r in rows if r.get("exit_quote_ok") and r["fee_bps"] == 500],
        "fee_3000_all":  [r for r in rows if r.get("exit_quote_ok") and r["fee_bps"] == 3000],
        "fee_10000_all": [r for r in rows if r.get("exit_quote_ok") and r["fee_bps"] == 10000],
    }

    results = []
    equity_records = []

    for universe_name, universe_rows in universes.items():
        if not universe_rows:
            continue
        sorted_sigs = sorted(universe_rows, key=lambda x: x["wts"])
        for notional in NOTIONALS:
            for max_c in MAX_CONCURRENT:
                for cooldown_m in COOLDOWNS:
                    cooldown_s = cooldown_m * 60
                    sim = simulate_portfolio(sorted_sigs, max_c, cooldown_s, float(notional))
                    row = {
                        "universe": universe_name,
                        "notional_usd": notional,
                        "max_concurrent": max_c,
                        "cooldown_min": cooldown_m,
                        "universe_total_rows": len(universe_rows),
                        **{k: v for k, v in sim.items() if k != "equity_curve_usd"},
                    }
                    results.append(row)
                    # Equity curve record
                    ec = sim.get("equity_curve_usd", [])
                    for i, e in enumerate(ec):
                        equity_records.append({
                            "universe": universe_name,
                            "notional_usd": notional,
                            "max_concurrent": max_c,
                            "cooldown_min": cooldown_m,
                            "trade_idx": i,
                            "equity_usd": e,
                        })

    return {"results": results, "equity_records": equity_records, "universes": {k: len(v) for k, v in universes.items()}}


def _sclass(n: int) -> str:
    if n < 30: return "anecdote"
    if n < 100: return "smoke"
    return "very_weak_feasibility"


def find_best_policy(results: list[dict]) -> dict:
    """Best policy for fee_100_all by PF × win_rate (must have PF>1, win>40%, n>20)."""
    candidates = [
        r for r in results
        if r["universe"] == "fee_100_all"
        and (r.get("profit_factor") or 0) > 1.0
        and (r.get("win_rate") or 0) >= 0.40
        and r.get("n_trades", 0) >= 20
    ]
    if not candidates:
        # relax
        candidates = [r for r in results if r["universe"] == "fee_100_all"]
    if not candidates:
        return {}
    return max(candidates, key=lambda r: (r.get("profit_factor") or 0) * (r.get("win_rate") or 0))


def write_matrix(results: list[dict], equity_records: list[dict]) -> None:
    # CSV
    if results:
        keys = sorted({k for r in results for k in r})
        with (DIAG_OUT / "portfolio_replay_matrix.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in results:
                w.writerow(r)
    if equity_records:
        keys2 = sorted({k for r in equity_records for k in r})
        with (DIAG_OUT / "equity_curves.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys2)
            w.writeheader()
            for r in equity_records:
                w.writerow(r)


def write_replay_md(results: list[dict], universes: dict[str, int], best: dict) -> None:
    out = []
    out.append("# Sprint055-A — Portfolio Replay\n")
    out.append(f"Generated: {_now()} · No RPC · offline simulation on Sprint054 labeled candidates\n")

    out.append("## Universe sizes\n")
    for k, v in universes.items():
        out.append(f"- `{k}`: {v} rows")

    out.append("\n## Best simple policy (fee_100_all)\n")
    if best:
        out.append("| Param | Value |")
        out.append("| --- | --- |")
        for k in ["notional_usd", "max_concurrent", "cooldown_min", "n_trades", "win_rate",
                  "profit_factor", "roi_pct", "mean_ret", "median_ret", "max_drawdown_usd",
                  "max_drawdown_pct_of_notional", "loss_tail_p10", "pool_top1_share",
                  "trades_per_day_active", "days_active"]:
            out.append(f"| {k} | {best.get(k, '—')} |")

    out.append("\n## fee_100_all: PF × notional sweep (max_concurrent=1, cooldown=5m)\n")
    out.append("| notional | max_concurrent | cooldown_m | n_trades | win_rate | PF | roi_pct | max_dd_usd | top1 | sclass |")
    out.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in [x for x in results if x["universe"] == "fee_100_all"]:
        out.append(
            f"| {r['notional_usd']} | {r['max_concurrent']} | {r['cooldown_min']} | "
            f"{r['n_trades']} | {r.get('win_rate', '—')} | {r.get('profit_factor', '—')} | "
            f"{r.get('roi_pct', '—')} | {r.get('max_drawdown_usd', '—')} | "
            f"{r.get('pool_top1_share', '—')} | {_sclass(r.get('n_trades', 0))} |"
        )

    out.append("\n## fee_100_h2050 (heat_20_50 only): key results\n")
    out.append("| notional | max_concurrent | cooldown_m | n_trades | win_rate | PF | roi_pct | max_dd_usd |")
    out.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in [x for x in results if x["universe"] == "fee_100_h2050"]:
        out.append(
            f"| {r['notional_usd']} | {r['max_concurrent']} | {r['cooldown_min']} | "
            f"{r['n_trades']} | {r.get('win_rate', '—')} | {r.get('profit_factor', '—')} | "
            f"{r.get('roi_pct', '—')} | {r.get('max_drawdown_usd', '—')} |"
        )

    out.append("\n## Cross-fee comparison (notional=$50, max_concurrent=3, cooldown=5m)\n")
    out.append("| universe | n_trades | win_rate | PF | roi_pct | max_dd | sclass |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in [x for x in results if x["notional_usd"] == 50 and x["max_concurrent"] == 3 and x["cooldown_min"] == 5]:
        out.append(
            f"| {r['universe']} | {r['n_trades']} | {r.get('win_rate', '—')} | "
            f"{r.get('profit_factor', '—')} | {r.get('roi_pct', '—')} | "
            f"{r.get('max_drawdown_usd', '—')} | {_sclass(r.get('n_trades', 0))} |"
        )

    out.append("\n## Hard non-claims\n")
    out.append("- Simulation uses Sprint054 OOS labels only (113 fee_100 rows); sample is smoke-level")
    out.append("- ROI figures DO NOT account for market impact, real slippage, failed txs in live")
    out.append("- `NO_ALPHA_CLAIM = true` — forward paper shadow (Sprint055-B) required before any further claim")

    (AUDIT_OUT / "portfolio_replay.md").write_text("\n".join(out))
    print("\n".join([l for l in out if l.startswith("| ") or l.startswith("##") or "**" in l][:30]))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"[portfolio_replay] start {_now()}", flush=True)
    data = run_replay()
    results = data["results"]
    equity_records = data["equity_records"]
    universes = data["universes"]

    write_matrix(results, equity_records)

    best = find_best_policy(results)
    write_replay_md(results, universes, best)

    json.dump({"best_policy": best, "universe_sizes": universes, "generated_at": _now()},
              open(DIAG_OUT / "portfolio_replay_summary.json", "w"), indent=2, default=str)

    print(f"\n[portfolio_replay] results: {len(results)} policy rows, {len(equity_records)} equity points")
    print(f"\nbest policy: notional={best.get('notional_usd')} max_c={best.get('max_concurrent')} cooldown={best.get('cooldown_min')}m")
    print(f"  trades={best.get('n_trades')} win={best.get('win_rate')} PF={best.get('profit_factor')} roi={best.get('roi_pct')}% dd={best.get('max_drawdown_usd')}")


if __name__ == "__main__":
    main()
