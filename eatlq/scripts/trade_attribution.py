"""
Trade Attribution Report -- reads exec:trades from Redis and produces
PnL breakdown by signal source, exit reason, and other dimensions.

Usage:
    REDIS_URL=redis://redis:6379/0 python scripts/trade_attribution.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import defaultdict

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("trade_attribution")


def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis.from_url(redis_url, decode_responses=True)

    trades_raw = r.lrange("exec:trades", 0, -1)
    if not trades_raw:
        print("No trades found in exec:trades")
        return

    buys = []
    sells = []
    for raw in trades_raw:
        try:
            t = json.loads(raw)
        except Exception:
            continue
        if t.get("action") == "buy":
            buys.append(t)
        elif t.get("action") == "sell":
            sells.append(t)

    print(f"\n{'='*60}")
    print(f"  TRADE ATTRIBUTION REPORT")
    print(f"{'='*60}")
    print(f"  Total buys:  {len(buys)}")
    print(f"  Total sells: {len(sells)}")
    print()

    # --- PnL by Signal Source ---
    by_source: dict[str, list[float]] = defaultdict(list)
    for s in sells:
        source = s.get("signal_source", "unknown")
        pnl = float(s.get("pnl_usd", 0))
        by_source[source].append(pnl)

    print(f"  PnL by Signal Source:")
    print(f"  {'Source':<20} {'Count':>6} {'Total PnL':>12} {'Win Rate':>10} {'Avg Win':>10} {'Avg Loss':>10}")
    for source, pnls in sorted(by_source.items()):
        n = len(pnls)
        total = sum(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins) / n * 100 if n else 0
        aw = sum(wins) / len(wins) if wins else 0
        al = sum(losses) / len(losses) if losses else 0
        print(f"  {source:<20} {n:>6} {total:>+12.4f} {wr:>9.1f}% {aw:>+10.4f} {al:>+10.4f}")
    print()

    # --- PnL by Exit Reason ---
    by_reason: dict[str, list[float]] = defaultdict(list)
    for s in sells:
        reason = s.get("reason", "unknown")
        pnl = float(s.get("pnl_usd", 0))
        by_reason[reason].append(pnl)

    print(f"  PnL by Exit Reason:")
    print(f"  {'Reason':<20} {'Count':>6} {'Total PnL':>12} {'Avg PnL':>10}")
    for reason, pnls in sorted(by_reason.items()):
        n = len(pnls)
        total = sum(pnls)
        avg = total / n if n else 0
        print(f"  {reason:<20} {n:>6} {total:>+12.4f} {avg:>+10.4f}")
    print()

    # --- Cap Hit Rate ---
    cap_hits = 0
    for b in buys:
        raw_pos = float(b.get("raw_position_usd", 0))
        capped_pos = float(b.get("capped_position_usd", 0))
        if raw_pos > 0 and capped_pos > 0 and raw_pos > capped_pos * 1.01:
            cap_hits += 1
    if buys:
        print(f"  Cap Hit Rate: {cap_hits}/{len(buys)} ({cap_hits/len(buys)*100:.1f}%)")
        print(f"    (raw_position > capped_position by >1%)")
    print()

    # --- Cost Disclaimer ---
    print(f"  {'='*60}")
    print(f"  NOTE: PnL values in exec:trades use estimated costs, not")
    print(f"  actual on-chain gas/slippage/tax. Use shadow_report.py for")
    print(f"  forward-looking executable-quote analysis.")
    print(f"  {'='*60}\n")


if __name__ == "__main__":
    main()
