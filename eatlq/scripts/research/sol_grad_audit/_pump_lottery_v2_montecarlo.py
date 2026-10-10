#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lottery v2 Monte Carlo 仓位模拟（向量化 bootstrap + mega 压力测试）."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

OUT_DIR = Path(__file__).resolve().parent
POSITIONS = [0.0002, 0.0005, 0.001, 0.0015, 0.0025]
POSITION_LABELS = ["0.02pct", "0.05pct", "0.10pct", "0.15pct", "0.25pct"]
HORIZONS = [100, 300, 500]
MEGA_TH = 10.0
N_SIM = 10_000
SEED = 42
RUIN_TH = 0.50


def load_new_returns() -> np.ndarray:
    data = json.loads((OUT_DIR / "_pump_lottery_v2_replay.json").read_text())
    return np.array([
        r["real_return_10U"]
        for r in data["results"]
        if r.get("cohort") == "lottery_v2" and not r.get("known_replay")
        and r.get("real_return_10U") is not None
    ], dtype=float)


def sample_batch(
    rng: np.random.Generator,
    pool: np.ndarray,
    n_sim: int,
    horizon: int,
    mega_scale: float,
) -> np.ndarray:
    mega = pool[pool >= MEGA_TH]
    rest = pool[pool < MEGA_TH]
    if len(mega) == 0:
        return rng.choice(rest, size=(n_sim, horizon), replace=True)
    if len(rest) == 0:
        return rng.choice(mega, size=(n_sim, horizon), replace=True)
    p_mega = (len(mega) / len(pool)) * mega_scale
    use_mega = rng.random((n_sim, horizon)) < p_mega
    rm = rng.choice(mega, size=(n_sim, horizon), replace=True)
    rr = rng.choice(rest, size=(n_sim, horizon), replace=True)
    return np.where(use_mega, rm, rr)


def simulate_batch(rets: np.ndarray, w: float) -> dict:
    """rets: (n_sim, horizon) -> metrics."""
    n_sim, horizon = rets.shape
    eq = np.ones(n_sim)
    peak = np.ones(n_sim)
    max_dd = np.zeros(n_sim)
    lose = np.zeros(n_sim, dtype=int)
    max_lose = np.zeros(n_sim, dtype=int)

    for t in range(horizon):
        r = rets[:, t]
        eq = eq * (1.0 + w * r)
        peak = np.maximum(peak, eq)
        max_dd = np.maximum(max_dd, np.where(peak > 0, (peak - eq) / peak, 0.0))
        lose = np.where(r <= 0, lose + 1, 0)
        max_lose = np.maximum(max_lose, lose)

    finals = eq - 1.0

    def pct(a, q):
        return float(np.percentile(a, q))

    return {
        "final_return": {
            "p5": pct(finals, 5),
            "p25": pct(finals, 25),
            "median": pct(finals, 50),
            "p75": pct(finals, 75),
            "p95": pct(finals, 95),
            "mean": float(finals.mean()),
        },
        "max_drawdown": {
            "p5": pct(max_dd, 5),
            "median": pct(max_dd, 50),
            "p95": pct(max_dd, 95),
            "mean": float(max_dd.mean()),
        },
        "ruin_prob": float(np.mean(eq <= RUIN_TH)),
        "longest_losing_streak": {"median": pct(max_lose, 50), "p95": pct(max_lose, 95)},
        "final_eq": eq,
    }


def after_100_no_mega(rng, rest, w, n_sim):
    if len(rest) == 0:
        return {"median": None, "p25": None, "p5": None}
    r100 = rng.choice(rest, size=(n_sim, 100), replace=True)
    eq = np.ones(n_sim)
    for t in range(100):
        eq *= 1.0 + w * r100[:, t]
    ret = eq - 1.0
    return {
        "median": float(np.median(ret)),
        "p25": float(np.percentile(ret, 25)),
        "p5": float(np.percentile(ret, 5)),
    }


def run_config(pool, w, horizon, rng, mega_scale, n_sim):
    rets = sample_batch(rng, pool, n_sim, horizon, mega_scale)
    out = simulate_batch(rets, w)
    rest = pool[pool < MEGA_TH]
    out["after_100_no_mega_return"] = after_100_no_mega(rng, rest, w, n_sim)
    out["horizon"] = horizon
    out["position_w"] = w
    out["mega_scale"] = mega_scale
    out["n_sim"] = n_sim
    del out["final_eq"]
    return out


def evaluate_pass(va: dict, vb: dict) -> dict:
    w005, w010 = 0.0005, 0.001
    a005 = va.get(f"w{w005}_h500_ms1.0", {})
    a010 = va.get(f"w{w010}_h500_ms1.0", {})
    half = vb.get("ms0.5", {}).get(f"w{w005}_h500_ms0.5", {})

    pass_005 = (
        (a005.get("final_return", {}).get("median") or 0) > 0
        and (a005.get("final_return", {}).get("p25") or -1) > -0.20
        and (half.get("final_return", {}).get("median") or -1) > -0.10
    )
    pass_010 = (a010.get("max_drawdown", {}).get("p95") or 1) < 0.15

    reasons = []
    if not pass_005:
        reasons.append("0.05%: median/p25/mega-half 未达标")
    if not pass_010:
        reasons.append("0.10%: p95 DD >= 15%")

    drop1 = None  # filled in main
    return {
        "verdict": "PASS" if pass_005 and pass_010 else "FAIL",
        "pass_0.05pct": pass_005,
        "pass_0.10pct": pass_010,
        "mega_half_median_500": half.get("final_return", {}).get("median"),
        "reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-sim", type=int, default=N_SIM)
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_lottery_v2_montecarlo.json"))
    args = ap.parse_args()

    pool = load_new_returns()
    rng = np.random.default_rng(SEED)
    mega_n = int((pool >= MEGA_TH).sum())

    version_a = {}
    for w, pl in zip(POSITIONS, POSITION_LABELS):
        for h in HORIZONS:
            key = f"w{w}_h{h}_ms1.0"
            version_a[key] = run_config(pool, w, h, rng, 1.0, args.n_sim)

    version_b = {}
    for ms in [1.0, 0.5, 0.25, 0.0]:
        version_b[f"ms{ms}"] = {}
        for w in POSITIONS:
            for h in HORIZONS:
                key = f"w{w}_h{h}_ms{ms}"
                version_b[f"ms{ms}"][key] = run_config(pool, w, h, rng, ms, args.n_sim)

    def drop_top(k):
        if k <= 0:
            return pool.copy()
        idx = np.argsort(pool)[::-1]
        return pool[idx[k:]].copy()

    sensitivity = {
        "full": run_config(pool, 0.0005, 500, rng, 1.0, args.n_sim),
        "drop_top1": run_config(drop_top(1), 0.0005, 500, rng, 1.0, args.n_sim),
        "drop_top3": run_config(drop_top(3), 0.0005, 500, rng, 1.0, args.n_sim),
    }

    shuffled = pool.copy()
    rng.shuffle(shuffled)
    seq = np.tile(shuffled, int(np.ceil(500 / len(shuffled))))[:500]
    curves = {}
    for w, pl in zip(POSITIONS, POSITION_LABELS):
        eq = 1.0
        peak = 1.0
        mdd = 0.0
        curve = [eq]
        for r in seq:
            eq *= 1.0 + w * r
            curve.append(eq)
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak else 0)
        curves[pl] = {"final_return": float(eq - 1), "max_dd": mdd}

    depends_top1 = (sensitivity["drop_top1"]["final_return"]["median"] or 0) <= 0
    verdict = evaluate_pass(version_a, version_b)
    verdict["depends_on_top1"] = depends_top1
    verdict["drop_top1_median_005"] = sensitivity["drop_top1"]["final_return"]["median"]
    if depends_top1:
        verdict["verdict"] = "CONDITIONAL_FAIL"
        verdict["reasons"] = verdict.get("reasons", []) + [
            "0.05%×500 drop_top1 median<=0 — 盈利依赖 mega",
        ]

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pool_n": len(pool),
            "pool_mega_n": mega_n,
            "n_sim": args.n_sim,
            "ruin_threshold_equity": RUIN_TH,
        },
        "version_a": version_a,
        "version_b": version_b,
        "sensitivity_drop_top": sensitivity,
        "shuffled_single_path": curves,
        "verdict": verdict,
    }

    Path(args.output).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    md = OUT_DIR / "_pump_lottery_v2_montecarlo_readout.md"
    L = [
        "# Lottery v2 Monte Carlo",
        "",
        f"池 n={len(pool)} mega={mega_n} | sims={args.n_sim}",
        f"## **{verdict['verdict']}**",
        f"- 0.05%: {verdict['pass_0.05pct']} | 0.10%: {verdict['pass_0.10pct']}",
        f"- mega×0.5 median500: {verdict.get('mega_half_median_500')}",
        f"- 靠 top1: {verdict.get('depends_on_top1')}",
        "",
        "## Version A",
        "| pos | h | med | p25 | p5 | p95 | medDD | p95DD | ruin | 100noMega |",
        "|-----|---|-----|-----|----|----|-------|-------|------|-----------|",
    ]
    for pl, w in zip(POSITION_LABELS, POSITIONS):
        for h in HORIZONS:
            b = version_a[f"w{w}_h{h}_ms1.0"]
            fr, dd = b["final_return"], b["max_drawdown"]
            nm = b["after_100_no_mega_return"]
            L.append(
                f"| {pl} | {h} | {fr['median']:+.1%} | {fr['p25']:+.1%} | {fr['p5']:+.1%} | {fr['p95']:+.1%} | "
                f"{dd['median']:.1%} | {dd['p95']:.1%} | {b['ruin_prob']:.1%} | {nm['median']:+.2%} |"
            )
    L += ["", "## Version B (0.05%, 500)", "| mega× | med | p25 | p5 | p95DD | ruin |"]
    for ms in [1.0, 0.5, 0.25, 0.0]:
        b = version_b[f"ms{ms}"][f"w0.0005_h500_ms{ms}"]
        fr, dd = b["final_return"], b["max_drawdown"]
        L.append(f"| {ms} | {fr['median']:+.1%} | {fr['p25']:+.1%} | {fr['p5']:+.1%} | {dd['p95']:.1%} | {b['ruin_prob']:.1%} |")
    L += ["", "## drop top (0.05%, 500)"]
    for k in ["full", "drop_top1", "drop_top3"]:
        fr = sensitivity[k]["final_return"]
        L.append(f"- {k}: med={fr['median']:+.1%} p25={fr['p25']:+.1%} p5={fr['p5']:+.1%}")
    md.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Done {verdict['verdict']} -> {args.output}")


if __name__ == "__main__":
    main()
