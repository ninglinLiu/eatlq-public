#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frozen Lottery vs Control 资金曲线对比（daily block bootstrap）."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

OUT_DIR = Path(__file__).resolve().parent
POSITIONS = [0.0002, 0.0005, 0.001, 0.0015, 0.0025]
POSITION_LABELS = ["0.02pct", "0.05pct", "0.10pct", "0.15pct", "0.25pct"]
HORIZON = 500
MEGA_TH = 10.0
N_SIM = 10_000
SEED = 42
RUIN_TH = 0.50


def load_cohort_data() -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    data = json.loads((OUT_DIR / "_pump_lottery_v2_frozen_audit.json").read_text())
    lot: dict[str, list[float]] = {}
    ctrl: dict[str, list[float]] = {}
    for r in data["results"]:
        rr = r.get("real_return_10U")
        if rr is None:
            continue
        day = r.get("day") or "unknown"
        if r.get("cohort") == "lottery_v2":
            lot.setdefault(day, []).append(float(rr))
        elif r.get("cohort") == "matched_control":
            ctrl.setdefault(day, []).append(float(rr))
    return {d: np.array(v) for d, v in lot.items()}, {d: np.array(v) for d, v in ctrl.items()}


def flatten(blocks: dict[str, np.ndarray]) -> np.ndarray:
    if not blocks:
        return np.array([], dtype=float)
    return np.concatenate(list(blocks.values()))


def apply_stress(blocks: dict[str, np.ndarray], mode: str) -> dict[str, np.ndarray]:
    if mode == "baseline":
        return blocks
    if mode == "remove_top1":
        return _drop_top_blocks(blocks, 1)
    if mode == "remove_top3":
        return _drop_top_blocks(blocks, 3)
    if mode == "remove_top5":
        return _drop_top_blocks(blocks, 5)
    if mode == "no_p1000":
        return {d: arr[arr < MEGA_TH] for d, arr in blocks.items() if np.any(arr < MEGA_TH)}
    if mode == "halve_mega":
        return _halve_mega(blocks)
    return blocks


def _drop_top_blocks(blocks: dict[str, np.ndarray], k: int) -> dict[str, np.ndarray]:
    flat = flatten(blocks)
    if k <= 0 or len(flat) <= k:
        return blocks
    idx = np.argsort(flat)[::-1][:k]
    top_vals = set(flat[idx].tolist())
    out = {}
    for d, arr in blocks.items():
        remaining = arr.tolist()
        for v in sorted(top_vals, reverse=True):
            if v in remaining:
                remaining.remove(v)
        if remaining:
            out[d] = np.array(remaining, dtype=float)
    return out


def _halve_mega(blocks: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)
    out = {}
    for d, arr in blocks.items():
        keep = [x for x in arr if not (x >= MEGA_TH and rng.random() < 0.5)]
        if keep:
            out[d] = np.array(keep, dtype=float)
    return out


def sample_daily_block_batch(
    rng: np.random.Generator,
    blocks: dict[str, np.ndarray],
    n_sim: int,
    horizon: int,
) -> np.ndarray:
    days = list(blocks.keys())
    if not days:
        return np.zeros((n_sim, horizon))
    day_arrays = [blocks[d] for d in days]
    weights = np.array([len(a) for a in day_arrays], dtype=float)
    weights /= weights.sum()

    rets = np.zeros((n_sim, horizon))
    pos = np.zeros(n_sim, dtype=int)
    for _ in range(horizon):
        active = pos < horizon
        if not active.any():
            break
        sim_idx = np.where(active)[0]
        chosen = rng.choice(len(days), size=len(sim_idx), p=weights)
        for si, di in zip(sim_idx, chosen):
            arr = day_arrays[di]
            need = horizon - pos[si]
            n = min(len(arr), need)
            rets[si, pos[si] : pos[si] + n] = arr[:n]
            pos[si] += n
    return rets


def sample_iid_batch(
    rng: np.random.Generator,
    pool: np.ndarray,
    n_sim: int,
    horizon: int,
    mega_scale: float = 1.0,
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
    n_sim, horizon = rets.shape
    eq = np.ones(n_sim)
    peak = np.ones(n_sim)
    max_dd = np.zeros(n_sim)

    for t in range(horizon):
        r = rets[:, t]
        eq *= 1.0 + w * r
        peak = np.maximum(peak, eq)
        max_dd = np.maximum(max_dd, np.where(peak > 0, (peak - eq) / peak, 0.0))

    finals = eq - 1.0

    def pct(a, q):
        return float(np.percentile(a, q))

    return {
        "median": pct(finals, 50),
        "p5": pct(finals, 5),
        "p25": pct(finals, 25),
        "p75": pct(finals, 75),
        "p95": pct(finals, 95),
        "mdd_p95": pct(max_dd, 95),
        "ruin": float(np.mean(eq <= RUIN_TH)),
    }


def run_mc(
    blocks: dict[str, np.ndarray],
    w: float,
    rng: np.random.Generator,
    n_sim: int,
    method: str = "daily_block",
) -> dict:
    pool = flatten(blocks)
    if pool.size == 0:
        return {
            "position": w,
            "horizon": HORIZON,
            "n_sim": n_sim,
            "method": method,
            "pool_n": 0,
            "median": None,
            "p5": None,
            "p25": None,
            "p75": None,
            "p95": None,
            "mdd_p95": None,
            "ruin": None,
        }
    if method == "daily_block":
        rets = sample_daily_block_batch(rng, blocks, n_sim, HORIZON)
    else:
        rets = sample_iid_batch(rng, pool, n_sim, HORIZON)
    out = simulate_batch(rets, w)
    out.update(
        {
            "position": w,
            "horizon": HORIZON,
            "n_sim": n_sim,
            "method": method,
            "pool_n": len(pool),
        }
    )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-sim", type=int, default=N_SIM)
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_lottery_v2_frozen_capital_compare.json"))
    args = ap.parse_args()

    lot_blocks, ctrl_blocks = load_cohort_data()
    rng = np.random.default_rng(SEED)

    stresses = [
        "baseline",
        "remove_top1",
        "remove_top3",
        "remove_top5",
        "no_p1000",
        "halve_mega",
    ]

    rows = []
    for group_name, blocks in [("lottery_v2", lot_blocks), ("matched_control", ctrl_blocks)]:
        for stress in stresses:
            b = apply_stress(blocks, stress)
            for w, plabel in zip(POSITIONS, POSITION_LABELS):
                m = run_mc(b, w, rng, args.n_sim, method="daily_block")
                rows.append({"group": group_name, "stress": stress, "position_label": plabel, **m})

    for group_name, blocks in [("lottery_v2", lot_blocks), ("matched_control", ctrl_blocks)]:
        m = run_mc(blocks, 0.0005, rng, args.n_sim, method="iid")
        rows.append({"group": group_name, "stress": "baseline_iid", "position_label": "0.05pct", **m})

    # drop_top1/3 median columns on baseline = same as remove_top1/3 stress at same position
    lookup = {(r["group"], r["stress"], r["position_label"]): r for r in rows}
    for r in rows:
        if r["stress"] != "baseline":
            continue
        k1 = (r["group"], "remove_top1", r["position_label"])
        k3 = (r["group"], "remove_top3", r["position_label"])
        r["drop_top1_median"] = lookup[k1].get("median") if k1 in lookup else None
        r["drop_top3_median"] = lookup[k3].get("median") if k3 in lookup else None
    for r in rows:
        if r["stress"] in ("remove_top1", "remove_top3"):
            r["drop_top1_median"] = r.get("median") if r["stress"] == "remove_top1" else None
            r["drop_top3_median"] = r.get("median") if r["stress"] == "remove_top3" else None

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "horizon": HORIZON,
            "n_sim": args.n_sim,
            "lottery_n": len(flatten(lot_blocks)),
            "control_n": len(flatten(ctrl_blocks)),
            "method_primary": "daily_block_bootstrap",
            "days": sorted(lot_blocks.keys()),
        },
        "rows": rows,
        "summary_baseline_daily": _compare_table(rows, "baseline"),
    }

    Path(args.output).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    csv_path = OUT_DIR / "_pump_lottery_v2_frozen_capital_compare.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fields = [
            "group",
            "stress",
            "position_label",
            "median",
            "p5",
            "p25",
            "p75",
            "p95",
            "mdd_p95",
            "ruin",
            "drop_top1_median",
            "drop_top3_median",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    md = OUT_DIR / "_pump_lottery_v2_frozen_capital_compare_readout.md"
    lines = [
        "# Lottery vs Control 资金曲线对比（daily block bootstrap）",
        "",
        f"Lottery n={out['meta']['lottery_n']} | Control n={out['meta']['control_n']} | "
        f"{HORIZON}笔 × {args.n_sim} sims",
        "",
        "## Baseline（daily block）",
        "",
        "| 组 | 仓位 | 500 med | p5 | p25 | p75 | p95 | MDD p95 | ruin | drop_top1 med | drop_top3 med |",
        "|----|------|---------|----|----|-----|-----|---------|------|---------------|---------------|",
    ]
    for r in rows:
        if r["stress"] != "baseline":
            continue
        lines.append(
            f"| {r['group']} | {r['position_label']} | {r['median']:+.1%} | {r['p5']:+.1%} | {r['p25']:+.1%} | "
            f"{r['p75']:+.1%} | {r['p95']:+.1%} | {r['mdd_p95']:.1%} | {r['ruin']:.1%} | "
            f"{r.get('drop_top1_median') or 0:+.1%} | {r.get('drop_top3_median') or 0:+.1%} |"
        )

    lines.extend(["", "## 压力测试（0.05% 仓位，daily block）", ""])
    lines.append("| 组 | stress | median | p5 | MDD p95 | drop_top1 |")
    lines.append("|----|--------|--------|----|---------|-----------|")
    for r in rows:
        if r["position_label"] != "0.05pct":
            continue
        if r["stress"] == "baseline_iid":
            lines.append(
                f"| {r['group']} | {r['stress']} | {r['median']:+.1%} | {r['p5']:+.1%} | "
                f"{r['mdd_p95']:.1%} | — |"
            )
        elif r["stress"] != "baseline":
            drop1 = f"{r['median']:+.1%}" if r["stress"] == "remove_top1" else "—"
            lines.append(
                f"| {r['group']} | {r['stress']} | {r['median']:+.1%} | {r['p5']:+.1%} | "
                f"{r['mdd_p95']:.1%} | {drop1} |"
            )

    lines.extend(["", "## 结论", ""])
    lines.append(_verdict_text(out["summary_baseline_daily"]))
    lines.append("")
    lines.append(_full_verdict(out["summary_baseline_daily"], rows))
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done -> {args.output}")


def _compare_table(rows: list[dict], stress: str) -> dict:
    out = {}
    for pl in POSITION_LABELS:
        lot = next(
            (r for r in rows if r["group"] == "lottery_v2" and r["stress"] == stress and r["position_label"] == pl),
            None,
        )
        ctrl = next(
            (
                r
                for r in rows
                if r["group"] == "matched_control" and r["stress"] == stress and r["position_label"] == pl
            ),
            None,
        )
        if lot and ctrl and lot.get("median") is not None and ctrl.get("median") is not None:
            out[pl] = {
                "lottery_median": lot["median"],
                "control_median": ctrl["median"],
                "lottery_p5": lot["p5"],
                "control_p5": ctrl["p5"],
                "lottery_mdd_p95": lot["mdd_p95"],
                "control_mdd_p95": ctrl["mdd_p95"],
                "lottery_drop_top1": lot.get("drop_top1_median"),
                "control_drop_top1": ctrl.get("drop_top1_median"),
                "lottery_wins_median": lot["median"] > ctrl["median"],
                "lottery_wins_p5": lot["p5"] > ctrl["p5"],
                "lottery_wins_mdd": lot["mdd_p95"] < ctrl["mdd_p95"],
                "lottery_wins_drop_top1": (lot.get("drop_top1_median") or -999) > (ctrl.get("drop_top1_median") or -999),
            }
    return out


def _verdict_text(summary: dict) -> str:
    w05 = summary.get("0.05pct", {})
    if not w05:
        return "insufficient data"
    parts = []
    parts.append("Lottery median 更好" if w05.get("lottery_wins_median") else "Control median 更好")
    parts.append("Lottery p5 更好" if w05.get("lottery_wins_p5") else "Control p5 更好")
    parts.append("Lottery 回撤更低" if w05.get("lottery_wins_mdd") else "Control 回撤更低")
    parts.append(
        "Lottery drop_top1 更能活"
        if w05.get("lottery_wins_drop_top1")
        else "Control drop_top1 更能活"
    )
    return " | ".join(parts)


def _full_verdict(summary: dict, rows: list) -> str:
    wins = {k: 0 for k in ("median", "p5", "mdd", "drop_top1")}
    for pl, s in summary.items():
        if s.get("lottery_wins_median"):
            wins["median"] += 1
        if s.get("lottery_wins_p5"):
            wins["p5"] += 1
        if s.get("lottery_wins_mdd"):
            wins["mdd"] += 1
        if s.get("lottery_wins_drop_top1"):
            wins["drop_top1"] += 1
    n = len(summary)
    lot_stress = [
        r
        for r in rows
        if r["group"] == "lottery_v2" and r["position_label"] == "0.05pct" and r["stress"] not in ("baseline",)
    ]
    ctrl_stress = [
        r
        for r in rows
        if r["group"] == "matched_control" and r["position_label"] == "0.05pct" and r["stress"] not in ("baseline",)
    ]
    stress_wins = sum(
        1 for l, c in zip(sorted(lot_stress, key=lambda x: x["stress"]), sorted(ctrl_stress, key=lambda x: x["stress"]))
        if (l.get("median") or -999) > (c.get("median") or -999)
    )
    lines = [
        f"Baseline 仓位 sweep: Lottery wins median {wins['median']}/{n}, p5 {wins['p5']}/{n}, "
        f"MDD {wins['mdd']}/{n}, drop_top1 {wins['drop_top1']}/{n}",
        f"Stress tests @0.05%: Lottery median wins {stress_wins}/{len(lot_stress)} scenarios",
    ]
    if wins["median"] >= 3 and wins["p5"] >= 3:
        lines.append("→ Lottery 在资金曲线上保留结构性价值（尽管 p100/p300 不占优）")
    elif wins["median"] <= 1 and wins["p5"] <= 1:
        lines.append("→ Control 在资金曲线全面占优；Lottery v2 无 ex-post 优势")
    else:
        lines.append("→ 混合结果：Lottery 仅在部分指标/仓位占优，需结合 mega 依赖度判断")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
