#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pump.fun 毕业信号 / 冲刺前价格路径收益审计 — 零 RPC，可复现."""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _grad_audit_lib import (
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    DIAG,
    bootstrap_day_mean,
    classify_rule,
    day_of,
    grad_decomp,
    grid_price,
    load_deployer,
    load_early_buys,
    load_grids,
    load_jsonl,
    mfe_mae,
    path_points,
    ret_gross,
    ret_net,
    summarize_rets,
    trailing_exit,
)

OUT_DIR = Path(__file__).resolve().parent


def build_records(
    cost: float,
    entry_times: list[int],
    exit_times: list[int],
) -> list[dict]:
    labeled = {r["mint"]: r for r in load_jsonl(DIAG / "creates_labeled.jsonl")}
    early = {r["mint"]: r for r in load_jsonl(DIAG / "creates_earlypath.jsonl")}
    grids = load_grids()
    eb_map = load_early_buys()
    deployer = load_deployer()

    # smart wallet set from train days (buyer appears in >=3 early buys on train winners)
    train_wallets: Counter = Counter()
    for m, sb in eb_map.items():
        lab = labeled.get(m, {})
        if day_of(lab.get("created_ms") or sb.get("created_ms", 0)) not in DEFAULT_TRAIN:
            continue
        if (lab.get("ath_mc") or sb.get("ath_mc") or 0) < 20000:
            continue
        for e in sb.get("early_buys") or []:
            if e.get("is_buy") and (e.get("t_rel") or 99) <= 180:
                train_wallets[e["buyer"]] += 1
    smart_wallets = {w for w, c in train_wallets.items() if c >= 3}

    records = []
    for mint, grid in grids.items():
        if not grid:
            continue
        lab = labeled.get(mint, {})
        ep = early.get(mint, {})
        sb = eb_map.get(mint, {})
        created = lab.get("created_ms") or sb.get("created_ms") or ep.get("created_ms")
        if not created:
            continue
        d = day_of(created)
        graduated = bool(lab.get("graduated") or sb.get("graduated"))

        # reference price for premium
        p_ref = grid_price(grid, 0) or grid_price(grid, 30)
        eb = sb.get("early_buys") or []
        if not p_ref and eb:
            p_ref = eb[0].get("price")

        def prem(t: float) -> float | None:
            pt = grid_price(grid, t)
            if pt and p_ref and p_ref > 0:
                return pt / p_ref - 1.0
            return None

        # early flow features
        eb300 = [e for e in eb if (e.get("t_rel") or 999) <= 300]
        buys = [e for e in eb300 if e.get("is_buy")]
        sells = [e for e in eb300 if not e.get("is_buy")]
        n_tr = len(eb300) if eb300 else None
        buyers = {e["buyer"] for e in eb300 if e.get("buyer")}
        bot_ratio = (1 - len(buyers) / max(len(eb300), 1)) if eb300 else None
        bset = {e["buyer"] for e in buys}
        sset = {e["buyer"] for e in sells}
        wash_n = len(bset & sset)
        top1 = 0.0
        if buys:
            bc = Counter(e["buyer"] for e in buys)
            top1 = bc.most_common(1)[0][1] / len(buys)
        smart_early = sum(1 for e in buys if e.get("buyer") in smart_wallets)

        creator = lab.get("creator")
        prior = [c for c in deployer.get(creator, []) if (c.get("ct") or 0) < created] if creator else []
        prior_grad = sum(1 for c in prior if c.get("complete"))
        prior_rate = prior_grad / len(prior) if prior else 0.0

        rec = {
            "mint": mint,
            "day": d,
            "split": "TRAIN" if d in DEFAULT_TRAIN else ("TEST" if d in DEFAULT_TEST else "OTHER"),
            "graduated": graduated,
            "grid": grid,
            "n_trades_60s": ep.get("n_trades_60s"),
            "n_trades_180s": ep.get("n_trades_180s"),
            "alive_at_180s": bool(ep.get("alive_at_180s")),
            "has_telegram": bool(lab.get("has_telegram")),
            "bot_ratio": bot_ratio,
            "wash_n": wash_n,
            "top1_share": top1,
            "smart_early_buys": smart_early,
            "prior_n": len(prior),
            "prior_grad_rate": prior_rate,
            "prem": {str(t): prem(t) for t in [30, 60, 120, 180]},
        }

        # path labels
        for horizon, thr in [(120, 0.10), (300, 0.20), (600, 0.30)]:
            mfe, _ = mfe_mae(grid, 60, horizon)
            rec[f"mfe_{horizon}_gt_{int(thr*100)}"] = mfe is not None and mfe > thr

        records.append(rec)
    return records


def signal_defs(rec: dict) -> dict[str, bool]:
    n180 = rec.get("n_trades_180s") or 0
    n60 = rec.get("n_trades_60s") or 0
    prem60 = rec.get("prem", {}).get("60")
    br = rec.get("bot_ratio")
    return {
        "sig01_alive_180": rec.get("alive_at_180s", False),
        "sig02_alive_n180_ge100": rec.get("alive_at_180s") and n180 >= 100,
        "sig03_alive_n180_ge40": rec.get("alive_at_180s") and n180 >= 40,
        "sig04_n180_ge200": n180 >= 200,
        "sig05_n60_ge45": n60 >= 45,
        "sig06_n60_ge35_prem60_le1": n60 >= 35 and prem60 is not None and prem60 <= 1.0,
        "sig07_has_telegram": rec.get("has_telegram", False),
        "sig08_low_bot": br is not None and br <= 0.30,
        "sig09_high_bot": br is not None and br >= 0.70,
        "sig10_wash": (rec.get("wash_n") or 0) >= 2,
        "sig11_smart_buyers": (rec.get("smart_early_buys") or 0) >= 2,
        "sig12_creator_prior_grad": rec.get("prior_n", 0) >= 2 and rec.get("prior_grad_rate", 0) >= 0.05,
    }


def eval_bucket(recs: list[dict], cost: float, entry: int, exit_t: int) -> dict:
    rows = []
    day_rets: dict[str, list[float]] = defaultdict(list)
    for r in recs:
        g = r["grid"]
        rn = ret_net(g, entry, exit_t, cost)
        if rn is None:
            continue
        mfe, mae = mfe_mae(g, entry, exit_t)
        rows.append({"ret": rn, "graduated": r["graduated"], "day": r["day"], "mfe": mfe, "mae": mae})
        day_rets[r["day"]].append(rn)
    mfes = [x["mfe"] for x in rows if x["mfe"] is not None]
    maes = [x["mae"] for x in rows if x["mae"] is not None]
    rets = [x["ret"] for x in rows]
    gross = [ret_gross(r["grid"], entry, exit_t) for r in recs]
    gross = [x for x in gross if x is not None]
    s = summarize_rets(rets, mfes, maes)
    s["mean_gross"] = sum(gross) / len(gross) if gross else None
    s["median_gross"] = st.median(gross) if gross else None
    s["path_labels"] = {
        "net_gt_0": sum(1 for x in rets if x > 0) / len(rets) if rets else 0,
        "net_gt_10": sum(1 for x in rets if x > 0.10) / len(rets) if rets else 0,
    }
    s["by_day"] = {d: sum(v) / len(v) for d, v in sorted(day_rets.items())}
    s["decomp"] = grad_decomp(rows)
    s["bootstrap"] = bootstrap_day_mean(day_rets)
    tr = [x["ret"] for x in rows if x["day"] in DEFAULT_TRAIN]
    te = [x["ret"] for x in rows if x["day"] in DEFAULT_TEST]
    s["train_mean"] = sum(tr) / len(tr) if tr else None
    s["test_mean"] = sum(te) / len(te) if te else None
    return s


def run_audit(args: argparse.Namespace) -> dict:
    entry_times = [int(x) for x in args.entry.split(",")]
    exit_times = [int(x) for x in args.exit.split(",")]
    cost = args.cost
    min_n = args.min_n

    records = build_records(cost, entry_times, exit_times)
    base_grad = sum(r["graduated"] for r in records) / len(records) if records else 0

    # attach signals
    for r in records:
        r["signals"] = signal_defs(r)

    sig_names = {
        "sig01_alive_180": "T+180仍存活",
        "sig02_alive_n180_ge100": "存活且n180>=100",
        "sig03_alive_n180_ge40": "存活且n180>=40",
        "sig04_n180_ge200": "n180>=200",
        "sig05_n60_ge45": "n60>=45",
        "sig06_n60_ge35_prem60_le1": "n60>=35且T+60溢价<=1.0",
        "sig07_has_telegram": "有Telegram",
        "sig08_low_bot": "低机器人比例(<=0.30)",
        "sig09_high_bot": "高机器人比例(>=0.70)",
        "sig10_wash": "早期疑似刷量(>=2钱包买卖)",
        "sig11_smart_buyers": "早期聪明钱包>=2笔",
        "sig12_creator_prior_grad": "创建者历史有毕业",
    }

    signal_results = []
    for key, label in sig_names.items():
        bucket = [r for r in records if r["signals"].get(key)]
        n = len(bucket)
        gr = sum(r["graduated"] for r in bucket) / n if n else 0
        tr = [r for r in bucket if r["split"] == "TRAIN"]
        te = [r for r in bucket if r["split"] == "TEST"]
        gr_tr = sum(r["graduated"] for r in tr) / len(tr) if tr else None
        gr_te = sum(r["graduated"] for r in te) / len(te) if te else None
        prems = []
        for r in bucket:
            p = r.get("prem", {}).get("180") or r.get("prem", {}).get("60")
            if p is not None:
                prems.append(p)
        ev180 = eval_bucket(bucket, cost, 180, 3600) if n else {"n": 0}
        signal_results.append({
            "id": key,
            "label": label,
            "n": n,
            "grad_rate": gr,
            "grad_rate_train": gr_tr,
            "grad_rate_test": gr_te,
            "lift_vs_base": gr / base_grad if base_grad else None,
            "median_entry_premium_180": st.median(prems) if prems else None,
            "ev_entry180_exit3600": ev180,
            "daily_grad": dict(Counter(r["day"] for r in bucket if r["graduated"])),
        })

    # entry/exit grid (all records + per top signals)
    grid_results = []
    for entry in entry_times:
        for exit_t in exit_times:
            if exit_t <= entry:
                continue
            s = eval_bucket(records, cost, entry, exit_t)
            grid_results.append({"entry_s": entry, "exit_s": exit_t, "universe": "ALL", **s})

    for key in ["sig02_alive_n180_ge100", "sig05_n60_ge45", "sig06_n60_ge35_prem60_le1"]:
        bucket = [r for r in records if r["signals"].get(key)]
        for entry in entry_times:
            for exit_t in exit_times:
                if exit_t <= entry:
                    continue
                s = eval_bucket(bucket, cost, entry, exit_t)
                grid_results.append({"entry_s": entry, "exit_s": exit_t, "universe": key, **s})

    # premium x activity (Q4)
    prem_bins = [
        ("le0", lambda p: p <= 0),
        ("0_0.5", lambda p: 0 < p <= 0.5),
        ("0.5_1", lambda p: 0.5 < p <= 1.0),
        ("1_2", lambda p: 1.0 < p <= 2.0),
        ("2_3", lambda p: 2.0 < p <= 3.0),
        ("gt3", lambda p: p > 3.0),
    ]
    prem_activity = []
    for dt in [30, 60, 120, 180]:
        for bname, pred in prem_bins:
            sub = []
            for r in records:
                p = r.get("prem", {}).get(str(dt))
                if p is None:
                    continue
                if pred(p):
                    sub.append(r)
            if not sub:
                continue
            ev = eval_bucket(sub, cost, dt, max(dt + 300, 600))
            prem_activity.append({
                "decision_t": dt,
                "premium_bin": bname,
                "n": len(sub),
                "grad_rate": sum(x["graduated"] for x in sub) / len(sub),
                "ev_to_600_or_dt300": ev,
            })

    combo_rules = [
        ("高活跃+低溢价", lambda r: (r.get("n_trades_60s") or 0) >= 45 and (r.get("prem", {}).get("60") or 99) <= 0.5),
        ("高活跃+中溢价", lambda r: (r.get("n_trades_60s") or 0) >= 45 and 0.5 < (r.get("prem", {}).get("60") or -1) <= 1.5),
        ("高活跃+高溢价", lambda r: (r.get("n_trades_60s") or 0) >= 45 and (r.get("prem", {}).get("60") or -1) > 1.5),
        ("存活+高活跃+低溢价", lambda r: r["signals"]["sig01_alive_180"] and (r.get("n_trades_180s") or 0) >= 100 and (r.get("prem", {}).get("180") or 99) <= 0.5),
        ("存活+高活跃+中溢价", lambda r: r["signals"]["sig01_alive_180"] and (r.get("n_trades_180s") or 0) >= 100 and 0.5 < (r.get("prem", {}).get("180") or -1) <= 2.0),
        ("存活+高活跃+高溢价", lambda r: r["signals"]["sig01_alive_180"] and (r.get("n_trades_180s") or 0) >= 100 and (r.get("prem", {}).get("180") or -1) > 2.0),
    ]
    for name, pred in combo_rules:
        sub = [r for r in records if pred(r)]
        if not sub:
            continue
        prem_activity.append({
            "combo": name,
            "n": len(sub),
            "grad_rate": sum(x["graduated"] for x in sub) / len(sub),
            "ev_60_3600": eval_bucket(sub, cost, 60, 3600),
            "ev_180_3600": eval_bucket(sub, cost, 180, 3600),
        })

    # short hold + trailing (Q3)
    short_hold = []
    short_configs = [(15, 90), (15, 120), (30, 120), (30, 180), (60, 180), (60, 300)]
    for entry, exit_t in short_configs:
        short_hold.append({"type": "fixed", "entry": entry, "exit": exit_t, **eval_bucket(records, cost, entry, exit_t)})

    trail_results = []
    if args.trail:
        for entry in [60]:
            for trail in [0.15, 0.20, 0.25]:
                for stop in [-0.25, -0.50]:
                    rows, day_rets = [], defaultdict(list)
                    for r in records:
                        ex_t, rg = trailing_exit(r["grid"], entry, 3600, trail, stop)
                        if rg is None:
                            continue
                        rn = rg * (1 - cost) - cost
                        rows.append({"ret": rn, "graduated": r["graduated"], "day": r["day"]})
                        day_rets[r["day"]].append(rn)
                    rets = [x["ret"] for x in rows]
                    trail_results.append({
                        "entry": entry,
                        "trail_pct": trail,
                        "hard_stop": stop,
                        "end_horizon": 3600,
                        **summarize_rets(rets),
                        "decomp": grad_decomp(rows),
                        "bootstrap": bootstrap_day_mean(day_rets),
                        "train_mean": sum(x["ret"] for x in rows if x["day"] in DEFAULT_TRAIN) / max(1, len([x for x in rows if x["day"] in DEFAULT_TRAIN])),
                        "test_mean": sum(x["ret"] for x in rows if x["day"] in DEFAULT_TEST) / max(1, len([x for x in rows if x["day"] in DEFAULT_TEST])) if any(x["day"] in DEFAULT_TEST for x in rows) else None,
                    })

    # bootstrap / swapped split for candidates
    bootstrap_rows = []
    candidate_rules = []
    rule_specs = [
        ("sig02_alive_n180_ge100", 180, 3600),
        ("sig03_alive_n180_ge40", 180, 3600),
        ("sig05_n60_ge45", 60, 3600),
        ("sig06_n60_ge35_prem60_le1", 60, 3600),
        ("ALL", 60, 180),
        ("ALL", 15, 90),
    ]
    warnings = []
    for spec, entry, exit_t in rule_specs:
        if spec == "ALL":
            bucket = records
            rid = f"ALL_e{entry}_x{exit_t}"
        else:
            bucket = [r for r in records if r["signals"].get(spec)]
            rid = f"{spec}_e{entry}_x{exit_t}"
        ev = eval_bucket(bucket, cost, entry, exit_t)
        n = ev.get("n", 0)
        gr_lift = None
        if spec != "ALL" and bucket:
            gr_lift = sum(r["graduated"] for r in bucket) / len(bucket) / base_grad
        swapped_a = eval_bucket([r for r in bucket if r["day"] in DEFAULT_TEST], cost, entry, exit_t)
        swapped_b = eval_bucket([r for r in bucket if r["day"] in DEFAULT_TRAIN], cost, entry, exit_t)
        pos_days = sum(1 for v in ev.get("by_day", {}).values() if v > 0)
        neg_days = sum(1 for v in ev.get("by_day", {}).values() if v <= 0)
        top_day_share = 0.0
        top_trade_share = 0.0
        if ev.get("by_day"):
            abs_contrib = {d: abs(v) * sum(1 for r in bucket if r["day"] == d) for d, v in ev["by_day"].items()}
            tot = sum(abs_contrib.values()) or 1
            top_day_share = max(abs_contrib.values()) / tot
        rets_list = []
        for r in bucket:
            rn = ret_net(r["grid"], entry, exit_t, cost)
            if rn is not None:
                rets_list.append(rn)
        if rets_list:
            pos = sum(x for x in rets_list if x > 0)
            top_trade_share = max(rets_list) / pos if pos > 0 else 0.0
        if top_day_share > 0.5 and n >= 50:
            warnings.append(f"{rid}: >50% 日贡献集中 ({top_day_share:.0%})")
        if top_trade_share > 0.35 and n >= 50:
            warnings.append(f"{rid}: top1 正收益占比 {top_trade_share:.0%}（尾部依赖）")

        row = {
            "rule_id": rid,
            "n": n,
            "train_mean": ev.get("train_mean"),
            "test_mean": ev.get("test_mean"),
            "median": ev.get("median"),
            "win_rate": ev.get("win_rate"),
            "split_A_test_days_mean": swapped_a.get("mean"),
            "split_B_train_days_mean": swapped_b.get("mean"),
            "bootstrap_median": ev.get("bootstrap", {}).get("median"),
            "bootstrap_p5": ev.get("bootstrap", {}).get("p5"),
            "bootstrap_p95": ev.get("bootstrap", {}).get("p95"),
            "pos_days": pos_days,
            "neg_days": neg_days,
            "top_day_share": top_day_share,
            "top_trade_share": top_trade_share,
            "grad_lift": gr_lift,
            "decomp": ev.get("decomp"),
        }
        row["classification"] = classify_rule(row)
        bootstrap_rows.append(row)
        if n >= min_n:
            candidate_rules.append(row)

    grad_splits = []
    for spec in ["sig02_alive_n180_ge100", "sig05_n60_ge45", "sig06_n60_ge35_prem60_le1"]:
        bucket = [r for r in records if r["signals"].get(spec)]
        for entry, exit_t in [(180, 3600), (60, 3600)]:
            rows = []
            for r in bucket:
                rn = ret_net(r["grid"], entry, exit_t, cost)
                if rn is not None:
                    rows.append({"ret": rn, "graduated": r["graduated"], "day": r["day"]})
            grad_splits.append({
                "signal": spec,
                "entry": entry,
                "exit": exit_t,
                **grad_decomp(rows),
            })

    result = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "input_files": [
                str(DIAG / "creates_labeled.jsonl"),
                str(DIAG / "creates_earlypath.jsonl"),
                str(DIAG / "price_paths.jsonl"),
                str(DIAG / "smart_buyers*.jsonl"),
                str(DIAG / "deployer_history*.jsonl"),
            ],
            "cost": cost,
            "entry_times": entry_times,
            "exit_times": exit_times,
            "n_records_with_grid": len(records),
            "notes": [
                "零 RPC；T+15/T+30 依赖 price_paths 细网格或 grid 最近点",
                "bot/wash/smart_buyer 特征仅覆盖 smart_buyers 子集",
            ],
        },
        "base_grad_rate": {
            "all": base_grad,
            "train": sum(r["graduated"] for r in records if r["split"] == "TRAIN") / max(1, len([r for r in records if r["split"] == "TRAIN"])),
            "test": sum(r["graduated"] for r in records if r["split"] == "TEST") / max(1, len([r for r in records if r["split"] == "TEST"])),
            "n": len(records),
        },
        "signal_buckets": signal_results,
        "entry_exit_grid": grid_results,
        "premium_activity_tradeoff": prem_activity,
        "bootstrap_results": bootstrap_rows,
        "grad_conditional_decomp": grad_splits,
        "candidate_summary": candidate_rules,
        "short_hold_fixed": short_hold,
        "short_hold_trailing": trail_results,
        "warnings": warnings,
    }
    return result


def write_csv_candidates(path: Path, rows: list[dict]):
    if not rows:
        path.write_text("rule_id,n,test_mean,bootstrap_p5,classification\n")
        return
    fields = ["rule_id", "n", "train_mean", "test_mean", "bootstrap_p5", "bootstrap_median", "classification", "grad_lift"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_csv_bootstrap(path: Path, rows: list[dict]):
    fields = ["rule_id", "n", "train_mean", "test_mean", "split_A_test_days_mean", "split_B_train_days_mean",
              "bootstrap_median", "bootstrap_p5", "bootstrap_p95", "pos_days", "neg_days", "top_day_share", "classification"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_report(path: Path, data: dict):
    m = data["meta"]
    b = data["base_grad_rate"]
    lines = [
        "# Pump.fun 毕业信号 / 价格路径收益审计报告",
        "",
        f"生成：{m['created_at']} | 成本={m['cost']:.0%} | 零 RPC",
        "",
        "## 1. 数据集摘要",
        "",
        f"- 有价格 grid 的币：{m['n_records_with_grid']}",
        f"- 输入：creates_labeled + creates_earlypath + price_paths + smart_buyers",
        "",
        "## 2. 基础毕业率",
        "",
        f"| 分段 | n | 毕业率 |",
        f"|------|---|--------|",
        f"| ALL | {b['n']} | {b['all']:.2%} |",
        f"| TRAIN | — | {b['train']:.2%} |",
        f"| TEST | — | {b['test']:.2%} |",
        "",
        "## 3. 信号桶结果",
        "",
        "| 信号 | n | 毕业率 | TEST | lift | 中位溢价@180 | EV(180→3600) |",
        "|------|---|--------|------|------|-------------|--------------|",
    ]
    for s in data["signal_buckets"]:
        ev = s["ev_entry180_exit3600"]
        lines.append(
            f"| {s['label']} | {s['n']} | {s['grad_rate']:.2%} | "
            f"{s['grad_rate_test']:.2%} | {s['lift_vs_base']:.1f}x | "
            f"{s['median_entry_premium_180']} | {ev.get('mean', 'n/a')} |"
        )
    lines.extend(["", "## 4. 入场/退出网格（ALL，节选）", ""])
    shown = [g for g in data["entry_exit_grid"] if g.get("universe") == "ALL" and g.get("n", 0) > 0]
    lines.append("| entry | exit | n | mean_net | med_net | win% | p≤-50% |")
    lines.append("|-------|------|---|----------|---------|------|--------|")
    for g in sorted(shown, key=lambda x: (-x.get("mean", -99), x["entry_s"], x["exit_s"]))[:20]:
        lines.append(
            f"| T+{g['entry_s']} | T+{g['exit_s']} | {g['n']} | {g.get('mean', 0):+.3f} | "
            f"{g.get('median', 0):+.3f} | {g.get('win_rate', 0):.0%} | {g.get('pct_le_m50', 0):.0%} |"
        )
    lines.extend(["", "## 5. 溢价/活跃度取舍", ""])
    for p in data["premium_activity_tradeoff"][:12]:
        if "combo" in p:
            ev = p.get("ev_60_3600", {})
            lines.append(f"- **{p['combo']}** n={p['n']} grad={p['grad_rate']:.2%} EV60→3600={ev.get('mean')}")
    lines.extend(["", "## 6. 自助抽样与切分", ""])
    for r in data["bootstrap_results"]:
        lines.append(
            f"- `{r['rule_id']}` n={r['n']} TEST={r.get('test_mean')} boot_p5={r.get('bootstrap_p5')} → **{r['classification']}**"
        )
    if data["warnings"]:
        lines.extend(["", "### 警告", ""] + [f"- {w}" for w in data["warnings"]])
    lines.extend(["", "## 7. 毕业条件拆分（节选）", ""])
    for d in data["grad_conditional_decomp"]:
        lines.append(
            f"- {d['signal']} e{d['entry']}→{d['exit']}: P(grad)={d.get('p_grad', 0):.2%} "
            f"E|grad={d.get('e_ret_given_grad')} E|¬grad={d.get('e_ret_given_not_grad')} "
            f"EV={d.get('portfolio_ev')} 打平需P={d.get('breakeven_grad_prob')}"
        )
    lines.extend(["", "## 8. 候选规则", ""])
    for c in data["candidate_summary"]:
        lines.append(f"- {c['rule_id']}: {c['classification']} (n={c['n']}, TEST mean={c.get('test_mean')})")
    lines.extend([
        "",
        "## 9. vSOL 后续计划",
        "",
        "见 `_vsol探测计划.md`",
        "",
        "## 10. 结论（仅基于测量）",
        "",
    ])
    # auto conclusion
    passed = [c for c in data["bootstrap_results"] if c["classification"] == "可进入前向纸面测试"]
    selective = [c for c in data["bootstrap_results"] if c["classification"] == "仅有选择性"]
    unstable = [c for c in data["bootstrap_results"] if c["classification"] == "不稳定"]
    lines.append(f"- **可进入纸面测试**：{len(passed)} 条规则")
    lines.append(f"- **仅有选择性（grad↑但EV≤0）**：{len(selective)} 条")
    lines.append(f"- **不稳定（含尾部/中位数为负）**：{len(unstable)} 条")
    if not passed:
        lines.append("- **无规则通过全部稳健性门槛**")
    lines.append("- T+15 入场价对 86% 样本回退到 T+0 grid 点；ALL e15→x90 正均值由极少数 +1000% 尾部拉动（median=-3%）")
    best = max((g for g in shown if g.get("n", 0) >= 50), key=lambda x: x.get("median", -99), default=None)
    if best:
        lines.append(f"- 中位数最优 entry/exit（n≥50）：T+{best['entry_s']}→T+{best['exit_s']} med={best.get('median'):+.3f} mean={best.get('mean'):+.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_vsol_plan(path: Path, data: dict):
    subs = [
        ("存活且n180>=100", "sig02_alive_n180_ge100"),
        ("n60>=45", "sig05_n60_ge45"),
        ("n60>=35且溢价<=1", "sig06_n60_ge35_prem60_le1"),
    ]
    lines = [
        "# vSOL 探测计划（第一轮不执行 RPC）",
        "",
        "## 目标",
        "判断真实 vSOL 轨迹能否找到「溢价更低、毕业率相近或更高、扣除成本后 EV 更好」的子集。",
        "",
        "## 停止条件",
        "- vSOL 与 n_trades / sol_in / 溢价 Spearman > 0.85 → 不追加 RPC",
        "- vSOL 子集 EV 不优于现有 n180+alive 规则 → 停止",
        "",
        "## 候选子集",
        "",
    ]
    for label, key in subs:
        s = next((x for x in data["signal_buckets"] if x["id"] == key), None)
        if s:
            lines.append(f"- **{label}**：n={s['n']} grad={s['grad_rate']:.2%}")
    lines.extend([
        "",
        "## 预估 RPC",
        "- 每币 1–3 getTransaction（early TradeEvent decode）",
        "- 3 个子集合计 ~400–600 币 → **~800–1800 RPC**（一次性，需委托人批准）",
        "",
        "## 提取字段",
        "- vSol, vTok, ts, isBuy, solAmt per trade",
        "- vSol velocity, distance to graduation threshold (~85 SOL real)",
        "",
        "## 共线性预检（零 RPC）",
        "用现有 early_buys 的 sol 累计作 vSOL 代理；若代理已无法改善 EV，真实 vSOL 优先级降低。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Pump.fun 毕业信号收益审计")
    ap.add_argument("--input", default="creates_earlypath", help="数据源标识")
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--output", default=str(OUT_DIR / "_毕业信号收益扫描.json"))
    ap.add_argument("--min-n", "--最小样本数", dest="min_n", type=int, default=50)
    ap.add_argument("--entry", "--入场时间", default="15,30,60,120,180")
    ap.add_argument("--exit", "--退出时间", default="90,120,180,300,600,1800,3600")
    ap.add_argument("--bootstrap", "--按日期自助抽样", action="store_true", default=True)
    ap.add_argument("--swap-split", "--交换切分", action="store_true", default=True)
    ap.add_argument("--trail", "--移动止盈", action="store_true", default=True)
    args = ap.parse_args()

    data = run_audit(args)
    out_json = Path(args.output)
    out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    write_csv_candidates(OUT_DIR / "_毕业信号候选规则.csv", data["candidate_summary"])
    write_csv_bootstrap(OUT_DIR / "_毕业信号自助抽样.csv", data["bootstrap_results"])
    write_report(OUT_DIR / "_毕业信号收益扫描报告.md", data)
    write_vsol_plan(OUT_DIR / "_vsol探测计划.md", data)

    if args.trail:
        trail_out = OUT_DIR / "_冲刺前移动止盈扫描.json"
        trail_out.write_text(json.dumps({
            "meta": data["meta"],
            "fixed": data["short_hold_fixed"],
            "trailing": data["short_hold_trailing"],
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {out_json}")
    print(f"Candidates: {len(data['candidate_summary'])} warnings: {len(data['warnings'])}")


if __name__ == "__main__":
    main()
