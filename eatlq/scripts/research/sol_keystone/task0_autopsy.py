#!/usr/bin/env python3
"""Task 0 — autopsy of prior Solana research (zero RPC)."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "_sol_autopsy.md"

EVIDENCE = """
# Solana 旧失败解剖 (Task 0)

生成：2026-06-25  
方法：仓内 artifact 检索 + git/审计读 out；**零 RPC**。

## 0.1 检索结果

| 类别 | 路径 | 说明 |
|------|------|------|
| Solana Hunter / kstorm | **SEED_ONLY** | `docs/retrospectives/2026-05-09_two_month_retrospective_evidence_index.md` L64：前传 Solana Hunter **无仓内独立 artifact**，仅 LASZLO 复盘 docx 叙述 |
| pump.fun 主回测 | `mvp/audits/sprint099_solana_righttail/` | 1488→4227 新币样本，RPC 已缓存 |
| 聪明钱跟随 | `mvp/diagnostics/sol/smart_buyers*.jsonl` + `mvp/audits/sprint099/dirAB_verdict_cn.md` | 大样本 n=2015，净 EV 负 |
| 延迟/执行 | `scripts/research/sol/latency_decay.py` + `mvp/audits/sprint101_speed_thesis/latency_decay_verdict_cn.md` | 同块上限仍负 |
| 毕业后 AMM | `mvp/audits/sprint099_solana_righttail/dirAB_verdict_cn.md` A 节 | N=69，系统性负（sell-the-graduation） |
| ClickHouse Sol 表 | **无** | `SHOW TABLES FROM eatlq` 无 sol/pump/raydium 表；本轮 universe 来自 `mvp/diagnostics/sol/*.jsonl` |

## 0.2 死因判定：**(B) 离线有边、执行被吃** — 部分成立；旧线主因是 **(A) 对可部署策略离线也没边**

### 证据链

**1. 旧「Solana Hunter / 聪明钱」线 → 更接近 (A) + 跟错对象**

- 大样本聪明钱跟随（n=225 smart-present，成本 3%）：**净 EV −2.2%**，毛 EV 仅 +0.8%（`dirAB_verdict_cn.md` L67-71）
- 选择性真实（lift 2.5x）但 **「信号预测赢家 ≠ 跟着进场点能赚钱」** — 与 KEYSTONE 结构性凸性不同，属幸存者偏差跟单
- 延迟衰减（`latency_decay_verdict_cn.md`）：SMART_FILL 同块上限 **−12.8% 净 EV**；提前进场更亏，非单纯「慢 60s 被吃」

**2. 执行/MEV 层 → (B) 成立但未单独量化**

- 复盘 seed：`Solana / 三明治` — 「执行语义和抢跑现实会杀死纸面策略」（`2026-05-09_two_month_retrospective_plain_cn.md` L42）
- sprint101 明确：**未建 geyser+Jito**；估算基建 ~$500–2000/mo，对 solo 本金结构性不成立
- 旧研究报的是 **单一 3% 往返成本**，未分离 `offline_edge` vs `executable_edge`（执行折让）— 本轮 Task 5 补此缺口

**3. 方法/数据 → (C) 部分曾存在，已修**

- 价格重建 bug 已根除（`dirAB_verdict_cn.md` 审查 P4：exec/spot 中位 0.998）
- 前视风险：`ath_mc` 仅作标签，特征用出生固定量 + 早期路径（已审计）
- 随机切分：旧线用 **按日 TRAIN/TEST**（06-11~13 / 06-14~15），非随机 — 可继承

### 综合裁定

| 子问题 | 归类 | 说明 |
|--------|------|------|
| 聪明钱跟单可部署？ | **(A)** | 离线净 EV 负，不可部署 |
| 右尾是否存在？ | 存在但稀有 | ATH≥$20k ~2.9%（stage123） |
| 执行/MEV 是否杀死纸面边？ | **(B)** | 旧线未量化折让；bonding curve 上「同块买在 spike 顶」是结构问题 |
| look-ahead / 方法 bug？ | **(C) 已修** | 价格路径已自测 |

**本轮 KEYSTONE 移植是否继续：是。** 旧失败主因是 **跟错对象（聪明钱/静态元数据）**，不是 KEYSTONE「剪左尾 + 凸性篮子」已验证边本身。Sol 右尾肥、rug 率高，与 Base KEYSTONE 假设对齐；需用 **结构性早期路径特征**（非聪明钱）+ **执行折让** 重验。

## 0.3 与本轮对策映射

| 旧死因 | 本轮对策 |
|--------|----------|
| MEV/三明治未量化 | Task 5 分离 offline vs executable |
| 聪明钱幸存者偏差 | **不做跟单**；走左尾 ML + 等权凸性篮子 |
| ClickHouse 无 Sol | 用已缓存 JSONL（4227 labeled, 2104 price grid），零新 RPC |
| 事后硬 gate | 继承 KEYSTONE 宪章：浅过滤 + 时间 holdout |
"""


def main():
    OUT.write_text(EVIDENCE.strip() + "\n")
    meta = {
        "verdict": "B_partial_A_for_old_smart_money",
        "primary_old_failure": "smart_money_survivorship_not_executable",
        "execution_unquantified": True,
        "continue_keystone_port": True,
        "clickhouse_sol_data": False,
        "evidence_paths": [
            "mvp/audits/sprint099_solana_righttail/",
            "mvp/audits/sprint101_speed_thesis/latency_decay_verdict_cn.md",
            "docs/retrospectives/2026-05-09_two_month_retrospective_evidence_index.md",
        ],
    }
    (Path(__file__).resolve().parent / "_sol_autopsy_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n"
    )
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
