"""
sprint072_exit_horizon_sweep.py  — A1 + A5 combined
Paper-simulate 2h / 4h / 6h exit strategies on sprint055_paper_shadow.csv (530 rows).
Also simulates 6h trailing stop (A5).

RESEARCH ONLY. No live trading. No state changes to paper shadow.

Output: sprint072_alpha_waterfall/
  exit_horizon_sweep_results.json
  exit_horizon_sweep_trades.csv
"""

import csv, json, math, pathlib, statistics
from collections import defaultdict

REPO    = pathlib.Path(__file__).resolve().parent.parent
SRC_CSV = REPO / "logs" / "sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp" / "audits" / "sprint072_alpha_waterfall"
OUT_DIR.mkdir(parents=True, exist_ok=True)

GAS_PCT    = 0.002    # same as paper shadow
TRADE_SIZE = 10.0     # 10U per trade
INITIAL_EQ = 100.0

# Trailing stop: close early if price drops N% from entry (approximated from horizon returns)
TRAILING_STOP_PCT = -0.20   # -20% from entry

HORIZONS = ["30m", "1h", "2h", "4h", "6h"]


def load_rows() -> list[dict]:
    rows = []
    with open(SRC_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            row = {
                "signal_time": r["signal_time"],
                "pool":         r["pool"],
                "pool_age_sec": float(r["pool_age_sec"]) if r["pool_age_sec"] else None,
                "activity_heat": float(r["activity_heat"]) if r["activity_heat"] else 0,
                "buy_pressure_ratio": float(r["buy_pressure_ratio"]) if r["buy_pressure_ratio"] else 0,
            }
            # Collect all horizon returns
            for h in HORIZONS:
                ok_key  = f"exit_{h}_ok"
                ret_key = f"net_return_{h}"
                retag_key = f"net_return_{h}_ag"
                row[f"ok_{h}"]  = r.get(ok_key) == "True"
                row[f"ret_{h}"] = float(r[ret_key])  if r.get(ret_key)  else None
                row[f"retag_{h}"] = float(r[retag_key]) if r.get(retag_key) else None
            rows.append(row)
    return sorted(rows, key=lambda x: x["signal_time"])


def simulate_strategy(rows: list[dict], exit_horizon: str,
                      cooldown_anchor: str = "entry",
                      cooldown_sec: int = 1800,
                      max_concurrent: int = 3,
                      age_gate: tuple | None = None,
                      trailing_stop: bool = False,
                      trailing_stop_pct: float = TRAILING_STOP_PCT,
                      label: str = "") -> dict:
    """
    Simulate a single strategy variant.
    age_gate: None or (min_sec, max_sec) for pool_age_sec filter
    trailing_stop: approximate trailing stop using intermediate horizon returns
    """
    cash = INITIAL_EQ
    open_pos = {}       # pool -> entry_time_str
    pool_last_action = {}  # pool -> signal_time_str (entry or exit depending on anchor)
    trades = []
    skipped = {"cooldown": 0, "concurrent": 0, "capital": 0, "no_exit": 0, "age_gate": 0}

    for row in rows:
        pool = row["pool"]
        st   = row["signal_time"]

        # Age gate filter (A3 variant)
        if age_gate is not None:
            age = row.get("pool_age_sec")
            if age is None or not (age_gate[0] <= age <= age_gate[1]):
                skipped["age_gate"] += 1
                continue

        # Exit available?
        if not row.get(f"ok_{exit_horizon}") or row.get(f"retag_{exit_horizon}") is None:
            skipped["no_exit"] += 1
            continue

        # Capital
        if cash < TRADE_SIZE:
            skipped["capital"] += 1
            continue

        # Concurrent limit
        if pool in open_pos:
            skipped["concurrent"] += 1
            continue
        if len(open_pos) >= max_concurrent:
            skipped["concurrent"] += 1
            continue

        # Cooldown
        last_act = pool_last_action.get(pool)
        if last_act is not None:
            from datetime import datetime, timezone
            t_last = datetime.fromisoformat(last_act).astimezone(timezone.utc)
            t_now  = datetime.fromisoformat(st).astimezone(timezone.utc)
            elapsed = (t_now - t_last).total_seconds()
            if elapsed < cooldown_sec:
                skipped["cooldown"] += 1
                continue

        # Enter
        open_pos[pool] = st
        cash -= TRADE_SIZE
        pool_last_action[pool] = st

        # Determine effective return (trailing stop approximation)
        ret_ag = row[f"retag_{exit_horizon}"]
        effective_horizon = exit_horizon
        if trailing_stop and ret_ag is not None:
            # Approximate: scan intermediate horizons in order, stop early if cumulative return < threshold
            h_order = ["30m", "1h", "2h", "4h", "6h"]
            h_idx   = h_order.index(exit_horizon) if exit_horizon in h_order else len(h_order) - 1
            for h_i in range(h_idx + 1):
                h = h_order[h_i]
                r_h = row.get(f"ret_{h}")
                if r_h is not None and r_h < trailing_stop_pct:
                    # Stop hit — use this earlier return (minus gas)
                    ret_ag = r_h - GAS_PCT
                    effective_horizon = h
                    break

        pnl    = TRADE_SIZE * ret_ag
        cash  += TRADE_SIZE + pnl

        # Update last action
        if cooldown_anchor == "exit":
            pool_last_action[pool] = st  # simplified: same signal_time for exit anchor

        open_pos.pop(pool, None)
        trades.append({
            "signal_time":      st,
            "pool":             pool,
            "pool_age_sec":     row.get("pool_age_sec"),
            "activity_heat":    row.get("activity_heat"),
            "ret_ag":           round(ret_ag, 6),
            "pnl":              round(pnl, 4),
            "win":              pnl > 0,
            "effective_horizon": effective_horizon,
            "strategy":         label,
        })

    n = len(trades)
    if n == 0:
        return {"label": label, "n_trades": 0, "pf": None, "wr": None,
                "equity": INITIAL_EQ, "return_pct": 0, "skipped": skipped, "trades": []}

    wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] <= 0]
    wr     = sum(1 for t in trades if t["win"]) / n
    pf     = sum(wins) / sum(losses) if losses else float("inf")
    equity = INITIAL_EQ + sum(t["pnl"] for t in trades)

    return {
        "label":          label,
        "exit_horizon":   exit_horizon,
        "age_gate":       age_gate,
        "trailing_stop":  trailing_stop,
        "n_trades":       n,
        "win_rate":       round(wr, 4),
        "profit_factor":  round(pf, 4),
        "equity":         round(equity, 4),
        "return_pct":     round((equity - INITIAL_EQ) / INITIAL_EQ * 100, 2),
        "mean_ret":       round(statistics.mean(t["ret_ag"] for t in trades), 6),
        "median_ret":     round(statistics.median(t["ret_ag"] for t in trades), 6),
        "skipped":        skipped,
        "trades":         trades,
    }


def compute_per_pool_stats(trades: list[dict]) -> dict:
    by_pool = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        p = t["pool"][:12]
        by_pool[p]["n"] += 1
        by_pool[p]["wins"] += int(t["win"])
        by_pool[p]["pnl"] += t["pnl"]
    return {p: {"n": v["n"], "wr": round(v["wins"]/v["n"], 3),
                "pnl": round(v["pnl"], 4)} for p, v in sorted(by_pool.items(), key=lambda x: -x[1]["n"])}


def main():
    print("=" * 60)
    print("Sprint072 — A1/A5: Exit Horizon Sweep + Trailing Stop")
    print("=" * 60)

    rows = load_rows()
    print(f"Loaded {len(rows)} rows with valid entry quotes")

    # Baseline: current A1 at 30m
    strats = [
        # A1 variants — exit at different horizons
        dict(exit_horizon="30m", label="D_30m_baseline",   trailing_stop=False),
        dict(exit_horizon="1h",  label="A1_1h_control",    trailing_stop=False),
        dict(exit_horizon="2h",  label="A1_2h_extended",   trailing_stop=False),
        dict(exit_horizon="4h",  label="A1_4h_extended",   trailing_stop=False),
        dict(exit_horizon="6h",  label="A1_6h_extended",   trailing_stop=False),
        # A5 variants — 6h with trailing stop
        dict(exit_horizon="6h",  label="A5_6h_trail20",    trailing_stop=True, trailing_stop_pct=-0.20),
        dict(exit_horizon="4h",  label="A5_4h_trail20",    trailing_stop=True, trailing_stop_pct=-0.20),
        dict(exit_horizon="6h",  label="A5_6h_trail30",    trailing_stop=True, trailing_stop_pct=-0.30),
        # A3 variants — age gate on 30m and 4h
        dict(exit_horizon="30m", label="A3_age_30m_3600_100k", trailing_stop=False, age_gate=(3600, 100000)),
        dict(exit_horizon="4h",  label="A3_age_4h_3600_100k",  trailing_stop=False, age_gate=(3600, 100000)),
        dict(exit_horizon="30m", label="A3_age_30m_3600_60k",  trailing_stop=False, age_gate=(3600, 60000)),
    ]

    all_results = []
    all_trades  = []

    for cfg in strats:
        label   = cfg.pop("label")
        ts      = cfg.pop("trailing_stop", False)
        ts_pct  = cfg.pop("trailing_stop_pct", TRAILING_STOP_PCT)
        ag      = cfg.pop("age_gate", None)
        res = simulate_strategy(
            rows, label=label, trailing_stop=ts, trailing_stop_pct=ts_pct,
            age_gate=ag, **cfg
        )
        res["pool_stats"] = compute_per_pool_stats(res["trades"])
        all_trades.extend(res["trades"])
        result_summary = {k: v for k, v in res.items() if k != "trades"}
        all_results.append(result_summary)
        pf_str  = f"{res['profit_factor']:.3f}" if res["profit_factor"] else "N/A"
        wr_str  = f"{res['win_rate']:.3f}" if res["win_rate"] else "N/A"
        ag_str  = f" age={ag}" if ag else ""
        ts_str  = f" trail={ts_pct}" if ts else ""
        print(f"  {label:35s}: n={res['n_trades']:>4}  WR={wr_str}  PF={pf_str}"
              f"  equity={res['equity']:.2f}U  ret={res['return_pct']:.1f}%{ag_str}{ts_str}")

    # Save results
    results_path = OUT_DIR / "exit_horizon_sweep_results.json"
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults → {results_path}")

    # Save trades CSV
    trades_path = OUT_DIR / "exit_horizon_sweep_trades.csv"
    if all_trades:
        with open(trades_path, "w", newline="") as f:
            keys = list(all_trades[0].keys())
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(sorted(all_trades, key=lambda x: (x["strategy"], x["signal_time"])))
    print(f"Trades  → {trades_path}")

    # Per-pool breakdown for best strategies
    print("\n--- Per-pool breakdown: A1_4h_extended vs baseline ---")
    for s in all_results:
        if s["label"] in ("D_30m_baseline", "A1_4h_extended", "A5_6h_trail20", "A3_age_30m_3600_100k"):
            print(f"\n  [{s['label']}] (n={s['n_trades']}, PF={s['profit_factor']}, WR={s['win_rate']})")
            for pool, ps in (s.get("pool_stats") or {}).items():
                print(f"    {pool}: n={ps['n']} WR={ps['wr']:.3f} pnl={ps['pnl']:+.2f}U")

    print("\n=== FINAL RANKING (by PF) ===")
    ranked = sorted([r for r in all_results if r["profit_factor"]], key=lambda x: -x["profit_factor"])
    for r in ranked:
        print(f"  PF={r['profit_factor']:>7.3f}  WR={r['win_rate']:.3f}  n={r['n_trades']:>4}"
              f"  ret={r['return_pct']:+6.1f}%  {r['label']}")

    return all_results


if __name__ == "__main__":
    main()
