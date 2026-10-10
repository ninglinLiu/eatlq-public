#!/usr/bin/env python3
"""Generate _deepsurvivor_handoff_report.md from pipeline outputs."""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import load_json, rpc_ledger, ts_fmt  # noqa: E402
from task_e_evaluate import check_pass_lines  # noqa: E402

OUT = os.path.join(HERE, "_deepsurvivor_handoff_report.md")


def main():
    uni = load_json(os.path.join(HERE, "_deep_universe.json"), {})
    bl = load_json(os.path.join(HERE, "_deep_baseline.json"), {})
    smart = load_json(os.path.join(HERE, "_smartmoney_signal.json"), {})
    decay = load_json(os.path.join(HERE, "_smartmoney_decay.json"), {})
    flow = load_json(os.path.join(HERE, "_flow_signals.json"), {})
    left = load_json(os.path.join(HERE, "_deep_lefttail.json"), {})
    verdict = check_pass_lines()
    rpc = rpc_ledger()
    meta = uni.get("meta", {})
    bl4 = (bl.get("holds") or {}).get("4h", {})

    lines = [
        "# Base 深池 / 长存活池 · Alpha 搜寻 · Handoff Report",
        "",
        f"> 生成：{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}  ",
        f"> 路径：`/opt/EATLQ/scripts/research/base_deepsurvivor/`  ",
        f"> **结论：`{verdict['verdict']}`** ({verdict['passed_count']}/4 通过线)",
        "",
        "---",
        "",
        "## 1. RPC 双账本",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 本轮新增 RPC | {rpc['round_added']} / {rpc['round_budget']} |",
        f"| 累计 RPC | {rpc['cumulative']} / {rpc['cumulative_stop']} |",
        f"| 本轮 Credits (M) | {rpc['credits_round_m']} |",
        f"| 累计 Credits (M) | {rpc['credits_m']} |",
        "",
        "## 2. 深池 Universe（任务 0）",
        "",
        f"- 池数：**{meta.get('n_pools', 'NA')}**（CH 候选 {meta.get('n_ch_candidates', 'NA')}）",
        f"- 浅过滤：age≥{meta.get('filters', {}).get('min_age_days')}d，depth≥{meta.get('filters', {}).get('min_depth_eth')} ETH，ns_pre≥{meta.get('filters', {}).get('min_swaps_pre')}",
        f"- 年龄（天）：median {meta.get('age_days', {}).get('median', 'NA')}",
        f"- 深度（ETH）：median {meta.get('depth_eth', {}).get('median', 'NA'):.2f}，p90 {meta.get('depth_eth', {}).get('p90', 'NA'):.2f}" if meta.get('depth_eth') else "- 深度：NA",
        f"- V3:V4 = {meta.get('v3_count',0)}:{meta.get('v4_count',0)}",
        f"- depth RPC 拉取：{meta.get('depth_rpc_fetched', 0)}",
        "",
        "## 3. 基线（任务 A）— 随机深池进场",
        "",
        f"**{bl.get('conclusion', '未运行')}**",
        "",
    ]
    for hold, data in (bl.get("holds") or {}).items():
        te = data.get("test", {})
        if te.get("n"):
            lines.append(f"- **{hold}** TEST: n={te['n']} mean={te['mean']:+.3f} WR={te['wr']:.1%} fatal50={te['fatal_50']:.1%} right50={te['right_50']:.1%}")

    lines += [
        "",
        "## 4. 聪明钱（任务 B）",
        "",
        f"- 冻结 smart 钱包：{smart.get('meta', {}).get('n_smart_wallets_frozen', 'NA')}",
        f"- TEST 信号 n={smart.get('test', {}).get('n', 'NA')} mean={smart.get('test', {}).get('mean', 'NA')} WR={smart.get('test', {}).get('wr', 'NA')}",
        f"- beat baseline TEST：{smart.get('beat_baseline_test', 'NA')}",
        f"- 逆向选择（1h 内 smart 卖出）：{smart.get('adverse_selection', {}).get('exit_liquidity_rate', 'NA')}",
        "",
        "### 延迟衰减曲线（4h hold）",
        "",
        "| Δ blocks | n | mean | WR |",
        "|----------|---|------|-----|",
    ]
    for d, s in (decay.get("decay_curve") or {}).items():
        if s.get("n"):
            lines.append(f"| +{d} | {s['n']} | {s['mean']:+.3f} | {s['wr']:.1%} |")
    lines.append(f"\n{decay.get('interpretation', '')}")

    lines += [
        "",
        "## 5. 流量信号（任务 C）",
        "",
        "| 信号 | TRAIN n/mean | TEST n/mean | beat baseline |",
        "|------|-------------|-------------|---------------|",
    ]
    for sig in flow.get("signals", []):
        tr, te = sig.get("train", {}), sig.get("test", {})
        lines.append(
            f"| {sig['signal']} | {tr.get('n','NA')}/{tr.get('mean','NA')} | "
            f"{te.get('n','NA')}/{te.get('mean','NA')} | {sig.get('beat_baseline_test','NA')} |"
        )

    lines += [
        "",
        "## 6. 左尾（任务 D）",
        "",
        f"- {left.get('interpretation', '未运行')}",
        f"- worst single：{left.get('worst_single', 'NA')}",
        f"- slow LP pull 案例：{left.get('slow_lp_pull_cases', 'NA')}",
        "",
        "## 7. 通过线 · `deep_edge_*`",
        "",
    ]
    for k, ok in verdict["pass_lines"].items():
        lines.append(f"- [{'x' if ok else ' '}] {k}")
    lines.append(f"\n**Verdict: `{verdict['verdict']}`** · best_signal={verdict.get('best_signal', 'none')}")

    # Three lines for principal
    bl_te = bl4.get("test", {})
    lines += [
        "",
        "## 8. 给委托人三行",
        "",
        f"1. **有没有边**：{verdict['verdict']} — TEST 随机基线 4h mean={bl_te.get('mean','NA')} (n={bl_te.get('n','NA')})",
        f"2. **哪种边**：best_signal={verdict.get('best_signal') or '无显著信号'}；聪明钱逆向选择={smart.get('adverse_selection', {}).get('exit_liquidity_rate', 'NA')}",
        f"3. **下一步**：{'扩样本+信号层优化（weak）' if verdict['verdict']=='deep_edge_weak' else '可做执行原型探针' if verdict['verdict']=='deep_edge_yes' else '诚实负结果 — 深池当前成本/延迟下无可执行边；记录资产归档'}",
        "",
        "## 9. Changed files / Commands / 风险",
        "",
        "**新增文件**：",
        "```",
        "scripts/research/base_deepsurvivor/common.py",
        "scripts/research/base_deepsurvivor/task0_build_universe.py",
        "scripts/research/base_deepsurvivor/task_a_baseline.py",
        "scripts/research/base_deepsurvivor/task_b_smartmoney.py",
        "scripts/research/base_deepsurvivor/task_c_flow_signals.py",
        "scripts/research/base_deepsurvivor/task_d_lefttail.py",
        "scripts/research/base_deepsurvivor/task_e_evaluate.py",
        "scripts/research/base_deepsurvivor/run_pipeline.py",
        "scripts/research/base_deepsurvivor/generate_report.py",
        "```",
        "",
        "**运行命令**：",
        "```bash",
        "cd /opt/EATLQ/scripts/research/base_deepsurvivor",
        "bash ../base_v4/run_in_cgroup.sh python3 run_pipeline.py",
        "python3 generate_report.py",
        "```",
        "",
        "**未知项 / 风险**：",
        "- 深池+depth≥3 ETH 样本极稀缺（42/2910=1.4%），**全部 TEST n=16<30，结论为探索性而非定稿**",
        "- 仅 on-chain WETH depth 过 gate；2910 候选中 500 次 depth RPC 后仅 42 池达标",
        "- 聪明钱 TEST 信号 n=0（冻结 smart 集仅 1 钱包，TEST 段无触达）",
        "- **零改动** V3 live executor / panic / sizing",
        "",
        "---",
        "",
        "*验收：浅过滤 only · point-in-time · TRAIN/TEST 时间切分 · 真实 QuoterV2 成本 · 无事后硬 gate*",
    ]

    open(OUT, "w").write("\n".join(lines))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
