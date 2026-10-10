"""
sprint067_counterfactual.py
100U live counterfactual for sprint055 paper shadow CSV.
Read-only analysis — no live trading, no threshold changes.
Generated: 2026-05-14
"""

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CSV_PATH = Path("/opt/EATLQ/logs/sprint055_paper_shadow.csv")
OUT_DIR = Path("/opt/EATLQ/mvp/audits/sprint067_paper_shadow_counterfactual")
OUT_DIR.mkdir(parents=True, exist_ok=True)

STAKE_TOTAL = 100.0
COST_LEVELS = [0.0, 0.002, 0.005, 0.010]  # 0U, 0.2%, 0.5%, 1.0%


# ---------------------------------------------------------------------------
# Load CSV
# ---------------------------------------------------------------------------
def load_csv():
    rows = []
    with open(CSV_PATH) as f:
        for r in csv.DictReader(f):
            if r["exit_30m_ok"] != "True":
                continue
            r["_pool"] = r["pool"]
            r["_time"] = datetime.fromisoformat(r["signal_time"])
            r["_ret_raw"] = float(r["net_return_30m"])
            # gas_proxy_pct already deducted in net_return_30m_ag
            # net_return_30m_ag = net_return_30m - gas_proxy_pct (always 0.002)
            r["_ret_ag"] = float(r["net_return_30m_ag"])
            r["_date"] = r["signal_time"][:10]
            r["_hour"] = r["signal_time"][:13]
            rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------
def equity_stats(trades, initial=100.0):
    """
    trades: list of (stake, net_return_ag) tuples
    Returns dict of summary stats.
    """
    if not trades:
        return {"trade_count": 0, "final_equity": initial, "total_return_pct": 0.0,
                "win_rate": None, "PF": None, "max_single_loss": None,
                "max_drawdown_pct": None, "daily_pnl": {}}

    stake_sum = sum(s for s, _ in trades)
    total_pnl = sum(s * r for s, r in trades)
    final = initial + total_pnl

    rets = [r for _, r in trades]
    wins = [s * r for s, r in trades if r > 0]
    losses = [abs(s * r) for s, r in trades if r <= 0]
    win_rate = sum(1 for r in rets if r > 0) / len(rets)
    pf = sum(wins) / sum(losses) if losses else float("inf")
    max_loss_pct = min(rets)

    # Max drawdown (over cumulative equity curve)
    equity = initial
    peak = initial
    max_dd = 0.0
    for s, r in trades:
        equity += s * r
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd

    return {
        "trade_count": len(trades),
        "final_equity": round(final, 4),
        "total_pnl": round(total_pnl, 4),
        "total_return_pct": round((final / initial - 1) * 100, 4),
        "win_rate": round(win_rate, 4),
        "PF": round(pf, 4),
        "max_single_trade_loss_pct": round(max_loss_pct * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "mean_return_pct": round(sum(rets) / len(rets) * 100, 4),
        "median_return_pct": round(statistics.median(rets) * 100, 4),
    }


def daily_pnl(trades_with_date, initial=100.0):
    by_day = defaultdict(list)
    for date, s, r in trades_with_date:
        by_day[date].append((s, r))
    result = {}
    for d in sorted(by_day):
        items = by_day[d]
        pnl = sum(s * r for s, r in items)
        result[d] = {
            "n": len(items),
            "pnl": round(pnl, 4),
            "mean_ret_pct": round(sum(r for _, r in items) / len(items) * 100, 4),
            "win_rate": round(sum(1 for _, r in items if r > 0) / len(items), 4),
        }
    return result


def pool_concentration(trades_with_pool):
    pool_pnl = defaultdict(float)
    pool_n = defaultdict(int)
    total_pnl = 0.0
    for pool, s, r in trades_with_pool:
        pnl = s * r
        pool_pnl[pool] += pnl
        pool_n[pool] += 1
        total_pnl += pnl
    result = {}
    for p in sorted(pool_pnl, key=lambda x: -pool_n[x]):
        result[p[:12]] = {
            "n": pool_n[p],
            "pnl": round(pool_pnl[p], 4),
            "share_of_total_pnl": round(pool_pnl[p] / total_pnl, 4) if total_pnl != 0 else None,
        }
    return result


# ---------------------------------------------------------------------------
# Strategy A: observation_equal_weight
# ---------------------------------------------------------------------------
def strategy_a_equal_weight(rows):
    """411 signals, equal stake. Math baseline only — NOT a real strategy."""
    n = len(rows)
    stake = STAKE_TOTAL / n
    trades = [(stake, r["_ret_ag"]) for r in rows]
    td = [(r["_date"], stake, r["_ret_ag"]) for r in rows]
    tp = [(r["_pool"], stake, r["_ret_ag"]) for r in rows]
    stats = equity_stats(trades)
    stats["daily_pnl"] = daily_pnl(td)
    stats["pool_concentration"] = pool_concentration(tp)
    stats["note"] = "MATH_BASELINE_ONLY: 411 signals NOT 411 independent trades"
    return stats


# ---------------------------------------------------------------------------
# Strategy B: pool_hour_dedup
# ---------------------------------------------------------------------------
def strategy_b_pool_hour_dedup(rows):
    """Same pool, same hour → only first signal."""
    seen = set()
    selected = []
    for r in rows:
        key = (r["_pool"], r["_hour"])
        if key not in seen:
            seen.add(key)
            selected.append(r)
    n = len(selected)
    stake = STAKE_TOTAL / n if n > 0 else 0
    trades = [(stake, r["_ret_ag"]) for r in selected]
    td = [(r["_date"], stake, r["_ret_ag"]) for r in selected]
    tp = [(r["_pool"], stake, r["_ret_ag"]) for r in selected]
    stats = equity_stats(trades)
    stats["daily_pnl"] = daily_pnl(td)
    stats["pool_concentration"] = pool_concentration(tp)
    stats["note"] = f"Deduped to {n} signals (one per pool/hour). Still equal-weight across entire period."
    return stats


# ---------------------------------------------------------------------------
# Strategy C: first_signal_per_pool
# ---------------------------------------------------------------------------
def strategy_c_first_per_pool(rows):
    """Each pool: only the very first qualifying signal. 3 trades total."""
    seen = set()
    selected = []
    for r in sorted(rows, key=lambda x: x["_time"]):
        if r["_pool"] not in seen:
            seen.add(r["_pool"])
            selected.append(r)
    n = len(selected)
    stake = STAKE_TOTAL / n if n > 0 else 0
    trades = [(stake, r["_ret_ag"]) for r in selected]
    td = [(r["_date"], stake, r["_ret_ag"]) for r in selected]
    tp = [(r["_pool"], stake, r["_ret_ag"]) for r in selected]
    stats = equity_stats(trades)
    stats["daily_pnl"] = daily_pnl(td)
    stats["pool_concentration"] = pool_concentration(tp)
    detail = []
    for r in selected:
        detail.append({
            "pool": r["_pool"],
            "entry_time": r["signal_time"],
            "pool_age_sec": r["pool_age_sec"],
            "onset_lag_sec": r["onset_lag_sec"],
            "activity_heat": r["activity_heat"],
            "buy_pressure_ratio": r["buy_pressure_ratio"],
            "entry_block": r["entry_block"],
            "net_return_30m_raw": r["_ret_raw"],
            "gas_proxy_pct": float(r["gas_proxy_pct"]),
            "net_return_30m_ag": r["_ret_ag"],
            "pnl_on_{:.0f}U".format(stake): round(stake * r["_ret_ag"], 4),
        })
    stats["first_entry_detail"] = detail
    stats["note"] = "3 trades only: one entry per pool at first qualifying signal"
    return stats


# ---------------------------------------------------------------------------
# Strategy D: one_position_per_pool_30m_cooldown
# ---------------------------------------------------------------------------
def strategy_d_cooldown(rows):
    """
    Enter on first qualifying signal per pool.
    30m cooldown: ignore new signals for same pool until 30m after last entry.
    Re-enter allowed after cooldown. Sequential time ordering.
    """
    # blocks ≈ 2s/block on Base; 30m = 900s = 450 blocks
    COOLDOWN_SEC = 30 * 60  # 1800s

    pool_last_entry_time = {}
    selected = []
    for r in sorted(rows, key=lambda x: x["_time"]):
        t = r["_time"]
        pool = r["_pool"]
        if pool in pool_last_entry_time:
            elapsed = (t - pool_last_entry_time[pool]).total_seconds()
            if elapsed < COOLDOWN_SEC:
                continue
        pool_last_entry_time[pool] = t
        selected.append(r)

    n = len(selected)
    stake = STAKE_TOTAL / n if n > 0 else 0
    trades = [(stake, r["_ret_ag"]) for r in selected]
    td = [(r["_date"], stake, r["_ret_ag"]) for r in selected]
    tp = [(r["_pool"], stake, r["_ret_ag"]) for r in selected]
    stats = equity_stats(trades)
    stats["daily_pnl"] = daily_pnl(td)
    stats["pool_concentration"] = pool_concentration(tp)
    stats["note"] = f"30m cooldown per pool → {n} trades. Equal-weight {STAKE_TOTAL/n:.2f}U each."
    return stats


# ---------------------------------------------------------------------------
# Strategy E: capital_capped_live_sim
# ---------------------------------------------------------------------------
def strategy_e_capital_capped(rows):
    """
    100U total capital. Fixed 10U per trade.
    Max 10 concurrent positions.
    Same pool: only 1 active position allowed.
    30m hold → exit at exit_30m block, then capital freed.
    Sequential by signal_time.
    """
    STAKE_PER_TRADE = 10.0
    MAX_CONCURRENT = 10
    HOLD_SEC = 30 * 60  # 1800s

    active = {}  # pool -> (entry_time, stake, ret_ag)
    completed = []
    daily_pnl_map = defaultdict(float)
    pool_pnl_map = defaultdict(float)
    equity_curve = []
    equity = STAKE_TOTAL
    peak = STAKE_TOTAL
    max_dd = 0.0

    for r in sorted(rows, key=lambda x: x["_time"]):
        t = r["_time"]
        pool = r["_pool"]

        # Close expired positions
        to_close = [p for p, (et, st, rt, dt) in active.items()
                    if (t - et).total_seconds() >= HOLD_SEC]
        for p in to_close:
            et, st, rt, dt = active.pop(p)
            pnl = st * rt
            equity += st + pnl
            completed.append((dt, st, rt))
            daily_pnl_map[dt] += pnl
            pool_pnl_map[p] += pnl
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Check if we can enter
        if pool in active:
            continue  # pool already has active position
        if len(active) >= MAX_CONCURRENT:
            continue
        if equity < STAKE_PER_TRADE:
            continue

        # Enter
        equity -= STAKE_PER_TRADE
        active[pool] = (t, STAKE_PER_TRADE, r["_ret_ag"], r["_date"])

    # Close remaining open positions at last observed time
    for pool, (et, st, rt, dt) in active.items():
        pnl = st * rt
        equity += st + pnl
        completed.append((dt, st, rt))
        daily_pnl_map[dt] += pnl
        pool_pnl_map[pool] += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    final = equity
    n = len(completed)
    if n == 0:
        return {"trade_count": 0, "final_equity": STAKE_TOTAL, "note": "no trades executed"}

    rets = [r for _, _, r in completed]
    wins = sum(1 for r in rets if r > 0)
    gross_wins = sum(abs(st * r) for _, st, r in completed if r > 0)
    gross_losses = sum(abs(st * r) for _, st, r in completed if r <= 0)
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    daily_out = {}
    for d in sorted(daily_pnl_map):
        day_trades = [(st, r) for dt, st, r in completed if dt == d]
        daily_out[d] = {
            "n": len(day_trades),
            "pnl": round(daily_pnl_map[d], 4),
            "mean_ret_pct": round(sum(r for _, r in day_trades) / len(day_trades) * 100, 4),
            "win_rate": round(sum(1 for _, r in day_trades if r > 0) / len(day_trades), 4),
        }

    pool_conc = {}
    total_pnl = final - STAKE_TOTAL
    for pool, pnl in sorted(pool_pnl_map.items(), key=lambda x: -abs(x[1])):
        pool_conc[pool[:12]] = {
            "pnl": round(pnl, 4),
            "share_of_total_pnl": round(pnl / total_pnl, 4) if total_pnl != 0 else None,
        }

    return {
        "trade_count": n,
        "final_equity": round(final, 4),
        "total_pnl": round(final - STAKE_TOTAL, 4),
        "total_return_pct": round((final / STAKE_TOTAL - 1) * 100, 4),
        "win_rate": round(wins / n, 4),
        "PF": round(pf, 4),
        "max_single_trade_loss_pct": round(min(rets) * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "mean_return_pct": round(sum(rets) / n * 100, 4),
        "median_return_pct": round(statistics.median(rets) * 100, 4),
        "daily_pnl": daily_out,
        "pool_concentration": pool_conc,
        "note": f"10U/trade, max 10 concurrent, 1 position/pool, 30m cooldown → {n} trades",
    }


# ---------------------------------------------------------------------------
# Cost sensitivity
# ---------------------------------------------------------------------------
def cost_sensitivity(rows, strategies_rets):
    """
    For each strategy, recompute final equity at each cost_per_trade level.
    cost_per_trade is additional round-trip cost fraction (e.g. 0.002 = 0.2%).
    Note: net_return_30m_ag already includes gas_proxy_pct=0.002 deduction.
    Additional costs here represent slippage / MEV / extra gas above proxy.
    """
    result = {}
    for strat_name, trade_list in strategies_rets.items():
        row_result = {}
        for extra_cost in COST_LEVELS:
            pnl = sum(s * (r - extra_cost) for s, r in trade_list)
            final = STAKE_TOTAL + pnl
            row_result[f"extra_{int(extra_cost*1000):03d}bps"] = round(final, 4)
        result[strat_name] = row_result
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    rows = load_csv()
    print(f"Loaded {len(rows)} labeled 30m rows from CSV")

    strat_a = strategy_a_equal_weight(rows)
    strat_b = strategy_b_pool_hour_dedup(rows)
    strat_c = strategy_c_first_per_pool(rows)
    strat_d = strategy_d_cooldown(rows)
    strat_e = strategy_e_capital_capped(rows)

    # Build cost sensitivity input: (stake, ret_ag) lists per strategy
    n_a = len(rows)
    rows_b = [r for r in rows if True]  # recompute for B
    seen_b = set()
    rows_b_sel = []
    for r in rows:
        key = (r["_pool"], r["_hour"])
        if key not in seen_b:
            seen_b.add(key)
            rows_b_sel.append(r)

    seen_c = set()
    rows_c_sel = []
    for r in sorted(rows, key=lambda x: x["_time"]):
        if r["_pool"] not in seen_c:
            seen_c.add(r["_pool"])
            rows_c_sel.append(r)

    # D: re-run to get trade list
    COOLDOWN_SEC = 1800
    pool_last = {}
    rows_d_sel = []
    for r in sorted(rows, key=lambda x: x["_time"]):
        t = r["_time"]
        pool = r["_pool"]
        if pool in pool_last and (t - pool_last[pool]).total_seconds() < COOLDOWN_SEC:
            continue
        pool_last[pool] = t
        rows_d_sel.append(r)

    # E: fixed 10U
    e_trades = []  # (stake, ret_ag)
    active = {}
    completed_e = []
    equity_e = STAKE_TOTAL
    HOLD_SEC = 1800
    for r in sorted(rows, key=lambda x: x["_time"]):
        t = r["_time"]
        pool = r["_pool"]
        to_close = [p for p, (et, st, rt, dt) in active.items()
                    if (t - et).total_seconds() >= HOLD_SEC]
        for p in to_close:
            et, st, rt, dt = active.pop(p)
            equity_e += st + st * rt
            completed_e.append((st, rt))
        if pool in active:
            continue
        if len(active) >= 10:
            continue
        if equity_e < 10.0:
            continue
        equity_e -= 10.0
        active[pool] = (t, 10.0, r["_ret_ag"], r["_date"])
    for pool, (et, st, rt, dt) in active.items():
        equity_e += st + st * rt
        completed_e.append((st, rt))

    cost_trades = {
        "A_equal_weight": [(STAKE_TOTAL / n_a, r["_ret_ag"]) for r in rows],
        "B_pool_hour_dedup": [(STAKE_TOTAL / len(rows_b_sel), r["_ret_ag"]) for r in rows_b_sel],
        "C_first_per_pool": [(STAKE_TOTAL / len(rows_c_sel), r["_ret_ag"]) for r in rows_c_sel],
        "D_30m_cooldown": [(STAKE_TOTAL / len(rows_d_sel), r["_ret_ag"]) for r in rows_d_sel],
        "E_capital_capped": completed_e,
    }
    cost_sens = cost_sensitivity(rows, cost_trades)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_csv": str(CSV_PATH),
        "total_rows_loaded": len(rows),
        "important_caveat": (
            "411 signals ≠ 411 independent trades. "
            "Signals are 5-min snapshots of 3 pools. "
            "net_return_30m_ag already deducts gas_proxy_pct=0.002 (0.2%). "
            "No additional AMM fee deducted (QuoterV2 returns include fee impact). "
            "Cost sensitivity adds extra_cost on top of existing gas proxy."
        ),
        "strategies": {
            "A_observation_equal_weight": strat_a,
            "B_pool_hour_dedup": strat_b,
            "C_first_signal_per_pool": strat_c,
            "D_one_position_per_pool_30m_cooldown": strat_d,
            "E_capital_capped_live_sim": strat_e,
        },
        "cost_sensitivity_final_equity": cost_sens,
    }

    out_path = OUT_DIR / "sprint067_counterfactual_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Wrote: {out_path}")

    # Print summary
    print("\n=== 100U COUNTERFACTUAL SUMMARY ===")
    print(f"{'Strategy':<35} {'Trades':>7} {'Final Equity':>13} {'Return%':>9} {'WR':>7} {'PF':>7} {'MaxDD%':>8}")
    print("-" * 90)
    for name, s in output["strategies"].items():
        short = name.split("_", 1)[1][:32]
        tc = s.get("trade_count", 0)
        fe = s.get("final_equity", 100)
        tr = s.get("total_return_pct", 0)
        wr = s.get("win_rate")
        pf = s.get("PF")
        dd = s.get("max_drawdown_pct")
        print(f"  {short:<33} {tc:>7} {fe:>13.4f}U {tr:>8.2f}% "
              f"{str(round(wr,3)) if wr else 'N/A':>7} "
              f"{str(round(pf,3)) if pf else 'N/A':>7} "
              f"{str(round(dd,2))+'%' if dd else 'N/A':>8}")

    print("\n=== FIRST ENTRY PER POOL ===")
    for d in strat_c.get("first_entry_detail", []):
        print(f"  Pool {d['pool'][:12]}  entry={d['entry_time']}  "
              f"heat={d['activity_heat']}  raw_ret={d['net_return_30m_raw']:+.4f}  "
              f"ag_ret={d['net_return_30m_ag']:+.4f}  "
              f"pnl={list(d.values())[-1]:+.4f}U")

    print("\n=== COST SENSITIVITY (final equity, 100U start) ===")
    print(f"{'Strategy':<30} {'0U extra':>11} {'0.2% extra':>11} {'0.5% extra':>11} {'1.0% extra':>11}")
    print("-" * 70)
    for strat, vals in cost_sens.items():
        vs = list(vals.values())
        print(f"  {strat:<28} {vs[0]:>11.4f} {vs[1]:>11.4f} {vs[2]:>11.4f} {vs[3]:>11.4f}")

    print(f"\nOutput: {out_path}")
    return output


if __name__ == "__main__":
    main()
