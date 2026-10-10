---
task_id: GL5
title: 保守 KEYSTONE 在“真能不能卖掉”口径下的历史 edge 重算
author: 审查型指挥 Agent（人类已定向：reprice_conservative）
date: 2026-07-16
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
  broadcast_allowed: false
depends_on:
  - GL3_close_gl12_alpha_beta（冻结 +56% / 431-116 estimand）
  - reaudit10（秒级/块级出场窗、CONFIRMED_LP_RUG=-100% 口径）
  - GL4_live_track_record_reconciliation（真金失败模式：atomic 过关后仍 rug、48 次卖单失败）
  - D5 atomic buy→sell roundtrip 卖出模拟
  - transfer-net 储备重建（= balanceOf(pool)@block）
  - 已标注 universe JSON + fork-replay + archive RPC(prettiest-blue-aura)
---

# GL5 · 保守 KEYSTONE：把“卖出”改成“真能不能卖掉”后，历史 edge 还剩多少

## 0. 一句话目标

**不动任何真钱。** 取 GL3 冻结的那个约 +56% 的**保守 KEYSTONE estimand（原样、同 config、同秒级延迟）**，把它每一笔的**出场重新定价**：用 GL4 揭示的真实失败模式（“下单时 atomic 卖出模拟过关 ≠ 你真去卖时卖得掉”）去检验每个报价型出场是否**在假定的真实延迟下真的可成交**；rug/撤池导致的卖不掉按真实经济结果计（≈−100% 或末窗真实可回收值），**不给点内报价救济**。最终回答一个二元问题：

> **保守 F-on KEYSTONE 的历史超额收益，在“真能卖掉”口径下是幸存、还是主要是报价幻觉？**

任一答案都对决策有用：
- **幸存**（重定价后 delta 很小）→ 说明 GL4 的真金失败主要归咎于**上错了激进版(F off)**，保守版仍值得走 rug-硬化的真钱验证。
- **崩塌**（重定价后 edge 大幅缩水/转负）→ 说明 +56% 很大程度是报价把 rug 洗白了，KEYSTONE 本身脆,应转方向。

## 1. 硬约束（宪章）

- 纯离线/只读研究。**禁止**下单、签名、广播、启动 executor、写 Redis/signals、改 live 配置。`GO=NOT_MADE`，不自判 GO。
- **全程预注册**：§4 的所有阈值/窗口/延迟/容差，必须在看任何重定价结果前写进 preregistration 文件并出 SHA，之后**禁止**因结果回改。
- **对称诚实（禁止过度悲观 = 禁止过度乐观）**：
  - 确认 rug/撤池（链上储备真实坍塌，或真实卖单 status=0/revert）→ 记真实损失，不给报价救济。
  - 确认池仍存活且真能卖 → 允许按真实/atomic 成交价出场,**不许**默认全损。
  - **既不能确认存活、也不能确认坍塌**（数据不足）→ 记为 `UNRESOLVED`,给**双边界（乐观界/悲观界）区间**,**禁止**默认 −100%,也**禁止**默认按报价满额成交。
- 断言用 `==`；出回归测试；3–5 池 golden 链上独立对账；全部可复现，工件出 SHA256。

## 2. Step 0 —— 先钉死“被重算的对象”（不许换 config）

1. 从 GL3/reaudit10 工件**逐字段 pin** 出那个约 +56% 保守 estimand 的完整 config，并写进报告：
   - 选币规则 / F 名单版本（**必须确认是 F-ON 的哪一版**）、trail、TP ladder、仓位、模型、日期区间（431/116 那组）、**出场延迟窗（秒级/块级：1/5/15 块，以 reaudit10 为准）**。
   - 该 estimand 原本对 confirmed rug 怎么处理（reaudit10 是 −100%）、无真卖时怎么处理（atomic-guarded quote）。
2. **Arm-0 复现**：原样重跑该 config、原样用它的旧出场模型，**必须复现出与 GL1-12 IRON 记录一致的 headline（容差 §4 预注册）**。复现不出 → 停,报告口径漂移,不许继续。
   - 这是全流程的锚：证明我们重算的确实是那个 +56%，不是新造的东西。

## 3. Step 1 —— “真能不能卖掉”出场模型（本任务的核心）

对 Arm-0 里**每一笔** KEYSTONE 入场，在其**每一个 ladder 出场块**（按预注册的秒级/块级延迟），用下列**优先级**决定该腿的真实出场价，全程只用链上真相：

1. **有真实卖单证据**：出场块附近（预注册窗口内）存在**任意地址**对该 token 的真实 SELL swap（status=1）→ 用真实成交价（VWAP 或最近一笔，预注册择一）。这是最强证据。
2. **无真卖，但需前瞻可成交性检验**（GL4 的核心补丁）：
   - (a) **前瞻 rug 扫描**：从入场块起、覆盖到该出场块**再往后一个预注册的“真实执行滞后带”**（对应真金真去卖的那点延迟），扫描池储备(transfer-net = balanceOf(pool))。若在你**真能触发卖出之前**储备已坍塌 ≥ `RUG_COLLAPSE_PCT`（预注册，建议 90–99% 做敏感性），或该窗口内出现真实卖单 revert/status=0 的证据 → **判该腿在坍塌点已不可卖**：出场价 = 坍塌前最后一个真实可执行价；若坍塌发生在能出场之前 → 记 ≈ 真实可回收值（多为 ≈−100%）。**不给点内报价救济。**
   - (b) **仅当前瞻扫描确认池到出场时刻仍存活**，才允许用 D5 atomic buy→sell roundtrip 在出场块做一次卖出模拟，成交价含真实 haircut；atomic revert → 视同不可卖，退回 (a) 的坍塌处理。
3. **数据不足以判定 (a)/(b)**（RPC 缺档、区间无法覆盖）→ 该腿标 `UNRESOLVED`，给双边界（乐观=按 atomic 报价成交；悲观=按不可卖坍塌），**不并入点估计**，单列区间。

> 与 reaudit10 的差别（必须在报告讲清，避免重复劳动）：reaudit10 只在**入场决策块**做 atomic 守卫、对 confirmed rug 记 −100%；GL5 新增的是**前瞻可成交性**——决策块 atomic 过关**不代表**你按真实延迟真去卖时池子还在。GL4 的 48 次失败卖单 + 5 个坍塌未卖仓就是这条的实证。

## 4. 预注册（看结果前必须冻结 + SHA）

至少冻结：
- `RUG_COLLAPSE_PCT`（主值 + 敏感性档，如 90/95/99%）；储备口径 = transfer-net（golden 对 balanceOf）。
- 出场延迟窗（1/5/15 块，与 reaudit10 一致）+ “真实执行滞后带”块数。
- 真卖单匹配窗口、VWAP/最近一笔的择一规则、atomic haircut 口径。
- Arm-0 复现容差。
- 右尾判据：pooled mean>0、每窗下限、top-1% 贡献上限、MC 终值 p5 / 回撤 p95 阈值。
- `UNRESOLVED` 双边界的确切算法。
- **一律禁止看结果回改。**

## 5. 三臂对照 + 分解（预注册的产出）

- **Arm-0**：原 config + 原旧出场模型 → 复现 headline（锚）。
- **Arm-1**：同入场、同延迟，**新“真能卖掉”出场模型** → 点估计 + `UNRESOLVED` 区间。
- **基线**：同龄无条件随机买入，在**同一新出场模型**下的收益（比条件 alpha，不比绝对值）。
- **必出分解表**：
  1. 全部出场腿里，旧模型靠**报价救济**的比例。
  2. 这些报价救济腿里，前瞻扫描判定**真 rug/不可卖**的比例（= 报价洗白 rug 的规模）vs **确认存活、报价公允**的比例。
  3. 保守 F-on population 到底**碰到多少 rug**（对照 GL4 激进版“关 F 吃 207 rug”）——若 F-on 几乎不碰 rug，则 Arm-1≈Arm-0，edge 幸存；若 F-on 也大量吃 rug 被报价藏了，则 edge 崩。
- **右尾裁决**：Arm-1 是否仍满足 §4 右尾判据。给出 Arm-0→Arm-1 的 delta,以及“+56% 里有多少是报价幻觉”的量化结论。

## 6. Golden 对账 + 回归

- 抽 3–5 个腿（含至少 2 个被判 rug/不可卖、1 个判存活可卖），用 archive RPC 独立核 balanceOf(pool)@block、真实卖单、atomic 模拟，逐一贴锚。
- 回归测试覆盖：优先级 1/2a/2b/3 分支、坍塌阈值边界、UNRESOLVED 区间、Arm-0 复现,断言 `==`。

## 7. 交付物

- `GL5_conservative_realizable_exit_repricing_20260716.md`（主报告：Step0 pin、三臂、分解表、右尾裁决、结论二选一）
- `GL5_preregistration_20260716.md`（+ SHA）
- 机读 JSON（逐腿：入场/出场块、判定分支、出场价、PnL、UNRESOLVED 标记）
- `GL5_plain_language_20260716.md`（人话版：保守版到底扛不扛得住 rug、+56% 有多少是报价幻觉；尽量中文、把表格讲成人话，工作量要足）
- `GL5_independent_line_review_20260716.md`（独立逐行终审）
- artifact SHA256 清单 + 回归测试证据 JSON

## 8. 指挥裁定（给 codex 的边界与授权）

- **不许**为了让 Arm-1 好看去调任何阈值；不许换 config；不许把 UNRESOLVED 塞进点估计。
- **不许过度悲观**：数据不足 ≠ 全损；只有链上确认坍塌/卖单失败才记真损。
- 允许你在 §3/§4 的框架内自定实现细节，但**每个阈值都要先预注册**。
- 一次做完（Step0→回归），最后一次性交我审。`GO=NOT_MADE`。
