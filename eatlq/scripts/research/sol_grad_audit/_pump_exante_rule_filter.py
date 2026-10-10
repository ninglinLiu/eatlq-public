#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""旧 top5%（56 币）纯规则负过滤矩阵 — 链上 real 验证."""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from _pump_exante_filtered_rank import build_enhanced_rows, forensic_bucket

OUT_DIR = Path(__file__).resolve().parent
KKK2 = "KKK2KqnP7D7EjYBn33yRkhuJEW9rQjE2bFHHGUKpump"
MEGA_PROXY_COL = "same_dev_same_buyer_cluster"
MEGA_PROXY_MIN = 0.95


def load_old_top5() -> set[str]:
    p = OUT_DIR / "_pump_exante_top5_replay.json"
    return {
        r["mint"]
        for r in json.loads(p.read_text())["results"]
        if r.get("cohort") == "exante_top5"
    }


def mega_proxy(row: pd.Series) -> bool:
    return float(row.get(MEGA_PROXY_COL) or 0) >= MEGA_PROXY_MIN


def rule_defs() -> dict[str, Callable[[pd.Series], bool]]:
    """Return True = DROP this coin (fail filter)."""

    def r1(r):
        return float(r["buy_then_sell_fast_ratio"]) > 0.6

    def r2(r):
        return float(r["slope_buys_0_15"]) < 0

    def r3(r):
        return float(r["dev_past_created_count"]) > 200

    def r4(th: float):
        def fn(r):
            return float(r["bot_ratio_15"]) > th
        return fn

    def r5(r):
        return r1(r) or r2(r)

    def r6(r):
        return r1(r) or r2(r) or r3(r)

    def r7(r):
        return r1(r) and r2(r)

    def r8(r):
        return r1(r) or (r2(r) and float(r["bot_ratio_15"]) > 0.35)

    def r9(r):
        if mega_proxy(r):
            return False
        return r1(r) or r2(r)

    def r10(r):
        return float(r["buy_then_sell_fast_ratio"]) > 1.5

    def r13(r):
        return (
            float(r["buy_then_sell_fast_ratio"]) <= 0.6
            and float(r["slope_buys_0_15"]) < 0
            and float(r["dev_past_created_count"]) > 200
        )

    return {
        "R0_none": lambda r: False,
        "R1_fast_flip_gt_0.6": r1,
        "R2_slope_buys_lt_0": r2,
        "R3_dev_created_gt_200": r3,
        "R4_bot_ratio_gt_0.35": r4(0.35),
        "R4_bot_ratio_gt_0.40": r4(0.40),
        "R4_bot_ratio_gt_0.50": r4(0.50),
        "R5_R1_OR_R2": r5,
        "R6_R1_OR_R2_OR_R3": r6,
        "R7_R1_AND_R2": r7,
        "R8_R1_OR_R2bot35": r8,
        "R9_R1orR2_unless_mega": r9,
        "R10_fast_flip_gt_1.5": r10,
        "R13_flip0_slope_neg_dev200": r13,
    }


def portfolio_stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    srt = sorted(rets, reverse=True)
    n = len(rets)
    return {
        "n": n,
        "median": st.median(rets),
        "mean": sum(rets) / n,
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / n,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / n,
        "drop_top1_mean": st.mean(srt[1:]) if n > 1 else None,
    }


def eval_rule(sub: pd.DataFrame, drop_fn: Callable[[pd.Series], bool], rule_name: str) -> dict:
    sub = sub.copy()
    sub["drop"] = sub.apply(drop_fn, axis=1)
    kept = sub[~sub["drop"]]
    dropped = sub[sub["drop"]]

    def grp_counts(frame: pd.DataFrame) -> dict[str, int]:
        if frame.empty:
            return {}
        g = frame["real_return_10U"].apply(forensic_bucket)
        return g.value_counts().to_dict()

    all_g = grp_counts(sub)
    kept_g = grp_counts(kept)
    drop_g = grp_counts(dropped)

    rets = kept["real_return_10U"].tolist()
    srt = sorted(rets, reverse=True)
    n_fatal = all_g.get("fatal_loss", 0)
    n_winner = all_g.get("winner", 0) + all_g.get("mega_winner", 0)
    n_mega = all_g.get("mega_winner", 0)

    fatal_in_drop = drop_g.get("fatal_loss", 0)
    winners_kept = kept_g.get("winner", 0) + kept_g.get("mega_winner", 0)
    mega_kept = kept_g.get("mega_winner", 0)

    kkk_kept = KKK2 in set(kept["mint"])

    return {
        "rule": rule_name,
        "retain_n": len(kept),
        "drop_n": len(dropped),
        "fatal_total": n_fatal,
        "fatal_removed": fatal_in_drop,
        "fatal_removed_pct": fatal_in_drop / n_fatal if n_fatal else 0,
        "fatal_retained": kept_g.get("fatal_loss", 0),
        "winner_total": n_winner,
        "winner_kept": winners_kept,
        "mega_total": n_mega,
        "mega_kept": mega_kept,
        "kkk2_kept": kkk_kept,
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / len(rets) if rets else 0,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / len(rets) if rets else 0,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / len(rets) if rets else 0,
        "median": st.median(rets) if rets else None,
        "mean": sum(rets) / len(rets) if rets else None,
        "drop_top1_mean": st.mean(srt[1:]) if len(rets) > 1 else None,
        "drop_top3_mean": st.mean(srt[3:]) if len(rets) > 3 else None,
        "kept_mints": kept["mint"].tolist(),
        "dropped_fatal_mints": dropped[dropped["real_return_10U"].apply(forensic_bucket) == "fatal_loss"]["mint"].tolist(),
    }


def answer_questions(results: list[dict], sub: pd.DataFrame) -> dict:
    by = {r["rule"]: r for r in results}
    kkk = sub[sub["mint"] == KKK2].iloc[0]
    return {
        "Q1_fast_flip_R1": {
            "fatal_removed_pct": by["R1_fast_flip_gt_0.6"]["fatal_removed_pct"],
            "fatal_removed": by["R1_fast_flip_gt_0.6"]["fatal_removed"],
            "kkk2_kept": by["R1_fast_flip_gt_0.6"]["kkk2_kept"],
            "winner_kept": by["R1_fast_flip_gt_0.6"]["winner_kept"],
            "verdict": "KKK2 flip=1.0 > 0.6 → R1 会误杀 mega" if not by["R1_fast_flip_gt_0.6"]["kkk2_kept"] else "keeps KKK2",
        },
        "Q2_slope_R2": {
            "fatal_removed_pct": by["R2_slope_buys_lt_0"]["fatal_removed_pct"],
            "kkk2_kept": by["R2_slope_buys_lt_0"]["kkk2_kept"],
            "winner_kept": by["R2_slope_buys_lt_0"]["winner_kept"],
            "note": "KKK2/winner 的 slope 均为 -1；R2 无法区分 mega 与 fatal",
        },
        "Q3_dev_gt_200_R3": {
            "fatal_removed_pct": by["R3_dev_created_gt_200"]["fatal_removed_pct"],
            "kkk2_kept": by["R3_dev_created_gt_200"]["kkk2_kept"],
            "winner_kept": by["R3_dev_created_gt_200"]["winner_kept"],
            "kkk2_dev_count": float(kkk["dev_past_created_count"]),
            "verdict": "R3 保留 KKK2(dev=4) 但会杀掉 2/3 winner（dev>200）",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_exante_rule_filter.json"))
    args = ap.parse_args()

    replay_paths = [
        OUT_DIR / "_pump_exante_top5_replay.json",
        OUT_DIR / "_pump_tail_realgas_replay.json",
    ]
    rows = build_enhanced_rows(replay_paths)
    df = pd.DataFrame(rows)
    old_mints = load_old_top5()
    sub = df[df["mint"].isin(old_mints)].copy()
    sub["forensic"] = sub["real_return_10U"].apply(forensic_bucket)

    rules = rule_defs()
    results = [eval_rule(sub, fn, name) for name, fn in rules.items()]
    qa = answer_questions(results, sub)

    # best rules by composite: kkk kept + median + fatal removal
    scored = []
    for r in results:
        if r["rule"] == "R0_none":
            continue
        score = 0
        if r["kkk2_kept"]:
            score += 3
        if r["winner_kept"] >= 3:
            score += 2
        if r.get("median") is not None and r["median"] >= -0.05:
            score += 2
        if r.get("drop_top1_mean") is not None and r["drop_top1_mean"] >= -0.05:
            score += 1
        score += r["fatal_removed_pct"]
        scored.append({**r, "_score": score})
    scored.sort(key=lambda x: x["_score"], reverse=True)

    # Core / Lottery basket simulation
    core_keep = sub[sub["dev_past_created_count"] <= 200]
    lottery = sub[(sub["same_dev_same_buyer_cluster"] >= MEGA_PROXY_MIN) & (sub["dev_past_created_count"] <= 50)]
    baskets = {
        "core_dev_le_200": portfolio_stats(core_keep["real_return_10U"].tolist()) if len(core_keep) else {},
        "lottery_cluster_dev_le_50": portfolio_stats(lottery["real_return_10U"].tolist()) if len(lottery) else {},
    }

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "n_old_top5": len(sub),
            "mega_proxy": f"{MEGA_PROXY_COL} >= {MEGA_PROXY_MIN}",
            "kkk2_profile": sub[sub["mint"] == KKK2].iloc[0][[
                "buy_then_sell_fast_ratio", "slope_buys_0_15", "dev_past_created_count",
                "bot_ratio_15", "same_dev_same_buyer_cluster", "real_return_10U",
            ]].to_dict(),
        },
        "forensic_counts": sub["forensic"].value_counts().to_dict(),
        "questions": qa,
        "baskets": baskets,
        "rules": results,
        "ranked_rules": [{k: v for k, v in r.items() if k not in ("kept_mints", "dropped_fatal_mints")} for r in scored[:5]],
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    csv_path = OUT_DIR / "_pump_exante_rule_filter.csv"
    fields = [
        "rule", "retain_n", "drop_n", "fatal_removed", "fatal_removed_pct", "fatal_retained",
        "winner_kept", "mega_kept", "kkk2_kept", "p_gt_100", "p_gt_300", "p_gt_1000",
        "median", "mean", "drop_top1_mean", "drop_top3_mean",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    md = OUT_DIR / "_pump_exante_rule_filter_readout.md"
    lines = [
        "# 旧 top5% 纯规则过滤矩阵（56 币，链上 real）",
        "",
        f"mega_proxy（R9）：`{MEGA_PROXY_COL} >= {MEGA_PROXY_MIN}`",
        "",
        "## 三个核心问题",
        "",
        f"**Q1 fast_flip>0.6（R1）**：删掉 fatal {qa['Q1_fast_flip_R1']['fatal_removed']}/24 ({qa['Q1_fast_flip_R1']['fatal_removed_pct']:.0%}) | KKK2 kept={qa['Q1_fast_flip_R1']['kkk2_kept']} | winner kept={qa['Q1_fast_flip_R1']['winner_kept']}",
        f"**Q2 slope<0（R2）**：fatal removed {qa['Q2_slope_R2']['fatal_removed_pct']:.0%} | KKK2 kept={qa['Q2_slope_R2']['kkk2_kept']} | winner kept={qa['Q2_slope_R2']['winner_kept']}",
        f"**Q3 dev>200（R3）**：fatal removed {qa['Q3_dev_gt_200_R3']['fatal_removed_pct']:.0%} | KKK2 kept={qa['Q3_dev_gt_200_R3']['kkk2_kept']} (dev={qa['Q3_dev_gt_200_R3']['kkk2_dev_count']:.0f}) | winner kept={qa['Q3_dev_gt_200_R3']['winner_kept']}/3",
        "",
        "## 规则矩阵",
        "",
        "| Rule | retain | fatal↓ | winner | mega | KKK2 | p100 | p1000 | median | drop_top1 |",
        "|------|--------|--------|--------|------|------|------|-------|--------|-----------|",
    ]
    baseline = next(r for r in results if r["rule"] == "R0_none")
    lines.append(
        f"| baseline | {baseline['retain_n']} | 0 | {baseline['winner_kept']} | {baseline['mega_kept']} | ✓ | "
        f"{baseline['p_gt_100']:.1%} | {baseline['p_gt_1000']:.1%} | {baseline['median']:+.1%} | {baseline['drop_top1_mean']:+.1%} |"
    )
    for r in results:
        if r["rule"] == "R0_none":
            continue
        med = f"{r['median']:+.1%}" if r["median"] is not None else "-"
        d1 = f"{r['drop_top1_mean']:+.1%}" if r["drop_top1_mean"] is not None else "-"
        lines.append(
            f"| {r['rule']} | {r['retain_n']} | {r['fatal_removed']}/24 | {r['winner_kept']}/3 | "
            f"{r['mega_kept']}/1 | {'✓' if r['kkk2_kept'] else '✗'} | {r['p_gt_100']:.1%} | "
            f"{r['p_gt_1000']:.1%} | {med} | {d1} |"
        )

    best = scored[0] if scored else None
    lines.extend([
        "",
        "## 结论",
        "",
        "- **R1/R2 单独不可用**：KKK2 与全部 winner 的 fast_flip≥1、slope=−1，与 fatal 重叠",
        "- **R3 保留 KKK2 但误杀 2 个 winner**（dev>200）；高频 dev 不是纯负信号",
        "- **R9（R1|R2 + mega_proxy 豁免）**：保留 KKK2 + 全部 winner，但 fatal 删除有限（cluster=1 的 fatal 也被豁免）",
        "",
        f"**综合最高分规则**：{best['rule'] if best else 'n/a'}",
        "",
        "### 补充规则（探索）",
        "- **R10 flip>1.5**：保留 KKK2(flip=1.0)，删极端刷量 fatal；p100≈4.3%，drop_top1≈−13%",
        "- **R13 flip≤0.6 & slope<0 & dev>200**：精准 fatal 画像，保留全部 winner+KKK2，但只删 3/24 fatal",
        "",
        "### Core / Lottery 拆分（旧 top5 内）",
        f"- Core（dev≤200, n={baskets['core_dev_le_200'].get('n')}）：median={baskets['core_dev_le_200'].get('median'):+.1%}，p1000={baskets['core_dev_le_200'].get('p_gt_1000',0):.1%}",
        f"- Lottery（cluster≥0.95 & dev≤50, n={baskets['lottery_cluster_dev_le_50'].get('n')}）：median={baskets['lottery_cluster_dev_le_50'].get('median'):+.1%}，p1000={baskets['lottery_cluster_dev_le_50'].get('p_gt_1000',0):.1%}",
        "",
        "**R9 mega_proxy 过宽**：旧 top5 里 21/56 满足 cluster≥0.95，fatal 也大量 cluster=1 → R9 等于不过滤",
        "",
        "详见 `_pump_exante_rule_filter.csv`",
    ])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} n={len(sub)} rules={len(results)}")


if __name__ == "__main__":
    main()
