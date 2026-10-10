#!/usr/bin/env python3
"""Pure GL11 bankroll, block-bootstrap, Kelly, and capacity calculations."""
from __future__ import annotations

import hashlib
import heapq
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

B0 = 10_000.0
RUIN_LINE = 2_000.0
PESS = -1.0229943556396193
CANDIDATES = (8, 16, 32, 64, 128, 256, 512, 1024)


def parse_utc(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def concurrency_filter(rows: list[dict], limit: int) -> list[dict]:
    """Causally admit time-sorted rows without looking at returns or labels."""
    active: list[datetime] = []
    out = []
    for row in sorted(rows, key=lambda x: (x["entry_time"], x["entry_block"], x["pool"])):
        while active and active[0] <= row["entry_time"]:
            heapq.heappop(active)
        if len(active) >= limit:
            continue
        heapq.heappush(active, row["exit_time"])
        out.append(row)
    return out


def empirical_metrics(values: np.ndarray, rug_flags: np.ndarray) -> dict:
    """Describe one complete empirical return vector without resolved-only cuts."""
    v = np.asarray(values, dtype=float)
    top = int(np.argmax(v))
    ex = np.delete(v, top)
    positive = np.maximum(v, 0)
    return {
        "n": int(v.size), "mean": float(np.mean(v)), "median": float(np.median(v)),
        "win_rate": float(np.mean(v > 0)), "loss_rate": float(np.mean(v < 0)),
        "rug_outcome_rate": float(np.mean(rug_flags)), "loss_floor_rate": float(np.mean(v <= -.95)),
        "min": float(np.min(v)), "p1": float(np.quantile(v, .01)), "p5": float(np.quantile(v, .05)),
        "p95": float(np.quantile(v, .95)), "max": float(np.max(v)),
        "ex_top1_mean": float(np.mean(ex)),
        "top1_positive_contribution": float(positive[top] / positive.sum()) if positive.sum() else 0.0,
        "vector_sha256": hashlib.sha256(json.dumps(v.tolist(), separators=(",", ":"),
                                                       allow_nan=False).encode()).hexdigest(),
    }


def kelly_grid(values: np.ndarray) -> dict:
    """Empirical log-growth argmax on the frozen 0..20% grid."""
    v = np.asarray(values, dtype=float)
    fs = np.arange(0, .2000001, .0001)
    best_f, best_g = 0.0, 0.0
    for f in fs:
        wealth = 1.0 + f * v
        if np.any(wealth <= 0):
            continue
        g = float(np.mean(np.log(wealth)))
        if g > best_g + 1e-15:
            best_f, best_g = float(f), g
    return {"full_kelly": best_f, "half_kelly": best_f / 2, "quarter_kelly": best_f / 4,
            "mean_log_growth": best_g, "grid_step": .0001, "grid_max": .20}


def month_groups(rows: list[dict]) -> tuple[list[str], dict[str, list[int]]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row["entry_time"].strftime("%Y-%m")].append(i)
    return sorted(groups), dict(groups)


def bootstrap_indices(groups: dict[str, list[int]], n_paths: int, horizon: int, seed: int) -> np.ndarray:
    """Circular-phase first month, then concatenate sampled calendar months."""
    keys = sorted(k for k, v in groups.items() if v)
    if not keys:
        raise ValueError("no nonempty month blocks")
    rng = np.random.default_rng(seed)
    out = np.empty((n_paths, horizon), dtype=np.int32)
    for p in range(n_paths):
        first = groups[keys[int(rng.integers(0, len(keys)))]]
        phase = int(rng.integers(0, len(first)))
        suffix = first[phase:]
        n0 = min(len(suffix), horizon)
        out[p, :n0] = suffix[:n0]
        pos = n0
        while pos < horizon:
            block = groups[keys[int(rng.integers(0, len(keys)))]]
            n = min(len(block), horizon - pos)
            out[p, pos:pos+n] = block[:n]
            pos += n
    return out


def simulate(values: np.ndarray, capacities: np.ndarray, sampled: np.ndarray,
             fraction: float, dollar_cap: float, candidates=CANDIDATES) -> dict:
    """Vectorized bankroll paths with capped stake, path ruin, and MDD."""
    n_paths, horizon = sampled.shape
    bank = np.full(n_paths, B0, dtype=float)
    peak = bank.copy()
    max_dd = np.zeros(n_paths, dtype=float)
    ruined = np.zeros(n_paths, dtype=bool)
    p_positive = {}
    cset = set(int(x) for x in candidates)
    for t in range(horizon):
        idx = sampled[:, t]
        stake = np.minimum(np.minimum(fraction * bank, dollar_cap), capacities[idx])
        bank += stake * values[idx]
        ruined |= bank <= RUIN_LINE
        peak = np.maximum(peak, bank)
        max_dd = np.maximum(max_dd, np.maximum(0.0, (peak - bank) / peak))
        n = t + 1
        if n in cset:
            p_positive[str(n)] = float(np.mean(bank > B0))
    required = next((n for n in candidates if p_positive.get(str(n), 0.0) >= .95), None)
    return {
        "terminal_p5": float(np.quantile(bank, .05)), "terminal_p50": float(np.median(bank)),
        "terminal_p95": float(np.quantile(bank, .95)), "p_final_above_initial": float(np.mean(bank > B0)),
        "ruin_probability": float(np.mean(ruined)), "mdd_p50": float(np.median(max_dd)),
        "mdd_p95": float(np.quantile(max_dd, .95)), "p_positive_by_n": p_positive,
        "required_n_95": required,
    }


def contiguous_months(start: datetime, end: datetime) -> list[str]:
    y, m = start.year, start.month
    out = []
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def n8_interpretation(p_loss: float) -> str:
    if p_loss >= .10:
        return "READILY_COMPATIBLE_WITH_SAMPLING_VARIANCE"
    if p_loss >= .01:
        return "POSSIBLE_BUT_TAIL_EVENT"
    return "UNLIKELY_PURE_VARIANCE_UNDER_HISTORICAL_DISTRIBUTION"


def viability_label(main_f, required_n, months_required, monthly_median) -> str:
    if (main_f is None or required_n is None or months_required is None
            or months_required > 36 or monthly_median is None or monthly_median < 100):
        return "NOT_VIABLE_UNDER_SURVIVABLE_SIZING"
    return "SURVIVABLE_OPERATING_POINT_DESCRIPTIVE"
