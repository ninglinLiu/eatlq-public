---
task_id: GL5-v3
title: 644/120 F-on 重定价——插上 archive RPC，把双边界收成点估计、把 rug 率钉成真数
author: 审查型指挥 Agent（人类已定向：gl5v3）
date: 2026-07-16
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
  broadcast_allowed: false
continues: GL5-v2（同一 644/120 F-on 对象、同一预注册骨架），仅补上 archive RPC 解析
target_object: 2026-06-24_Fon_644_to_120（与 GL5v2 完全同一对象，不许换）
---

# GL5-v3 · 用 archive RPC 把 GL5v2 的 INCONCLUSIVE 算清

## 0. 为什么有这一轮

GL5v2 已经把 644/120 F-on 对象钉死并逐位复现（月化 +8.5744%、单笔均值 +142.80%、100% 报价/模拟、真实 SELL=0）。但两件事因**授权 archive RPC 未注入**而只能给宽区间：
- 完整收益双边界 `[−36.07%, +143.70%]`（+1 档），跨零 → INCONCLUSIVE。
- F-on rug 率只能给 `[0%, 48.33%]`，无法确认/否认“F-on ⇒ rug≈0”。

**指挥已实测该节点为全 archive、支持历史状态查询**（当前块 48.5M；对 36.17M 老块的 `eth_getBalance` 与 `eth_call balanceOf` 均正常返回真值）。因此本轮唯一新增能力 = 注入 archive RPC，用真实链上储备与 atomic 卖出模拟，把 GL5v2 留下的 unresolved 腿**尽可能收敛成点估计**，并把 rug 率钉成真数。

## 1. RPC 注入（指挥提供，已 health-check）

```bash
export GL5V3_ARCHIVE_RPC="https://prettiest-blue-aura.base-mainnet.quiknode.pro//"
```

- 只读用途：`eth_call`（balanceOf / atomic buy→sell roundtrip）、`eth_getBalance`、`eth_getBlockByNumber`、`eth_getLogs`（重建 transfer-net）。
- **禁止** send/sign/broadcast、写 Redis/signals、启停 executor、改 live 配置、读 `.env` 里的任何私钥字段。只用上面这一个 URL。
- 记 RPC ledger（method + 参数 hash + response hash），出 SHA，可离线复算。
- 若该节点对某历史块返回 archive-not-supported / 限流 → **诚实标该腿仍 unresolved，不许**降级成 public RPC 偷偷补，也不许把失败当成安全或全损。

## 2. 硬约束（宪章，与 GL5v2 一致）

- 纯研究、只读链上。`GO=NOT_MADE`，不自判 GO。
- **沿用 GL5v2 的预注册**（同一对象、同一 offset 1/5/15、同一坍塌阈值 90/95/99% 敏感性、同一 haircut/gas、同一右尾判据）。**本轮新增的只有“如何用 archive 证据把 unresolved 归类”这一段,必须在跑解析前预注册并出 SHA,禁止看结果回改。**
- **对称诚实**（禁止过度悲观 = 禁止过度乐观）：
  - archive 重建储备**确认坍塌**（reserve 前瞻跌破阈值）或真实卖单 revert → 记真损,不给报价救济。
  - archive 确认池到出场时刻**仍存活** + atomic roundtrip 成功（含 haircut）→ 该腿可解,进点估计。
  - archive 也查不动(块不支持/超时) → **仍 unresolved,进双边界,不进点估计**;禁止默认 −100%,禁止默认满额成交。

## 3. 解析程序（本轮核心）

对 GL5v2 每个 offset 下每一条 unresolved 腿（+1 档 58 无存活 + 20 小额未解 + 11 稍后存活；+5/+15 同理），按顺序：

1. **前瞻储备重建**：用 `eth_getLogs` 拉该 pool 的 ERC20 transfers（token 与 WETH 两侧），或直接 `eth_call balanceOf(pool)@block` 在 [entry_block, exit_block + 真实执行滞后带] 逐点取真实储备。
   - 若储备在你真能出场前跌破 `RUG_COLLAPSE_PCT`（预注册主值+敏感性）→ 判 **confirmed collapse**：出场价 = 坍塌前最后真实可执行价;坍塌早于可出场 → 记真实可回收值(≈−100% 或残值)。
2. **存活则做 atomic 卖出模拟**：确认池到 exit_block 仍存活 → 在该块做 D5 atomic buy→sell exact-size roundtrip `eth_call`，含真实 haircut，得可实现出场价 → 该腿进点估计。atomic revert → 退回步骤 1 的坍塌处理。
3. **两步都无法判定**（archive 拒答/覆盖不到）→ 保持 unresolved，进双边界。

**目标产出**：把 GL5v2 的 `[−36.07%, +143.70%]` 尽量收敛;报告收敛后：
- 完整 120 population 的**点估计**（若剩余 unresolved 足够小）或**收窄后的双边界**（若仍有硬缺口，诚实标还剩多少 unresolved）。
- **F-on rug 率的真数**（confirmed collapse / 120），替换掉 `[0%,48%]`。

## 4. 三臂 + 分解（在 GL5v2 基础上更新）

- **Arm-0**：不变（已复现的自身 headline，锚）。
- **Arm-1（archive-resolved）**：新出场模型 + archive 解析后的点估计/收窄双界。给 Arm-0→Arm-1 delta。
- **同龄 644 baseline**：同样用 archive 解析其 unresolved（baseline 之前 ~90% unresolved，本轮尽量收敛），得到**真正的条件 alpha**（Arm-1 减 baseline），而不是稀疏子集 alpha。
- **分解表更新**：
  1. archive 解析后，报价救济腿里 confirmed rug / 确认存活可卖 / 仍 unresolved 三者比例。
  2. **F-on 是否真挡 rug**：用 confirmed collapse 真数直接回答，不再用区间搪塞。
  3. “headline 里多少是报价幻觉”：现在能对**大多数** population 给真数（旧报价 vs archive 可实现价的差）。
- **右尾裁决**：Arm-1 收敛后是否满足预注册右尾判据(pooled mean>0/每窗下限/top-1% 贡献/MC 终值 p5/回撤 p95);并明确 `unresolved` 是否已足够小到能下 EDGE_SURVIVES / EDGE_COLLAPSES,还是仍 INCONCLUSIVE。

## 5. 已知边界（必须在报告重申，不许掩盖）

- **钱包级可卖性**：canonical SELL 与 atomic 模拟证明的是 **pool 级**可卖;**不能**证明策略钱包不受 token 的地址级限制(transfer hook / 黑名单 / 限额)。报告须把 Arm-1 明确限定为 “pool-level 可实现”,不得升级为 “策略账户必得成交”。
- **入场价**是 [entry, entry+900] 内第三方 BUY proxy,非策略自身 receipt。保持 GL5v2 口径,不外推成可部署回报。
- atomic 模拟是点内 `eth_call`,不含真实抢跑/MEV/滑点竞争。

## 6. Golden + 回归 + 交付

- golden：抽 5 腿(至少 2 个 confirmed collapse、2 个 archive-alive-atomic-ok、1 个仍 unresolved),贴 balanceOf@block / getLogs / atomic 返回的原始 hash。
- 回归覆盖：解析三分支、坍塌阈值端点、atomic revert 退化、unresolved 保持双界、Arm-0 不变、RPC ledger 可复算,断言 `==`。
- 交付：
  - `GL5v3_Fon644_archive_resolve_20260716.md`（主报告：收敛后点估计/双界、rug 真数、三臂、分解、右尾裁决、EDGE 裁定候选）
  - `GL5v3_preregistration_20260716.md`（+ SHA，含新增解析归类段）
  - 机读 JSON（逐腿：解析分支、archive 证据、出场价、PnL、是否进点估计）
  - `GL5v3_plain_language_20260716.md`（人话版：保守版到底赚不赚、挡不挡 rug、还剩多少算不清；尽量中文，把表讲成人话，工作量足）
  - `GL5v3_independent_line_review_20260716.md`（独立逐行终审）
  - RPC ledger JSON + artifact SHA256 全清单(含终审两份) + 回归证据 JSON

## 7. 指挥裁定

- **不许**换对象、不许把 unresolved 塞进点估计、不许 archive 查不动就降级 public 偷补、不许把缺证据当安全或全损。
- **不许**为了让 Arm-1 好看调阈值;新增解析归类段先预注册。
- 钱包级可卖性缺口必须在结论显著位置重申。
- 一次做完(注入 RPC→解析→三臂→回归),最后一次性交我审。`GO=NOT_MADE`。
