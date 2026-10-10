---
task_id: GL5-v3
target_estimand: 2026-06-24_Fon_644_to_120
date: 2026-07-16
frozen_before_any_gl5v3_rpc: true
inherits_gl5v2_preregistration_sha256: c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e
research_only: true
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
---

# GL5v3 archive 解析增量预注册

本文件在任何 GL5v3 RPC 请求之前冻结。GL5v2 的对象、120/644 名单、offset、收益、成本、真卖优先级、双边界、右尾标准与裁决阈值全部原样继承；本轮只新增下述 archive 证据如何解析 GL5v2 `UNRESOLVED`。不得看结果后改规则。

## 1. 固定对象、时钟与 RPC 边界

- 输入只用 `base_gl5v2_Fon644_per_leg_20260716.json` 的同一 644 pool、120 F-on 子集、legacy `exb` 与 offset `1/5/15`。target=`exb+offset`；真实执行滞后仍为 target 后 15 blocks。
- 主仓位 0.0302 ETH；ETH/USD=1658.75；round-trip gas=$0.15；haircut=2%。return=`WETH_out/0.0302ETH - 1 - 0.15/50.09425 - 0.02`。
- 唯一 RPC 是任务书内 `GL5V3_ARCHIVE_RPC`。程序只在内存解析 URL；任何 artifact、日志和终端输出只记别名 `GL5V3_ARCHIVE_RPC_secret_omitted`。
- 仅允许 `eth_call`、`eth_getBalance`、`eth_getBlockByNumber`、`eth_getLogs`；不调用 send/sign/broadcast，不读取 `.env`，不降级其他节点。每请求最多 3 次、HTTP timeout 120 秒、最大并发 12、总 HTTP attempt 上限 40000。失败保持 unresolved。
- RPC ledger 每次 attempt 只写 UTC、method、参数 canonical SHA256、响应 canonical SHA256、success/error class、attempt；不写参数、响应原文或 URL。ledger 及摘要出 SHA。

## 2. 固定 archive 观测与 exact-size 定义

每个 pool 先取 `fee()` 与 `WETH.balanceOf(pool)` at entry。每条腿固定取 WETH balance grid：`target-1,target,target+1,target+5,target+15`（负块不适用；重复块去重）。不因中途结果增删点。entry 与 grid 任一点 RPC 失败即如实记录。

atomic guard 固定为 GL5v2 引用的 D5/B4 构造器式 `eth_call`：在 target block 以 0.0302 ETH 买 token，再立即把所得 token 卖回 WETH；state override 只给研究地址足额 ETH，不更改 token/pool 状态。它只证明该块、该模拟地址的 pool-level roundtrip 路径。

历史反事实仓位 token qty 固定由 GL5v2 entry BUY proxy 的 `token_raw/weth_raw * 0.0302ETH` 向下取整。target 块用 QuoterV2 exact-input-single 报该 qty 的 WETH out。缺 entry proxy、qty 非正、fee 不可读或 quote 失败，不得用 0.0302 ETH 新买所得替代历史仓位，保持 unresolved。

GL5v2 已有、满足 full-size 的 `REAL_SELL` 点值保持第一优先，不被 archive quote 覆盖；archive 储备仍可用于总体 rug 核对。小额 SELL 仍仅是 survival evidence，不能冒充 full-size 点值。

## 3. 新增归类规则（顺序冻结）

相对 reserve ratio=`balance(target-like block)/balance(entry)`。entry balance 缺失或为零时不能据此判坍塌。

1. `REAL_SELL_CARRY_FORWARD`：GL5v2 已有 full-size real SELL，沿用真实价及 return，进入点估计。
2. `ARCHIVE_ALIVE_ATOMIC_OK`：无合格真卖；entry/target reserve 均可读；target ratio 高于主坍塌阈值 0.05；atomic buy 和 sell 均成功；历史 qty 的 exact-size quote 成功且 WETH out>0。用该 quote 计算 return，进入 pool-level 点估计。
3. `ARCHIVE_IMPAIRED_BUT_ATOMIC_OK`：target ratio<=0.05，但 atomic sell 与历史 qty exact-size quote 均成功。可执行证据优先于静态低储备，按真实残值 quote 算 return，不判 rug。
4. `CONFIRMED_COLLAPSE`：entry、target 与 target+15 reserve 均可读；target ratio<=0.05；target+15 ratio 仍<=0.05；target atomic 没有成功 sell；且 exact-size quote 没有正回收证据。此时 gross recovery=0，return=`-1-cost`。若有可量化正残值，改用残值而非强置 -100%。
5. `UNRESOLVED`：其他全部情况，包括 RPC/archive 失败、target reserve 尚存但 atomic revert、atomic 成功而历史 qty quote 失败、低储备后 target+15 恢复、entry proxy/fee/qty 不完整。既不进入点估计，也不默认安全或全损。

主 collapse 为损失>=95%（ratio<=0.05）；敏感性只重算标签计数，阈值为损失>=90%（ratio<=0.10）与>=99%（ratio<=0.01）。等号属于 collapse。敏感性不改变主 Arm-1 收益。

“F-on rug 真数”仅在 120 条主 offset 全部得到可判的 pool-level collapse/alive/real-sell 分类时报告单一点；否则报告 confirmed collapse 下限与 `confirmed+collapse-relevant unresolved` 上限，不把下限称真数。

## 4. 三臂、baseline、双界与裁决

- Arm-0 必须与 GL5v2 逐位相同：pass=120、monthly=0.08574380412768878、MDD=-0.003931798477305182、fatal=2、mean_ret=1.4280241443411608；任何漂移停止。
- Arm-1 对 120 条逐 offset 使用上面顺序。点估计只统计 resolved；population 双界仍把 unresolved 分别替换为 GL5v2 optimistic Arm-0 return 与 `-1-cost` 后取 120 普通均值。
- baseline 对完整同龄 644 条使用完全相同规则。只有 selected 与 baseline 同 offset 都没有 unresolved 时，才把 `mean(selected)-mean(baseline)`称为“完整条件 alpha”；否则给双边界差并明确非点识别。
- quote illusion：在两臂均 resolved 的腿上比较 `max(0,Arm0)-max(0,Arm1)` 占 Arm0 正利润；未解腿单列。quote-washed rug=Arm0 quote/simulation 腿中 confirmed collapse。
- 右尾标准、bootstrap seed=20260716、10000 paths、EDGE_SURVIVES/COLLAPSES/INCONCLUSIVE 阈值全部继承 GL5v2。任一 primary offset 仍有 unresolved 时形式状态保持 INCONCLUSIVE。

## 5. Golden、回归与已知边界

golden 从最终分类内按 `sha256("GL5v3-golden|"+pool+":"+offset)` 最小值选：尽量 2 collapse、2 alive atomic-ok、1 unresolved；类别不足则用现有类别补足并披露，绝不为凑类别改规则。每条只公开 balance/atomic/quote 响应 hash 与 ledger request hash，不公开原 RPC 参数或 URL。

回归必须以 `==` 覆盖五个分类分支、0.10/0.05/0.01 端点、atomic revert 保持 unresolved/确认坍塌两种语境、unresolved 双界、Arm-0 不变、selected/baseline population 计数、ledger hash 可复算及 `GO=NOT_MADE`。

结论始终限定为 pool-level 可实现性：archive balance 与 atomic 模拟不能证明策略钱包不会遭地址级 blacklist、transfer hook 或限额。入场仍是第三方 BUY proxy；atomic 仍不含真实 MEV/竞争。不得写成“策略钱包一定卖得掉”，不得据此自判 GO。

冻结后发现事实错误只能另列勘误，不能静默改本文件。`GO=NOT_MADE`。
