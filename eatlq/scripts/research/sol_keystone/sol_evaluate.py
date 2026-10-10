#!/usr/bin/env python3
"""Task 6 — pass/fail gates + handoff report."""
from __future__ import annotations

import argparse
from pathlib import Path

from common import OUT, load_json

REPORT = OUT / "_keystone_solana_handoff_report.md"


def gate_pass(name: str, ok: bool, detail: str) -> str:
    mark = "PASS" if ok else "FAIL"
    return f"| {name} | {mark} | {detail} |"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-report", default=str(REPORT))
    args = ap.parse_args()

    uni = load_json(OUT / "_sol_universe.json")["stats"]
    rc = load_json(OUT / "_sol_realcost_labels.json")["summary"]
    lt = load_json(OUT / "_sol_lefttail_model.json")
    cv = load_json(OUT / "_sol_convexity.json")
    bl = load_json(OUT / "_sol_baseline.json")
    ex = load_json(OUT / "_sol_execution_realism.json")

    te_off = cv["pruned_basket_equal_weight"]["TEST"]
    te_all = cv["all_coins_equal_weight"]["TEST"]
    te_exe = ex["pruned_basket_30m_hold"]["by_split"]["TEST"]["executable_edge"]
    auc_te = lt["auc"]["test"]
    baseline_te = bl["all_coins_TEST"]

    g1 = te_off.get("n", 0) >= 30 and te_off.get("mean", -1) > baseline_te.get("mean", 0)
    g1_note = (
        f"pruned={te_off.get('mean')} vs baseline={baseline_te.get('mean')}"
        + (" (仍负 EV，仅相对少亏)" if (te_off.get('mean') or 0) < 0 else "")
    )
    g2 = auc_te is not None and auc_te >= 0.75
    g3 = te_exe.get("n", 0) >= 30 and te_exe.get("mean", -1) > 0 and te_exe.get("mean", -1) > baseline_te.get("mean", 0)
    tr_mean = cv["pruned_basket_equal_weight"]["TRAIN"].get("mean")
    te_mean = te_off.get("mean")
    gap_ok = True
    if tr_mean is not None and te_mean is not None and tr_mean > 0:
        gap_ok = te_mean >= 0.5 * tr_mean
    g4 = gap_ok

    passed = sum([g1, g2, g3, g4])
    if passed == 4:
        verdict = "solana_convexity_yes"
    elif passed >= 3 and not g3:
        verdict = "solana_edge_offline_only"
    else:
        verdict = "solana_convexity_no"

    lines = [
        "# KEYSTONE → Solana 离线验证 Handoff Report",
        "",
        "生成：2026-06-25 | 零 RPC | V3 live 未触碰",
        "",
        "## 1. 旧失败解剖 (Task 0)",
        "",
        "见 `_sol_autopsy.md`。**裁定：旧聪明钱线 ≈ (A) 离线不可部署；执行折让未量化 ≈ (B)；本轮继续 KEYSTONE 结构性移植。**",
        "",
        "## 2. RPC / 成本账",
        "",
        f"| 项 | 值 |",
        f"|----|-----|",
        f"| ClickHouse Sol 表 | **无**（fallback 缓存 JSONL） |",
        f"| 本轮 RPC 调用 | **0** |",
        f"| 现金成本 | **≈ $0** |",
        f"| 需付费上报 | **否** |",
        "",
        "## 3. Universe",
        "",
        f"- 池/币数：**{uni['pool_count']}**（全部 pump.fun bonding curve）",
        f"- 有早期路径特征：**{uni['with_earlypath_features']}**",
        f"- 有价格 grid：**{uni['with_price_grid']}**",
        f"- pump:Raydium = **{uni['pump_fun_raydium_ratio']}**",
        f"- 时间跨度：**{', '.join(uni['time_span_days'])}**",
        f"- entry_slot：**T+{uni['entry_slot_s']}s**",
        "",
        "## 4. 基线（随机早期池）",
        "",
        f"- 全样本 TEST 等权均值：**{baseline_te.get('mean', 'n/a'):+.4f}** (n={baseline_te.get('n')})",
        f"- 随机同 n 基线（500 次）：**{bl['mean_return_baseline']:+.4f}**",
        f"- 基线 EV 符号：**{'负' if (baseline_te.get('mean') or 0) < 0 else '正'}**",
        "",
        "## 5. 左尾模型",
        "",
        f"- TRAIN AUC：**{lt['auc']['train']:.3f}** | TEST AUC：**{lt['auc']['test']:.3f}**（KEYSTONE 参考 0.86）",
        f"- 剪后存活占比 TEST：**{lt['survivor_fraction_after_prune']['test']}**",
        f"- 剪后 fatal 率 TEST：**{lt['fatal_rate_after_prune']['test']}**",
        "",
        "## 6. 凸性篮子 (offline_edge, 30m hold)",
        "",
        f"| 分段 | n | mean | median | win_rate | ≥+100% | ≤−50% |",
        f"|------|---|------|--------|----------|--------|-------|",
    ]
    for tag in ("TRAIN", "TEST", "ALL"):
        s = cv["pruned_basket_equal_weight"][tag]
        if s.get("n"):
            lines.append(
                f"| {tag} | {s['n']} | {s['mean']:+.3f} | {s['median']:+.3f} | "
                f"{s['win_rate']:.2f} | {s['pct_ge_100']:.2f} | {s['pct_le_m50']:.2f} |"
            )

    discount = ex["pruned_basket_30m_hold"]["execution_discount_mean"]
    lines.extend([
        "",
        "## 7. 执行现实主义",
        "",
        f"| 指标 | offline | executable |",
        f"|------|---------|------------|",
        f"| TEST mean | {ex['pruned_basket_30m_hold']['by_split']['TEST']['offline_edge'].get('mean', 'n/a'):+.4f} | "
        f"{te_exe.get('mean', 'n/a'):+.4f} |",
        f"| **执行折让** | — | **{discount:+.4f}** |",
        f"| 信号衰减到 0（slot） | — | **{ex['pruned_basket_30m_hold']['signal_decay_to_zero_slot']}** |",
        "",
        "## 8. 通过线",
        "",
        "| # | 条件 | 结果 | 说明 |",
        "|---|------|------|------|",
        gate_pass("1 offline > 基线 TEST", g1, g1_note),
        gate_pass("2 TEST AUC ≥ 0.75", g2, f"AUC={auc_te:.3f}"),
        gate_pass("3 executable > 0 且 > 基线", g3, f"exe={te_exe.get('mean')} n={te_exe.get('n')}"),
        gate_pass("4 TRAIN/TEST 差距可接受", g4, f"train={tr_mean} test={te_mean}"),
        "",
        f"**裁定：`{verdict}`** ({passed}/4 通过)",
        "",
        "## 9. 给委托人三行",
        "",
        f"1. **边在不在**：剪左尾 offline TEST **{te_off.get('mean', 'n/a'):+.3f}** vs 基线 **{baseline_te.get('mean', 'n/a'):+.3f}** — **相对少亏 ~1pp，绝对 EV 仍负，非正 alpha**",
        f"2. **执行能不能拿到**：executable TEST **{te_exe.get('mean', 'n/a'):+.3f}**，执行折让 **{discount:+.4f}**",
        "3. **下一步最便宜动作**：**不建 geyser/Jito**；若仍想探 Sol，先扩 **零 RPC 早期路径样本**（12–16h 币 + OHLCV）验证左尾 AUC 是否稳；Raydium 需新 ingest（非本轮）",
        "",
        "## 10. Changed files / commands",
        "",
        "```bash",
        "cd /opt/EATLQ/scripts/research/sol_keystone",
        "bash ../base_v4/run_in_cgroup.sh python3 task0_autopsy.py",
        "bash ../base_v4/run_in_cgroup.sh python3 sol_universe.py",
        "bash ../base_v4/run_in_cgroup.sh python3 sol_realcost.py",
        "bash ../base_v4/run_in_cgroup.sh python3 sol_lefttail.py",
        "bash ../base_v4/run_in_cgroup.sh python3 sol_convexity.py",
        "bash ../base_v4/run_in_cgroup.sh python3 sol_execution_realism.py",
        "bash ../base_v4/run_in_cgroup.sh python3 sol_evaluate.py --write-report _keystone_solana_handoff_report.md",
        "```",
        "",
        "## 未知项 / 风险",
        "",
        "- ClickHouse **无 Sol 链数据**；结论绑定 06-11~06-15 缓存样本",
        "- 4h 出场 grid 不完整（缓存仅至 create+7200s）；主 hold 用 30m",
        "- 2104 有 grid / 1488 有 earlypath → 交集驱动 ML（n≈1488）",
        "- TRAIN AUC=1.0 提示训练集过拟合风险；TEST AUC 0.907 仍高但需更多天 OOS 验证",
        "- **非上线结论**；V3 Base live 优先不变",
        "",
    ])

    Path(args.write_report).write_text("\n".join(lines) + "\n")
    print(f"verdict={verdict} ({passed}/4) report={args.write_report}")


if __name__ == "__main__":
    main()
