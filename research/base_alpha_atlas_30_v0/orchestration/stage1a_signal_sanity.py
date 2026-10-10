#!/usr/bin/env python3
"""Stage1A local signal sanity — no PnL, no parameter search.

Reads only wave1_probe + tiny_wave1. Resource: single thread, call under nice/ionice.
"""
from __future__ import annotations

import csv
import json
import math
import os
import resource
import time
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("POLARS_MAX_THREADS", "1")

from orchestration.common import Bar  # noqa: E402
from strategies.F01_OFS_SIGN_RUNLEN.runner import Swap, signal_fn as f01_signal  # noqa: E402
from strategies.F03_VOL_JUMP_CONTINUE.runner import signal_fn as f03_signal  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "normalized" / "wave1_probe"
PANEL = ROOT / "shared_panel" / "tiny_wave1" / "base_alpha_atlas_tiny_wave1_panel.csv"
OUT = ROOT / "normalized" / "stage1a"
REPORTS = ROOT / "reports"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def load_v3r6_swaps(path: Path) -> list[Swap]:
    swaps: list[Swap] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                buy = row.get("is_buy_weth_to_token")
                sell = row.get("is_sell_token_to_weth")
                sign = None
                if buy == "True":
                    sign = 1
                elif sell == "True":
                    sign = -1
                notional = row.get("weth_side_amount_normalized")
                q = float(notional) if notional not in (None, "", "UNKNOWN") else None
                if q is not None:
                    q = abs(q)
                swaps.append(
                    Swap(
                        pool_id=row["pool_address"].lower(),
                        block_number=int(row["block_number"]),
                        log_index=int(row["source_log_index"]),
                        timestamp_ms=int(float(row["timestamp_ms"])),
                        trade_sign=sign,
                        quote_notional_eth=q,
                        transaction_index=None,
                        tx_hash=row.get("source_tx_hash"),
                    )
                )
            except (KeyError, ValueError):
                continue
    return swaps


def load_panel_bars(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                et = int(float(row["event_time"]))
                fa = int(float(row["feature_available_at"]))
                bars.append(
                    Bar(
                        pool_id=row["pool_id"].lower(),
                        event_time=et,
                        known_at=int(float(row["known_at"])),
                        feature_available_at=fa,
                        features={"close": float(row["close"]), "trade_count": float(row["trade_count"])},
                    )
                )
            except (KeyError, ValueError):
                continue
    return bars


def summarize(signals, label: str, runtime_s: float, peak_mb: float, coverage_stats: dict) -> dict:
    pools = Counter(s.pool_id for s in signals)
    # hour bucket from event_time (ms or s heuristic)
    per_hour: Counter = Counter()
    for s in signals:
        t = s.event_time
        if t > 10_000_000_000:  # ms
            t = t // 1000
        per_hour[t // 3600] += 1
    dup_keys = Counter()
    for s in signals:
        dup_keys[
            (
                s.pool_id,
                s.meta.get("trigger_block_number", s.event_time),
                s.meta.get("trigger_log_index", s.meta.get("bar_index")),
            )
        ] += 1
    dup_rate = sum(1 for v in dup_keys.values() if v > 1) / max(len(dup_keys), 1)

    unknown_rate = coverage_stats.get("unknown_rate", 0.0)
    return {
        "label": label,
        "signal_count": len(signals),
        "pools_covered": len(pools),
        "signals_per_pool": dict(pools.most_common(20)),
        "signals_per_pool_mean": (sum(pools.values()) / len(pools)) if pools else 0.0,
        "signals_per_hour": dict(sorted(per_hour.items())[:48]),
        "signals_per_hour_mean": (sum(per_hour.values()) / len(per_hour)) if per_hour else 0.0,
        "feature_coverage": coverage_stats,
        "UNKNOWN_rate": unknown_rate,
        "duplicate_signal_rate": dup_rate,
        "sample_triggers": [
            {
                "pool_id": s.pool_id,
                "observation_timestamp": s.event_time,
                "feature_available_at": s.feature_available_at,
                "trigger_coordinates": {
                    k: s.meta.get(k)
                    for k in (
                        "trigger_block_number",
                        "trigger_log_index",
                        "trigger_transaction_index",
                        "bar_index",
                        "bar_close_time",
                        "jump_ratio",
                        "run_length",
                    )
                    if k in s.meta or k in ("trigger_block_number", "trigger_log_index", "bar_index")
                },
            }
            for s in signals[:10]
        ],
        "runtime_s": runtime_s,
        "peak_memory_mb": peak_mb,
    }


def write_signals(path: Path, signals) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Prefer parquet if available; else JSONL (still named .parquet sidecar note)
    rows = []
    for s in signals:
        rows.append(
            {
                "strategy_id": s.strategy_id,
                "pool_id": s.pool_id,
                "side": s.side,
                "strength": s.strength,
                "observation_timestamp": s.event_time,
                "feature_available_at": s.feature_available_at,
                "meta_json": json.dumps(s.meta, sort_keys=True, default=str),
            }
        )
    wrote = None
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist(rows)
        pq.write_table(table, path)
        wrote = "parquet"
    except Exception:
        alt = path.with_suffix(".jsonl")
        with alt.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        # also write empty parquet marker path as jsonl pointer
        path.write_text(json.dumps({"format": "jsonl_fallback", "path": str(alt)}) + "\n")
        wrote = "jsonl_fallback"
    return wrote


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    v3 = PROBE / "run004_v3r6_candidate_pool_head20k.csv"

    # F01
    t0 = time.time()
    swaps = load_v3r6_swaps(v3)
    n_unknown_sign = sum(1 for s in swaps if s.trade_sign is None)
    n_unknown_notional = sum(1 for s in swaps if s.quote_notional_eth is None)
    f01_params = {
        "min_run_len": 3,
        "min_run_notional_eth": 0.01,
        "L_completed_runs": 20,
        "z_enter": None,
        "cooldown_swaps": 20,
        "min_swaps_in_pool": 10,
    }
    f01_sigs = f01_signal(swaps, f01_params)
    f01_rt = time.time() - t0
    f01_peak = rss_mb()
    f01_cov = {
        "n_swaps": len(swaps),
        "unknown_sign_rate": n_unknown_sign / max(len(swaps), 1),
        "unknown_notional_rate": n_unknown_notional / max(len(swaps), 1),
        "transaction_index": "UNKNOWN",
        "source": str(v3),
    }
    f01_sum = summarize(f01_sigs, "F01", f01_rt, f01_peak, {**f01_cov, "unknown_rate": f01_cov["unknown_sign_rate"]})
    f01_fmt = write_signals(OUT / "f01_signals.parquet", f01_sigs)

    # F03 — use smaller l default for tiny panel depth
    t1 = time.time()
    bars = load_panel_bars(PANEL)
    f03_params = {"k": 3.0, "l": 30, "arm": 0, "jump_cooldown_bars": 5}
    # pools with enough history
    f03_sigs = f03_signal(bars, f03_params)
    f03_rt = time.time() - t1
    f03_peak = rss_mb()
    f03_cov = {
        "n_bars": len(bars),
        "n_pools": len({b.pool_id for b in bars}),
        "unknown_rate": 0.0,
        "status_label": "FIXTURE_VALIDATED_SIGNAL_LOGIC",
        "source": str(PANEL),
        "l_used": 30,
        "note": "l=30 for tiny panel depth; search space still freezes l in {60,120}",
    }
    f03_sum = summarize(f03_sigs, "F03", f03_rt, f03_peak, f03_cov)
    f03_fmt = write_signals(OUT / "f03_signals.parquet", f03_sigs)

    # Orthogonality structure only
    # Align time units to seconds
    def key_pool_hour(s):
        t = s.event_time
        if t > 10_000_000_000:
            t = t // 1000
        return (s.pool_id, t // 3600)

    f01_keys = {key_pool_hour(s) for s in f01_sigs}
    f03_keys = {key_pool_hour(s) for s in f03_sigs}
    f01_pools = {s.pool_id for s in f01_sigs}
    f03_pools = {s.pool_id for s in f03_sigs}
    ortho = {
        "same_pool_hit_jaccard": jaccard(f01_pools, f03_pools),
        "same_pool_hour_jaccard": jaccard(f01_keys, f03_keys),
        "f01_pools": len(f01_pools),
        "f03_pools": len(f03_pools),
        "intersection_pools": sorted(f01_pools & f03_pools)[:20],
        "mechanism_note": (
            "F01=swap-run microstructure; F03=closed-bar vol jump. "
            "Overlap in pools/hours does not imply same mechanism."
        ),
        "primary_inputs": {
            "F01": ["trade_sign", "run_length", "run_notional_eth"],
            "F03": ["close_to_close_log_return", "realized_vol_baseline", "jump_ratio"],
        },
    }

    # Frequency correlation across shared hours (global)
    h1 = Counter(k[1] for k in f01_keys)
    h3 = Counter(k[1] for k in f03_keys)
    hours = sorted(set(h1) | set(h3))
    if len(hours) >= 2:
        m1 = sum(h1[h] for h in hours) / len(hours)
        m3 = sum(h3[h] for h in hours) / len(hours)
        num = sum((h1[h] - m1) * (h3[h] - m3) for h in hours)
        d1 = math.sqrt(sum((h1[h] - m1) ** 2 for h in hours))
        d3 = math.sqrt(sum((h3[h] - m3) ** 2 for h in hours))
        corr = num / (d1 * d3) if d1 > 0 and d3 > 0 else None
    else:
        corr = None
    ortho["trigger_frequency_corr_hourly"] = corr

    summary = {
        "f01": f01_sum,
        "f03": f03_sum,
        "formats": {"f01": f01_fmt, "f03": f03_fmt},
        "ortho": ortho,
        "forbidden_outputs": ["return", "PnL", "MFE", "MAE", "win_rate", "profit_factor", "best_parameter"],
    }
    (OUT / "sanity_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
