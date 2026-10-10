#!/usr/bin/env python3
"""Write R0 readout from signals + fork results."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"


def pct(x) -> str:
    if x is None:
        return "—"
    return f"{100.0 * float(x):+.1f}%"


def main() -> int:
    sig = json.loads((OUT / "signals.json").read_text(encoding="utf-8"))
    fork_p = OUT / "fork_results.json"
    fork = json.loads(fork_p.read_text(encoding="utf-8")) if fork_p.exists() else {}
    uni = sig["universe"]
    trades = fork.get("trades") or []
    sm = fork.get("summary") or {}

    lines = [
        "# B20 dump-reclaim R0 读出",
        "",
        f"生成：{datetime.now(timezone.utc).isoformat()}",
        "预注册：`PREREGISTRATION.md`",
        "",
        "## 宇宙",
        "",
        f"- 主池 {uni['n_primary']} · 有成交 {uni['n_with_swaps']} · MIN_PRE {uni['n_minpre']}",
        f"- 前 10 分钟砸 ≥20%：{uni['n_dump']}",
        f"- Arm A 发出（砸完回到 −5% 内）：**{uni['n_arm_a']}**",
        f"- Arm B（仅 Keystone 10 分钟定时，诊断用）：{uni['n_arm_b']}",
        "",
        "## 官方臂（fork + QuoterV2）",
        "",
    ]
    if not trades:
        lines.append("fork 尚未跑完。")
    else:
        lines += [
            f"- 信号 {sm.get('n_signals')} · dead {sm.get('n_dead')} · instant_hp {sm.get('n_instant_hp')} · "
            f"摩擦跳过 {sm.get('n_friction_skip')} · stranded {sm.get('n_stranded')} · 干净入场 {sm.get('n_entered_clean')}",
            f"- 计入官方尺子的 n={sm.get('n_official')}",
        ]
        if sm.get("n_official"):
            lines += [
                f"- 中位数 {pct(sm.get('median'))} · 均值 {pct(sm.get('mean'))} · 胜率 {pct(sm.get('win_rate'))}",
                f"- 最好 {pct(sm.get('best'))} · 最差 {pct(sm.get('worst'))} · 合计 {sm.get('sum_eth'):+.5f} ETH",
            ]
        lines += ["", "### 逐笔", "", "| token | pool | 入场块 | 出场 | label | official_net | qv2_net | fric |",
                  "|---|---|---:|---|---|---:|---:|---:|"]
        for t in trades:
            lines.append(
                f"| {t.get('sym','')} | `{t.get('pool','')[:10]}` | {t.get('entry_block')} | "
                f"{t.get('exit_why')} | {t.get('label')} | {pct(t.get('official_net'))} | "
                f"{pct(t.get('qv2_net'))} | {pct(t.get('fric'))} |"
            )

    n_off = sm.get("n_official") or 0
    med = sm.get("median")
    lines += ["", "## 预注册裁决", ""]
    if n_off < 8:
        verdict = "UNDERPOWERED"
        why = f"官方臂只有 {n_off} 笔，不足 8。"
    elif med is None:
        verdict = "NO_RESULT"
        why = "没有 official_net。"
    elif med <= 0:
        verdict = "FAIL"
        why = f"计入 hp/stranded 后中位数 {pct(med)} ≤ 0。"
    else:
        verdict = "OPEN_NOT_LIVE"
        why = f"中位数 {pct(med)} > 0，但本轮不是上线许可；先看是否单币集中。"
    lines += [f"**{verdict}** — {why}", ""]

    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Canonical narrative lives in ROOT/REPORT.md; do not overwrite it.
    print("\n".join(lines[:40]))
    print(f"\nwrote {OUT / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
