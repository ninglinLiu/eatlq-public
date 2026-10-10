# V4 Base 离线研究 · Phase 5 交付报告

> 生成时间：2026-06-25 · 路径：`/opt/EATLQ/scripts/research/base_v4/`  
> **Verdict：`v4_alpha_candidate_no`**（2/4 通过线）· **未动 V3 live**

---

## 1. RPC 双账本

| 账本 | Phase 5 任务 | 新增 | 累计 |
|------|-------------|------|------|
| **研究计数器** `eth_*` | A–D 分析 | **0** | — |
| | PnL refresh（896 anvil） | **0** | — |
| | 文件读数（会话末） | — | **10,685 / 20,000** |
| **QuickNode Credits（M）** | PnL refresh archive | **≈0.04–0.05M** 估算 | — |

**说明**

- Phase 4 结束时计数器 **8,177**；会话末 **10,685**（+2,508）。**PnL refresh 走本地 anvil fork，不计入研究计数器**；增量可能来自其他研究进程（需 Dashboard 交叉核对）。
- Phase 5 **未执行** Task E/F/G/I（见 §8）。
- **未做** Phase 4 式 listener 大块 catch-up。

---

## 2. 核心结论（给委托人）

**Phase 4 的「RD-B 100% clean」≠「能赚钱」。**

在 **Initialize 块、0.003 ETH 往返 roundtrip** 口径下（与 V3 keystone `ret = weth_out/spend - 1` 对齐）：

| 子集 | n | 正收益占比 | 中位 PnL | 右尾 ≥+100% | 致命 ≤−50% |
|------|---:|---:|---:|---:|---:|
| **V4 clean** | 112 | **0%** | **−2.6%** | 0% | 8.9% |
| **V4 RD-B** | 50 | **0%** | **−2.6%** | 0% | **0%** |
| **V4 RD-A** (clean) | 20 | 0% | −2.4% | 0% | 5% |
| **V4 vanilla** (clean) | 13 | 0% | ≈0% | 0% | 0% |
| **V3 clean** (realgas*) | 142 | 75.4% | +72% | 42.3% | 3.5% |

\*V3 列为 `_realgas_replay.json` walk-forward（含 trail/exit/gas），**非** raw Initialize roundtrip — **不可直接比绝对值**，但方向性足够判「V4 Initialize 进场无右尾」。

**一句话**：RD-B = **可安全退出（0% 致命）**，但在 Initialize 进场 **只有 ~−2.6% 摩擦，无正 EV、无右尾**；**不能**作为 Phase 6 部署依据。

---

## 3. 技术修复（PnL 可信性）

Phase 4 `_fork_replay_labels_roundtrip.json` 中 `weth_back` 因 BalanceDelta 解析 **全部为 uint256 垃圾值**。

- 修复：`v4_pool_swap.py` 改为 **WETH balance net / spend − 1**（与 V3 fork_replay 同级）。
- 重算：896 次 anvil replay → `_alpha_pnl_cache.json`（112 clean × 5 offsets + 3 晚发现 Δ）。

---

## 4. Task A · Clean PnL（n=112）

| 指标 | offset=300 | best_offset |
|------|------------|-------------|
| 正收益占比 | **0%** | **0%** |
| 中位 PnL | −2.6% | −2.6% |
| 均值 PnL | −11.6% | −11.2% |
| 致命 ≤−50% | 8.9% | 8.9% |
| 右尾 ≥+100% | 0% | 0% |

产出：`_alpha_pnl_clean.json`、`_alpha_pnl_summary.json`

**含义**：112 个「能 roundtrip 退出」的池，在 **Initialize 进场** 下 **无一盈利**；8.9% 在部分 offset 仍 ≤−50%（多为 unmatched RD clean）。

---

## 5. Task B · RD-B 深潜（n=50）

| 项 | 值 |
|---|---|
| roundtrip clean | 50/50 (100%) |
| PnL 正收益 | **0/50 (0%)** |
| 中位 PnL | **−2.6%**（全 offset 恒定） |
| 致命 | **0%** |
| jsonl 内 RD-B 总池 | **9,591**（仅 50 在 10k swap index 可打标） |

**vs RD-A clean (n=20)**：中位 −2.4%，致命 5% — RD-B **更安全**，但 **同样无 alpha**。

**vs unmatched RD clean (n=26)**：中位 −2.4%，致命 **34.6%** — 模板化有价值。

产出：`_alpha_rdb_deep.json`、`_alpha_template_compare.json`

---

## 6. Task C · 进场窗敏感性

**RD-B (n=50)**：offsets `[0,30,300,1800,7200]` 中位 PnL **均为 −2.6%** — **无早进溢价、无晚进衰减**（池内价格/费率静态）。

**vanilla (n=13)**：中位 ≈ **0%**（微负），全 offset 平坦。

**晚发现 Δ∈{100,500,2000}**：RD-B / vanilla median 与 offset=0 **相同** — 晚 2000 块 **无额外 PnL 衰减**（因本身已负或零）。

产出：`_alpha_offset_sensitivity.json`

---

## 7. Task D · Alpha 通过线

| # | 条件 | 结果 | 数据 |
|---|------|------|------|
| 1 | RD-B 正收益 > V3 或 ≥45% | **FAIL** | 0% vs 75.4% |
| 2 | RD-B 右尾 ≥+100% ≥5% | **FAIL** | 0% (n=50) |
| 3 | RD-B 致命 ≤ vanilla 且 clean 100% | **PASS** | 0% vs 0%；50/50 clean |
| 4 | offset 0–300 median ≥ late Δ2000 | **PASS*** | 均为 −2.6%（**平局非溢价**） |

**Verdict：`v4_alpha_candidate_no`**（2/4；核心 #1 #2 均 FAIL）

产出：`_alpha_pass_fail.json`

---

## 8. 未执行任务（及原因）

| 任务 | 状态 | 原因 |
|------|------|------|
| **E** 扩 RD-B roundtrip | 未做 | jsonl 9,591 池但 **Initialize 进场 PnL 已证无正 EV**；扩样本不改结论 |
| **F** swap index 延伸 | 未做 | 112 clean 已够；无 alpha 信号 |
| **G** listener follow | 未做 | Phase 4 catch-up 教训；本轮优先 PnL |
| **I** H-lite 聚合器 | 未做 | 无 alpha 前提下 ROI 低 |

---

## 9. 给委托人三行

1. **能做什么**：V4 RD-B = **安全退出家族**（0% 致命）；RD-A / 未匹配 RD = 危险；**Initialize 进场无赚钱边**（中位 −2.6%，0% 正收益）。
2. **不能做什么**：**不能上 live V4**；不能把「clean」当「可盈利」；不能跳过 roundtrip；V3 live **照旧**。
3. **Phase 6 最便宜动作**：**0 RPC** — 研究 **非 Initialize 进场窗**（首 swap 后 / 深池 / 活动热度触发）是否出现右尾；或 **暂停 V4 alpha 线**，把 RPC 留给 V3 衰减监控。

---

## 10. Hook 研究读数

**维持 `worth_research_hook_filter`**（结构 79.7% hooked）— 但 **alpha 线当前 NO**。

RD-B 价值在 **risk filter（auto-skip RD-A / unmatched）**，不在 **PnL edge**。

---

## 11. 验收自检

- [x] A/B/C/D 全部完成
- [x] 所有 PnL 带 n
- [x] `_alpha_pass_fail.json` 可审计
- [x] PnL refresh 研究计数器 **0**（anvil only）
- [x] 零改动 live executor / panic / sizing
- [x] 报告无「可上线 V4」

---

## 12. Changed files / commands

**代码**

- `v4_alpha_readout.py`（新建）
- `v4_pool_swap.py`（WETH balance PnL 修复）

**产出**

- `_alpha_pnl_cache.json`（112 池 × 896 replays）
- `_alpha_pnl_clean.json` / `_alpha_pnl_summary.json`
- `_alpha_rdb_deep.json` / `_alpha_template_compare.json`
- `_alpha_offset_sensitivity.json` / `_alpha_pass_fail.json`
- `_phase5_handoff_report.md`

**复现**

```bash
cd /opt/EATLQ/scripts/research/base_v4
python3 live_gate.py --check-open-pools
bash run_in_cgroup.sh python3 v4_alpha_readout.py --mode refresh-pnl --clean-only
python3 v4_alpha_readout.py --mode all --write-report _phase5_handoff_report.md
```

---

## 13. 仍未知

| 项 | 说明 |
|----|------|
| 非 Initialize 进场 | 是否在首 swap / 深池 / 热度触发后有正 EV — **本轮未测** |
| V3 vs V4 绝对 PnL | 口径不同（realgas trail vs raw roundtrip） |
| RD-B 9,591 池 | 仅 50 在 index；扩样本 **不改变 Initialize 无 alpha 结论** |
| 计数器 +2,508 | 非 anvil 路径；需 Dashboard 核对来源 |

*委托人：V4 当前证据支持「RD-B 可安全研究 / 可筛池」，**不支持「Initialize 进场有钱赚」**；Phase 6 应换进场窗或降 RPC 档。*
