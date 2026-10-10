"""
sprint068_dryrun_replay.py
Dry-run strategy proposal replay for paper shadow data.
Historical replay sanity only — not forward execution.
No live trading, no threshold changes, no model training.
Generated: 2026-05-14
"""

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

CSV_PATH = Path("/opt/EATLQ/logs/sprint055_paper_shadow.csv")
OUT_DIR = Path("/opt/EATLQ/mvp/audits/sprint068_dryrun_strategy_proposals")
OUT_DIR.mkdir(parents=True, exist_ok=True)

CAPITALS = [100.0, 300.0, 1000.0]
EXTRA_COSTS = [0.0, 0.002, 0.005, 0.010]
HOLD_SEC = 1800  # 30 minutes
COOLDOWN_SEC = 1800  # 30m cooldown

# ─────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────

def load_rows():
    rows = []
    with open(CSV_PATH) as f:
        for r in csv.DictReader(f):
            if r["exit_30m_ok"] != "True" or r["entry_quote_ok"] != "True":
                continue
            r["_time"] = datetime.fromisoformat(r["signal_time"])
            r["_date"] = r["signal_time"][:10]
            r["_pool"] = r["pool"]
            r["_heat"] = float(r["activity_heat"])
            r["_bpr"] = float(r["buy_pressure_ratio"])
            r["_pv"] = float(r["price_velocity_5m"])
            r["_onset"] = float(r["onset_lag_sec"])
            r["_age"] = float(r["pool_age_sec"])
            r["_ret_raw"] = float(r["net_return_30m"])
            r["_ret_ag"] = float(r["net_return_30m_ag"])  # already deducts gas_proxy=0.002
            rows.append(r)
    return sorted(rows, key=lambda x: x["_time"])


def compute_proxy_score(rows):
    """
    Construct pre-trade proxy score from observable-at-entry fields only.
    Signals used: activity_heat, buy_pressure_ratio, price_velocity_5m.
    onset_lag_sec: lower is better (fresher onset).
    All normalised to [0,1] using dataset min/max.
    POST_HOC_SELECTION_RISK=true: percentile thresholds computed on full dataset.
    Does NOT use any return / label field.
    """
    heats = [r["_heat"] for r in rows]
    bprs  = [r["_bpr"]  for r in rows]
    pvs   = [r["_pv"]   for r in rows]
    onsets = [r["_onset"] for r in rows]

    h_min, h_max = min(heats), max(heats)
    b_min, b_max = min(bprs),  max(bprs)
    p_min, p_max = min(pvs),   max(pvs)
    o_min, o_max = min(onsets), max(onsets)

    def norm(v, lo, hi):
        return (v - lo) / (hi - lo) if hi > lo else 0.5

    for r in rows:
        r["_proxy_score"] = (
            0.40 * norm(r["_heat"],  h_min,  h_max)
          + 0.30 * norm(r["_bpr"],   b_min,  b_max)
          + 0.20 * norm(r["_pv"],    p_min,  p_max)
          + 0.10 * (1.0 - norm(r["_onset"], o_min, o_max))  # lower onset_lag = fresher
        )
    return rows


# ─────────────────────────────────────────────
# Core simulation engine
# ─────────────────────────────────────────────

def simulate(rows, capital=100.0, stake_mode="equal", stake_value=None,
             max_concurrent=10, cooldown_sec=1800,
             per_pool_max_concurrent=1, extra_cost=0.0,
             score_percentile_cutoff=None, description=""):
    """
    General simulation engine.
    stake_mode:
      "equal"  — divide capital equally among all selected trades (post-hoc equal weight)
      "fixed"  — fixed stake_value per trade, capital recycled
      "score_weighted" — stake proportional to proxy_score, capped at max_fraction
      "kelly_025" / "kelly_050" / "kelly_aggressive" — fractional Kelly sizing

    score_percentile_cutoff: float [0,1] — only enter if proxy_score >= this percentile.
    Returns: dict with equity curve, stats, daily pnl, pool concentration.
    """
    # Pre-compute score threshold
    score_threshold = 0.0
    if score_percentile_cutoff is not None:
        scores = sorted(r["_proxy_score"] for r in rows)
        idx = int(len(scores) * (1.0 - score_percentile_cutoff))
        idx = max(0, min(idx, len(scores) - 1))
        score_threshold = scores[idx]

    # Empirical Kelly parameters (computed on full dataset — PROXY ONLY)
    all_rets = [r["_ret_ag"] for r in rows]
    emp_p = sum(1 for r in all_rets if r > 0) / len(all_rets)
    wins_  = [r for r in all_rets if r > 0]
    losses_= [abs(r) for r in all_rets if r <= 0]
    emp_b  = (sum(wins_) / len(wins_)) / (sum(losses_) / len(losses_)) if wins_ and losses_ else 1.0
    emp_kelly = (emp_p * emp_b - (1 - emp_p)) / emp_b if emp_b > 0 else 0.0

    kelly_fractions = {
        "kelly_025": 0.25 * emp_kelly,
        "kelly_050": 0.50 * emp_kelly,
        "kelly_aggressive": min(0.50, emp_kelly),  # cap at 50%
    }

    # Two-pass for equal-weight: first count selected trades, then size
    if stake_mode == "equal":
        # Simulate selection only (no capital) to count
        selected = _select_trades(rows, score_threshold, cooldown_sec, per_pool_max_concurrent)
        n_selected = len(selected)
        if n_selected == 0:
            return _empty_result(capital, description)
        stake_fn = lambda row, equity: capital / n_selected
    elif stake_mode == "fixed":
        selected = None  # dynamic selection
        if stake_value is None:
            raise ValueError("stake_value required for 'fixed' mode")
        stake_fn = lambda row, equity: stake_value
    elif stake_mode == "score_weighted":
        selected = _select_trades(rows, score_threshold, cooldown_sec, per_pool_max_concurrent)
        n_selected = len(selected)
        if n_selected == 0:
            return _empty_result(capital, description)
        base = capital * 0.10  # base 10% of capital per trade
        max_stake = capital * 0.20  # cap 20%
        # Normalise scores among selected
        sel_scores = [r["_proxy_score"] for r in selected]
        s_min, s_max = min(sel_scores), max(sel_scores)
        def stake_fn(row, equity):
            ns = (row["_proxy_score"] - s_min) / (s_max - s_min) if s_max > s_min else 0.5
            raw = base * (0.5 + ns)
            return min(raw, max_stake, equity * 0.20)
    elif stake_mode in kelly_fractions:
        selected = None
        kf = kelly_fractions[stake_mode]
        max_kelly_frac = 0.50 if stake_mode == "kelly_aggressive" else (
                         0.25 if stake_mode == "kelly_025" else 0.50)
        def stake_fn(row, equity):
            frac = kf * (row["_proxy_score"] + 0.5)  # score-adjust Kelly
            frac = min(max(frac, 0.01), max_kelly_frac)
            return equity * frac
    else:
        raise ValueError(f"Unknown stake_mode: {stake_mode}")

    # Run simulation
    active = {}   # pool -> (entry_time, stake, ret_ag, date)
    completed = []
    equity = capital
    peak = capital
    max_dd = 0.0
    skipped_capital = 0
    skipped_concurrent = 0
    skipped_cooldown = 0
    pool_last_exit = {}

    for row in rows:
        t    = row["_time"]
        pool = row["_pool"]
        ret  = row["_ret_ag"] - extra_cost

        # Close expired positions
        to_close = [p for p, (et, st, rt, dt) in active.items()
                    if (t - et).total_seconds() >= HOLD_SEC]
        for p in to_close:
            et, st, rt, dt = active.pop(p)
            equity += st + st * rt
            pool_last_exit[p] = et + timedelta(seconds=HOLD_SEC)
            completed.append({"pool": p, "date": dt, "stake": st, "ret_ag": rt})
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Score gate
        if score_percentile_cutoff is not None and row["_proxy_score"] < score_threshold:
            continue

        # Cooldown gate: check time since last EXIT for this pool
        if pool in pool_last_exit:
            since_exit = (t - pool_last_exit[pool]).total_seconds()
            if since_exit < cooldown_sec:
                skipped_cooldown += 1
                continue

        # Active position gate (per-pool limit)
        pool_active_count = sum(1 for p, _ in active.items() if p == pool)
        if pool_active_count >= per_pool_max_concurrent:
            skipped_cooldown += 1
            continue

        # Global concurrent gate
        if len(active) >= max_concurrent:
            skipped_concurrent += 1
            continue

        # Capital gate
        s = stake_fn(row, equity)
        if equity < s or s <= 0:
            skipped_capital += 1
            continue

        # Enter
        equity -= s
        active[pool] = (t, s, ret, row["_date"])

    # Close remaining
    for pool, (et, st, rt, dt) in active.items():
        equity += st + st * rt
        completed.append({"pool": pool, "date": dt, "stake": st, "ret_ag": rt})
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return _compute_stats(completed, equity, capital, max_dd, description,
                          skipped_capital, skipped_concurrent, skipped_cooldown,
                          emp_kelly, emp_p, emp_b)


def _select_trades(rows, score_threshold, cooldown_sec, per_pool_max_concurrent):
    """Dry-select trades without capital constraint, for equal-weight sizing."""
    active_pools = {}   # pool -> last_entry_time
    selected = []
    for row in rows:
        t    = row["_time"]
        pool = row["_pool"]
        if row["_proxy_score"] < score_threshold:
            continue
        if pool in active_pools:
            since = (t - active_pools[pool]).total_seconds()
            if since < cooldown_sec:
                continue
        active_pools[pool] = t
        selected.append(row)
    return selected


def _empty_result(capital, description):
    return {
        "description": description,
        "trade_count": 0,
        "final_equity": capital,
        "total_pnl": 0.0,
        "total_return_pct": 0.0,
        "win_rate": None,
        "PF": None,
        "max_drawdown_pct": 0.0,
        "max_single_loss_pct": None,
        "daily_pnl": {},
        "pool_concentration": {},
        "skipped": {},
    }


def _compute_stats(completed, final_equity, capital, max_dd, description,
                   sk_cap, sk_con, sk_cool, emp_kelly, emp_p, emp_b):
    if not completed:
        return _empty_result(capital, description)

    rets   = [c["ret_ag"] for c in completed]
    stakes = [c["stake"]  for c in completed]
    pnls   = [s * r for s, r in zip(stakes, rets)]

    wins  = [p for p in pnls if p > 0]
    losses= [abs(p) for p in pnls if p <= 0]
    win_rate = sum(1 for r in rets if r > 0) / len(rets)
    pf = sum(wins) / sum(losses) if losses else float("inf")
    total_pnl = final_equity - capital

    # Daily PnL
    daily = defaultdict(list)
    for c in completed:
        daily[c["date"]].append((c["stake"], c["ret_ag"]))
    daily_out = {}
    for d in sorted(daily):
        items = daily[d]
        dpnl = sum(s * r for s, r in items)
        daily_out[d] = {
            "n": len(items),
            "pnl": round(dpnl, 4),
            "mean_ret_pct": round(sum(r for _, r in items) / len(items) * 100, 4),
            "win_rate": round(sum(1 for _, r in items if r > 0) / len(items), 4),
        }

    # Pool concentration
    pool_pnl = defaultdict(float)
    pool_n   = defaultdict(int)
    for c in completed:
        pool_pnl[c["pool"]] += c["stake"] * c["ret_ag"]
        pool_n[c["pool"]]   += 1
    pool_conc = {}
    for p in sorted(pool_pnl, key=lambda x: -pool_n[x]):
        pool_conc[p[:12]] = {
            "n": pool_n[p],
            "pnl": round(pool_pnl[p], 4),
            "share_pct": round(pool_pnl[p] / total_pnl * 100, 1) if total_pnl != 0 else None,
        }

    worst_single = min(rets) * 100

    return {
        "description": description,
        "trade_count": len(completed),
        "final_equity": round(final_equity, 4),
        "total_pnl": round(total_pnl, 4),
        "total_return_pct": round((final_equity / capital - 1) * 100, 4),
        "win_rate": round(win_rate, 4),
        "PF": round(pf, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "max_single_loss_pct": round(worst_single, 4),
        "mean_return_pct": round(sum(rets) / len(rets) * 100, 4),
        "median_return_pct": round(statistics.median(rets) * 100, 4),
        "total_stake_deployed": round(sum(stakes), 4),
        "capital_turnover": round(sum(stakes) / capital, 2),
        "daily_pnl": daily_out,
        "pool_concentration": pool_conc,
        "skipped": {"capital": sk_cap, "concurrent": sk_con, "cooldown": sk_cool},
        "kelly_diagnostics": {
            "emp_p": round(emp_p, 4),
            "emp_b": round(emp_b, 4),
            "emp_kelly_full": round(emp_kelly, 4),
            "proxy_note": "Empirical on same dataset — PROXY only, not OOS-validated",
        },
    }


# ─────────────────────────────────────────────
# Strategy runners
# ─────────────────────────────────────────────

def run_all_strategies(rows, capital):
    results = {}

    # ── A. Fixed-size baselines ──────────────────────────────────────────
    results["A0_all_signals_dedup"] = simulate(
        rows, capital=capital, stake_mode="equal",
        cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10,
        description="30m cooldown, equal weight, all signals. Baseline.")

    results["A1_pool_hour_dedup"] = simulate(
        rows, capital=capital, stake_mode="equal",
        cooldown_sec=3600, per_pool_max_concurrent=1, max_concurrent=10,
        description="1h cooldown per pool, equal weight.")

    results["A2_first_signal_per_pool"] = simulate(
        rows, capital=capital, stake_mode="equal",
        cooldown_sec=999999, per_pool_max_concurrent=1, max_concurrent=10,
        description="One entry per pool ever. Equal weight across 3 entries.")

    # ── B. Confidence percentile (proxy score) ───────────────────────────
    for pct, label in [(0.05, "B05_top_5pct"), (0.10, "B10_top_10pct"),
                       (0.20, "B20_top_20pct"), (0.30, "B30_top_30pct")]:
        results[label] = simulate(
            rows, capital=capital, stake_mode="equal",
            score_percentile_cutoff=pct,
            cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10,
            description=f"Top {int(pct*100)}% proxy score, 30m cooldown, equal weight. "
                        f"POST_HOC_SELECTION_RISK=true (score computed on full dataset).")

    # ── C. Aggressive concentration ──────────────────────────────────────
    stake_20u = min(20.0, capital * 0.20)
    results["C1_top20_fixed_20U"] = simulate(
        rows, capital=capital, stake_mode="fixed", stake_value=stake_20u,
        score_percentile_cutoff=0.20, max_concurrent=5,
        cooldown_sec=1800, per_pool_max_concurrent=1,
        description=f"Top 20% proxy, {stake_20u:.0f}U/trade, max 5 concurrent. "
                    f"POST_HOC_SELECTION_RISK=true.")

    stake_33u = min(33.0, capital * 0.33)
    results["C2_top10_fixed_33U"] = simulate(
        rows, capital=capital, stake_mode="fixed", stake_value=stake_33u,
        score_percentile_cutoff=0.10, max_concurrent=3,
        cooldown_sec=1800, per_pool_max_concurrent=1,
        description=f"Top 10% proxy, {stake_33u:.0f}U/trade, max 3 concurrent. "
                    f"POST_HOC_SELECTION_RISK=true.")

    stake_50u = min(50.0, capital * 0.50)
    results["C3_top5_heavy_50U"] = simulate(
        rows, capital=capital, stake_mode="fixed", stake_value=stake_50u,
        score_percentile_cutoff=0.05, max_concurrent=2,
        cooldown_sec=1800, per_pool_max_concurrent=1,
        description=f"Top 5% proxy, {stake_50u:.0f}U/trade, max 2 concurrent. "
                    f"POST_HOC_SELECTION_RISK=true. HIGH CONCENTRATION RISK.")

    # ── D. Kelly-style sizing ────────────────────────────────────────────
    results["D0_score_weighted"] = simulate(
        rows, capital=capital, stake_mode="score_weighted",
        cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10,
        description="Score-weighted position size (0.5x–1.0x base, cap 20% equity). "
                    "Not mathematically valid Kelly. PROXY_KELLY=true.")

    results["D1_kelly_025"] = simulate(
        rows, capital=capital, stake_mode="kelly_025",
        cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10,
        description="0.25× fractional Kelly (empirical proxy). PROXY_KELLY=true. FULL_KELLY_VALID=false.")

    results["D2_kelly_050"] = simulate(
        rows, capital=capital, stake_mode="kelly_050",
        cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10,
        description="0.50× fractional Kelly (empirical proxy). PROXY_KELLY=true. FULL_KELLY_VALID=false.")

    results["D3_aggressive_kelly"] = simulate(
        rows, capital=capital, stake_mode="kelly_aggressive",
        cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10,
        description="Aggressive capped Kelly (cap 50% equity). PROXY_KELLY=true. HIGH RUIN RISK.")

    return results


# ─────────────────────────────────────────────
# Cost sensitivity runner
# ─────────────────────────────────────────────

def run_cost_sensitivity(rows, capital=100.0):
    """Run A0 and D1 across all cost levels for cost sensitivity table."""
    cost_results = {}
    for extra_cost in EXTRA_COSTS:
        key = f"extra_{int(extra_cost * 1000):03d}bps"
        cost_results[key] = {}
        for strat_id, kwargs in [
            ("A0_all_signals_dedup", dict(stake_mode="equal", cooldown_sec=1800,
                                          per_pool_max_concurrent=1, max_concurrent=10)),
            ("B10_top_10pct",        dict(stake_mode="equal", score_percentile_cutoff=0.10,
                                          cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10)),
            ("D1_kelly_025",         dict(stake_mode="kelly_025",
                                          cooldown_sec=1800, per_pool_max_concurrent=1, max_concurrent=10)),
        ]:
            r = simulate(rows, capital=capital, extra_cost=extra_cost,
                         description=strat_id, **kwargs)
            cost_results[key][strat_id] = {
                "final_equity": r["final_equity"],
                "total_return_pct": r["total_return_pct"],
            }
    return cost_results


# ─────────────────────────────────────────────
# Ruin risk for aggressive strategies
# ─────────────────────────────────────────────

def worst_case_pool_damage(rows, capital, strategy_result):
    """
    If the single worst pool had been the ONLY pool in the dataset,
    what would the equity be?
    """
    pool_pnl_raw = defaultdict(list)
    for c_info in strategy_result.get("pool_concentration", {}).items():
        pass  # we'll compute from raw trades
    # Recompute per-pool contribution using daily pnl
    damages = {}
    pool_conc = strategy_result.get("pool_concentration", {})
    for pool_key, v in pool_conc.items():
        damages[pool_key] = {
            "pnl": v["pnl"],
            "if_only_this_pool_equity": round(capital + v["pnl"], 4),
            "share_pct": v.get("share_pct"),
        }
    worst = min(pool_conc.items(), key=lambda x: x[1]["pnl"]) if pool_conc else None
    return {"per_pool": damages, "worst_pool": worst[0] if worst else None,
            "worst_pool_pnl": worst[1]["pnl"] if worst else None}


# ─────────────────────────────────────────────
# Output formatters
# ─────────────────────────────────────────────

def build_summary_row(strat_id, result, capital):
    return {
        "strategy_id": strat_id,
        "capital": capital,
        "trades": result["trade_count"],
        "final_equity": result["final_equity"],
        "total_return_pct": result["total_return_pct"],
        "win_rate": result.get("win_rate"),
        "PF": result.get("PF"),
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "max_single_loss_pct": result.get("max_single_loss_pct"),
        "top_pool_pnl_share_pct": list(result.get("pool_concentration", {}).values())[0].get("share_pct")
            if result.get("pool_concentration") else None,
        "capital_turnover": result.get("capital_turnover"),
        "post_hoc_selection_risk": "POST_HOC_SELECTION_RISK=true"
            if any(x in strat_id for x in ["B", "C"]) else "POST_HOC_SELECTION_RISK=false",
    }


def write_csv(path, rows_list):
    if not rows_list:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows_list[0].keys())
        w.writeheader()
        w.writerows(rows_list)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    rows = load_rows()
    rows = compute_proxy_score(rows)
    print(f"Loaded {len(rows)} labeled rows. Proxy scores computed.")

    scores = [r["_proxy_score"] for r in rows]
    print(f"Proxy score range: [{min(scores):.3f}, {max(scores):.3f}], mean={sum(scores)/len(scores):.3f}")

    # Score percentile thresholds
    scores_sorted = sorted(scores)
    for pct in [0.05, 0.10, 0.20, 0.30]:
        idx = int(len(scores_sorted) * (1.0 - pct))
        idx = max(0, min(idx, len(scores_sorted) - 1))
        print(f"  Top {int(pct*100)}% threshold: {scores_sorted[idx]:.4f} (n≥{int(len(scores)*pct)})")

    # ── Multi-capital replay ───────────────────────────────────────────────
    all_results = {}       # capital -> {strat_id -> result}
    all_summary_rows = []

    for capital in CAPITALS:
        print(f"\n=== Running strategies @ {capital}U ===")
        results = run_all_strategies(rows, capital)
        all_results[capital] = results
        for strat_id, r in results.items():
            row = build_summary_row(strat_id, r, capital)
            all_summary_rows.append(row)
            print(f"  {strat_id:<35} trades={r['trade_count']:3d}  "
                  f"equity={r['final_equity']:8.2f}U  "
                  f"ret={r['total_return_pct']:+6.2f}%  "
                  f"wr={r.get('win_rate', 0):.3f}  "
                  f"PF={r.get('PF', 0):.3f}  "
                  f"dd={r.get('max_drawdown_pct', 0):.2f}%")

    # ── Cost sensitivity ───────────────────────────────────────────────────
    print("\n=== Cost sensitivity @ 100U ===")
    cost_results = run_cost_sensitivity(rows, capital=100.0)
    cost_rows = []
    for cost_key, strats in cost_results.items():
        for strat_id, v in strats.items():
            cost_rows.append({"cost_level": cost_key, "strategy_id": strat_id, **v})
            print(f"  {cost_key}  {strat_id:<25} equity={v['final_equity']:8.2f}U")

    # ── Pool concentration per strategy (100U) ─────────────────────────────
    conc_rows = []
    for strat_id, r in all_results[100.0].items():
        for pool_key, v in r.get("pool_concentration", {}).items():
            conc_rows.append({
                "strategy_id": strat_id,
                "pool": pool_key,
                "n": v["n"],
                "pnl": v["pnl"],
                "share_pct": v.get("share_pct"),
            })

    # ── By-strategy summary (100U) ─────────────────────────────────────────
    by_strat_rows = [r for r in all_summary_rows if r["capital"] == 100.0]

    # ── By-capital summary for A0 and D1 ─────────────────────────────────
    by_capital_rows = [r for r in all_summary_rows
                       if r["strategy_id"] in ["A0_all_signals_dedup", "D1_kelly_025", "D2_kelly_050"]]

    # ── Kelly diagnostics ─────────────────────────────────────────────────
    sample_kd = all_results[100.0]["D1_kelly_025"].get("kelly_diagnostics", {})
    kelly_diag = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "CONFIDENCE_FIELD_FOUND": False,
        "CONFIDENCE_FIELD_MISSING": True,
        "PROXY_SCORE_FIELDS_USED": ["activity_heat", "buy_pressure_ratio",
                                    "price_velocity_5m", "onset_lag_sec"],
        "PROXY_SCORE_WEIGHTS": {"activity_heat": 0.40, "buy_pressure_ratio": 0.30,
                                "price_velocity_5m": 0.20, "onset_lag_sec_inv": 0.10},
        "POST_HOC_SELECTION_RISK": True,
        "POST_HOC_NOTE": "Percentile thresholds computed on full May11-May13 dataset. "
                         "Not valid for forward selection without out-of-sample validation.",
        "FULL_KELLY_VALID": False,
        "PROXY_KELLY_USED": True,
        "KELLY_INPUTS_AVAILABLE": False,
        "KELLY_INPUTS_NOTE": (
            "Kelly requires calibrated p (true win probability) and b (true payoff ratio). "
            "No model probability output exists in CSV. "
            "Using empirical win_rate/payoff_ratio from same dataset = PROXY only, not valid for live sizing."
        ),
        "empirical_p": sample_kd.get("emp_p"),
        "empirical_b": sample_kd.get("emp_b"),
        "empirical_full_kelly_fraction": sample_kd.get("emp_kelly_full"),
        "kelly_at_025": round(sample_kd.get("emp_kelly_full", 0) * 0.25, 4) if sample_kd else None,
        "kelly_at_050": round(sample_kd.get("emp_kelly_full", 0) * 0.50, 4) if sample_kd else None,
        "ruin_risk_note": (
            "Aggressive Kelly (D3) uses up to 50% equity per trade. "
            "With worst observed single-trade loss of -30%, one bad trade can draw down 15% of equity. "
            "Two concurrent bad positions could lose 30%+ instantly. NOT recommended for forward dry-run."
        ),
    }

    # ── Strategy candidates config ─────────────────────────────────────────
    candidates_config = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_csv": str(CSV_PATH),
        "replay_period": "2026-05-11 to 2026-05-13",
        "total_rows": len(rows),
        "freeze_time_for_forward_dryrun": "MUST_BE_SET_BY_USER_AFTER_STRATEGY_SELECTION",
        "FORWARD_DRYRUN_STARTED": False,
        "STRATEGY_SELECTION_REQUIRED": True,
        "shortlist": {
            "conservative": {
                "strategy_id": "A0_all_signals_dedup",
                "rationale": "30m cooldown, equal weight. Simple, predictable, no score filter.",
                "recommended_capital": 100,
                "trade_size_mode": "equal_weight_per_trade",
                "post_hoc_risk": False,
            },
            "balanced": {
                "strategy_id": "B10_top_10pct",
                "rationale": "Top 10% proxy score filter with 30m cooldown. Moderate selection. "
                             "WARNING: POST_HOC_SELECTION_RISK=true. Score threshold from full dataset.",
                "recommended_capital": 100,
                "trade_size_mode": "equal_weight_per_trade",
                "post_hoc_risk": True,
            },
            "aggressive": {
                "strategy_id": "C1_top20_fixed_20U",
                "rationale": "Top 20% proxy, fixed 20U/trade, max 5 concurrent. Higher turnover, "
                             "higher drawdown risk. POST_HOC_SELECTION_RISK=true.",
                "recommended_capital": 100,
                "trade_size_mode": "fixed_20U",
                "post_hoc_risk": True,
            },
            "kelly_style": {
                "strategy_id": "D1_kelly_025",
                "rationale": "0.25 fractional Kelly proxy. Modest sizing, empirically calibrated. "
                             "PROXY_KELLY=true, not mathematically valid Kelly.",
                "recommended_capital": 100,
                "trade_size_mode": "fractional_kelly_025",
                "post_hoc_risk": False,
            },
        },
        "user_placeholder_E": {
            "description": "User-configurable strategy slot. Fill in before activating forward dry-run.",
            "parameters": {
                "score_percentile_cutoff": "USER_CHOICE [0.05, 0.10, 0.20, 0.30, None]",
                "stake_mode": "USER_CHOICE [equal, fixed, score_weighted, kelly_025, kelly_050]",
                "stake_value_if_fixed": "USER_CHOICE (e.g. 10, 15, 20)",
                "max_concurrent_positions": "USER_CHOICE [1..10]",
                "cooldown_sec": "USER_CHOICE [1800, 3600, 7200]",
                "hold_time_sec": 1800,
                "stop_loss_pct": "NOT_SUPPORTED (no intra-period quotes in current schema)",
                "take_profit_pct": "NOT_SUPPORTED",
            },
            "activation": "NOT_STARTED — requires explicit user confirmation",
        },
    }

    # ── Write all artifacts ───────────────────────────────────────────────
    write_csv(OUT_DIR / "dryrun_replay_summary.csv", all_summary_rows)
    write_csv(OUT_DIR / "dryrun_replay_by_strategy.csv", by_strat_rows)
    write_csv(OUT_DIR / "dryrun_replay_by_capital.csv", by_capital_rows)
    write_csv(OUT_DIR / "dryrun_replay_by_cost.csv", cost_rows)
    write_csv(OUT_DIR / "dryrun_pool_concentration.csv", conc_rows)

    with open(OUT_DIR / "dryrun_kelly_sizing_diagnostics.json", "w") as f:
        json.dump(kelly_diag, f, indent=2)
    with open(OUT_DIR / "dryrun_strategy_candidates.json", "w") as f:
        json.dump(candidates_config, f, indent=2)

    # Save full per-strategy results for 100U
    with open(OUT_DIR / "dryrun_full_results_100U.json", "w") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "pool_concentration"}
                   for k, v in all_results[100.0].items()},
                  f, indent=2, default=str)

    # ── Print final summary table ─────────────────────────────────────────
    print("\n" + "=" * 110)
    print(f"{'STRATEGY (100U)':<35} {'Trades':>7} {'FinalEq':>10} {'Ret%':>8} {'WR':>6} {'PF':>6} {'MaxDD%':>8} {'TopPoolShare':>14} {'PostHoc':>10}")
    print("-" * 110)
    for strat_id, r in all_results[100.0].items():
        top_share = list(r.get("pool_concentration", {}).values())
        top_share_str = f"{top_share[0]['share_pct']:+.1f}%" if top_share and top_share[0].get("share_pct") else "N/A"
        ph = "YES" if any(x in strat_id for x in ["B", "C"]) else "no"
        print(f"  {strat_id:<33} {r['trade_count']:>7} {r['final_equity']:>10.2f} "
              f"{r['total_return_pct']:>8.2f} {r.get('win_rate', 0):>6.3f} "
              f"{r.get('PF', 0):>6.3f} {r.get('max_drawdown_pct', 0):>8.2f}% "
              f"{top_share_str:>14} {ph:>10}")

    print(f"\nArtifacts written to: {OUT_DIR}")
    print("FORWARD_DRYRUN_STARTED=false — awaiting user strategy selection.")
    return all_results, kelly_diag, candidates_config


if __name__ == "__main__":
    main()
