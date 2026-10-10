---
task_id: GL5-v2
title: 644/120 F-on KEYSTONE 在“真能不能卖掉”口径下的历史 edge 重定价
author: 审查型指挥 Agent（人类已定向：reprice_Fon_644）
date: 2026-07-16
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
  broadcast_allowed: false
supersedes_object: GL5 因 STOP_OBJECT_IDENTITY_MISMATCH 中止；本任务改钉一个真实存在的对象
target_object:
  name: 2026-06-24 conservative live-config rebacktest（F-on）
  source_json: mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest.json
  source_script: live_config_rebacktest.py（_load_blacklists / gate_masks().live_full）
  universe: 644 labeled pools → F-on 后 pass_n=120
  F: 245 safe-rug deployer + 2 PARADAENGI = 247 deployer + 33 skeleton
  size: ~0.0302 ETH（约 $50）；trail 35%
depends_on:
  - GL5（§3 可实现出场模型、对称诚实口径、预注册纪律）
  - GL4（真金失败模式：atomic 过关后仍 rug、48 次卖单失败）
  - D5 atomic buy→sell roundtrip 卖出模拟
  - transfer-net 储备重建（= balanceOf(pool)@block）
  - archive RPC(prettiest-blue-aura)
---

# GL5-v2 · 重定价真正带 F-on 防护的 644/120 对象

## 0. 为什么改钉这个对象（承接 GL5 的发现）

GL5 已用严格 `==` 证明：被称颂的 `+56%` 是 **431/116、零 F 黑名单、~92% 报价型证据**的对象（GL1-01 PINNED）。它**不是** F-on 保守版。真正带 rug 防护、最接近可部署形态的是 **2026-06-24 的 644/120 F-on 回测**。GL4 又证明真金的杀手是 **rug + 卖不掉**。因此本任务只做一件事：

> **把 644/120 F-on 这个真实存在的对象，按 GL4 揭示的“真能不能卖掉”口径重新定价，回答它在真实成交标准下还剩不剩 edge。**

**不许**把它的 headline 说成 `+56%`；**必须**先老实把它自己的 headline 数值钉出来。

## 1. 硬约束（宪章）

- 纯离线/只读研究。禁止下单、签名、广播、启动 executor、写 Redis/signals、改 live 配置。`GO=NOT_MADE`，不自判 GO。
- **全程预注册**：§4 所有阈值/窗口/延迟/容差，看任何重定价结果前写进预注册文件并出 SHA，之后禁止因结果回改。发现预注册与源码不符按 GL5 先例**明示勘误、不静默改**。
- **对称诚实（禁止过度悲观 = 禁止过度乐观）**：
  - 确认 rug/撤池（链上储备真实坍塌，或真实卖单 status=0/revert）→ 记真实损失，不给报价救济。
  - 确认池仍存活且真能卖 → 允许按真实/atomic 成交价出场，不许默认全损。
  - 既不能确认存活、也不能确认坍塌 → 记 `UNRESOLVED`，给**双边界区间**，禁止默认 −100%，也禁止默认按报价满额成交。
- 断言用 `==`；出回归测试；3–5 池 golden 链上独立对账；全部可复现，工件出 SHA256。

## 2. Step 0 —— 钉死对象 + 复现锚（不许换 config）

1. 从 `live_config_rebacktest.json` + 源码**逐字段 pin** 出该 644/120 F-on 对象的完整 config，写进报告（universe、日期窗与 **observed coverage**、模型阈值 p≤0.27、min-depth 门、notional 0.0302 ETH、trail 35%、haircut、F 名单构成 247+33、出场规则）。
   - **必须同时写 formal window 与 observed coverage**（承接 GL5 终审对 W3 partial coverage 的提醒）。
2. **Arm-0 复现**：原样重跑该对象的旧出场模型，**逐位 `==` 复现它自己 JSON 里的 headline（容差 §4 预注册）**。复现不出 → 停,报口径漂移,不许继续。
3. **必出该对象自身的证据构成**：120 笔里各有多少 **真卖单 / guarded 报价 / confirmed rug / unresolved**。
   - 这直接检验一个关键问题:**F-on 版是不是也和 +56% 一样高度依赖报价?** 若是,重定价就更关键。

## 3. Step 1 —— “真能不能卖掉”出场模型（核心，沿用 GL5 §3）

对 Arm-0 的每一笔 F-on 入场，在每个出场块（按预注册的秒级/块级延迟）用下列**优先级**定出真实出场价，全程只用链上真相：

1. **有真实卖单证据**：出场块附近预注册窗口内存在任意地址对该 token 的真实 SELL swap（status=1）→ 用真实成交价（VWAP 或最近一笔，预注册择一）。
2. **无真卖 → 前瞻可成交性检验**（GL4 核心补丁）：
   - (a) **前瞻 rug 扫描**：从入场块起、覆盖到出场块再往后一个预注册的“真实执行滞后带”，扫描池储备(transfer-net = balanceOf(pool))。若在你真能触发卖出之前储备已坍塌 ≥ `RUG_COLLAPSE_PCT`（预注册，90/95/99% 敏感性），或窗口内出现真实卖单 revert/status=0 → 判该腿坍塌点不可卖:出场价 = 坍塌前最后真实可执行价;坍塌早于可出场 → 记真实可回收值（多 ≈−100%）。**不给点内报价救济。**
   - (b) **仅当前瞻扫描确认池到出场时刻仍存活**，才允许 D5 atomic buy→sell roundtrip 在出场块做卖出模拟，含真实 haircut；atomic revert → 退回 (a) 的坍塌处理。
3. **数据不足判定** → 标 `UNRESOLVED`，给双边界（乐观=atomic 报价成交；悲观=不可卖坍塌），**不并入点估计**，单列区间。

## 4. 预注册（看结果前冻结 + SHA）

至少冻结：`RUG_COLLAPSE_PCT`（主值+敏感性）、储备口径(transfer-net，golden 对 balanceOf)、出场延迟窗(1/5/15 块)+真实执行滞后带、真卖匹配窗口、VWAP/最近一笔择一、atomic haircut、Arm-0 复现容差、右尾判据(pooled mean>0/每窗下限/top-1% 贡献上限/MC 终值 p5/回撤 p95)、UNRESOLVED 双边界算法。**一律禁止看结果回改。**

## 5. 三臂 + 分解（预注册产出）

- **Arm-0**：644/120 F-on 原旧出场模型 → 复现其自身 headline（锚）。
- **Arm-1**：同入场、同延迟，新“真能卖掉”出场模型 → 点估计 + UNRESOLVED 区间。
- **基线**：同一 644 universe 的同龄无条件随机买入，在**同一新出场模型**下的收益（比条件 alpha，不比绝对值）。
- **必出分解表**：
  1. 全部出场腿里旧模型靠报价救济的比例。
  2. 报价救济腿里,前瞻扫描判定真 rug/不可卖的比例（= 报价洗白 rug 规模）vs 确认存活、报价公允的比例。
  3. **F-on 到底还碰多少 rug**：验证“F-on ⇒ rug成交≈0”这个说法在**前瞻真扫描**下成不成立(而不是只信黑名单自称);对照 GL4 激进版关 F 吃 207 rug。
- **右尾裁决**：Arm-1 是否仍满足 §4 右尾判据。给 Arm-0→Arm-1 delta,以及“F-on 版的 headline 里有多少是报价幻觉”的量化结论。

## 6. Golden 对账 + 回归

- 抽 3–5 腿（含 ≥2 个判 rug/不可卖、1 个判存活可卖），用 archive RPC 独立核 balanceOf(pool)@block、真卖单、atomic 模拟,逐一贴锚。
- 回归覆盖:优先级 1/2a/2b/3 分支、坍塌阈值边界、UNRESOLVED 区间、Arm-0 复现、证据构成计数,断言 `==`。

## 7. 交付物

- `GL5v2_Fon644_realizable_exit_repricing_20260716.md`（主报告:Step0 pin + 自身 headline、三臂、分解表、右尾裁决、结论:F-on 版 edge 幸存/崩塌/inconclusive）
- `GL5v2_preregistration_20260716.md`（+ SHA）
- 机读 JSON（逐腿:入/出场块、判定分支、出场价、PnL、UNRESOLVED 标记、证据构成）
- `GL5v2_plain_language_20260716.md`（人话版:F-on 保守版到底扛不扛得住 rug、它自己的真数是多少、里头多少是报价幻觉;尽量中文,把表格讲成人话,工作量要足）
- `GL5v2_independent_line_review_20260716.md`（独立逐行终审）
- artifact SHA256 清单（**交付时必须包含本轮全部工件,含终审两份**,GL5 终审提醒过 hash ledger 不能漏）+ 回归测试证据 JSON

## 8. 指挥裁定（边界与授权）

- **不许**把 644/120 的 headline 说成 +56%;必须老实报它自己的数。
- **不许**为让 Arm-1 好看调阈值;不许换 config;不许把 UNRESOLVED 塞进点估计。
- **不许过度悲观**:数据不足 ≠ 全损;只有链上确认坍塌/卖单失败才记真损。
- **F-on 是否真挡住 rug** 必须用前瞻真扫描独立验证,不许只信黑名单自称。
- 允许在 §3/§4 框架内自定实现细节,但每个阈值先预注册。
- 一次做完(Step0→回归),最后一次性交我审。`GO=NOT_MADE`。
