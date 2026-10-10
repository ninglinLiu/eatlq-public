"""Sprint056B — Execution Friction Replay.

Pressure-tests fee_100 strategy against:
  - Entry delay (5s / 15s / 30s / 60s) — via price_velocity_5m drift proxy
  - Exit delay (10m / 15m / 30m)       — via q4 bundle multi-horizon labels
  - Notional ($10 / $50 / $100)        — via entry_amount_out_micro/tiny/stress
  - Failed-quote / out-of-range filter — via out_of_range_suspected flag
  - Same-pool cooldown (15m / 30m / 60m)
  - Max concurrent (1 / 3 / 5)

Answers: does this strategy survive real-world execution friction?
Outputs:
  - mvp/diagnostics/sprint056/friction_matrix.csv
  - mvp/audits/sprint056/friction_readout.md
"""

from __future__ import annotations
import csv, json, glob, math
from pathlib import Path
from collections import defaultdict
import statistics

REPO    = Path(__file__).parent.parent
Q4_DIR  = REPO / "mvp/audits/sprint039a/q4_bundle_combined"
S054_JSON = REPO / "mvp/diagnostics/sprint054/labeled_candidates.json"
OUT_DIR = REPO / "mvp/diagnostics/sprint056"
AUD_DIR = REPO / "mvp/audits/sprint056"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUD_DIR.mkdir(parents=True, exist_ok=True)

GAS_PROXY  = 0.002
WETH_MICRO = 0.00002   # ~0.04 USD @ $2000/ETH → ~$0.04 (micro, tiny signal)
WETH_TINY  = 0.0004    # ~$0.80 (tiny)
WETH_STRESS= 0.02      # ~$40 (stress test)
# Notional levels approximated from the above WETH amts at $2500/ETH
NOTIONAL_MAP = {"$10": "micro", "$50": "tiny", "$100": "stress"}

# ── data load ─────────────────────────────────────────────────────────────────

def load_q4_fee100() -> list[dict]:
    rows = []
    for fn in sorted(glob.glob(str(Q4_DIR / "*.csv"))):
        for r in csv.DictReader(open(fn)):
            if r.get("fee_bps") != "100":
                continue
            if r.get("entry_quote_ok") != "True":
                continue
            # net returns at multiple horizons
            def nr(col):
                v = r.get(col)
                if not v or v in ("", "None"):
                    return None
                try:
                    return float(v) - GAS_PROXY
                except ValueError:
                    return None

            nr5  = nr("net_return_5m")
            nr10 = nr("net_return_10m")
            nr15 = nr("net_return_15m")
            nr30 = nr("net_return_30m")
            ok10 = r.get("exit_ok_10m") == "True"
            ok15 = r.get("exit_ok_15m") == "True"
            ok30 = r.get("exit_ok_30m") == "True"

            if nr5 is None:
                continue

            # entry_slippage_proxy: < 0 means you received fewer tokens than "fair"
            esp = float(r.get("entry_slippage_proxy") or 0)

            # notional slippage proxy: relative token output difference
            ao_micro  = float(r.get("entry_amount_out_micro")  or 0)
            ao_tiny   = float(r.get("entry_amount_out_tiny")   or 0)
            ao_stress = float(r.get("entry_amount_out_stress") or 0)

            # relative slippage at each notional (relative to micro=1.0)
            slippage_tiny   = (ao_tiny   / ao_micro - 1) if ao_micro > 0 else 0
            slippage_stress = (ao_stress / ao_micro - 1) if ao_micro > 0 else 0

            rows.append({
                "pool_address":      r["pool_address"],
                "regime_week":       r["regime_week"],
                "entry_time_sec":    float(r["entry_time_sec"]),
                "activity_heat":     float(r.get("activity_heat") or 0),
                "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                "avg_liquidity":     float(r.get("avg_liquidity") or 0),
                "out_of_range":      r.get("out_of_range_suspected") == "True",
                "nr5":               nr5,
                "nr10":              nr10 if ok10 else None,
                "nr15":              nr15 if ok15 else None,
                "nr30":              nr30 if ok30 else None,
                "esp":               esp,
                "slippage_tiny":     slippage_tiny,
                "slippage_stress":   slippage_stress,
                "source":            "in_bundle",
            })
    return rows


def load_oos_fee100() -> list[dict]:
    raw  = json.load(open(S054_JSON))
    rows = []
    for c in raw:
        if not (c.get("fee_bps") == 100 and c.get("exit_quote_ok")
                and c.get("net_return_5m") is not None):
            continue
        nr5 = c["net_return_5m"] - GAS_PROXY
        rows.append({
            "pool_address":      c["pool_address"],
            "regime_week":       c.get("week", "oos"),
            "entry_time_sec":    float(c.get("entry_time_sec", 0)),
            "activity_heat":     float(c.get("activity_heat") or 0),
            "price_velocity_5m": float(c.get("price_velocity_5m") or 0),
            "avg_liquidity":     float(c.get("avg_liquidity") or 0),
            "out_of_range":      False,
            "nr5":               nr5,
            "nr10":              None,
            "nr15":              None,
            "nr30":              None,
            "esp":               0.0,
            "slippage_tiny":     0.0,
            "slippage_stress":   0.0,
            "source":            "oos",
        })
    return rows

# ── entry delay simulation ────────────────────────────────────────────────────

def apply_entry_delay(rows: list[dict], delay_secs: float) -> list[dict]:
    """
    Approximate: entry N seconds after signal, assuming linear price drift.
    delayed_nr = nr5 - price_velocity_5m * (delay_secs / 300)

    This is a first-order approximation. Actual drift depends on volatility.
    Conservative: if price is moving fast (high |velocity|), delay costs more.
    """
    result = []
    for r in rows:
        vel   = r["price_velocity_5m"]
        drift = vel * (delay_secs / 300.0)
        # Entry-side drift: if price moved up (vel>0) by delay time, you buy
        # at a higher price → exit value is reduced by drift
        delayed_nr = r["nr5"] - drift
        result.append({**r, "nr5": delayed_nr, "delay_secs": delay_secs})
    return result


# ── notional slippage adjustment ───────────────────────────────────────────────
# NOTE: entry_amount_out_micro/tiny/stress are raw token amounts for DIFFERENT
# WETH input sizes. They CANNOT be compared directly for slippage without knowing
# the exact WETH input amounts. We use entry_slippage_proxy (already a % figure)
# and scale it linearly with notional multiple instead.

NOTIONAL_REFERENCE_USD = 10.0   # baseline notional in USD

def apply_notional(rows: list[dict], notional_usd: float) -> list[dict]:
    """Adjust net return for notional slippage impact.

    For Uniswap V3 concentrated liquidity, price impact scales approximately
    linearly with trade size near current tick. entry_slippage_proxy captures
    slippage at the reference (micro) notional. We scale by (notional / ref).
    """
    multiplier = (notional_usd / NOTIONAL_REFERENCE_USD) - 1.0  # incremental
    result = []
    for r in rows:
        esp = abs(r.get("esp", 0))  # entry_slippage_proxy (% cost, usually small)
        extra_slippage = esp * multiplier
        adj = r["nr5"] - extra_slippage
        result.append({**r, "nr_notional": adj})
    return result


# ── portfolio replay ──────────────────────────────────────────────────────────

def portfolio_replay(rows: list[dict], return_col: str = "nr5",
                     cooldown_s: int = 900, max_c: int = 3,
                     notional: float = 10.0,
                     exclude_oor: bool = False) -> dict:
    filtered = [r for r in rows if r.get(return_col) is not None]
    if exclude_oor:
        filtered = [r for r in filtered if not r.get("out_of_range", False)]

    sorted_rows = sorted(filtered, key=lambda r: r["entry_time_sec"])
    pool_last: dict[str, float] = {}
    active: list[float] = []
    pnl_list: list[float] = []

    for r in sorted_rows:
        t  = r["entry_time_sec"]
        nr = r[return_col]
        active = [x for x in active if x > t]
        if pool_last.get(r["pool_address"], 0) + cooldown_s > t:
            continue
        if len(active) >= max_c:
            continue
        pool_last[r["pool_address"]] = t
        active.append(t + 300)
        pnl_list.append(nr * notional)

    if not pnl_list:
        return {"n": 0, "win_rate": 0, "pf": None, "roi_pct": 0}

    wins   = [p for p in pnl_list if p > 0]
    losses = [-p for p in pnl_list if p <= 0]
    pf     = round(sum(wins) / sum(losses), 3) if losses else None
    total_invested = len(pnl_list) * notional
    return {
        "n":        len(pnl_list),
        "win_rate": round(sum(1 for p in pnl_list if p > 0) / len(pnl_list), 4),
        "pf":       pf,
        "roi_pct":  round(100 * sum(pnl_list) / total_invested, 3),
    }

# ── stats helper ──────────────────────────────────────────────────────────────

def _stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    pos = [r for r in rets if r > 0]
    neg = [-r for r in rets if r <= 0]
    pf  = round(sum(pos)/sum(neg), 3) if neg else None
    return {
        "n":    len(rets),
        "hit":  round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "pf":   pf,
        "mean": round(statistics.mean(rets), 6),
    }

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ib  = load_q4_fee100()
    oos = load_oos_fee100()
    all_rows = ib + oos
    print(f"Total fee_100 rows: {len(all_rows)} ({len(ib)} in-bundle + {len(oos)} OOS)")
    print()

    # ── 1. BASELINE ──────────────────────────────────────────────────────────
    baseline = portfolio_replay(all_rows, "nr5", 900, 3, 10.0)
    print(f"BASELINE: n={baseline['n']} win={baseline['win_rate']:.3f} "
          f"pf={baseline['pf']} roi={baseline['roi_pct']:.2f}%")
    print()

    # ── 2. ENTRY DELAY SENSITIVITY ───────────────────────────────────────────
    delay_results: list[dict] = []
    print("=== ENTRY DELAY SENSITIVITY ===")
    for delay in [0, 5, 15, 30, 60]:
        delayed = apply_entry_delay(all_rows, delay)
        rp = portfolio_replay(delayed, "nr5", 900, 3, 10.0)
        rets = [r["nr5"] for r in delayed]
        s   = _stats(rets)
        print(f"  delay={delay:3d}s: raw hit={s['hit']:.3f} pf={s['pf']} | "
              f"replay trades={rp['n']} win={rp['win_rate']:.3f} pf={rp['pf']} roi={rp['roi_pct']:.2f}%")
        delay_results.append({
            "scenario": f"entry_delay_{delay}s",
            "n_raw": s["n"], "hit_raw": s["hit"], "pf_raw": s["pf"],
            **rp,
        })

    print()

    # ── 3. EXIT DELAY SENSITIVITY (multi-horizon) ────────────────────────────
    horizon_results: list[dict] = []
    print("=== EXIT HORIZON SENSITIVITY ===")
    for col, label, ok_col in [
        ("nr5",  "5m_exit",  "exit_ok_5m"),
        ("nr10", "10m_exit", "exit_ok_10m"),
        ("nr15", "15m_exit", "exit_ok_15m"),
        ("nr30", "30m_exit", "exit_ok_30m"),
    ]:
        subset = [r for r in all_rows if r.get(col) is not None]
        rets   = [r[col] for r in subset]
        if not rets:
            continue
        s  = _stats(rets)
        rp = portfolio_replay(subset, col, 900, 3, 10.0)
        print(f"  {label:12s}: n={len(subset):4d} hit={s['hit']:.3f} pf={s['pf']} | "
              f"replay n={rp['n']} win={rp['win_rate']:.3f} pf={rp['pf']} roi={rp['roi_pct']:.2f}%")
        horizon_results.append({
            "scenario": label, "n_raw": s["n"], "hit_raw": s["hit"], "pf_raw": s["pf"], **rp,
        })

    print()

    # ── 4. NOTIONAL SENSITIVITY ──────────────────────────────────────────────
    notional_results: list[dict] = []
    print("=== NOTIONAL SENSITIVITY (entry_slippage_proxy scaled linearly) ===")
    for notional_usd, notional_label in [(10.0, "$10"), (50.0, "$50"), (100.0, "$100")]:
        adj_rows = apply_notional(all_rows, notional_usd)
        rets = [r["nr_notional"] for r in adj_rows]
        s    = _stats(rets)
        rp   = portfolio_replay(
            [{**r, "nr5": r["nr_notional"]} for r in adj_rows],
            "nr5", 900, 3, notional_usd
        )
        print(f"  {notional_label:6s}: hit={s['hit']:.3f} pf={s['pf']} | "
              f"replay n={rp['n']} win={rp['win_rate']:.3f} pf={rp['pf']} roi={rp['roi_pct']:.2f}%")
        notional_results.append({
            "scenario": notional_label, "n_raw": s["n"],
            "hit_raw": s["hit"], "pf_raw": s["pf"], **rp,
        })

    print()

    # ── 5. OUT-OF-RANGE FILTER ───────────────────────────────────────────────
    oor_count = sum(1 for r in all_rows if r.get("out_of_range"))
    print(f"=== OUT-OF-RANGE FILTER ===")
    print(f"  OOR rows: {oor_count}/{len(all_rows)} "
          f"({100*oor_count/len(all_rows):.1f}%)")
    filtered_oor = portfolio_replay(all_rows, "nr5", 900, 3, 10.0, exclude_oor=True)
    print(f"  After OOR filter: n={filtered_oor['n']} win={filtered_oor['win_rate']:.3f} "
          f"pf={filtered_oor['pf']} roi={filtered_oor['roi_pct']:.2f}%")
    print()

    # ── 6. COOLDOWN + CONCURRENCY GRID ───────────────────────────────────────
    cc_results: list[dict] = []
    print("=== COOLDOWN × CONCURRENCY GRID (entry_delay=0, $10) ===")
    for cooldown in [900, 1800, 3600]:
        for max_c in [1, 3, 5]:
            rp = portfolio_replay(all_rows, "nr5", cooldown, max_c, 10.0)
            cc_results.append({
                "cooldown_min": cooldown//60, "max_c": max_c,
                **rp,
            })
            print(f"  cd={cooldown//60}m max_c={max_c}: "
                  f"n={rp['n']:3d} win={rp['win_rate']:.3f} "
                  f"pf={rp['pf']} roi={rp['roi_pct']:.2f}%")
    print()

    # ── 7. COMBINED WORST-CASE ───────────────────────────────────────────────
    print("=== WORST-CASE SIMULATION (30s delay + $50 notional + OOR filtered) ===")
    delayed_30 = apply_entry_delay([r for r in all_rows if not r.get("out_of_range")], 30)
    adj_50     = apply_notional(delayed_30, 50.0)
    worst      = portfolio_replay(
        [{**r, "nr5": r["nr_notional"]} for r in adj_50],
        "nr5", 1800, 3, 50.0
    )
    print(f"  n={worst['n']} win={worst['win_rate']:.3f} "
          f"pf={worst['pf']} roi={worst['roi_pct']:.2f}%")
    print()

    # ── write outputs ─────────────────────────────────────────────────────────
    all_results = delay_results + horizon_results + notional_results
    cols = ["scenario","n_raw","hit_raw","pf_raw","n","win_rate","pf","roi_pct"]
    with open(OUT_DIR / "friction_matrix.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_results)
    print("Wrote friction_matrix.csv")

    write_readout(
        baseline=baseline, delay_results=delay_results,
        horizon_results=horizon_results, notional_results=notional_results,
        oor_count=oor_count, n_total=len(all_rows),
        filtered_oor=filtered_oor, cc_results=cc_results, worst=worst,
    )
    print("Wrote friction_readout.md")


def write_readout(**kw) -> None:
    bl      = kw["baseline"]
    delays  = kw["delay_results"]
    horizons= kw["horizon_results"]
    nots    = kw["notional_results"]
    oor_c   = kw["oor_count"]; n_tot = kw["n_total"]
    foor    = kw["filtered_oor"]
    cc      = kw["cc_results"]
    worst   = kw["worst"]

    def row(r):
        pf = r.get('pf','?')
        return (f"| {r.get('scenario','?')} | {r.get('n_raw',0)} "
                f"| {r.get('hit_raw',0):.3f} | {pf} "
                f"| {r.get('n',0)} | {r.get('win_rate',0):.3f} "
                f"| {r.get('pf','?')} | {r.get('roi_pct',0):.2f}% |")

    lines = [
        "# Sprint056B — Execution Friction Replay Readout",
        "",
        "> **Methodology:** Entry delay simulated via linear drift approximation "
        "(`price_velocity_5m × delay/300`). Exit delay via multi-horizon QuoterV2 labels. "
        "Notional via `entry_amount_out_micro/tiny/stress` slippage delta.",
        "",
        f"**Dataset:** {n_tot} fee_100bps labeled rows (in-bundle + OOS)",
        "",
        f"**Baseline** (0-delay, $10, 15m cd, max_c=3): n={bl['n']} "
        f"win={bl['win_rate']:.3f} PF={bl['pf']} ROI={bl['roi_pct']:.2f}%",
        "",
        "## 1. Entry Delay Sensitivity",
        "",
        "| Scenario | N Raw | Hit Raw | PF Raw | Replay N | Win | Replay PF | ROI% |",
        "|----------|-------|---------|--------|----------|-----|-----------|------|",
    ]
    for r in delays:
        lines.append(row(r))

    lines += [
        "",
        "## 2. Exit Horizon (Hold Duration) Sensitivity",
        "",
        "| Scenario | N Raw | Hit Raw | PF Raw | Replay N | Win | Replay PF | ROI% |",
        "|----------|-------|---------|--------|----------|-----|-----------|------|",
    ]
    for r in horizons:
        lines.append(row(r))

    lines += [
        "",
        "## 3. Notional Sensitivity ($10 / $50 / $100)",
        "",
        "| Scenario | N Raw | Hit Raw | PF Raw | Replay N | Win | Replay PF | ROI% |",
        "|----------|-------|---------|--------|----------|-----|-----------|------|",
    ]
    for r in nots:
        lines.append(row(r))

    lines += [
        "",
        f"## 4. Out-of-Range Filter",
        "",
        f"- OOR rows: {oor_c}/{n_tot} ({100*oor_c/n_tot:.1f}%)",
        f"- After filter: n={foor['n']} win={foor['win_rate']:.3f} "
        f"PF={foor['pf']} ROI={foor['roi_pct']:.2f}%",
        "",
        "## 5. Cooldown × Concurrency Grid",
        "",
        "| Cooldown | Max-C | Trades | Win | PF | ROI% |",
        "|----------|-------|--------|-----|----|------|",
    ]
    for r in cc:
        lines.append(f"| {r['cooldown_min']}m | {r['max_c']} | {r['n']} "
                     f"| {r['win_rate']:.3f} | {r.get('pf','?')} | {r['roi_pct']:.2f}% |")

    lines += [
        "",
        "## 6. Worst-Case Combined",
        "(30s entry delay + $50 notional + OOR filtered + 30m cooldown + max_c=3)",
        "",
        f"- Trades: {worst['n']}  Win: {worst['win_rate']:.3f}  "
        f"PF: {worst['pf']}  ROI: {worst['roi_pct']:.2f}%",
        "",
        "## Verdict",
        "",
    ]

    # Determine verdict
    delay_30 = next((r for r in delays if "30s" in r["scenario"]), None)
    if delay_30 and delay_30.get("pf") and delay_30["pf"] > 1.0:
        lines.append("**FRICTION_RESILIENT** — strategy maintains positive PF at 30s entry delay.")
    elif delay_30 and delay_30.get("pf") and delay_30["pf"] > 0.8:
        lines.append("**FRICTION_MARGINAL** — strategy degraded but survives at 30s entry delay.")
    else:
        lines.append("**FRICTION_SENSITIVE** — strategy PF collapses at 30s entry delay. "
                     "Requires sub-5s execution.")

    (AUD_DIR / "friction_readout.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
