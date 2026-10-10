"""
Phase Gate Checker -- Reads daily evaluation data from Redis and determines
whether the system passes the gate criteria for advancing to the next phase.

Phase A Gate:
  - Run for >= 7 days
  - net_pnl_after_costs > 0 (sum of all daily net PnL)
  - trade_count >= 20 (total trades across all days)

Phase B Gate:
  - Run for >= 14 days
  - net_realized_pnl > 0 (actual on-chain PnL)
  - win_rate >= 45%
  - No SYSTEM_PANIC triggered

Usage:
    REDIS_URL=redis://redis:6379/0 python scripts/check_phase_gate.py --phase A
    REDIS_URL=redis://redis:6379/0 python scripts/check_phase_gate.py --phase B
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import redis


def main() -> None:
    parser = argparse.ArgumentParser(description="Check phase gate criteria")
    parser.add_argument("--phase", choices=["A", "B"], required=True)
    args = parser.parse_args()

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis.from_url(redis_url, decode_responses=True)

    keys = sorted(r.keys("exec:daily_eval:*"))
    if not keys:
        print("ERROR: No daily evaluation data found. Run the executor first.")
        sys.exit(1)

    days = []
    total_net_pnl = 0.0
    total_gross_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0

    for key in keys:
        raw = r.get(key)
        if not raw:
            continue
        data = json.loads(raw)
        days.append(data)
        total_net_pnl += data.get("net_pnl_usd", 0)
        total_gross_pnl += data.get("gross_pnl_usd", 0)
        total_trades += data.get("trade_count", 0)
        total_wins += data.get("win_count", 0)
        total_losses += data.get("loss_count", 0)

    num_days = len(days)
    total_win_rate = total_wins / total_trades * 100 if total_trades > 0 else 0

    print(f"\n{'='*50}")
    print(f"  Phase {args.phase} Gate Evaluation")
    print(f"{'='*50}")
    print(f"  Days of data:      {num_days}")
    print(f"  Total trades:      {total_trades}")
    print(f"  Win / Loss:        {total_wins} / {total_losses}")
    print(f"  Win rate:          {total_win_rate:.1f}%")
    print(f"  Gross PnL:         {total_gross_pnl:+.4f} USD")
    print(f"  Net PnL (costs):   {total_net_pnl:+.4f} USD")
    print(f"{'='*50}")

    if args.phase == "A":
        min_days = 7
        min_trades = 20
        checks = [
            ("days >= 7", num_days >= min_days, f"{num_days} vs {min_days}"),
            ("net_pnl > 0", total_net_pnl > 0, f"{total_net_pnl:+.4f}"),
            ("trades >= 20", total_trades >= min_trades, f"{total_trades} vs {min_trades}"),
        ]
    else:
        panic = r.get("SYSTEM_PANIC")
        min_days = 14
        checks = [
            ("days >= 14", num_days >= min_days, f"{num_days} vs {min_days}"),
            ("net_pnl > 0", total_net_pnl > 0, f"{total_net_pnl:+.4f}"),
            ("win_rate >= 45%", total_win_rate >= 45, f"{total_win_rate:.1f}%"),
            ("no SYSTEM_PANIC", not panic, f"{'CLEAR' if not panic else panic}"),
        ]

    all_pass = True
    for name, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}: {detail}")

    print(f"{'='*50}")
    if all_pass:
        next_phase = "B" if args.phase == "A" else "C"
        print(f"  GATE PASSED -- Proceed to Phase {next_phase}")
    else:
        print(f"  GATE FAILED -- Do NOT proceed. Fix issues and re-run.")
    print(f"{'='*50}\n")

    if num_days > 0:
        print("\nDaily breakdown:")
        print(f"  {'Day':<12} {'Trades':>7} {'Gross':>10} {'Net':>10} {'WinRate':>8}")
        for d in days:
            wr = d.get("win_rate", 0)
            print(f"  {d['day']:<12} {d.get('trade_count',0):>7} "
                  f"{d.get('gross_pnl_usd',0):>+10.4f} "
                  f"{d.get('net_pnl_usd',0):>+10.4f} "
                  f"{wr:>7.1f}%")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
