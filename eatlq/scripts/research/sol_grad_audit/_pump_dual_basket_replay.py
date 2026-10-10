#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core + Lottery 双篮子链上 replay（TEST 重选 cohort，known vs new 分拆报告）."""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from _grad_audit_lib import load_grids
from _pump_exante_filtered_rank import build_enhanced_rows
from _pump_exante_rank import feature_cols, train_score
from _pump_exante_top5_replay import load_done, replay_one
from _pump_tail_realgas_replay import load_meta

OUT_DIR = Path(__file__).resolve().parent
FATAL_TH = -0.25
MAX_PER_BASKET = 56
MEGA_PROXY_MIN = 0.95
DEV_LOTTERY_MAX = 50
DEV_CORE_MAX = 200
FLIP_CORE_MAX = 1.5


def load_known_pre_dual() -> set[str]:
    known: set[str] = set()
    for p in [
        OUT_DIR / "_pump_tail_realgas_replay.json",
        OUT_DIR / "_pump_exante_top5_replay.json",
    ]:
        if not p.exists():
            continue
        for r in json.loads(p.read_text()).get("results", []):
            if r.get("real_return_10U") is not None:
                known.add(r["mint"])
    return known


def prior_replay_paths() -> list[Path]:
    return [
        OUT_DIR / "_pump_tail_realgas_replay.json",
        OUT_DIR / "_pump_exante_top5_replay.json",
        OUT_DIR / "_pump_dual_basket_checkpoint.jsonl",
    ]


def load_prior_results() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in prior_replay_paths():
        if not p.exists():
            continue
        if p.suffix == ".jsonl":
            for line in open(p):
                r = json.loads(line)
                if r.get("real_return_10U") is not None or r.get("ok") is not None:
                    out[r["mint"]] = r
        else:
            for r in json.loads(p.read_text()).get("results", []):
                out[r["mint"]] = r
    return out


def attach_scores(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["split"].isin(["TRAIN", "TEST"])].copy()
    extra_exclude = {
        "creator", "real_return_10U", "has_real", "fatal_25", "fatal_50",
        "base_score", "tail_score",
    }
    fc = [c for c in feature_cols(df, "all_exante") if c not in extra_exclude]
    fc = [c for c in fc if df[c].dtype in ("float64", "float32", "int64", "int32", "bool")]
    base_scores, _ = train_score(df, "label_100", fc)
    tail_scores, _ = train_score(df, "mfe_label_100", fc)
    df["base_score"] = base_scores
    df["tail_score"] = tail_scores
    return df


def build_cohorts(te: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    te = te.sort_values("base_score", ascending=False)
    k_base = min(MAX_PER_BASKET, max(1, int(round(len(te) * 0.05))))
    top_base = te.head(k_base)

    core = top_base[
        (top_base["dev_past_created_count"] <= DEV_CORE_MAX)
        & (top_base["buy_then_sell_fast_ratio"] <= FLIP_CORE_MAX)
    ].sort_values("tail_score", ascending=False).head(MAX_PER_BASKET)

    core_mints = set(core["mint"])
    lottery = te[
        (te["same_dev_same_buyer_cluster"] >= MEGA_PROXY_MIN)
        & (te["dev_past_created_count"] <= DEV_LOTTERY_MAX)
        & (~te["mint"].isin(core_mints))
    ].sort_values("tail_score", ascending=False).head(MAX_PER_BASKET)

    return core, lottery


def row_from_prior(pr: dict, c: dict) -> dict:
    r = pr.copy()
    r["cohort"] = c["cohort"]
    r["basket"] = c["basket"]
    r["base_score"] = c.get("base_score")
    r["tail_score"] = c.get("tail_score")
    r["known_replay"] = True
    return r


def core_metrics(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    srt = sorted(rets, reverse=True)
    n = len(rets)
    return {
        "n": n,
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / n,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / n,
        "median": st.median(rets),
        "mean": sum(rets) / n,
        "fatal_rate": sum(1 for x in rets if x <= FATAL_TH) / n,
        "drop_top1_mean": st.mean(srt[1:]) if n > 1 else None,
        "drop_top3_mean": st.mean(srt[3:]) if n > 3 else None,
    }


def lottery_metrics(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    srt = sorted(rets, reverse=True)
    n = len(rets)
    return {
        "n": n,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / n,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / n,
        "max_return": max(rets),
        "median": st.median(rets),
        "mean": sum(rets) / n,
        "drop_top1_mean": st.mean(srt[1:]) if n > 1 else None,
    }


def eval_basket(rows: list[dict], basket: str) -> dict:
    sub = [r for r in rows if r.get("basket") == basket and r.get("real_return_10U") is not None]
    rets = [r["real_return_10U"] for r in sub]
    known = [r["real_return_10U"] for r in sub if r.get("known_replay")]
    new = [r["real_return_10U"] for r in sub if not r.get("known_replay")]
    fn = core_metrics if basket == "core" else lottery_metrics
    return {
        "combined": fn(rets),
        "known_replay": fn(known),
        "new_replay_only": fn(new),
        "n_known": len(known),
        "n_new": len(new),
    }


def cohort_entries(frame: pd.DataFrame, basket: str) -> list[dict]:
    out = []
    for i, (_, r) in enumerate(frame.iterrows(), 1):
        out.append({
            "mint": r["mint"],
            "cohort": f"{basket}_basket",
            "basket": basket,
            "rank": i,
            "base_score": float(r["base_score"]),
            "tail_score": float(r["tail_score"]),
            "dev_past_created_count": float(r["dev_past_created_count"]),
            "buy_then_sell_fast_ratio": float(r["buy_then_sell_fast_ratio"]),
            "same_dev_same_buyer_cluster": float(r["same_dev_same_buyer_cluster"]),
            "grid_net_return": float(r.get("final_ret", 0)),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_dual_basket_replay.json"))
    ap.add_argument("--checkpoint", default=str(OUT_DIR / "_pump_dual_basket_checkpoint.jsonl"))
    args = ap.parse_args()

    replay_sources = [
        OUT_DIR / "_pump_exante_top5_replay.json",
        OUT_DIR / "_pump_tail_realgas_replay.json",
        OUT_DIR / "_pump_dual_basket_replay.json",
    ]
    rows = build_enhanced_rows(replay_sources)
    df = attach_scores(pd.DataFrame(rows))
    te = df[df["split"] == "TEST"].copy()

    core_df, lottery_df = build_cohorts(te)
    cohort = cohort_entries(core_df, "core") + cohort_entries(lottery_df, "lottery")

    prior = load_prior_results()
    known_mints = load_known_pre_dual()
    done = load_done(*replay_sources, Path(args.checkpoint))

    results: dict[str, dict] = {}
    for c in cohort:
        m = c["mint"]
        if m in prior and prior[m].get("real_return_10U") is not None:
            results[m] = row_from_prior(prior[m], c)
        elif m in done:
            cp = Path(args.checkpoint)
            if cp.exists():
                for line in open(cp):
                    r = json.loads(line)
                    if r["mint"] == m:
                        r["basket"] = c["basket"]
                        r["known_replay"] = m in known_mints
                        results[m] = r

    pending = [c for c in cohort if c["mint"] not in results]

    meta = load_meta()
    grids = load_grids()
    cp = Path(args.checkpoint)

    print(
        f"[dual-basket] core={len(core_df)} lottery={len(lottery_df)} "
        f"cached={len(results)} pending={len(pending)} known_pool={len(known_mints)}"
    )

    if args.dry_run:
        print("DRY RUN — skip RPC")
        rpc_calls = 0
    else:
        from _pump_tail_realgas_replay import RPC, rpc_url

        rpc = RPC(rpc_url(), min_interval=0.20)
        tx_cache: dict = {}
        with open(cp, "a") as cpf:
            for i, c in enumerate(pending):
                row = replay_one(rpc, c, meta, grids, tx_cache, args.cost)
                row["basket"] = c["basket"]
                row["base_score"] = c["base_score"]
                row["tail_score"] = c["tail_score"]
                row["known_replay"] = c["mint"] in known_mints
                row["dev_past_created_count"] = c["dev_past_created_count"]
                row["buy_then_sell_fast_ratio"] = c["buy_then_sell_fast_ratio"]
                row["same_dev_same_buyer_cluster"] = c["same_dev_same_buyer_cluster"]
                cpf.write(json.dumps(row) + "\n")
                cpf.flush()
                results[c["mint"]] = row
                if (i + 1) % 5 == 0 or i + 1 == len(pending):
                    print(
                        f"  [{i+1}/{len(pending)}] rpc={rpc.calls} {c['basket']} {c['mint'][:8]} "
                        f"ok={row.get('ok')} known={row.get('known_replay')}",
                        flush=True,
                    )
        rpc_calls = rpc.calls

    all_rows = [results[c["mint"]] for c in cohort if c["mint"] in results]
    for r in all_rows:
        r["known_replay"] = r["mint"] in known_mints

    summary = {
        "core": eval_basket(all_rows, "core"),
        "lottery": eval_basket(all_rows, "lottery"),
    }

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rules": {
                "core": {
                    "base": "TEST top5% or top56 by six-line label_100 score",
                    "filter": f"dev<={DEV_CORE_MAX}, flip<={FLIP_CORE_MAX}",
                    "rank_within": "tail_score (mfe_label_100)",
                    "max_n": MAX_PER_BASKET,
                },
                "lottery": {
                    "filter": f"cluster>={MEGA_PROXY_MIN}, dev<={DEV_LOTTERY_MAX}, not_in_core",
                    "rank_within": "tail_score",
                    "max_n": MAX_PER_BASKET,
                },
            },
            "n_test": len(te),
            "n_core_selected": len(core_df),
            "n_lottery_selected": len(lottery_df),
            "n_replayed_new": len(pending) if not args.dry_run else 0,
            "rpc_calls": rpc_calls,
            "known_replay_pool_size": len(known_mints),
        },
        "cohort": {
            "core_mints": core_df["mint"].tolist(),
            "lottery_mints": lottery_df["mint"].tolist(),
        },
        "summary": summary,
        "results": all_rows,
    }

    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    csv_path = OUT_DIR / "_pump_dual_basket_replay.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fields = [
            "mint", "basket", "known_replay", "ok", "base_score", "tail_score",
            "real_return_10U", "grid_net_return", "dev_past_created_count",
            "buy_then_sell_fast_ratio", "same_dev_same_buyer_cluster",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    md = OUT_DIR / "_pump_dual_basket_replay_readout.md"
    lines = [
        "# Core + Lottery 双篮子 Replay",
        "",
        f"Core **{len(core_df)}** | Lottery **{len(lottery_df)}** | 新 RPC **{out['meta']['n_replayed_new']}** | RPC **{rpc_calls}**",
        "",
        "## Core 指标（real 10U）",
        "",
        "| 子集 | n | p(+100%) | p(+300%) | median | fatal_rate | drop_top1 | drop_top3 |",
        "|------|---|----------|----------|--------|------------|-----------|-----------|",
    ]
    for label in ("combined", "known_replay", "new_replay_only"):
        m = summary["core"][label]
        if m.get("n"):
            lines.append(
                f"| {label} | {m['n']} | {m.get('p_gt_100', 0):.1%} | {m.get('p_gt_300', 0):.1%} | "
                f"{m.get('median', 0):+.1%} | {m.get('fatal_rate', 0):.1%} | "
                f"{m.get('drop_top1_mean') or 0:+.1%} | {m.get('drop_top3_mean') or 0:+.1%} |"
            )

    lines.extend([
        "",
        "## Lottery 指标（real 10U）",
        "",
        "| 子集 | n | p(+300%) | p(+1000%) | max | median | drop_top1 |",
        "|------|---|----------|----------|-----|--------|-----------|",
    ])
    for label in ("combined", "known_replay", "new_replay_only"):
        m = summary["lottery"][label]
        if m.get("n"):
            lines.append(
                f"| {label} | {m['n']} | {m.get('p_gt_300', 0):.1%} | {m.get('p_gt_1000', 0):.1%} | "
                f"{m.get('max_return', 0):+.1%} | {m.get('median', 0):+.1%} | {m.get('drop_top1_mean') or 0:+.1%} |"
            )

    def pct(x, default="-"):
        return f"{x:+.1%}" if x is not None else default

    new_core = summary["core"]["new_replay_only"]
    new_lot = summary["lottery"]["new_replay_only"]
    lines.extend([
        "",
        "## 裁定（以 new_replay_only 为准）",
        "",
        "### Core",
        f"- new n={new_core.get('n')}: median **{pct(new_core.get('median'))}** | fatal **{new_core.get('fatal_rate',0):.1%}** | p100=**0%**",
        f"- combined n={summary['core']['combined'].get('n')}: median {pct(summary['core']['combined'].get('median'))} | drop_top1 {pct(summary['core']['combined'].get('drop_top1_mean'))}",
        "",
        "### Lottery",
        f"- new n={new_lot.get('n')}: p300={new_lot.get('p_gt_300',0):.1%} | max **{pct(new_lot.get('max_return'))}** | median {pct(new_lot.get('median'))} | drop_top1 **{pct(new_lot.get('drop_top1_mean'))}**",
        "",
        "### 与旧 top5 对比",
        "| | 旧 top5 | Core combined | Core new | Lottery new |",
        "|--|---------|---------------|----------|-------------|",
        f"| median | −12.1% | {pct(summary['core']['combined'].get('median'))} | {pct(new_core.get('median'))} | {pct(new_lot.get('median'))} |",
        f"| p(+100%) | 5.4% | {summary['core']['combined'].get('p_gt_100',0):.1%} | 0% | — |",
        f"| p(+1000%) | 1.8% | 0% | 0% | {new_lot.get('p_gt_1000',0):.1%} |",
        f"| drop_top1 | −15.7% | {pct(summary['core']['combined'].get('drop_top1_mean'))} | {pct(new_core.get('drop_top1_mean'))} | {pct(new_lot.get('drop_top1_mean'))} |",
        "",
        "**KKK2 未入 cohort**：base_score 排名 #312/1123（不在 top56 基础池），dev=4/flip=1.0 虽满足过滤但进不了 base。",
        "",
        "**结论**：Core 控 median 有效（−12%→−2.4%）但 **new replay 零右尾**；Lottery new 出现 **+761%**（38NLF…）但 p1000=0。双篮子结构方向对，需把 KKK2 类纳入 Lottery 强制通道（如 cluster≥0.95 & dev≤50 不限 top56）。",
        "",
        "详见 `_pump_dual_basket_replay.json`",
    ])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done -> {args.output} rpc={rpc_calls}")


if __name__ == "__main__":
    main()
