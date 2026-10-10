"""Summarize closed shadow signals grouped by signal_source.

Reads the Redis list `shadow:signals` (each element is a JSON blob produced
by the strategy service / shadow-closer), groups closed entries by source,
and prints headline stats: n, unique pools, win rate, avg/median net return,
and p25/p75.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from collections import defaultdict


def fetch_shadow_rows() -> list[str]:
    cmd = [
        "docker", "compose",
        "-f", "infra/compose/compose.yaml",
        "--env-file", "infra/compose/.env",
        "exec", "-T", "redis",
        "redis-cli", "lrange", "shadow:signals", "0", "-1",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


def summarize(rows: list[str]) -> None:
    by_src: dict[str, list[tuple[float, str, float]]] = defaultdict(list)
    total = 0
    closed = 0
    for ln in rows:
        total += 1
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("status") != "closed":
            continue
        try:
            net = float(r.get("net_return_pct", 0))
        except Exception:
            continue
        closed += 1
        src = str(r.get("signal_source", "?"))
        pool = str(r.get("pool_address", ""))
        try:
            conf = float(r.get("confidence", 0))
        except Exception:
            conf = 0.0
        by_src[src].append((net, pool, conf))

    print(f"total_entries={total} closed={closed}")
    header = (
        f"{'signal_source':<28} {'n':>6} {'uniq':>6} {'win%':>6} "
        f"{'avg%':>8} {'med%':>8} {'p25':>8} {'p75':>8}"
    )
    print(header)
    print("-" * len(header))
    for src in sorted(by_src.keys(), key=lambda s: -len(by_src[s])):
        arr = by_src[src]
        nets = sorted(a[0] for a in arr)
        pools = {a[1] for a in arr}
        n = len(nets)
        if n == 0:
            continue
        win = sum(1 for v in nets if v > 0) / n * 100
        avg = sum(nets) / n
        med = statistics.median(nets)
        p25 = nets[max(0, int(n * 0.25) - 1)] if n >= 4 else nets[0]
        p75 = nets[min(n - 1, int(n * 0.75))] if n >= 4 else nets[-1]
        print(
            f"{src:<28} {n:>6} {len(pools):>6} {win:>6.1f} "
            f"{avg:>+8.2f} {med:>+8.2f} {p25:>+8.2f} {p75:>+8.2f}"
        )


def main() -> int:
    rows = fetch_shadow_rows()
    if not rows:
        print("no shadow rows fetched", file=sys.stderr)
        return 1
    summarize(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
