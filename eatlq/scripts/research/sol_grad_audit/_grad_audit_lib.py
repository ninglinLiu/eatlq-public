#!/usr/bin/env python3
"""Shared helpers for pump.fun graduation / path-return audit."""
from __future__ import annotations

import glob
import json
import math
import random
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics/sol"
DEFAULT_TRAIN = {"06-11", "06-12", "06-13"}
DEFAULT_TEST = {"06-14", "06-15"}


def day_of(ms: int | float) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%m-%d")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def load_grids() -> dict[str, dict[str, float]]:
    grids: dict[str, dict[str, float]] = {}
    for r in load_jsonl(DIAG / "price_paths.jsonl"):
        if r.get("path"):
            grids[r["mint"]] = {str(int(k) if float(k) == int(float(k)) else k): float(v)
                                for k, v in r["path"].items()}
    for f in sorted(glob.glob(str(DIAG / "smart_buyers*.jsonl"))):
        for r in load_jsonl(Path(f)):
            m = r.get("mint")
            if not m:
                continue
            g = grids.setdefault(m, {})
            for k, v in (r.get("grid") or {}).items():
                kk = str(int(k) if float(k) == int(float(k)) else k)
                if kk not in g and v:
                    g[kk] = float(v)
    return grids


def load_early_buys() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted(glob.glob(str(DIAG / "smart_buyers*.jsonl"))):
        for r in load_jsonl(Path(f)):
            m = r.get("mint")
            if m and m not in out:
                out[m] = r
    return out


def load_deployer() -> dict[str, list]:
    hist: dict[str, list] = defaultdict(list)
    for f in glob.glob(str(DIAG / "deployer_history*.jsonl")):
        for r in load_jsonl(Path(f)):
            c = r.get("creator")
            if c:
                hist[c].extend(r.get("coins") or [])
    return dict(hist)


def grid_price(grid: dict[str, float], t: float) -> float | None:
    if not grid:
        return None
    pts = sorted((float(k), k) for k in grid)
    best = None
    for ts, k in pts:
        v = grid[k]
        if v and ts <= t + 1e-9:
            best = v
    if best is not None:
        return best
    for ts, k in pts:
        v = grid[k]
        if v:
            return v
    return None


def path_points(grid: dict[str, float], t0: float, t1: float) -> list[tuple[float, float]]:
    pts = sorted((float(k), grid[k]) for k in grid if grid[k])
    return [(t, p) for t, p in pts if t0 <= t <= t1]


def ret_gross(grid: dict[str, float], entry: float, exit_t: float) -> float | None:
    pe, px = grid_price(grid, entry), grid_price(grid, exit_t)
    if pe and px and pe > 0:
        return px / pe - 1.0
    return None


def ret_net(grid: dict[str, float], entry: float, exit_t: float, cost: float) -> float | None:
    g = ret_gross(grid, entry, exit_t)
    return g * (1.0 - cost) - cost if g is not None else None


def mfe_mae(grid: dict[str, float], entry: float, exit_t: float) -> tuple[float | None, float | None]:
    pe = grid_price(grid, entry)
    if not pe or pe <= 0:
        return None, None
    path = path_points(grid, entry, exit_t)
    if not path:
        g = ret_gross(grid, entry, exit_t)
        if g is None:
            return None, None
        return max(g, 0.0), min(g, 0.0)
    rets = [p / pe - 1.0 for _, p in path]
    return max(rets), min(rets)


def trailing_exit(
    grid: dict[str, float],
    entry: float,
    end_t: float,
    trail_pct: float,
    hard_stop: float | None = None,
) -> tuple[float | None, float | None]:
    """Return (exit_time, net_gross_return before cost)."""
    pe = grid_price(grid, entry)
    if not pe or pe <= 0:
        return None, None
    path = path_points(grid, entry, end_t)
    if not path:
        return end_t, ret_gross(grid, entry, end_t)
    peak = pe
    exit_t = end_t
    exit_ret = path[-1][1] / pe - 1.0
    for t, p in path:
        if p > peak:
            peak = p
        r = p / pe - 1.0
        dd = p / peak - 1.0 if peak > 0 else 0.0
        if hard_stop is not None and r <= hard_stop:
            return t, r
        if dd <= -trail_pct:
            return t, r
        exit_t, exit_ret = t, r
    return exit_t, exit_ret


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    i = max(0, min(len(s) - 1, int(q * (len(s) - 1))))
    return s[i]


def summarize_rets(rets: list[float], mfes: list[float] | None = None, maes: list[float] | None = None) -> dict:
    if not rets:
        return {"n": 0}
    n = len(rets)
    wins = [x for x in rets if x > 0]
    out = {
        "n": n,
        "mean": sum(rets) / n,
        "median": st.median(rets),
        "win_rate": len(wins) / n,
        "pct_gt_10": sum(1 for x in rets if x > 0.10) / n,
        "pct_gt_25": sum(1 for x in rets if x > 0.25) / n,
        "pct_gt_50": sum(1 for x in rets if x > 0.50) / n,
        "pct_le_m10": sum(1 for x in rets if x <= -0.10) / n,
        "pct_le_m25": sum(1 for x in rets if x <= -0.25) / n,
        "pct_le_m50": sum(1 for x in rets if x <= -0.50) / n,
        "best": max(rets),
        "worst": min(rets),
    }
    if mfes:
        out["mfe_median"] = st.median(mfes)
        out["mfe_p75"] = pct(mfes, 0.75)
    if maes:
        out["mae_median"] = st.median(maes)
        out["mae_p25"] = pct(maes, 0.25)
    return out


def grad_decomp(rows: list[dict], ret_key: str = "ret") -> dict:
    g = [r for r in rows if r.get("graduated")]
    ng = [r for r in rows if not r.get("graduated")]
    n = len(rows)
    if n == 0:
        return {"n": 0}
    pg = len(g) / n
    eg = sum(r[ret_key] for r in g) / len(g) if g else None
    eng = sum(r[ret_key] for r in ng) / len(ng) if ng else None
    ev = pg * (eg or 0) + (1 - pg) * (eng or 0) if eg is not None and eng is not None else None
    breakeven = None
    if eg is not None and eng is not None and eg - eng != 0:
        breakeven = -eng / (eg - eng)
    return {
        "n": n,
        "p_grad": pg,
        "e_ret_given_grad": eg,
        "e_ret_given_not_grad": eng,
        "portfolio_ev": ev,
        "breakeven_grad_prob": breakeven,
        "n_grad": len(g),
        "n_not_grad": len(ng),
    }


def bootstrap_day_mean(day_rets: dict[str, list[float]], n_boot: int = 2000, seed: int = 1) -> dict:
    days = list(day_rets.keys())
    if not days:
        return {}
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample_days = [rng.choice(days) for _ in range(len(days))]
        pool = []
        for d in sample_days:
            pool.extend(day_rets[d])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    return {
        "median": means[len(means) // 2] if means else None,
        "p5": means[int(0.05 * len(means))] if means else None,
        "p95": means[int(0.95 * len(means))] if means else None,
    }


def classify_rule(stats: dict) -> str:
    n = stats.get("n_test") or stats.get("n") or 0
    te = stats.get("test_mean")
    boot_p5 = stats.get("bootstrap_p5")
    train_mean = stats.get("train_mean")
    gl = stats.get("grad_lift")
    median = stats.get("median")
    top_share = stats.get("top_trade_share") or stats.get("top_day_share") or 0
    win_rate = stats.get("win_rate")

    if n < 50:
        base = "探索性"
    elif n < 100:
        base = "观察名单"
    elif n < 200:
        base = "观察名单"
    else:
        base = "候选"
    if te is None:
        return "不稳定" if n >= 50 else "无边际"
    # tail / concentration guard
    if te > 0 and median is not None and median <= 0:
        return "不稳定"
    if te > 0 and top_share > 0.35:
        return "不稳定"
    if te > 0 and win_rate is not None and win_rate < 0.15:
        return "不稳定"
    if te <= 0 and (boot_p5 is None or boot_p5 <= 0):
        if gl is not None and gl > 1.3:
            return "仅有选择性"
        return "无边际"
    if train_mean is not None and te > 0 and train_mean <= 0:
        return "不稳定"
    if boot_p5 is not None and boot_p5 <= 0:
        return "不稳定"
    if n >= 100 and te > 0 and boot_p5 is not None and boot_p5 > 0:
        if median is not None and median > 0:
            return "可进入前向纸面测试"
        return "观察名单"
    if te > 0:
        return "观察名单"
    return base if base != "候选" else "仅有选择性"
