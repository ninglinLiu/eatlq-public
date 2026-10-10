#!/usr/bin/env python3
"""Sprint086 — Batched TP / SL / Trailing exit backtest over real-cost CSVs.

Reads sprint086 overlay outputs (or raw paper shadow CSVs) and replays
each candidate trade through a 3-tier exit policy aligned with
whitepaper §止盈止损 "Trailing / Time / Hard":

  - TP1 at +X% : sell 30%
  - TP2 at +Y% : sell another 30%
  - SL at -Z%  : hard stop entire position
  - Time stop  : at horizon T sell remainder

Inputs (per row):
  - signal_time / pool / token / fee_bps
  - net_return_5m / 10m / 15m / 30m / 1h / 2h / 4h / 6h / 12h / 24h
    (already gas-adjusted via the existing *_ag columns OR the new *_real columns)

Notional cost applied per leg using the same real_cost_model as overlay
(but we keep per-leg gas + slippage scaled to the leg notional).

Outputs:
  - mvp/audits/sprint086_real_cost_overlay/batched_exit_summary.json
  - mvp/audits/sprint086_real_cost_overlay/batched_exit_readout.md
  - mvp/audits/sprint086_real_cost_overlay/batched_exit_per_trade.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

EXIT_LABELS = ["5m", "10m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "24h"]
EXIT_SECONDS = {
    "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "12h": 43200, "24h": 86400,
}


@dataclass
class ExitPolicy:
    name: str
    tp1_pct: float
    tp1_size: float
    tp2_pct: float
    tp2_size: float
    sl_pct: float          # negative number, e.g. -0.10 for -10%
    time_horizon: str      # one of EXIT_LABELS
    trailing_drawdown_pct: float | None = None  # e.g. 0.05 = 5% pullback from peak


# Three pre-canned policies covering whitepaper guidance
DEFAULT_POLICIES: list[ExitPolicy] = [
    ExitPolicy("aggressive_5_15_neg10_1h",  tp1_pct=0.05, tp1_size=0.30, tp2_pct=0.15, tp2_size=0.30, sl_pct=-0.10, time_horizon="1h"),
    ExitPolicy("conservative_3_8_neg6_30m", tp1_pct=0.03, tp1_size=0.40, tp2_pct=0.08, tp2_size=0.30, sl_pct=-0.06, time_horizon="30m"),
    ExitPolicy("trailing_5_pull3_2h",       tp1_pct=0.05, tp1_size=0.50, tp2_pct=0.20, tp2_size=0.30, sl_pct=-0.10, time_horizon="2h", trailing_drawdown_pct=0.03),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sprint086 batched exit backtest")
    p.add_argument(
        "--inputs",
        nargs="+",
        default=[
            "mvp/audits/sprint086_real_cost_overlay/sprint055_paper_shadow_realcost.csv",
            "mvp/audits/sprint086_real_cost_overlay/alpha_001_paper_shadow_realcost.csv",
            "mvp/audits/sprint086_real_cost_overlay/alpha_005_paper_shadow_realcost.csv",
        ],
    )
    p.add_argument(
        "--out-dir",
        default="mvp/audits/sprint086_real_cost_overlay",
    )
    p.add_argument("--use-real-cost", action="store_true", default=True,
                   help="Use net_return_{h}_real if present, else net_return_{h}_ag")
    p.add_argument("--per-leg-cost-pct", type=float, default=0.012,
                   help="Approx cost per exit leg (gas + slippage + fee) — applied per partial sell")
    return p.parse_args()


def _as_float(v) -> float | None:
    if v in (None, "", "None", "\\N", "nan"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trajectory(row: dict, use_real: bool) -> list[tuple[str, int, float]]:
    """Return list of (label, seconds, net_return_at_horizon)."""
    out: list[tuple[str, int, float]] = []
    for h in EXIT_LABELS:
        col_real = f"net_return_{h}_real"
        col_ag = f"net_return_{h}_ag"
        col_gross = f"net_return_{h}"
        if use_real:
            v = _as_float(row.get(col_real))
            if v is None:
                v = _as_float(row.get(col_ag))
            if v is None:
                v = _as_float(row.get(col_gross))
        else:
            v = _as_float(row.get(col_ag)) or _as_float(row.get(col_gross))
        if v is None:
            continue
        out.append((h, EXIT_SECONDS[h], v))
    return out


def apply_policy(traj: list[tuple[str, int, float]], policy: ExitPolicy, per_leg_cost: float, sell_path_ok: bool) -> dict:
    """Simulate batched exits along the recorded horizon trajectory.

    Position starts at 100% size. We walk the trajectory in time order.
    At each checkpoint we know the current MTM return.
    Trigger rules (checked in priority order):
      1. SL hit (current return <= sl_pct) -> sell ALL remaining at this return
      2. Trailing pullback (peak - current >= trailing_drawdown_pct) -> sell ALL remaining
      3. TP2 first time we cross tp2_pct -> sell tp2_size of REMAINING
      4. TP1 first time we cross tp1_pct -> sell tp1_size of REMAINING
      5. Time horizon reached -> sell ALL remaining at that horizon return

    Per-leg cost subtracted on every partial sell.
    If sell_path_ok is False, the policy returns -1.0 (entire position lost).
    """
    if not sell_path_ok:
        return {"policy": policy.name, "net": -1.0, "exit_reason": "sell_path_blocked", "legs": 0}

    if not traj:
        return {"policy": policy.name, "net": 0.0, "exit_reason": "no_data", "legs": 0}

    remaining = 1.0
    realised = 0.0
    peak = 0.0
    tp1_hit = False
    tp2_hit = False
    exit_reason = "time_horizon"
    legs = 0

    time_limit_sec = EXIT_SECONDS.get(policy.time_horizon, EXIT_SECONDS["30m"])

    for label, sec, ret in traj:
        if sec > time_limit_sec:
            break
        peak = max(peak, ret)

        # 1) hard SL
        if remaining > 0 and ret <= policy.sl_pct:
            realised += remaining * (ret - per_leg_cost)
            legs += 1
            remaining = 0
            exit_reason = "stop_loss"
            break

        # 2) trailing pullback
        if remaining > 0 and policy.trailing_drawdown_pct is not None:
            if peak >= policy.tp1_pct and (peak - ret) >= policy.trailing_drawdown_pct:
                realised += remaining * (ret - per_leg_cost)
                legs += 1
                remaining = 0
                exit_reason = "trailing_stop"
                break

        # 3) TP2 first crossing
        if remaining > 0 and not tp2_hit and ret >= policy.tp2_pct:
            size = min(remaining, policy.tp2_size)
            realised += size * (ret - per_leg_cost)
            remaining -= size
            tp2_hit = True
            legs += 1

        # 4) TP1 first crossing
        if remaining > 0 and not tp1_hit and ret >= policy.tp1_pct:
            size = min(remaining, policy.tp1_size)
            realised += size * (ret - per_leg_cost)
            remaining -= size
            tp1_hit = True
            legs += 1

    # 5) Time horizon close: sell remainder at the last in-window mark
    if remaining > 0 and traj:
        # find last (sec <= time_limit_sec)
        last_in_window = None
        for label, sec, ret in traj:
            if sec <= time_limit_sec:
                last_in_window = (label, sec, ret)
        if last_in_window is not None:
            realised += remaining * (last_in_window[2] - per_leg_cost)
            legs += 1
            remaining = 0

    return {
        "policy": policy.name,
        "net": round(realised, 6),
        "exit_reason": exit_reason,
        "legs": legs,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
    }


def pf(vals: list[float]) -> float | None:
    wins = sum(v for v in vals if v > 0)
    losses = abs(sum(v for v in vals if v < 0))
    if losses > 0:
        return round(wins / losses, 4)
    if wins > 0:
        return float("inf")
    return None


def main() -> int:
    args = parse_args()
    out_dir = REPO / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    per_trade_csv = out_dir / "batched_exit_per_trade.csv"

    summaries: list[dict] = []
    all_per_trade: list[dict] = []
    for inp in args.inputs:
        path = REPO / inp
        if not path.exists():
            summaries.append({"input": inp, "status": "missing"})
            continue
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        by_policy: dict[str, list[float]] = {p.name: [] for p in DEFAULT_POLICIES}
        wr_by_policy: dict[str, list[int]] = {p.name: [] for p in DEFAULT_POLICIES}
        for r in rows:
            sell_ok = str(r.get("sell_path_ok", "True")).strip().lower() in ("true", "1", "yes")
            traj = trajectory(r, args.use_real_cost)
            row_perf: dict[str, float] = {}
            for pol in DEFAULT_POLICIES:
                res = apply_policy(traj, pol, args.per_leg_cost_pct, sell_ok)
                by_policy[pol.name].append(res["net"])
                wr_by_policy[pol.name].append(1 if res["net"] > 0 else 0)
                row_perf[pol.name] = res["net"]
            all_per_trade.append({
                "source": Path(inp).name,
                "pool": r.get("pool"),
                "token": r.get("token_other") or r.get("token"),
                "fee_bps": r.get("fee_bps"),
                "signal_time": r.get("signal_time"),
                "sell_path_ok": sell_ok,
                **{f"net_{k}": v for k, v in row_perf.items()},
            })
        s = {
            "input": inp,
            "status": "ok",
            "rows": len(rows),
            "per_leg_cost_pct": args.per_leg_cost_pct,
            "policies": {},
        }
        for pol in DEFAULT_POLICIES:
            vals = by_policy[pol.name]
            wr = wr_by_policy[pol.name]
            s["policies"][pol.name] = {
                "n": len(vals),
                "mean_return": round(sum(vals) / max(len(vals), 1), 6),
                "win_rate": round(sum(wr) / max(len(wr), 1), 4),
                "pf": pf(vals),
                "tp1": {"pct": pol.tp1_pct, "size": pol.tp1_size},
                "tp2": {"pct": pol.tp2_pct, "size": pol.tp2_size},
                "sl_pct": pol.sl_pct,
                "time_horizon": pol.time_horizon,
                "trailing": pol.trailing_drawdown_pct,
            }
        summaries.append(s)

    # Write per-trade CSV
    if all_per_trade:
        fieldnames = list(all_per_trade[0].keys())
        with per_trade_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(all_per_trade)

    # Summary JSON
    decision = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "alpha_id": "AR-SP086-BATCHED-EXIT",
        "family": "F4",
        "use_real_cost": args.use_real_cost,
        "per_leg_cost_pct": args.per_leg_cost_pct,
        "policies": [pol.__dict__ for pol in DEFAULT_POLICIES],
        "summaries": summaries,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
    }
    (out_dir / "batched_exit_summary.json").write_text(json.dumps(decision, indent=2) + "\n")

    md = [
        "# Sprint086 — Batched TP/SL Exit Backtest",
        "",
        f"Generated: {decision['generated_at_utc']}  ",
        f"Per-leg cost: **{args.per_leg_cost_pct*100:.2f}%**  ",
        f"Use real cost: **{args.use_real_cost}**  ",
        "",
        "## Policies",
        "",
        "| name | TP1 | TP2 | SL | time | trailing |",
        "|---|---|---|---|---|---|",
    ]
    for pol in DEFAULT_POLICIES:
        md.append(
            f"| `{pol.name}` | +{pol.tp1_pct*100:.0f}% / {pol.tp1_size*100:.0f}% | "
            f"+{pol.tp2_pct*100:.0f}% / {pol.tp2_size*100:.0f}% | "
            f"{pol.sl_pct*100:+.0f}% | {pol.time_horizon} | "
            f"{(pol.trailing_drawdown_pct*100):.0f}%" if pol.trailing_drawdown_pct else "–" + " |"
        )
    md.append("")
    md.append("## Per-source × policy")
    md.append("")
    md.append("| source | policy | n | mean | WR | PF |")
    md.append("|---|---|--:|--:|--:|--:|")
    for s in summaries:
        if s.get("status") != "ok":
            md.append(f"| {Path(s['input']).name} | – | (missing) | – | – | – |")
            continue
        for pname, ps in s["policies"].items():
            md.append(
                f"| {Path(s['input']).name} | `{pname}` | {ps['n']} | "
                f"{ps['mean_return']} | {ps['win_rate']} | {ps['pf']} |"
            )
    md.append("")
    md.append("## Decision flags")
    md.append("")
    md.append("```json")
    md.append(json.dumps(decision, indent=2))
    md.append("```")
    (out_dir / "batched_exit_readout.md").write_text("\n".join(md) + "\n")

    print(json.dumps({"sources": len(summaries), "out_dir": args.out_dir}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
