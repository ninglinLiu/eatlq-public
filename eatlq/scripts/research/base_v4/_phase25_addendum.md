# Phase 2.5 续研摘要

> 接 Phase 2 · **RPC 新增 0**（累计仍 **7,885/20,000**）

---

## 本轮完成

### 1. 模板家族统计（0 RPC）
- 重跑 `v4_hook_classifier.py --cache-only`（7785 hook 全在 bytecode cache）
- **69.9%** 池（56,471/80,793）匹配 20 个已知模板
- 产出：`_template_family_stats.json`、`_template_label_crosstab.json`

**模板 × 弱标签（n=211，描述性）**

| 家族 | n | swap_ok |
|---|---|---|
| template:returns_delta_top_a | 60 | 98.3% |
| returns_delta_high（未匹配模板） | 59 | 100% |
| template:returns_delta_top_b | 50 | 86.0% |
| vanilla | 25 | 100% |
| template:returns_delta_top_c | 3 | 66.7% |

→ Top RD 模板 A/B 在弱标签上有可比分层；**非显著、非 exit 证明**。

### 2. Index 内 WETH 池打标补全（0 RPC）
- 修复 batch **覆盖写入 bug**（现累积写入 `done` 全集）
- 补标 index 内剩余 WETH → **合并 n=211**（= index 内全部 211 WETH 池）
- 弱标签：**200 ok / 6 blocked / 5 dead**（94.8%）

### 3. 更新 Phase 4–5
- RD tier n=182，swap_ok **95.1%** vs vanilla **100%**（n=25）
- readout 维持：`worth_research_hook_filter` + `tx_replay_viable`

---

## 给委托人三行

1. **能做什么**：20 个 hook 模板覆盖近 70% 新池；211 个 WETH 池全有弱标签，可按模板家族比 replay 成功率。
2. **不能做什么**：仍不能证明买卖退出；RD 模板 B replay ok 86%（n=50）只是线索，不是风控规则。
3. **最便宜下一步**：0 RPC — 把模板家族接进 listener 实时分类；或停在这里等 roundtrip 有解。

---

## 变更文件
- `v4_hook_classifier.py` — `--cache-only`
- `v4_template_family.py` — 新建
- `v4_fork_replay.py` — `--resume-unlabeled`、batch 累积写入修复

---

*2026-06-24 · 非 deployable · live 未动*
