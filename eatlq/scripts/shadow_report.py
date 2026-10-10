"""
Shadow Report -- reads closed shadow signals from Redis and computes
performance metrics grouped by system_version x signal_source.

Implements the quantified gate criteria:
  - N >= 200 closed signals
  - Mean net_return_pct > 0 after removing top 5% outliers
  - 95% bootstrap CI lower bound > 0
  - P90 loss < 30%
  - Max drawdown of cumulative PnL < 50%
  - At least one source independently net_return > 0 with N >= 50

Usage:
    REDIS_URL=redis://redis:6379/0 python scripts/shadow_report.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import defaultdict

import numpy as np
import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("shadow_report")

SHADOW_LIST_KEY = "shadow:signals"


def _bootstrap_ci(data: np.ndarray, n_boot: int = 10000, ci: float = 0.95) -> tuple:
    """Compute bootstrap confidence interval for the mean."""
    rng = np.random.default_rng(42)
    means = np.array([rng.choice(data, size=len(data), replace=True).mean()
                       for _ in range(n_boot)])
    alpha = (1 - ci) / 2
    lo = float(np.percentile(means, alpha * 100))
    hi = float(np.percentile(means, (1 - alpha) * 100))
    return lo, hi


def _max_drawdown(returns: np.ndarray) -> float:
    """Max drawdown of cumulative returns (percentage)."""
    cumulative = np.cumsum(returns)
    peak = np.maximum.accumulate(cumulative)
    dd = peak - cumulative
    return float(dd.max()) if len(dd) > 0 else 0.0


def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis.from_url(redis_url, decode_responses=True)

    total = r.llen(SHADOW_LIST_KEY)
    if total == 0:
        log.info("No shadow signals found.")
        return

    log.info("Reading %d shadow entries...", total)

    cohorts: dict[str, list[dict]] = defaultdict(list)
    open_count = 0

    for i in range(total):
        raw = r.lindex(SHADOW_LIST_KEY, i)
        if raw is None:
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue

        if rec.get("status") != "closed":
            open_count += 1
            continue

        key = f"{rec.get('system_version', '?')}|{rec.get('signal_source', '?')}"
        cohorts[key].append(rec)

    log.info("Open (pending close): %d", open_count)
    print(f"\n{'='*70}")
    print(f"  SHADOW PERFORMANCE REPORT")
    print(f"{'='*70}\n")

    all_returns = []
    source_stats = {}

    for cohort_key in sorted(cohorts.keys()):
        records = cohorts[cohort_key]
        returns = np.array([r.get("net_return_pct", 0.0) for r in records])
        n = len(returns)

        if n == 0:
            continue

        trimmed = np.sort(returns)[:int(n * 0.95)] if n >= 20 else returns
        mean_raw = float(returns.mean())
        mean_trimmed = float(trimmed.mean()) if len(trimmed) > 0 else 0.0
        win_rate = float((returns > 0).sum() / n * 100)
        p90_loss = float(np.percentile(returns, 10))
        max_dd = _max_drawdown(returns)

        ci_lo, ci_hi = _bootstrap_ci(returns) if n >= 20 else (0.0, 0.0)

        version, source = cohort_key.split("|", 1)
        all_returns.extend(returns.tolist())

        source_stats[cohort_key] = {
            "n": n, "mean_raw": mean_raw, "mean_trimmed": mean_trimmed,
            "ci_lo": ci_lo, "win_rate": win_rate,
        }

        print(f"  [{version}] {source}")
        print(f"  {'─'*50}")
        print(f"  Signals:          {n}")
        print(f"  Mean net return:  {mean_raw:+.2f}% (raw) / {mean_trimmed:+.2f}% (trimmed)")
        print(f"  Win rate:         {win_rate:.1f}%")
        print(f"  P90 loss:         {p90_loss:.2f}%")
        print(f"  Max drawdown:     {max_dd:.2f}%")
        if n >= 20:
            print(f"  95% CI:           [{ci_lo:+.2f}%, {ci_hi:+.2f}%]")
        print()

    if not all_returns:
        print("  No closed signals to analyze.\n")
        return

    all_arr = np.array(all_returns)
    total_n = len(all_arr)
    trimmed_all = np.sort(all_arr)[:int(total_n * 0.95)] if total_n >= 20 else all_arr
    ci_lo_all, ci_hi_all = _bootstrap_ci(all_arr) if total_n >= 20 else (0.0, 0.0)
    max_dd_all = _max_drawdown(all_arr)
    p90_all = float(np.percentile(all_arr, 10))

    print(f"{'='*70}")
    print(f"  AGGREGATE (all versions, all sources)")
    print(f"{'='*70}")
    print(f"  Total closed:     {total_n}")
    print(f"  Mean net return:  {all_arr.mean():+.2f}% (raw) / {trimmed_all.mean():+.2f}% (trimmed)")
    print(f"  95% CI:           [{ci_lo_all:+.2f}%, {ci_hi_all:+.2f}%]")
    print(f"  P90 loss:         {p90_all:.2f}%")
    print(f"  Max drawdown:     {max_dd_all:.2f}%")
    print()

    print(f"{'='*70}")
    print(f"  GATE CHECK")
    print(f"{'='*70}")

    checks = [
        ("N >= 200", total_n >= 200, f"{total_n}"),
        ("Trimmed mean > 0", trimmed_all.mean() > 0, f"{trimmed_all.mean():+.4f}%"),
        ("95% CI lower > 0", ci_lo_all > 0, f"{ci_lo_all:+.4f}%"),
        ("P90 loss < 30%", abs(p90_all) < 30, f"{p90_all:.2f}%"),
        ("Max DD < 50%", max_dd_all < 50, f"{max_dd_all:.2f}%"),
    ]

    any_source_positive = False
    for key, stats in source_stats.items():
        if stats["n"] >= 50 and stats["mean_trimmed"] > 0:
            any_source_positive = True
            break
    checks.append(("At least 1 source positive (N>=50)", any_source_positive, ""))

    all_pass = True
    for name, passed, detail in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name}: {detail}")

    print()
    if all_pass:
        print("  >>> GATE PASSED -- Real money decision can proceed <<<")
    else:
        print("  >>> GATE FAILED -- Stay in shadow mode <<<")
    print()


if __name__ == "__main__":
    main()
