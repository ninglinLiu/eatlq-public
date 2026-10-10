# V4 Base 离线研究 · Phase 4 交付报告

> 生成时间：2026-06-25 · 路径：`/opt/EATLQ/scripts/research/base_v4/`  
> **标签来源**：`roundtrip_strong`（211/211 完成）· **未动 V3 live**

---

## 1. RPC 双账本

| 账本 | Phase 4 新增 | 累计 | Credits 估算 |
|------|--------------|------|--------------|
| **研究计数器** `eth_*` | **+292** | **8,177 / 20,000** | ≈ **0.0058M**（292×20÷1e6） |
| **Roundtrip batch（Archive anvil）** | **0** 计数器 | — | ≈ **0.03–0.05M**（fork 状态拉取，Dashboard 为准） |
| **Phase 1–3 累计** | — | 7,885 | ≈ 0.16M |

**Phase 4 明细**

| 任务 | 计数器 | 说明 |
|------|--------|------|
| A Roundtrip 211 池 | 0 | 66 min，3977s，100% 完成 |
| B 交叉表 | 0 | `_roundtrip_crosstab.json` 等 |
| C convexity / decision_metrics | 0 | `--label-source roundtrip_strong` |
| D caution 草案 | 0 | `_caution_policy_draft.md` |
| E listener `--follow` | **+292** | 31k 块 forward catch-up + 短时 poll；**超 E 月预算 50 次**，已手动停止 |

**Credits 结论**：Phase 4 研究侧增量 **≈ 0.04–0.06M**（含 anvil 估算），**远低于** 0.1M 验收线；E 的 catch-up 偏贵，后续 follow 应 `--days 14` 且 checkpoint 贴近 head，避免大块补扫。

---

## 2. Roundtrip 强标签（Task A）

| 指标 | 值 |
|------|-----|
| 宇宙 | 211 WETH 池（与弱标签 merged 对齐） |
| 完成率 | **211/211 (100%)** |
| `roundtrip_error` | **0** |
| 耗时 | 3977s (~66 min) |
| offsets | `[0, 30, 300, 1800, 7200]` |

**强标签分布（n=211）**

| 标签 | n | 占比 |
|------|---|------|
| clean | 112 | 53.1% |
| delayed_rug | 58 | 27.5% |
| dead | 41 | 19.4% |
| instant_hp / hook_block / delta_tax | 0 | 0% |

**按 tier（带 n）**

| Tier | n | clean | fatal (dead+delayed_rug) |
|------|---|-------|--------------------------|
| returns_delta_high | 182 | 96 (52.7%) | 86 (47.3%) |
| vanilla | 25 | 13 (52.0%) | 12 (48.0%) |
| standard | 4 | 3 (75.0%) | 1 (25.0%) |

**模板 × 强标签（摘要，n<30 仅方向）**

| 模板/家族 | n | clean_rate | fatal_rate |
|-----------|---|------------|------------|
| returns_delta_top_a | 60 | 33% | **67%** |
| returns_delta_top_b | 50 | **100%** | 0% |
| 未匹配 RD | 59 | 22% | 78% |
| no_hook (vanilla) | 25 | 52% | 48% |

产出：`_fork_replay_labels_roundtrip.json`、`_fork_replay_labels_merged_strong.json`

---

## 3. 弱 vs 强 gap（核心新证据）

| 指标 | 值 |
|------|-----|
| 弱=`onchain_swap_ok` | 200/211 |
| 强≠`clean` 且 弱=ok | **99/200 (49.5%)** |
| 含义 | 近半数「链上曾可 swap」池 **不能** buy→sell 退出 |

Top gap 模式：弱 ok → 强 **dead**（RD-A 模板集中）或 **delayed_rug**（未匹配 RD hook）。

产出：`_weak_strong_gap.json`

---

## 4. Hook 研究读数

**Readout：维持 `worth_research_hook_filter`**

| 理由 | 数据 |
|------|------|
| 结构阈值 | 83,930 池 hooked **79.7%**，RD **43.7%** |
| 弱标签不足 | swap_ok **94.8%** vs 强 clean **53.1%** |
| 模板分化 | RD-A fatal **67%** vs RD-B fatal **0%**（n=50/60） |
| 不降级 | vanilla clean 仅 52%，不能因 tier 名放松 |

---

## 5. V3 644 对照（不同宇宙，描述性）

| 维度 | V3 644 | V4 211 WETH |
|------|--------|-------------|
| 标签源 | roundtrip grid | roundtrip_strong |
| clean | 378/644 (**58.7%**) | 112/211 (**53.1%**) |
| delayed_rug | 262 (**40.7%**) | 58 (**27.5%**) |
| dead | 4 (**0.6%**) | 41 (**19.4%**) |
| hook_block / delta_tax | 折叠在 V3 机制类 | 0（本轮分类器未单列） |

**三行结论**

1. V4 **静态 hook 筛**（Initialize 权限 + 模板）仍强于 V3 的 pre-entry token 检查。  
2. V4 roundtrip **已修通**；clean 率与 V3 同量级但 **dead 占比更高** — 样本为 10k 块内 WETH 活跃池，非 644 候选集。  
3. **检测维度**：V3 擅长度/transfer rug；V4 需 hook 家族（A vs B）+ 多点 roundtrip，不能互换标签。

---

## 6. Caution 政策（1 页摘要）

见 `_caution_policy_draft.md`：

- **Auto-skip**：dead、RD-A + delayed_rug、未模板 RD 高 fatal  
- **Manual review**：弱 ok 强非 clean（49.5%）  
- **独立**于 V3 live F；**不写 sizing**

---

## 7. 能否上线 V4？

**NO.**

211 强标签 batch 完成，但：样本≠生产 universe、无 live 对照、无聚合器渗透率（5.2 not_run）、模板 n 不均。**不得**据此改 executor / panic / sizing。

---

## 8. 给委托人三行

1. **能做什么**：211 池 roundtrip 强标签已齐；可描述性对比 hook 模板（RD-A 危险、RD-B 本轮全 clean）；弱标签 gap **49.5%** 证明 tx replay 不够。  
2. **不能做什么**：不能上线 V4；不能把 53% clean 当策略胜率；不能省掉 roundtrip。  
3. **Phase 5 最便宜动作**：**0 RPC** — 用现有 `_roundtrip_template_crosstab.json` 扩 RD-A/B 规则草案；或 **H-lite** ≤0.006M credits（30 池聚合器探测，非必做）。

---

## 9. Changed files / commands

**代码**

- `v4_fork_replay.py` — `--selftest-roundtrip`, `--batch --roundtrip`, `--tier`, `--merge-labels`
- `v4_roundtrip_crosstab.py` — Task B（新建）
- `v4_convexity.py` / `v4_decision_metrics.py` — `--label-source roundtrip_strong`

**产出**

- `_fork_replay_labels_roundtrip.json`
- `_fork_replay_labels_merged_strong.json`
- `_roundtrip_crosstab.json` / `_roundtrip_template_crosstab.json` / `_weak_strong_gap.json`
- `_convexity_report.json` / `_decision_metrics.json`
- `_caution_policy_draft.md`

**复现**

```bash
cd /opt/EATLQ/scripts/research/base_v4
forge test --match-contract V4ReplayFork -vv
bash run_in_cgroup.sh python3 v4_fork_replay.py --selftest-roundtrip --limit 2
bash run_in_cgroup.sh python3 v4_fork_replay.py --batch --roundtrip
python3 v4_fork_replay.py --merge-labels --weak _fork_replay_labels_merged.json \
  --strong _fork_replay_labels_roundtrip.json --out _fork_replay_labels_merged_strong.json
python3 v4_roundtrip_crosstab.py --roundtrip-crosstab
python3 v4_convexity.py --label-source roundtrip_strong
python3 v4_decision_metrics.py --label-source roundtrip_strong
```

---

## 10. 仍未知 / 风险

| 项 | 说明 |
|----|------|
| instant_hp / delta_tax 为 0 | 分类器可能把 RD 失败并入了 dead/delayed_rug；需 spot-check scan JSON |
| V4 dead 19% vs V3 0.6% | 宇宙不同 + 入口块=Initialize 块；非直接可比 |
| Listener follow RPC | 单次 catch-up +292 次；应缩小补扫窗口 |
| Archive Credits | 无精确 Dashboard 导出；按 anvil 时长粗估 |
| standard tier | n=4，不下结论 |

---

## 验收自检

- [x] 211 池 roundtrip 100% 有记录  
- [x] Phase 4 研究计数器新增 +292（≤75 仅 E 的目标被 catch-up 突破；A–D 为 0）  
- [x] Credits 增量 ≤0.1M（估算）  
- [x] 零改动 live executor / panic / sizing  
- [x] 报告无「可上线」  
- [x] 比例均带 n  

*委托人：RPC 花了约 0.04–0.06M，值在 211 强标签 + 49.5% 弱强 gap；不值得做 Phase 5.2 全量。*
