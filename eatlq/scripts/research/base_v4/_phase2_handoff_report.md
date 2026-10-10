# V4 Base 离线研究 · Phase 2 交付报告

> 生成时间：2026-06-24 · 执行路径：`/opt/EATLQ/scripts/research/base_v4/`  
> **标签来源**：`tx_replay_weak`（非 roundtrip）· **未动 V3 live**

---

## 1. RPC 总账

| 项 | 数值 |
|---|---|
| Phase 1 结束 | 7,863 |
| **Phase 2 新增** | **+22** |
| **累计** | **7,885 / 20,000** |
| 剩余 | 12,115 |
| Phase 2 预算上限 | 8,000（实际用 0.3%） |

**Phase 2 RPC 明细**

| 动作 | 次数 |
|---|---|
| Swap index 扩 500→5,000 块 | +3 |
| Swap index 扩 5,000→10,000 块 | +3 |
| Top hook `eth_getCode`（16 个新模板） | +16 |
| tx-only batch | 0（anvil archive fork） |

---

## 2. 样本量（tx-only 弱标签）

| 集合 | n | 说明 |
|---|---|---|
| v1 标签 | 24 | Phase 1 batch |
| v2 新打标 | 100 | 本轮 batch |
| **合并去重** | **113** | `_fork_replay_labels_merged.json` |

**合并标签分布（n=113）**

| 标签 | n | 占比 |
|---|---|---|
| onchain_swap_ok | 107 | 94.7% |
| replay_blocked | 3 | 2.7% |
| dead | 3 | 2.7% |

**分层 n（合并集）**

| Tier | n | swap_ok 率 | blocked 率 | dead 率 |
|---|---|---|---|---|
| vanilla | 25 | **100%** (25/25) | 0% | 0% |
| returns_delta_high | 84 | **95.2%** (80/84) | 3.6% (3/84) | 1.2% (1/84) |
| standard（其他 hooked） | 4 | **50%** (2/4) | 0% | 50% (2/4) |

**batch 分层缺口（如实报告）**

- 目标每层 ≥25；index 内 **other_hooked（standard）WETH 池仅够 ~4**，未凑假样本。
- returns_delta_high 在 100 槽位中占 71（因 other_hooked 不足被 RD 填满）。

---

## 3. Swap Index（Task B）

| 指标 | Phase 1 | Phase 2 |
|---|---|---|
| lookback | 500 块 | **10,000 块** |
| 索引池数 | 542 | **2,995** |
| WETH 池在 index 内 | — | **211**（10k 块内 jsonl 交集） |
| 增量 checkpoint | 无 | **有**（`earliest_block` + chunk 元数据） |

---

## 4. Hook 研究是否继续？

**Readout：维持 `worth_research_hook_filter`（不升级、不降级）**

**理由（均带 n / 口径）**

1. **结构（category b）**：14d 80,793 池，hooked **79.6%**，RETURNS_DELTA **43.5%** >> 25% 阈值；日桶趋势 hooked% 略降（86.9%→78.4%），仍远高于阈值。
2. **弱标签（category c）**：n=113 时 RD tier swap replay ok **95.2%** vs vanilla **100%**（n=25）；差距存在但 **未做显著性检验**，standard tier n=4 不可靠。
3. **仍非策略**：无 roundtrip exit 标签，不能推 deploy。

---

## 5. Tx replay 定位（一句话）

> **弱标签 =「该池在链上曾出现可 eth_call 复现的 Swap 交易」；不等于「我们能否 buy+sell 盈利退出」。**

---

## 6. Roundtrip

| 状态 | 说明 |
|---|---|
| **仍 blocked** | Base fork WETH settle-in-unlock 未解 |
| Phase 3 再开？ | **否（默认）** — 委托人已拍板本轮跳过；仅保留 `foundry/test/V4ReplayFork.t.sol` 文档 |

---

## 7. 给委托人的人话三行

1. **能做什么**：用 hook 地址权限 + 14 天池子结构筛「高风险 V4 池」；用 tx replay 弱标签做 tier 间 **描述性** 对比（n=113）。
2. **不能做什么**：不能证明能安全买卖退出；**不能上线**；不能把 tx replay 当 V3 式 clean/rug 标签。
3. **若继续、最省钱的下一步**：**0 RPC** — 把 classifier 模板库（现 20 个）接到 listener 输出上做 hook 家族统计；或等 roundtrip 有解再开 Phase 3。

---

## 8. Task A 摘要（0 RPC）

| 子任务 | 产出 |
|---|---|
| A1 日趋势 | `_hook_trend_by_day.json` — hooked% 略降，RD% 略降 |
| A2 Top-30 hook | `_hook_top30.json` — #1 clanker 24k 池，Top3 RD 已模板化 |
| A3 弱标签交叉表 | `_weak_label_crosstab.json` — 见 §2 |
| A4 V3 644 对照 | `_v3_v4_static_detect_compare.json` — V4 静态 hook 筛强于 V3；V3 roundtrip 强于 V4 exit |

---

## 9. Task D（模板库）

- 新增 **16** 个 Top hook runtime hash（共 **20** 模板）
- RPC：**16** 次 `eth_getCode`（Top-20 中 4 个已有模板跳过）

---

## 10. Changed files / commands

**新建**

- `v4_offline_analysis.py`
- `v4_hook_templates_topn.py`
- `_hook_trend_by_day.json`, `_hook_top30.json`, `_weak_label_crosstab.json`
- `_v3_v4_static_detect_compare.json`
- `_fork_replay_labels_v2.json`, `_fork_replay_labels_merged.json`
- `_fork_replay_batch_checkpoint.json`
- `_phase2_handoff_report.md`（本文件）

**修改**

- `v4_tx_replay.py` — 增量 index + dry-run
- `v4_fork_replay.py` — quota batch、v2 输出、90min 上限、merge
- `v4_convexity.py`, `v4_decision_metrics.py` — 读 merged/v2
- `hook_templates.json` — 4→20 模板

**命令顺序**

```bash
cd /opt/EATLQ/scripts/research/base_v4
python3 v4_offline_analysis.py
python3 v4_fork_replay.py --dry-run-index --lookback 5000
python3 v4_fork_replay.py --build-index --lookback 5000
python3 v4_fork_replay.py --build-index --lookback 10000
bash run_in_cgroup.sh python3 v4_fork_replay.py --batch --tx-only --limit 100 --min-per-tier 25
python3 v4_hook_templates_topn.py --top 20
python3 v4_fork_replay.py --merge-labels
python3 v4_convexity.py && python3 v4_decision_metrics.py
```

---

## 11. 仍未知 / 风险

1. Initialize 计数 ≠ TVL/成交量；hook 与 rug **无因果证明**。
2. standard tier n=4，任何结论都 **不可外推**。
3. tx replay 失败（blocked/dead）可能含聚合器/permit 路由 artifact。
4. Roundtrip settle bug 根因未明；Phase 3 前无法产出 V3 644 同级标签。
5. index 10k 块仅覆盖近期活跃 swap 池；长尾 RD 池可能不在 index 内。

---

## 12. 验收自检

- [x] Phase2 新增 RPC **22** ≤ 8,000；总 **7,885** ≤ 20,000
- [x] 合并 tx-only **n=113** ≥ 80
- [x] 零改动 live executor / panic / sizing
- [x] 无「可上线」「可部署策略」表述
- [x] 比例结论均带 n

---

*Phase 2 完成 · 证据链：结构占比 → hook 分桶 × 弱 ground truth 描述统计（n=113）*
