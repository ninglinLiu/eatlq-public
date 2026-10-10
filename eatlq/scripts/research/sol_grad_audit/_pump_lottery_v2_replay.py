#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lottery v2 右尾通道验证：cluster+低频dev，无 top56 限制."""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from _grad_audit_lib import load_grids
from _pump_dual_basket_replay import (
    DEV_CORE_MAX,
    DEV_LOTTERY_MAX,
    FLIP_CORE_MAX,
    MEGA_PROXY_MIN,
    attach_scores,
    build_cohorts,
    load_known_pre_dual,
    load_prior_results,
    prior_replay_paths,
)
from _pump_exante_filtered_rank import build_enhanced_rows
from _pump_exante_top5_replay import load_done, replay_one
from _pump_tail_realgas_replay import load_meta

OUT_DIR = Path(__file__).resolve().parent
KKK2 = "KKK2KqnP7D7EjYBn33yRkhuJEW9rQjE2bFHHGUKpump"
FATAL25, FATAL50 = -0.25, -0.50
MAX_REPLAY_CAP = 400  # lottery v2 pending priority


def replay_json_sources() -> list[Path]:
    return [
        OUT_DIR / "_pump_exante_top5_replay.json",
        OUT_DIR / "_pump_tail_realgas_replay.json",
        OUT_DIR / "_pump_dual_basket_replay.json",
    ]


def build_lottery_v2_cohort(te: pd.DataFrame) -> pd.DataFrame:
    core_df, _ = build_cohorts(te)
    core_m = set(core_df["mint"])
    lot = te[
        (te["same_dev_same_buyer_cluster"] >= MEGA_PROXY_MIN)
        & (te["dev_past_created_count"] <= DEV_LOTTERY_MAX)
        & (~te["mint"].isin(core_m))
    ].sort_values("tail_score", ascending=False)
    lot = lot.copy()
    lot["in_core_overlap"] = False
    return lot


def build_matched_control(te: pd.DataFrame, lot_mints: set[str], n: int, seed: int) -> pd.DataFrame:
    core_df, _ = build_cohorts(te)
    core_m = set(core_df["mint"])
    pool = te[
        (~te["mint"].isin(lot_mints))
        & (~te["mint"].isin(core_m))
        & ~(
            (te["same_dev_same_buyer_cluster"] >= MEGA_PROXY_MIN)
            & (te["dev_past_created_count"] <= DEV_LOTTERY_MAX)
        )
    ]
    if len(pool) < n:
        pool = te[~te["mint"].isin(lot_mints)]
    return pool.sample(n=min(n, len(pool)), random_state=seed).sort_values("tail_score", ascending=False)


def load_old_top5_mints() -> set[str]:
    p = OUT_DIR / "_pump_exante_top5_replay.json"
    if not p.exists():
        return set()
    return {
        r["mint"] for r in json.loads(p.read_text())["results"]
        if r.get("cohort") == "exante_top5"
    }


def longest_losing_streak(rets: list[float]) -> int:
    best = cur = 0
    for r in rets:
        if r <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def capital_curve(rets: list[float], scale: float) -> list[float]:
    eq = 1.0
    curve = [eq]
    for r in rets:
        eq *= 1.0 + scale * r
        curve.append(eq)
    return curve


def portfolio_stats(rets: list[float], label: str = "") -> dict:
    if not rets:
        return {"label": label, "n": 0}
    srt = sorted(rets, reverse=True)
    n = len(rets)
    out = {
        "label": label,
        "n": n,
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / n,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / n,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / n,
        "median": st.median(rets),
        "mean": sum(rets) / n,
        "drop_top1_mean": st.mean(srt[1:]) if n > 1 else None,
        "drop_top3_mean": st.mean(srt[3:]) if n > 3 else None,
        "max_return": max(rets),
        "fatal_rate_25": sum(1 for x in rets if x <= FATAL25) / n,
        "fatal_rate_50": sum(1 for x in rets if x <= FATAL50) / n,
        "longest_losing_streak": longest_losing_streak(rets),
        "capital_final_0.1x": capital_curve(rets, 0.1)[-1],
        "capital_final_0.25x": capital_curve(rets, 0.25)[-1],
        "capital_final_1.0x": capital_curve(rets, 1.0)[-1],
    }
    return out


def eval_splits(rows: list[dict], label: str) -> dict:
    rets_all = [r["real_return_10U"] for r in rows if r.get("real_return_10U") is not None]
    known = [r["real_return_10U"] for r in rows if r.get("known_replay") and r.get("real_return_10U") is not None]
    new = [r["real_return_10U"] for r in rows if not r.get("known_replay") and r.get("real_return_10U") is not None]
    wo_kkk = [r["real_return_10U"] for r in rows if r.get("mint") != KKK2 and r.get("real_return_10U") is not None]
    with_kkk = rets_all
    return {
        "combined": portfolio_stats(rets_all, f"{label}_combined"),
        "known_replay": portfolio_stats(known, f"{label}_known"),
        "new_replay_only": portfolio_stats(new, f"{label}_new"),
        "combined_without_kkk2": portfolio_stats(wo_kkk, f"{label}_no_kkk2"),
        "combined_with_kkk2": portfolio_stats(with_kkk, f"{label}_with_kkk2"),
    }


def verdict_lottery_v2(new_stats: dict, combined_stats: dict, combined_no_kkk: dict) -> dict:
    reasons = []
    v = "FAIL"
    n_new = new_stats.get("n", 0)
    if n_new >= 30:
        if (new_stats.get("p_gt_300") or 0) >= 0.03:
            if (new_stats.get("max_return") or 0) >= 3.0:
                if (new_stats.get("drop_top1_mean") or -99) >= -0.05:
                    v = "PASS"
                else:
                    reasons.append(f"new drop_top1 {new_stats.get('drop_top1_mean')} < -5%")
            else:
                reasons.append(f"new max {new_stats.get('max_return')} < +300%")
        else:
            reasons.append(f"new p300 {new_stats.get('p_gt_300')} < 3%")
    else:
        reasons.append(f"new n={n_new} < 30")

    strong = False
    if v == "PASS":
        p1000_no_kkk = combined_no_kkk.get("p_gt_1000") or 0
        if p1000_no_kkk >= 0.01:
            strong = True
            v = "STRONG"
        elif (combined_stats.get("p_gt_1000") or 0) >= 0.01 or (combined_stats.get("max_return") or 0) >= 10.0:
            pass  # PASS remains
        else:
            reasons.append("combined p1000 < 1% and max < +1000%")

    return {"verdict": v, "strong": strong, "reasons": reasons}


def cohort_entry(row: pd.Series, cohort: str, rank: int) -> dict:
    return {
        "mint": row["mint"],
        "cohort": cohort,
        "rank": rank,
        "tail_score": float(row["tail_score"]),
        "dev_past_created_count": float(row["dev_past_created_count"]),
        "same_dev_same_buyer_cluster": float(row["same_dev_same_buyer_cluster"]),
        "grid_net_return": float(row.get("final_ret", 0)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-replay", type=int, default=MAX_REPLAY_CAP)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_lottery_v2_replay.json"))
    ap.add_argument("--checkpoint", default=str(OUT_DIR / "_pump_lottery_v2_checkpoint.jsonl"))
    args = ap.parse_args()

    rows = build_enhanced_rows(replay_json_sources())
    df = attach_scores(pd.DataFrame(rows))
    te = df[df["split"] == "TEST"].copy()

    lot_df = build_lottery_v2_cohort(te)
    lot_mints = set(lot_df["mint"])
    ctrl_df = build_matched_control(te, lot_mints, n=len(lot_df), seed=args.seed)

    lot_cohort = [cohort_entry(r, "lottery_v2", i) for i, (_, r) in enumerate(lot_df.iterrows(), 1)]
    ctrl_cohort = [cohort_entry(r, "matched_control", i) for i, (_, r) in enumerate(ctrl_df.iterrows(), 1)]

    prior = load_prior_results()
    known_pre = load_known_pre_dual()
    done = load_done(*prior_replay_paths(), Path(args.checkpoint))

    def hydrate(cohort: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for c in cohort:
            m = c["mint"]
            if m in prior and prior[m].get("real_return_10U") is not None:
                r = prior[m].copy()
                r.update({"cohort": c["cohort"], "tail_score": c["tail_score"], "known_replay": m in known_pre})
                out[m] = r
            elif m in done and Path(args.checkpoint).exists():
                for line in open(args.checkpoint):
                    rr = json.loads(line)
                    if rr["mint"] == m:
                        rr["known_replay"] = m in known_pre
                        out[m] = rr
        return out

    lot_results = hydrate(lot_cohort)
    ctrl_results = hydrate(ctrl_cohort)

    lot_pending = [c for c in lot_cohort if c["mint"] not in lot_results]
    # prioritize lottery v2 pending; cap control new RPC separately
    ctrl_cap = min(len(ctrl_cohort), max(0, args.max_replay - len(lot_pending)))
    if len(lot_pending) > args.max_replay:
        lot_pending = lot_pending[: args.max_replay]
    ctrl_pending = [c for c in ctrl_cohort if c["mint"] not in ctrl_results][:ctrl_cap]

    print(
        f"[lottery-v2] candidates={len(lot_df)} ctrl={len(ctrl_df)} "
        f"lot_cached={len(lot_results)} lot_pending={len(lot_pending)} "
        f"ctrl_pending={len(ctrl_pending)} kkk2_in={KKK2 in lot_mints}"
    )

    meta = load_meta()
    grids = load_grids()
    cp = Path(args.checkpoint)
    rpc_calls = 0

    if not args.dry_run:
        from _pump_tail_realgas_replay import RPC, rpc_url

        rpc = RPC(rpc_url(), min_interval=0.20)
        tx_cache: dict = {}
        pending_all = [(c, lot_results) for c in lot_pending] + [(c, ctrl_results) for c in ctrl_pending]
        with open(cp, "a") as cpf:
            for i, (c, store) in enumerate(pending_all):
                row = replay_one(rpc, c, meta, grids, tx_cache, args.cost)
                row["tail_score"] = c["tail_score"]
                row["known_replay"] = c["mint"] in known_pre
                row["dev_past_created_count"] = c["dev_past_created_count"]
                row["same_dev_same_buyer_cluster"] = c["same_dev_same_buyer_cluster"]
                cpf.write(json.dumps(row) + "\n")
                cpf.flush()
                store[c["mint"]] = row
                if (i + 1) % 10 == 0 or i + 1 == len(pending_all):
                    print(f"  [{i+1}/{len(pending_all)}] rpc={rpc.calls} {c['cohort'][:8]} ok={row.get('ok')}", flush=True)
        rpc_calls = rpc.calls

    for c in lot_cohort:
        if c["mint"] in lot_results:
            lot_results[c["mint"]]["known_replay"] = c["mint"] in known_pre
    for c in ctrl_cohort:
        if c["mint"] in ctrl_results:
            ctrl_results[c["mint"]]["known_replay"] = c["mint"] in known_pre

    lot_rows = [lot_results[c["mint"]] for c in lot_cohort if c["mint"] in lot_results]
    ctrl_rows = [ctrl_results[c["mint"]] for c in ctrl_cohort if c["mint"] in ctrl_results]

    # old top5 from prior replays
    old_mints = load_old_top5_mints()
    old_rows = [prior[m] for m in old_mints if m in prior and prior[m].get("real_return_10U") is not None]
    for r in old_rows:
        r["cohort"] = "old_exante_top5"
        r["known_replay"] = True

    lot_eval = eval_splits(lot_rows, "lottery_v2")
    ctrl_eval = eval_splits(ctrl_rows, "matched_control")
    old_eval = eval_splits(old_rows, "old_top5")

    vd = verdict_lottery_v2(
        lot_eval["new_replay_only"],
        lot_eval["combined"],
        lot_eval["combined_without_kkk2"],
    )

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rules": {
                "lottery_v2": "TEST & cluster>=0.95 & dev<=50 & not_in_core; NO top56; rank=tail_score",
                "matched_control": f"random n={len(ctrl_df)} from TEST \\ lottery \\ core, seed={args.seed}",
                "core_frozen": True,
            },
            "n_candidates": len(lot_df),
            "n_control": len(ctrl_df),
            "kkk2_in_cohort": KKK2 in lot_mints,
            "rpc_calls": rpc_calls,
            "n_replayed_this_run": len(lot_pending) + len(ctrl_pending) if not args.dry_run else 0,
        },
        "verdict": vd,
        "lottery_v2": lot_eval,
        "matched_control": ctrl_eval,
        "old_exante_top5": old_eval,
        "cohort_mints": lot_df["mint"].tolist(),
        "results": lot_rows + ctrl_rows,
    }

    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # CSV + MD readout
    _write_csv(lot_rows, ctrl_rows, old_rows)
    _write_readout(out, lot_eval, ctrl_eval, old_eval, vd)
    print(f"Done verdict={vd['verdict']} rpc={rpc_calls} -> {args.output}")


def _write_csv(lot_rows, ctrl_rows, old_rows):
    path = OUT_DIR / "_pump_lottery_v2_replay.csv"
    fields = ["mint", "cohort", "known_replay", "ok", "tail_score", "real_return_10U", "dev_past_created_count"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in lot_rows + ctrl_rows + old_rows:
            w.writerow(r)


def _fmt_stats(s: dict) -> str:
    if not s.get("n"):
        return "n=0"
    return (
        f"n={s['n']} p100={s.get('p_gt_100',0):.1%} p300={s.get('p_gt_300',0):.1%} p1000={s.get('p_gt_1000',0):.1%} "
        f"med={s.get('median',0):+.1%} d1={s.get('drop_top1_mean') or 0:+.1%} max={s.get('max_return',0):+.1%} "
        f"fatal25={s.get('fatal_rate_25',0):.1%} streak={s.get('longest_losing_streak')} "
        f"cap0.25x={s.get('capital_final_0.25x',0):.3f}"
    )


def _write_readout(out, lot_eval, ctrl_eval, old_eval, vd):
    md = OUT_DIR / "_pump_lottery_v2_replay_readout.md"
    lines = [
        "# Lottery v2 右尾通道验证（最后一次）",
        "",
        f"**Verdict: {vd['verdict']}**" + (" ✓ STRONG" if vd.get("strong") else ""),
        "",
        "规则：`cluster>=0.95 & dev<=50 & ∉Core`，**无 top56**，按 tail_score 排序",
        f"候选 **{out['meta']['n_candidates']}** | KKK2 在 cohort：**{out['meta']['kkk2_in_cohort']}** | RPC **{out['meta']['rpc_calls']}**",
        "",
        "## Lottery v2",
        "",
        "| 子集 | n | p100 | p300 | p1000 | median | drop_top1 | drop_top3 | max | fatal25 | streak | cap@0.25x |",
        "|------|---|------|------|-------|--------|-----------|-----------|-----|---------|--------|-----------|",
    ]
    for key in ("combined", "new_replay_only", "known_replay", "combined_without_kkk2", "combined_with_kkk2"):
        s = lot_eval.get(key, {})
        if s.get("n"):
            lines.append(
                f"| {key} | {s['n']} | {s['p_gt_100']:.1%} | {s['p_gt_300']:.1%} | {s['p_gt_1000']:.1%} | "
                f"{s['median']:+.1%} | {s.get('drop_top1_mean') or 0:+.1%} | {s.get('drop_top3_mean') or 0:+.1%} | "
                f"{s['max_return']:+.1%} | {s['fatal_rate_25']:.1%} | {s['longest_losing_streak']} | {s['capital_final_0.25x']:.3f} |"
            )

    lines.extend(["", "## 三组对比（combined）", ""])
    for name, ev in [("Lottery v2", lot_eval), ("Matched control", ctrl_eval), ("Old ex-ante top5", old_eval)]:
        s = ev["combined"]
        lines.append(f"- **{name}**: {_fmt_stats(s)}")

    lines.extend(["", "## PASS 标准检查", ""])
    new = lot_eval["new_replay_only"]
    comb = lot_eval["combined"]
    checks = [
        ("new n>=30", new.get("n", 0) >= 30, new.get("n")),
        ("new p300>=3%", (new.get("p_gt_300") or 0) >= 0.03, f"{new.get('p_gt_300',0):.1%}"),
        ("new max>=+300%", (new.get("max_return") or 0) >= 3.0, f"{new.get('max_return',0):+.1%}"),
        ("new drop_top1>=-5%", (new.get("drop_top1_mean") or -99) >= -0.05, f"{new.get('drop_top1_mean') or 0:+.1%}"),
        ("combined p1000>=1% OR max>=1000%", (comb.get("p_gt_1000") or 0) >= 0.01 or (comb.get("max_return") or 0) >= 10, "ok"),
    ]
    for label, ok, val in checks:
        lines.append(f"- [{'x' if ok else ' '}] {label}: {val}")

    if vd.get("reasons"):
        lines.extend(["", "### 未达标原因"] + [f"- {x}" for x in vd["reasons"]])

    lines.extend([
        "",
        "## 定性",
        "- **Core**：冻结，不作 forward",
        "- **Lottery v2**：右尾通道候选验证",
        "- **Combined**：未证明可 forward",
        "",
        "Core 不要加戏。",
    ])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
