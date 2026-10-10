"""
Sprint 073 — Rolling Live-Readiness Gate Dashboard
===================================================
Evaluates L0→L3 gates in real time against the A6 dry-run state.
Prints a concise status and writes a machine-readable JSON.

Run manually or add to cron:
  */15 * * * * cd /opt/EATLQ && python3 scripts/sprint073_rolling_gate_dashboard.py \
               >> logs/sprint073_gate_dashboard.log 2>&1

Output:
  mvp/audits/sprint073_live_review/sprint073_gate_status.json
"""
from __future__ import annotations

import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# ─── Paths ──────────────────────────────────────────────────────────────────
REPO        = Path("/opt/EATLQ")
STATE_FILE  = REPO / "logs/sprint073_a6_dryrun_state.json"
FEE_STATE   = REPO / "logs/sprint073_fee_expanded_state.json"
OUT_JSON    = REPO / "mvp/audits/sprint073_live_review/sprint073_gate_status.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

# ─── L0 static facts (confirmed once, stored here) ─────────────────────────
L0_FACTS = {
    "EXECUTOR_DRY_RUN":           "1 (confirmed in container env)",
    "EXECUTOR_PRIVATE_KEY":       "set (env-only, .env gitignored)",
    "LIVE_TRADING_MODE":          "NOT_SET → strategy defaults to dry-run",
    "time_decay_min_240_behavior": (
        "CONDITIONAL exit only — triggers 'time_decay' reason ONLY when "
        "quote_out <= 0 OR quote_out <= entry (position at loss/zero). "
        "Profitable positions are NOT force-exited at 4h; they rely on "
        "take_profit=15% or trailing_stop=5%. "
        "ACTION NEEDED: add hard 4h exit for live A6 strategy."
    ),
    "honeypot_check":             "REJECT-on-fail (not fail-open) for both V2 and V3/CL pools",
    "daily_loss_circuit_breaker": "EXECUTOR_DAILY_LOSS_PCT=20% confirmed",
    "panic_key":                  "SYSTEM_PANIC redis key confirmed",
    "sell_path_fail_open":        "balanceOf fail → uses stored token_amount; ensures exit attempt",
    "git_secret_check":           ".env and .env.* in .gitignore confirmed",
}

# ─── Gate thresholds ────────────────────────────────────────────────────────
GATE_L1 = {"min_trades": 10}
GATE_L2 = {"min_trades": 20, "min_pools": 2}
GATE_L3 = {"label": "manual live review queue", "trade_size_usd": 10}


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def get_forward_trades(state: dict) -> list[dict]:
    """Trades recorded after the strategy's first cron run (forward, not historical backfill)."""
    # The historical backfill was on 2026-05-15. Forward trades have signal_time > that date.
    CUTOFF = "2026-05-15T14:00:00"  # after initial historical run
    return [t for t in state.get("trades", []) if t.get("signal_time", "") > CUTOFF]


def pool_stats(trades: list[dict]) -> dict:
    by_pool: dict[str, list] = defaultdict(list)
    for t in trades:
        by_pool[t["pool"]].append(t["ret_4h"])
    result = {}
    for pool, rets in by_pool.items():
        wins = [r for r in rets if r > 0]
        losses = [abs(r) for r in rets if r <= 0]
        result[pool] = {
            "n": len(rets),
            "wr": round(len(wins) / len(rets), 3) if rets else 0,
            "pf": round(sum(wins) / sum(losses), 3) if losses else float("inf"),
            "pct_of_total": 0,  # filled below
        }
    total = sum(v["n"] for v in result.values())
    for pool in result:
        result[pool]["pct_of_total"] = round(result[pool]["n"] / total, 3) if total else 0
    return result


def compute_pf(trades: list[dict]) -> float:
    wins   = [t["ret_4h"] for t in trades if t["ret_4h"] > 0]
    losses = [abs(t["ret_4h"]) for t in trades if t["ret_4h"] <= 0]
    return round(sum(wins) / sum(losses), 4) if losses else float("inf")


def gate_l0_status() -> dict:
    return {
        "passed": True,
        "items": L0_FACTS,
        "action_item": (
            "IMPLEMENT hard 4h exit for live A6: "
            "strategy service must emit close signal at entry_time+4h, "
            "OR add EXECUTOR_HARD_EXIT_MIN=240 env var to force sell at 4h regardless of P&L."
        ),
    }


def gate_l1_status(fwd: list[dict]) -> dict:
    n       = len(fwd)
    passed  = n >= GATE_L1["min_trades"]
    if not fwd:
        return {"passed": False, "forward_trades": 0, "needed": GATE_L1["min_trades"]}

    wins = [t for t in fwd if t["ret_4h"] > 0]
    pf   = compute_pf(fwd)
    wr   = round(len(wins) / n, 3)
    ps   = pool_stats(fwd)
    top_pool_pct = max((v["pct_of_total"] for v in ps.values()), default=0)

    healthy = pf >= 2.0 and wr >= 0.55
    return {
        "passed":           passed,
        "forward_trades":   n,
        "needed":           GATE_L1["min_trades"],
        "win_rate":         wr,
        "profit_factor":    pf,
        "top_pool_pct":     round(top_pool_pct, 3),
        "health":           "STRONG" if healthy else ("WEAK" if pf >= 1.0 else "STOP"),
        "recommendation":   (
            "Continue accumulating" if not passed else
            ("HEALTHY — proceed to L2 watch" if healthy else
             ("WEAK — continue monitoring" if pf >= 1.0 else
              "STOP — kill dry-run, strategy failed forward"))
        ),
        "pool_breakdown":   ps,
    }


def gate_l2_status(fwd: list[dict]) -> dict:
    n        = len(fwd)
    ps       = pool_stats(fwd)
    n_pools  = len(ps)
    top_pct  = max((v["pct_of_total"] for v in ps.values()), default=1.0)
    pf       = compute_pf(fwd) if fwd else 0
    wr       = round(sum(1 for t in fwd if t["ret_4h"] > 0) / n, 3) if n else 0

    passed = (n >= GATE_L2["min_trades"] and
              n_pools >= GATE_L2["min_pools"] and
              top_pct <= 0.75 and
              pf >= 2.0)
    return {
        "passed":           passed,
        "forward_trades":   n,
        "unique_pools":     n_pools,
        "top_pool_pct":     round(top_pct, 3),
        "profit_factor":    pf,
        "win_rate":         wr,
        "needs": {
            "trades":       max(0, GATE_L2["min_trades"] - n),
            "pools":        max(0, GATE_L2["min_pools"] - n_pools),
            "pf_gap":       max(0, round(2.0 - pf, 3)),
        },
        "next":             "Enter live review queue at 10U/trade" if passed else "Keep accumulating forward trades",
    }


def gate_l3_status(l2: dict) -> dict:
    return {
        "passed":           False,   # manual gate — never auto-passes
        "prerequisite_l2":  l2["passed"],
        "trade_size_usd":   GATE_L3["trade_size_usd"],
        "manual_checklist": [
            "R2: hard 4h exit implemented in executor (EXECUTOR_HARD_EXIT_MIN=240)",
            "R5-L1: time_decay_min=240 behavior documented and accepted",
            "R5-L3: EXECUTOR_PRIVATE_KEY confirmed env-only in prod",
            "Panic / daily-loss / sell-path all confirmed in staging smoke",
            "Announce start, set max_concurrent=1 and max_position_usd=15",
        ],
        "note": "L3 requires explicit human approval. Script never auto-promotes.",
    }


def print_dashboard(result: dict) -> None:
    now_str = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  A6 Rolling Gate Dashboard  —  {now_str}")
    print(f"{'='*60}")

    l0 = result["L0"]
    l1 = result["L1"]
    l2 = result["L2"]
    l3 = result["L3"]
    hist = result["historical"]

    mark = lambda p: "✅" if p else "⏳"

    print(f"\n{mark(l0['passed'])} L0  Static safety checks")
    print(f"   DRY_RUN=1  KEY=env-only  PANIC=ok  HONEYPOT=reject-on-fail")
    print(f"   ⚠️  time_decay_min: NOT a hard 4h exit for profitable positions")

    print(f"\n{mark(l1['passed'])} L1  Forward trades ≥ {GATE_L1['min_trades']}")
    fwd_n = l1['forward_trades']
    print(f"   {fwd_n}/{GATE_L1['min_trades']} forward trades  |  "
          f"WR={l1.get('win_rate',0):.0%}  PF={l1.get('profit_factor',0):.2f}  "
          f"top_pool={l1.get('top_pool_pct',0):.0%}")
    if l1.get('health'):
        print(f"   Health: {l1['health']} — {l1['recommendation']}")

    print(f"\n{mark(l2['passed'])} L2  Forward ≥ {GATE_L2['min_trades']} + ≥ {GATE_L2['min_pools']} pools + PF≥2")
    print(f"   {l2['forward_trades']} trades  {l2['unique_pools']} pools  "
          f"top={l2['top_pool_pct']:.0%}  PF={l2['profit_factor']:.2f}")
    if not l2["passed"]:
        needs = l2["needs"]
        gaps = []
        if needs["trades"]:   gaps.append(f"{needs['trades']} more trades")
        if needs["pools"]:    gaps.append(f"{needs['pools']} more pool(s)")
        if needs["pf_gap"]:   gaps.append(f"PF +{needs['pf_gap']}")
        if gaps: print(f"   Need: {', '.join(gaps)}")

    print(f"\n{'🔴' if not l3['passed'] else '🟢'} L3  Live review queue (10U) — MANUAL GATE")
    print(f"   prerequisite_l2={l3['prerequisite_l2']}  |  never auto-promotes")

    print(f"\n   Historical backfill: {hist['trades']} trades  "
          f"WR={hist['wr']:.0%}  PF={hist['pf']:.2f}  eq={hist['equity']:.1f}")
    print(f"{'='*60}\n")


def main() -> None:
    state = load_state()
    if state is None:
        print("ERROR: A6 dry-run state not found. Run sprint073_a6_diversity_4h_dryrun.py first.")
        sys.exit(1)

    fwd_trades   = get_forward_trades(state)
    all_trades   = state.get("trades", [])

    stats        = state.get("stats", {})
    n_all        = stats.get("total_trades", 0)
    wins_all     = stats.get("wins", 0)
    gains_all    = stats.get("total_gain", 0.0)
    losses_all   = stats.get("total_loss", 0.0)
    pf_all       = round(gains_all / losses_all, 4) if losses_all else float("inf")
    wr_all       = round(wins_all / n_all, 3) if n_all else 0.0

    l0 = gate_l0_status()
    l1 = gate_l1_status(fwd_trades)
    l2 = gate_l2_status(fwd_trades)
    l3 = gate_l3_status(l2)

    result = {
        "generated_at_utc": utc_now().isoformat(),
        "strategy":         state.get("strategy_id"),
        "L0":               l0,
        "L1":               l1,
        "L2":               l2,
        "L3":               l3,
        "historical": {
            "trades":   n_all,
            "wr":       wr_all,
            "pf":       pf_all,
            "equity":   round(state.get("equity", 100.0), 2),
        },
        "open_positions":   len(state.get("open_positions", {})),
        "cron":             "2-57/5 * * * * (running)",
    }

    OUT_JSON.write_text(json.dumps(result, indent=2, default=str))
    print_dashboard(result)


if __name__ == "__main__":
    main()
