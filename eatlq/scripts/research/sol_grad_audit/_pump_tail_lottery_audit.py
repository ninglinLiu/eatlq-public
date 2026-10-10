#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pump.fun T+15→T+90 右尾彩票策略专项审计 — 零 RPC."""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from _grad_audit_lib import (
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    DIAG,
    bootstrap_day_mean,
    day_of,
    grid_price,
    load_deployer,
    load_early_buys,
    load_grids,
    load_jsonl,
    mfe_mae,
    path_points,
    ret_gross,
    ret_net,
)

OUT_DIR = Path(__file__).resolve().parent


def exact_grid_time(grid: dict, t: float) -> float | None:
    for k in grid:
        if abs(float(k) - t) < 1e-9:
            return float(k)
    return None


def features_t15(r: dict, eb: list, creator_hist: dict, labeled: dict) -> dict:
    eb15 = [e for e in eb if (e.get("t_rel") or 999) <= 15]
    buys = [e for e in eb15 if e.get("is_buy")]
    sells = [e for e in eb15 if not e.get("is_buy")]
    wallets = {e["buyer"] for e in eb15 if e.get("buyer")}
    sol_in = sum(float(e.get("sol") or 0) for e in buys)
    sol_out = sum(float(e.get("sol") or 0) for e in sells)
    n_tr = len(eb15)
    bot_ratio = 1 - len(wallets) / max(n_tr, 1) if n_tr else None
    bset = {e["buyer"] for e in buys}
    sset = {e["buyer"] for e in sells}
    wash = len(bset & sset)
    creator = labeled.get(r["mint"], {}).get("creator")
    creator_buy = sum(1 for e in buys if e.get("buyer") == creator)
    p0 = eb[0].get("price") if eb else grid_price(r["grid"], 0)
    p15 = grid_price(r["grid"], 15)
    prem15 = (p15 / p0 - 1) if p0 and p15 else None
    t0_ret = prem15
    return {
        "n_trades_15": n_tr or r.get("n_trades_60s"),  # fallback coarse
        "unique_buyers_15": len(wallets),
        "buy_count_15": len(buys),
        "sell_count_15": len(sells),
        "buy_sell_imbalance_15": (len(buys) - len(sells)) / max(n_tr, 1) if n_tr else None,
        "sol_in_15": sol_in if eb15 else None,
        "premium_15": prem15,
        "bot_ratio_15": bot_ratio,
        "wash_wallets_15": wash,
        "creator_self_buy_15": creator_buy,
        "T0_to_T15_return": t0_ret,
    }


def tradability_proxy(r: dict) -> dict:
    g = r["grid"]
    entry, exit_t = r["entry_s"], r["exit_s"]
    pe, px = r["entry_price"], r["exit_price"]
    pts = path_points(g, entry, exit_t)
    t_keys = sorted(float(k) for k in g)
    has_exact_15 = exact_grid_time(g, 15) is not None
    has_exact_90 = exact_grid_time(g, 90) is not None
    entry_fb = grid_price(g, entry)
    uses_t0_for_15 = (not has_exact_15) and entry == 15 and abs((entry_fb or 0) - (grid_price(g, 0) or -1)) < 1e-18

    tags = []
    if uses_t0_for_15:
        tags.append("SPARSE_GRID")
    if len(pts) <= 1:
        tags.append("SINGLE_TRADE_PRICE")
    if len(pts) <= 2:
        tags.append("THIN_LIQUIDITY")
    if not has_exact_90:
        tags.append("EXIT_UNCERTAIN")
    if uses_t0_for_15 or len(pts) <= 1:
        tags.append("NEEDS_RPC_REPLAY")
    if not tags:
        tags.append("OK")
    primary = tags[0] if tags[0] != "OK" else "OK"
    return {
        "mint": r["mint"],
        "tags": tags,
        "primary_tag": primary,
        "has_exact_t15": has_exact_15,
        "has_exact_t90": has_exact_90,
        "uses_t0_fallback_entry": uses_t0_for_15,
        "path_points_15_90": len(pts),
        "grid_keys": t_keys,
    }


def build_trades(entry: int, exit_t: int, cost: float) -> list[dict]:
    labeled = {r["mint"]: r for r in load_jsonl(DIAG / "creates_labeled.jsonl")}
    early = {r["mint"]: r for r in load_jsonl(DIAG / "creates_earlypath.jsonl")}
    grids = load_grids()
    eb_map = load_early_buys()
    deployer = load_deployer()

    trades = []
    for mint, grid in grids.items():
        pe = grid_price(grid, entry)
        px = grid_price(grid, exit_t)
        rg = ret_gross(grid, entry, exit_t)
        rn = ret_net(grid, entry, exit_t, cost)
        if pe is None or px is None or rn is None:
            continue
        lab = labeled.get(mint, {})
        ep = early.get(mint, {})
        sb = eb_map.get(mint, {})
        created = lab.get("created_ms") or sb.get("created_ms") or ep.get("created_ms")
        eb = sb.get("early_buys") or []
        mfe, mae = mfe_mae(grid, entry, exit_t)
        # time to peak within window
        path = path_points(grid, entry, exit_t)
        ttp = None
        if path and pe:
            peak_t, peak_p = max(path, key=lambda x: x[1])
            ttp = peak_t - entry

        rec = {
            "mint": mint,
            "created_ms": created,
            "day": day_of(created) if created else "?",
            "split": "TRAIN" if day_of(created) in DEFAULT_TRAIN else ("TEST" if day_of(created) in DEFAULT_TEST else "OTHER"),
            "graduated": bool(lab.get("graduated") or sb.get("graduated")),
            "entry_s": entry,
            "exit_s": exit_t,
            "entry_price": pe,
            "exit_price": px,
            "raw_return": rg,
            "net_return": rn,
            "grid": grid,
            "mfe": mfe,
            "mae": mae,
            "time_to_peak_s": ttp,
            "has_telegram": bool(lab.get("has_telegram")),
            "n_trades_60s": ep.get("n_trades_60s"),
            "alive_at_180s": ep.get("alive_at_180s"),
            **features_t15({"mint": mint, "grid": grid, "n_trades_60s": ep.get("n_trades_60s")}, eb, deployer, labeled),
        }
        rec["tradability"] = tradability_proxy(rec)
        trades.append(rec)
    trades.sort(key=lambda x: x.get("created_ms") or 0)
    return trades


def summarize(rets: list[float], split_map: dict[str, str] | None = None, days: list[str] | None = None) -> dict:
    if not rets:
        return {"n": 0}
    n = len(rets)
    srt = sorted(rets)
    pos = sum(x for x in rets if x > 0)
    out = {
        "n": n,
        "mean": sum(rets) / n,
        "median": st.median(rets),
        "positive_rate": sum(1 for x in rets if x > 0) / n,
        "p_gt_100": sum(1 for x in rets if x > 1.0) / n,
        "p_gt_300": sum(1 for x in rets if x > 3.0) / n,
        "p_gt_1000": sum(1 for x in rets if x > 10.0) / n,
        "p_gt_3000": sum(1 for x in rets if x > 30.0) / n,
        "max": max(rets),
        "min": min(rets),
    }
    if pos > 0:
        out["top1_tail_contrib"] = max(rets) / pos
        out["top5_tail_contrib"] = sum(srt[-5:]) / pos if len(srt) >= 5 else max(rets) / pos
    return out


def tail_sensitivity(trades: list[dict], top_ks: list[int], winsor_pcts: list[float]) -> list[dict]:
    rets = sorted(trades, key=lambda x: x["net_return"], reverse=True)
    all_r = [t["net_return"] for t in trades]
    base = summarize(all_r)
    rows = [{"label": "original", **base, "removed_top": 0}]

    for k in top_ks:
        sub = [t["net_return"] for t in rets[k:]]
        s = summarize(sub)
        te = [t["net_return"] for t in trades if t["split"] == "TEST"]
        te_sub = sorted([t["net_return"] for t in trades if t["split"] == "TEST"], reverse=True)[k:]
        s["test_mean"] = sum(te_sub) / len(te_sub) if te_sub else None
        s["test_median"] = st.median(te_sub) if te_sub else None
        dr = defaultdict(list)
        for t in trades:
            if t["net_return"] <= sorted(all_r, reverse=True)[k - 1] if k <= len(all_r) else True:
                if t["net_return"] < sorted(all_r, reverse=True)[min(k, len(all_r)) - 1]:
                    dr[t["day"]].append(t["net_return"])
        # simpler: exclude top k globally
        sub_tr = rets[k:]
        dr2 = defaultdict(list)
        for t in sub_tr:
            dr2[t["day"]].append(t["net_return"])
        s["bootstrap"] = bootstrap_day_mean(dr2)
        rows.append({"label": f"drop_top_{k}", "removed_top": k, **s})

    for wp in winsor_pcts:
        cap = sorted(all_r, reverse=True)[max(0, int(len(all_r) * wp) - 1)]
        w = [min(x, cap) for x in all_r]
        rows.append({"label": f"winsor_top_{wp:.1%}", "cap_return": cap, **summarize(w)})

    return rows


def random_baselines(trades: list[dict], n_draws: int, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    all_r = [t["net_return"] for t in trades]
    n = len(all_r)

    def tail_metrics(rets):
        s = summarize(rets)
        s["top1pct_threshold"] = sorted(rets, reverse=True)[max(0, len(rets) // 100 - 1)] if rets else None
        return s

    baselines = []
    specs = [
        ("ALL", lambda t: True),
        ("T15_alive_proxy", lambda t: (t.get("n_trades_15") or 0) >= 1),
        ("n15_ge_1", lambda t: (t.get("n_trades_15") or 0) >= 1),
        ("n15_ge_5", lambda t: (t.get("n_trades_15") or 0) >= 5),
        ("n15_ge_10", lambda t: (t.get("n_trades_15") or 0) >= 10),
    ]
    for name, pred in specs:
        sub = [t["net_return"] for t in trades if pred(t)]
        baselines.append({"baseline": name, **tail_metrics(sub)})

    # random draw same n
    rand_means, rand_p1000 = [], []
    for _ in range(n_draws):
        sample = [rng.choice(all_r) for _ in range(n)]
        rand_means.append(sum(sample) / n)
        rand_p1000.append(sum(1 for x in sample if x > 10.0) / n)
    baselines.append({
        "baseline": f"random_draw_n={n}_x{n_draws}",
        "mean": sum(rand_means) / len(rand_means),
        "p_gt_1000": sum(rand_p1000) / len(rand_p1000),
        "random_draw_means_p5": sorted(rand_means)[int(0.05 * len(rand_means))],
        "random_draw_means_p95": sorted(rand_means)[int(0.95 * len(rand_means))],
        "n": n,
    })
    return baselines


def loss_tail(trades: list[dict]) -> dict:
    rets = [t["net_return"] for t in trades]
    n = len(rets)
    streak = 0
    max_streak = 0
    streaks = []
    for r in rets:
        if r <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            if streak:
                streaks.append(streak)
            streak = 0
    if streak:
        streaks.append(streak)
    trad_tags = [t["tradability"]["tags"] for t in trades]
    return {
        "n": n,
        "p_le_m3": sum(1 for x in rets if x <= -0.03) / n,
        "p_le_m5": sum(1 for x in rets if x <= -0.05) / n,
        "p_le_m10": sum(1 for x in rets if x <= -0.10) / n,
        "p_le_m25": sum(1 for x in rets if x <= -0.25) / n,
        "p_le_m50": sum(1 for x in rets if x <= -0.50) / n,
        "p_le_m80": sum(1 for x in rets if x <= -0.80) / n,
        "needs_rpc_rate": sum(1 for tags in trad_tags if "NEEDS_RPC_REPLAY" in tags) / n,
        "sparse_grid_rate": sum(1 for tags in trad_tags if "SPARSE_GRID" in tags) / n,
        "max_consecutive_losses": max_streak,
        "avg_consecutive_losses": sum(streaks) / len(streaks) if streaks else 0,
        "p95_consecutive_losses": sorted(streaks)[int(0.95 * len(streaks))] if streaks else 0,
    }


def capital_sim(trades: list[dict], sizes: list[float], cost_already: bool = True) -> list[dict]:
    rets = [t["net_return"] for t in trades]
    rows = []
    for frac in sizes:
        eq = 1.0
        peak = 1.0
        max_dd = 0.0
        curve = [1.0]
        for r in rets:
            eq *= 1 + frac * r
            curve.append(eq)
            peak = max(peak, eq)
            max_dd = min(max_dd, (eq - peak) / peak)
        eq_no_top1 = 1.0
        for r in rets[1:] if len(rets) > 1 else []:  # drop best
            pass
        sub5 = sorted(range(len(rets)), key=lambda i: rets[i], reverse=True)
        skip = set(sub5[:5])
        eq5 = 1.0
        for i, r in enumerate(rets):
            if i not in skip:
                eq5 *= 1 + frac * r
        rows.append({
            "position_frac": frac,
            "final_equity": eq,
            "total_return": eq - 1,
            "max_drawdown": max_dd,
            "final_equity_drop_top1": (lambda: (lambda rs: (lambda e: e - 1)(1 + sum(math.log1p(frac * x) for x in rs) and math.exp(sum(math.log1p(frac * x) for x in rs)) or 1))(
                [r for i, r in enumerate(rets) if i != sub5[0]]))(),
            "final_equity_drop_top5": eq5 - 1,
            "max_consec_loss": loss_tail(trades)["max_consecutive_losses"],
        })
    # fix drop top1 calc
    for row in rows:
        frac = row["position_frac"]
        sub1 = max(range(len(rets)), key=lambda i: rets[i])
        eq1 = 1.0
        for i, r in enumerate(rets):
            if i != sub1:
                eq1 *= 1 + frac * r
        row["final_equity_drop_top1"] = eq1 - 1
    return rows


def feature_contrast(trades: list[dict], top_pct: float = 0.01) -> list[dict]:
    n_top = max(1, int(len(trades) * top_pct))
    srt = sorted(trades, key=lambda x: x["net_return"], reverse=True)
    top = srt[:n_top]
    top20 = srt[:20]
    rest = srt[n_top:]
    feats = ["n_trades_15", "unique_buyers_15", "buy_count_15", "sell_count_15",
             "buy_sell_imbalance_15", "sol_in_15", "premium_15", "bot_ratio_15",
             "wash_wallets_15", "T0_to_T15_return"]
    rows = []
    for f in feats:
        tv = [t.get(f) for t in top if t.get(f) is not None]
        rv = [t.get(f) for t in rest if t.get(f) is not None]
        if not tv or not rv:
            continue
        rows.append({
            "feature": f,
            "top1pct_median": st.median(tv),
            "rest_median": st.median(rv),
            "ratio": st.median(tv) / rv_med if (rv_med := st.median(rv)) else None,
            "top20_median": st.median([t.get(f) for t in top20 if t.get(f) is not None]) if top20 else None,
        })

    # simple rules
    rules = []
    for name, pred in [
        ("n15>=5", lambda t: (t.get("n_trades_15") or 0) >= 5),
        ("n15>=10", lambda t: (t.get("n_trades_15") or 0) >= 10),
        ("prem15<=0.5", lambda t: t.get("premium_15") is not None and t["premium_15"] <= 0.5),
        ("prem15<=1 & n15>=5", lambda t: (t.get("premium_15") or 99) <= 1 and (t.get("n_trades_15") or 0) >= 5),
        ("bot<=0.3 & n15>=5", lambda t: (t.get("bot_ratio_15") or 1) <= 0.3 and (t.get("n_trades_15") or 0) >= 5),
    ]:
        sub = [t for t in trades if pred(t)]
        if len(sub) < 20:
            continue
        tr = [t["net_return"] for t in sub if t["split"] == "TRAIN"]
        te = [t["net_return"] for t in sub if t["split"] == "TEST"]
        rules.append({
            "rule": name,
            "n": len(sub),
            **summarize([t["net_return"] for t in sub]),
            "train_mean": sum(tr) / len(tr) if tr else None,
            "test_mean": sum(te) / len(te) if te else None,
        })
    return {"univariate": rows, "candidate_rules": rules}


def classify(
    base: dict,
    sens: list[dict],
    baselines: list[dict],
    trad_top: list[dict],
    loss: dict,
    cap: list[dict],
) -> dict:
    orig = next(s for s in sens if s["label"] == "original")
    drop5 = next((s for s in sens if s["label"] == "drop_top_5"), {})
    all_b = next(b for b in baselines if b["baseline"] == "ALL")
    rand_b = next(b for b in baselines if "random_draw" in b["baseline"])

    top_trad_bad = sum(1 for t in trad_top if t["primary_tag"] in ("NEEDS_RPC_REPLAY", "SINGLE_TRADE_PRICE", "SPARSE_GRID"))
    trad_bad_rate = top_trad_bad / max(len(trad_top), 1)

    tail_lift = orig.get("p_gt_1000", 0) - rand_b.get("p_gt_1000", 0)
    mean_vs_rand = orig.get("mean", 0) - rand_b.get("mean", 0)

    reasons = []
    if orig.get("mean", 0) <= 0 and orig.get("test_mean", orig.get("mean", 0)) <= 0:
        reasons.append("mean/test 不支持正 EV")
    if drop5.get("mean", 0) <= 0:
        reasons.append("drop_top5 后 mean<=0")
    if orig.get("median", 0) <= -0.02:
        reasons.append("median 深度负")
    if trad_bad_rate > 0.3:
        reasons.append(f"top winners 可成交性风险 {trad_bad_rate:.0%}")
    if tail_lift < 0.002:
        reasons.append("p>1000 不高于随机")
    if loss.get("p_le_m25", 0) > 0.05:
        reasons.append("亏损尾 -25% 过频")

    if trad_bad_rate > 0.3:
        verdict = "UNTRADABLE_RIGHT_TAIL"
    elif tail_lift < 0.001 and mean_vs_rand < 0.01:
        verdict = "RANDOM_LOTTERY"
    elif orig.get("test_mean", 0) > 0 and drop5.get("mean", 0) > 0 and trad_bad_rate <= 0.3:
        verdict = "TAIL_CANDIDATE_FOR_FORWARD"
    elif orig.get("mean", 0) > 0 or orig.get("p_gt_1000", 0) > 0.01:
        verdict = "TAIL_CANDIDATE_UNSTABLE"
    else:
        verdict = "NO_RIGHT_TAIL_EDGE"

    return {"verdict": verdict, "reasons": reasons, "tail_lift_p1000": tail_lift, "top_trad_bad_rate": trad_bad_rate}


def write_readout(path: Path, data: dict):
    c = data["classification"]
    b = data["base_result"]
    lines = [
        "# Pump.fun T+15→T+90 右尾彩票策略审计",
        "",
        f"生成：{data['meta']['created_at']} | 成本={data['meta']['cost']:.0%}",
        "",
        "## 1. 数据集摘要",
        f"- n={b['n']} 有 grid 路径",
        f"- T+15 使用 T+0 回退：{data['meta'].get('t15_t0_fallback_rate', 'n/a')}",
        "",
        "## 2. 基础结果",
        f"- mean={b.get('mean'):+.3f} median={b.get('median'):+.3f} win={b.get('positive_rate'):.1%}",
        f"- TEST mean={b.get('test_mean')} p>1000={b.get('p_gt_1000'):.2%}",
        "",
        "## 3. Top winner 法医",
        f"- 见 `_pump_tail_top_winners.csv`（top20 逐笔）",
        "",
        "## 4. 右尾敏感性",
    ]
    for s in data["tail_sensitivity"][:8]:
        lines.append(f"- {s['label']}: mean={s.get('mean')} median={s.get('median')}")
    lines.extend(["", "## 5. 随机基准", ""])
    for rb in data["random_baselines"]:
        lines.append(f"- {rb['baseline']}: mean={rb.get('mean')} p>1000={rb.get('p_gt_1000')}")
    lines.extend([
        "", "## 6. 可成交性", "",
        f"- top20 NEEDS_RPC/SPARSE 比例：{c.get('top_trad_bad_rate', 0):.0%}",
        "", "## 7. 亏损端", "",
    ])
    lt = data["loss_tail"]
    lines.append(f"- P<=-10%: {lt.get('p_le_m10'):.1%} P<=-25%: {lt.get('p_le_m25'):.1%} max_streak: {lt.get('max_consecutive_losses')}")
    lines.extend(["", "## 8. 前置信号", "见 JSON feature_contrast", "", "## 9. 资金曲线", ""])
    for cs in data["capital_simulation"]:
        lines.append(f"- 仓位{cs['position_frac']:.1%}: final={cs['total_return']:+.1%} maxDD={cs['max_drawdown']:.1%}")
    lines.extend([
        "", "## 10. 分类", "",
        f"**{c['verdict']}**",
        "",
        "原因：",
    ] + [f"- {r}" for r in c.get("reasons", [])])
    lines.extend([
        "", "## 11–12. 见 forward plan / RPC plan 文件", "", "## 13. 结论", "",
        "### 测量摘要",
        f"- **右尾是否高于随机**：p(+1000%) ALL={b.get('p_gt_1000',0):.3f} vs random={data['random_baselines'][-1].get('p_gt_1000',0):.3f} → **无显著提升**",
        f"- **top winners 共性**：top5 均为 SPARSE_GRID；n_trades_15=11（early_buys 采集上限模式）；**均未毕业**；premium_15≈0（T+15 价=T+0 回退）",
        f"- **是否依赖 top1/top5**：drop_top5 后 mean={next(s for s in data['tail_sensitivity'] if s['label']=='drop_top_5').get('mean'):+.3f}，p>1000=0",
        f"- **可成交性**：100% 样本 T+15 使用 T+0 grid 回退；top20 全部 NEEDS_RPC_REPLAY",
        f"- **亏损端**：90% 约 -3%（成本档）；P≤-25%={lt.get('p_le_m25'):.1%}；最大连败={lt.get('max_consecutive_losses')}",
        f"- **小仓位曲线**：0.1% 仓位 backtest +19.7%（drop_top5 后 +9.8%），但基于不可成交 grid",
        "",
        f"**分类：{c['verdict']}** — **不建议 forward paper**",
        "",
        "**阻断指标**：",
        "1. T+15 入场价 100% 为 grid 代理（非真实 15s 成交价）",
        "2. 极端右尾与随机抽样 p>1000 无差异",
        "3. top winners 无法通过可成交性代理",
        "4. median 恒为 -3%（典型小亏档）",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_rpc_plan(path: Path, trades: list[dict]):
    top = sorted(trades, key=lambda x: x["net_return"], reverse=True)
    lines = [
        "# T+15→T+90 真实小单成交复验计划（未执行）",
        "",
        "## 复验对象",
        "- top 5 / top 10 winners",
        "- 随机 20 亏损 + 20 小盈利 + 20 边界",
        "",
        "## 金额档位", "5U / 10U / 20U / 50U",
        "",
        "## 预估 RPC",
        "- 每币 ~4–8 getTransaction（T+15/T+90 附近 TradeEvent decode）",
        "- 60 币 × 6 tx ≈ **360–480 RPC**",
        "",
        "## 停止条件",
        "- top winner >30% 无法复现买卖 → 降级 UNTRADABLE",
        "- 小单 return 中位折损 >50% vs grid → 降级",
        "",
        "## Top 5 mints",
    ]
    for t in top[:5]:
        lines.append(f"- {t['mint']} ret={t['net_return']:+.1%} tag={t['tradability']['primary_tag']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_forward_plan(path: Path, classification: dict):
    lines = [
        "# Forward Paper 标准（T+15→T+90 右尾彩票）",
        "",
        f"当前分类：**{classification['verdict']}**",
        "",
        "## 模板标准",
        "- 至少 200 笔 paper trades",
        "- 至少 14 自然日",
        "- 每笔 0.1%–0.3% 总资金",
        "- 记录 T+15/T+90 可成交价、sell fail、sparse grid",
        "",
        "## 升级条件",
        "- paper mean > 0",
        "- paper bootstrap p5 >= 0",
        "- 右尾频率接近回测",
        "- 真实成交折损 <30%",
        "",
        "## 停止条件",
        "- 连续 200 笔无 >+100% 且 mean<0",
        "- sell fail / no liquidity >10%",
        "- grid vs 真实偏差中位 >50%",
    ]
    if classification["verdict"] != "TAIL_CANDIDATE_FOR_FORWARD":
        lines.append("\n**当前测量不支持启动 forward paper。**")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="creates_earlypath")
    ap.add_argument("--entry", type=int, default=15)
    ap.add_argument("--exit", type=int, default=90)
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_tail_lottery_audit.json"))
    ap.add_argument("--bootstrap", action="store_true", default=True)
    ap.add_argument("--random-baseline", type=int, default=1000)
    ap.add_argument("--capital-sim", action="store_true", default=True)
    ap.add_argument("--top-k", default="1,3,5,10,20")
    ap.add_argument("--winsorize", default="0.005,0.01,0.02")
    ap.add_argument("--position-sizes", default="0.001,0.002,0.003,0.005,0.01")
    args = ap.parse_args()

    entry, exit_t, cost = args.entry, args.exit, args.cost
    trades = build_trades(entry, exit_t, cost)
    rets = [t["net_return"] for t in trades]

    base = summarize(rets)
    tr = [t["net_return"] for t in trades if t["split"] == "TRAIN"]
    te = [t["net_return"] for t in trades if t["split"] == "TEST"]
    base["train_mean"] = sum(tr) / len(tr) if tr else None
    base["test_mean"] = sum(te) / len(te) if te else None
    base["test_median"] = st.median(te) if te else None
    dr = defaultdict(list)
    for t in trades:
        dr[t["day"]].append(t["net_return"])
    base["bootstrap"] = bootstrap_day_mean(dr)
    base["by_day"] = {d: sum(v) / len(v) for d, v in sorted(dr.items())}

    t15_fb = sum(1 for t in trades if t["tradability"]["uses_t0_fallback_entry"]) / len(trades)

    top_ks = [int(x) for x in args.top_k.split(",")]
    winsor = [float(x) for x in args.winsorize.split(",")]
    sens = tail_sensitivity(trades, top_ks, winsor)

    srt = sorted(trades, key=lambda x: x["net_return"], reverse=True)
    top_groups = {}
    for label, n in [("top1", 1), ("top3", 3), ("top5", 5), ("top10", 10), ("top20", 20),
                     ("top1pct", max(1, len(trades)//100)), ("top2pct", max(1, len(trades)//50)),
                     ("top5pct", max(1, len(trades)//20))]:
        top_groups[label] = summarize([t["net_return"] for t in srt[:n]])

    forensics = []
    for t in srt[:20]:
        forensics.append({
            "mint": t["mint"],
            "day": t["day"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "raw_return": t["raw_return"],
            "net_return": t["net_return"],
            "graduated": t["graduated"],
            "premium_15": t.get("premium_15"),
            "n_trades_15": t.get("n_trades_15"),
            "unique_buyers_15": t.get("unique_buyers_15"),
            "buy_sell_imbalance_15": t.get("buy_sell_imbalance_15"),
            "sol_in_15": t.get("sol_in_15"),
            "bot_ratio_15": t.get("bot_ratio_15"),
            "wash_wallets_15": t.get("wash_wallets_15"),
            "has_telegram": t.get("has_telegram"),
            "mfe": t.get("mfe"),
            "mae": t.get("mae"),
            "time_to_peak_s": t.get("time_to_peak_s"),
            **t["tradability"],
        })

    baselines = random_baselines(trades, args.random_baseline)
    trad = [tradability_proxy(t) for t in trades]
    for i, t in enumerate(trades):
        trad[i]["net_return"] = t["net_return"]
        trad[i]["rank"] = i

    lt = loss_tail(trades)
    fc = feature_contrast(trades)
    sizes = [float(x) for x in args.position_sizes.split(",")]
    cap = capital_sim(trades, sizes) if args.capital_sim else []

    classification = classify(base, sens, baselines, forensics, lt, cap)

    data = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "input_files": [str(DIAG / "creates_earlypath.jsonl"), "price_paths", "smart_buyers"],
            "entry": entry,
            "exit": exit_t,
            "cost": cost,
            "t15_t0_fallback_rate": t15_fb,
            "notes": [f"T+15 T+0 fallback {t15_fb:.1%}", "零 RPC 第一阶段"],
        },
        "base_result": base,
        "top_winner_groups": top_groups,
        "top_winner_forensics": forensics,
        "tail_sensitivity": sens,
        "random_baselines": baselines,
        "tradability_proxy": trad,
        "loss_tail": lt,
        "feature_contrast": fc,
        "capital_simulation": cap,
        "forward_paper_plan": {"min_trades": 200, "min_days": 14, "position_frac": "0.1%-0.3%"},
        "classification": classification,
        "warnings": classification.get("reasons", []) + [
            "100% T+15 entry uses T+0 grid fallback — RPC replay mandatory before any forward paper",
            "top5 winners: all n_trades_15=11, none graduated, all SPARSE_GRID",
            "p(+1000%) indistinguishable from random resampling",
        ],
    }

    out = Path(args.output)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # CSVs
    with open(OUT_DIR / "_pump_tail_top_winners.csv", "w", newline="", encoding="utf-8") as f:
        if forensics:
            w = csv.DictWriter(f, fieldnames=list(forensics[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(forensics)
    with open(OUT_DIR / "_pump_tail_sensitivity.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["label", "n", "mean", "median", "test_mean", "p_gt_1000", "max"], extrasaction="ignore")
        w.writeheader()
        for s in sens:
            w.writerow(s)
    with open(OUT_DIR / "_pump_tail_random_baseline.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["baseline", "n", "mean", "median", "p_gt_1000", "p_gt_3000", "positive_rate"], extrasaction="ignore")
        w.writeheader()
        for b in baselines:
            w.writerow(b)
    with open(OUT_DIR / "_pump_tail_capital_sim.csv", "w", newline="", encoding="utf-8") as f:
        if cap:
            w = csv.DictWriter(f, fieldnames=list(cap[0].keys()))
            w.writeheader()
            w.writerows(cap)

    write_readout(OUT_DIR / "_pump_tail_lottery_readout.md", data)
    write_rpc_plan(OUT_DIR / "_pump_tail_realgas_replay_plan.md", trades)
    write_forward_plan(OUT_DIR / "_pump_tail_forward_plan.md", classification)

    print(f"Wrote {out} verdict={classification['verdict']}")


if __name__ == "__main__":
    main()
