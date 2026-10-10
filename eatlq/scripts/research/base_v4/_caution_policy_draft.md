# V4 内部 Caution 政策草案（非实盘规则）

> **状态**：研究草案 · **不得**写入 live executor / sizing / panic  
> **标签来源**：`roundtrip_strong`（211 WETH 池，offsets `[0,30,300,1800,7200]`）  
> **生成**：Phase 4 · 2026-06-25

---

## 1. Auto-skip 条件（强标签 + 模板 + tier）

以下任一满足 → **研究管线自动跳过**（非 live 黑名单）：

| 条件 | n（211 集） | 依据 |
|------|-------------|------|
| 强标签 ∈ `{dead, instant_hp, hook_block, delta_tax}` | dead **41/211** (19.4%) | 入口或即时 roundtrip 失败 |
| 强标签 = `delayed_rug` **且** tier = `returns_delta_high` **且** 模板 = `returns_delta_top_a` | 交叉子集见 `_roundtrip_template_crosstab.json` | RD-A 模板 clean **33%** (20/60)，fatal **67%** |
| 强标签 = `delayed_rug` **且** hook_family = 未匹配模板 RD | n≈59，fatal **78%** | 弱标签 100% swap_ok 但 roundtrip 大量 rug |
| tier = `standard` **且** 强标签 ≠ `clean` | 1/4 | n 过小，仅作方向性 |

**不 auto-skip**（需人工或更大样本）：

- `returns_delta_top_b`：n=50，roundtrip clean **100%** — 与 RD-A 相反，不能一并挡
- vanilla tier：clean **52%** (13/25)，fatal **48%** — 与 RD 接近，单靠 tier 不够

---

## 2. Manual review 条件

| 条件 | 说明 |
|------|------|
| 弱=`onchain_swap_ok` 且 强≠`clean` | **99/200** (49.5%) — 必须人工看 scan |
| 强=`delayed_rug` | **58/211** (27.5%) — 看 flip_block / offset |
| 强=`roundtrip_error` | **0/211** 本轮 — 若未来 >5% 先查 anvil |
| 新 hook 无模板且 7d Initialize **>50 池** | 触发 Task G `getCode`（≤15 RPC） |
| 模板 RD-A vs RD-B 冲突 | 同 tier 不同家族，禁止合并规则 |

---

## 3. 明确不做的事

- **不写**「可进场 / 可 sizing / 可部署 V4」
- **不修改** V3 live F 黑名单 / deployer 闸 / cluster_blacklist
- **不用** tx_replay_weak 替代 roundtrip 做 exit 决策
- **不用** 本表覆盖 `_labeled_universe.json`（644 V3 地面真值）

---

## 4. 与 V3 F 黑名单关系

| 维度 | V3 live F | 本草案 |
|------|-----------|--------|
| 数据源 | 644 roundtrip + deployer FP 验证 | 211 V4 WETH + hook 模板 |
| 作用域 | live executor | 研究 / 离线筛选 only |
| 更新 | 已上线 fixture | 独立 JSON，**零写入** live Redis |
| 重叠 | 无强制对齐 | V4 hook 结构风险 ⊄ V3 token 路径 |

---

## 5. worth_research_hook_filter

**维持**（不升级、不降级）：

- 14d Initialize hooked **79.7%**，RD **43.7%** >> 25% 阈值
- 强标签证明：弱 swap_ok **≠** clean（49.5% gap）
- 模板分化：RD-A fatal **67%** vs RD-B fatal **0%**（同 tier）→ 值得继续 hook 家族研究

---

*本文件供团队讨论；任何实盘变更须委托人另开任务。*
