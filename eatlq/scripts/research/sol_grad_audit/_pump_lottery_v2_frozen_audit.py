#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lottery v2 冻结规则扩窗审计（Step A–D）
规则冻结：cluster>=0.95, dev<=50, T+15→T+90, 10U
按日报告 + matched control；不重训、不调参。
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from _grad_audit_lib import load_grids
from _pump_exante_filtered_rank import build_enhanced_rows
from _pump_exante_top5_replay import load_done, replay_one
from _pump_tail_realgas_replay import load_meta

OUT_DIR = Path(__file__).resolve().parent

# ── Step A: FROZEN ──
CLUSTER_MIN = 0.95
DEV_MAX = 50.0
ENTRY_S, EXIT_S = 15, 90
NOTIONAL = "10U"
MEGA_TH = 10.0


def frozen_lottery_row(r: pd.Series) -> bool:
    return (
        float(r["same_dev_same_buyer_cluster"]) >= CLUSTER_MIN
        and float(r["dev_past_created_count"]) <= DEV_MAX
    )


def activity_score(r: pd.Series) -> float:
    return float(r.get("b0_buys", 0) + r.get("b1_buys", 0) + r.get("b2_buys", 0))


def replay_sources() -> list[Path]:
    return sorted(OUT_DIR.glob("_pump_*replay.json"))


def load_all_prior() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in replay_sources():
        if p.name == "_pump_lottery_v2_frozen_audit.json":
            continue
        try:
            for r in json.loads(p.read_text()).get("results", []):
                if r.get("real_return_10U") is not None:
                    out[r["mint"]] = r
        except Exception:
            pass
    cp = OUT_DIR / "_pump_lottery_v2_frozen_checkpoint.jsonl"
    if cp.exists():
        for line in open(cp):
            r = json.loads(line)
            if r.get("real_return_10U") is not None:
                out[r["mint"]] = r
    return out


def match_control_day(lot_d: pd.DataFrame, pool_d: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Same-day NN match on activity score, without replacement."""
    if pool_d.empty or lot_d.empty:
        return pool_d.iloc[:0]
    rng = random.Random(seed)
    pool = pool_d.copy()
    pool["_act"] = pool.apply(activity_score, axis=1)
    lot = lot_d.copy()
    lot["_act"] = lot.apply(activity_score, axis=1)
    used = set()
    picks = []
    for _, row in lot.sort_values("_act").iterrows():
        cands = pool[~pool["mint"].isin(used)]
        if cands.empty:
            break
        idx = (cands["_act"] - row["_act"]).abs().idxmin()
        used.add(pool.loc[idx, "mint"])
        picks.append(idx)
    if not picks:
        return pool_d.iloc[:0]
    return pool.loc[picks].drop(columns=["_act"], errors="ignore")


def portfolio_row(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    srt = sorted(rets, reverse=True)
    n = len(rets)
    return {
        "n": n,
        "p100": sum(1 for x in rets if x >= 1.0) / n,
        "p300": sum(1 for x in rets if x >= 3.0) / n,
        "p1000": sum(1 for x in rets if x >= 10.0) / n,
        "median": st.median(rets),
        "mean": sum(rets) / n,
        "max": max(rets),
        "drop_top1": st.mean(srt[1:]) if n > 1 else None,
    }


def daily_table(rows: list[dict], cohort: str) -> dict[str, dict]:
    by_day: dict[str, list[float]] = {}
    for r in rows:
        if r.get("cohort") != cohort:
            continue
        rr = r.get("real_return_10U")
        if rr is None:
            continue
        d = r.get("day") or "unknown"
        by_day.setdefault(d, []).append(rr)
    return {d: portfolio_row(v) for d, v in sorted(by_day.items())}


def cohort_entry(row: pd.Series, cohort: str) -> dict:
    return {
        "mint": row["mint"],
        "day": row["day"],
        "cohort": cohort,
        "activity": activity_score(row),
        "dev_past_created_count": float(row["dev_past_created_count"]),
        "same_dev_same_buyer_cluster": float(row["same_dev_same_buyer_cluster"]),
        "split": row.get("split"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-replay", type=int, default=500)
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_lottery_v2_frozen_audit.json"))
    ap.add_argument("--checkpoint", default=str(OUT_DIR / "_pump_lottery_v2_frozen_checkpoint.jsonl"))
    args = ap.parse_args()

    df = pd.DataFrame(build_enhanced_rows(replay_sources()))
    # all days with early_buys + grid (not TEST-only)
    lot_df = df[df.apply(frozen_lottery_row, axis=1)].copy()

    ctrl_parts = []
    for day, lot_d in lot_df.groupby("day"):
        pool_d = df[(df["day"] == day) & ~df.apply(frozen_lottery_row, axis=1)]
        ctrl_parts.append(match_control_day(lot_d, pool_d, seed=hash(day) % 10000 + args.seed))
    ctrl_df = pd.concat(ctrl_parts, ignore_index=True) if ctrl_parts else pd.DataFrame()

    lot_cohort = [cohort_entry(r, "lottery_v2") for _, r in lot_df.iterrows()]
    ctrl_cohort = [cohort_entry(r, "matched_control") for _, r in ctrl_df.iterrows()]

    prior = load_all_prior()
    done = load_done(*replay_sources(), Path(args.checkpoint))

    results: dict[str, dict] = {}
    for c in lot_cohort + ctrl_cohort:
        m = c["mint"]
        if m in prior:
            r = prior[m].copy()
            r.update({"cohort": c["cohort"], "day": c["day"]})
            results[m] = r

    pending = [c for c in lot_cohort + ctrl_cohort if c["mint"] not in results]
    # lottery pending first
    pending.sort(key=lambda x: (0 if x["cohort"] == "lottery_v2" else 1, x["mint"]))
    pending = pending[: args.max_replay]

    print(
        f"[frozen] lottery={len(lot_df)} control={len(ctrl_df)} "
        f"cached={len(results)} pending={len(pending)} days={sorted(lot_df['day'].unique())}"
    )

    rpc_calls = 0
    if not args.dry_run and pending:
        from _pump_tail_realgas_replay import RPC, rpc_url

        meta = load_meta()
        grids = load_grids()
        rpc = RPC(rpc_url(), min_interval=0.20)
        tx_cache: dict = {}
        cp = Path(args.checkpoint)
        with open(cp, "a") as cpf:
            for i, c in enumerate(pending):
                row = replay_one(rpc, {**c, "score": 0}, meta, grids, tx_cache, args.cost)
                row["day"] = c["day"]
                row["activity"] = c["activity"]
                cpf.write(json.dumps(row) + "\n")
                cpf.flush()
                results[c["mint"]] = row
                if (i + 1) % 20 == 0 or i + 1 == len(pending):
                    print(f"  [{i+1}/{len(pending)}] rpc={rpc.calls} {c['day']} {c['cohort'][:6]}", flush=True)
        rpc_calls = rpc.calls

    all_rows = []
    for c in lot_cohort + ctrl_cohort:
        if c["mint"] in results:
            all_rows.append(results[c["mint"]])

    lot_rows = [r for r in all_rows if r.get("cohort") == "lottery_v2"]
    ctrl_rows = [r for r in all_rows if r.get("cohort") == "matched_control"]

    daily_lot = daily_table(all_rows, "lottery_v2")
    daily_ctrl = daily_table(all_rows, "matched_control")

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rules_frozen": {
                "cluster_min": CLUSTER_MIN,
                "dev_max": DEV_MAX,
                "entry_s": ENTRY_S,
                "exit_s": EXIT_S,
                "notional": NOTIONAL,
                "note": "NO tuning; no top56; no core filter; all available days",
            },
            "data_coverage": {
                "days_available": sorted(lot_df["day"].unique().tolist()),
                "days_missing_vs_request": ["06-10", "06-16", "06-17", "06-03/04 sparse"],
                "n_lottery": len(lot_df),
                "n_control": len(ctrl_df),
                "target_n1000": "NOT MET — max lottery n=455 in cache; need more chain data",
            },
            "rpc_calls": rpc_calls,
            "n_replayed_this_run": len(pending) if not args.dry_run else 0,
        },
        "aggregate": {
            "lottery_v2": portfolio_row([r["real_return_10U"] for r in lot_rows if r.get("real_return_10U") is not None]),
            "matched_control": portfolio_row([r["real_return_10U"] for r in ctrl_rows if r.get("real_return_10U") is not None]),
        },
        "daily_lottery_v2": daily_lot,
        "daily_matched_control": daily_ctrl,
        "results": all_rows,
    }

    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # daily CSV
    csv_path = OUT_DIR / "_pump_lottery_v2_frozen_daily.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "cohort", "n", "p100", "p300", "p1000", "median", "max", "drop_top1"])
        for day in sorted(set(list(daily_lot.keys()) + list(daily_ctrl.keys()))):
            for label, tbl in [("lottery_v2", daily_lot), ("matched_control", daily_ctrl)]:
                s = tbl.get(day, {"n": 0})
                if s.get("n"):
                    w.writerow([
                        day, label, s["n"], f"{s['p100']:.4f}", f"{s['p300']:.4f}", f"{s['p1000']:.4f}",
                        f"{s['median']:.6f}", f"{s['max']:.6f}",
                        f"{s.get('drop_top1') or 0:.6f}",
                    ])

    md = OUT_DIR / "_pump_lottery_v2_frozen_readout.md"
    lines = [
        "# Lottery v2 冻结规则扩窗审计",
        "",
        "**规则冻结**：cluster≥0.95 | dev≤50 | T+15→T+90 | 10U",
        "",
        f"Lottery **n={len(lot_df)}** | Control **n={len(ctrl_df)}** | RPC **{rpc_calls}**",
        "",
        "> ⚠️ 缓存数据仅覆盖 **06-11~06-15**（5天），Lottery n=455 **未达 1000** 目标。",
        "",
        "## 按日对比",
        "",
        "| 日期 | cohort | n | p100 | p300 | p1000 | median | max | drop_top1 |",
        "|------|--------|---|------|------|-------|--------|-----|-----------|",
    ]
    for day in sorted(set(list(daily_lot.keys()) + list(daily_ctrl.keys()))):
        for label, tbl in [("lottery_v2", daily_lot), ("matched_control", daily_ctrl)]:
            s = tbl.get(day, {})
            if s.get("n"):
                lines.append(
                    f"| {day} | {label} | {s['n']} | {s['p100']:.1%} | {s['p300']:.1%} | {s['p1000']:.1%} | "
                    f"{s['median']:+.1%} | {s['max']:+.1%} | {s.get('drop_top1') or 0:+.1%} |"
                )
    agg = out["aggregate"]
    lines.extend([
        "",
        "## 全样本 aggregate",
        "",
        f"- Lottery: {agg['lottery_v2']}",
        f"- Control: {agg['matched_control']}",
        "",
        "## Step D 状态",
        f"- Lottery n={len(lot_df)} / 1000 目标：{'未达标' if len(lot_df)<1000 else '达标'}",
        "",
        "需新数据采集（06-10/06-16+）才能扩至 1000+。",
    ])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done -> {args.output}")


if __name__ == "__main__":
    main()
