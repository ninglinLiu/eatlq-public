---
sop_applied: true
alpha_id: AR-GL3-CLOSE-GL12
family: F1/F4/F7
next_gate: FULL_INTERVAL_RECOVERY_SCAN_THEN_INDEPENDENT_LINE_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL3 · GL1-12 全区间恢复与有界识别预注册

冻结日期：2026-07-15 UTC。本文件及其 SHA256 sidecar 必须早于任何 GL3
结果查询、恢复扫描或收益读取；冻结后不得根据结果改规则。

## 1. 唯一 estimand 与冻结人群

- 全宇宙：GL1 冻结的同龄 431 个候选；KEYSTONE 是其中冻结的 116 个。
- 入场：`entry_block + 1`；退出目标块分别为
  `trigger_block + 1/+5/+15`。这是 reaudit10 与 GL2 权威 trigger 结果中约
  `+56%` 条件 alpha 的同一 estimand；禁止使用 `b0+2/+6/+16` 的立即往返
  ladder。
- 固定仓位：入场得到的真实整数 `true_position_token_raw`；固定投入
  `0.001 WETH`；WETH 原生计价；成本沿用 GL2 同日 ETH close 下两腿合计
  `0.15 USD` 的冻结换算。
- 残余人群机械取自冻结 GL2-A 结果：每窗 `resolved=false` 的
  `149/152/153` 个 cell。不得按池是否存活、是否有事件、是否 KEYSTONE 删行。
- 截止块固定为 `46,865,124`。

冻结输入及 SHA256：

- `mvp/diagnostics/base_gl2_a_universe_ev_20260715.json` =
  `966cf597c837070a40a3cce9c2defcdf11f25230c97adb93c69892bb62c5f197`
- `mvp/diagnostics/base_reaudit10_seconds_rpc_20260715.json` =
  `e62e0f37bb6a2756256795bfaa0d5f9677b73e5e8d0836370ebf1afbdbbd5e2d`
- `mvp/diagnostics/base_gl2_a_trigger_evidence_reviewed_20260715.json` =
  `827889eab4248980ec874fa030205e431090d56831703fbc09c6c03aba840cc3`
- `mvp/diagnostics/base_2min_accel_b3_eth_daily_20260714.json` =
  `728fc565687dddf9792553fd5ffc31072cd260b00dbe451bea54c9cad2f8e129`

## 2. 全区间事件覆盖与真储备

每个 residual cell 从其目标退出块（含）扫描至 cutoff（含）：

1. 从 ClickHouse `uniswap_v3_mints_decoded FINAL`、
   `uniswap_v3_burns_decoded FINAL`、`uniswap_v3_swaps_decoded FINAL` 取
   `chain_id=8453, removed=0` 的 `(block, tx_hash, log_index)` 及原始整数。
2. 用 prettiest-blue-aura `eth_getLogs` 按最多 5,000 blocks 的固定连续分片
   扫同池的 Mint/Burn/Swap 原始事件；分片不得跳块。CH 与 RPC 的事件键集合必须
   完全相等；任一 RPC 分片失败、键不等或截止块不可取，相关 cell 留 unresolved。
3. 对退出块前一块、退出块、每个 Mint/Burn/Swap 事件块及 cutoff，读取 WETH 与
   target token 的 `balanceOf(pool, block)` 原始整数。
4. 对每个事件块，从 RPC 原始 Transfer logs 分别汇总两种 token 对 pool 的全部流入
   与流出，断言
   `balanceOf(pool,b)-balanceOf(pool,b-1) == block 内 net Transfer to pool`。
   同块多个交易一并汇总，避免把块级余额差误当单笔差。任何 token 的日志覆盖或
   整数等式失败即为真数据缺口，相关 cell 留 unresolved。

Mint/Burn 是流动性阶跃边界；Swap 用来证明真实卖出并补足储备轨迹。不得用
`slot0`、decoded liquidity 或代理 recipient 代替上述真实 token 余额。

## 3. 回流、持续不可卖与 delayed exit

候选恢复块按 `(block, transaction_index/log_index, tx_hash)` 排序，集合为：

- 所有成功 Mint（`liquidity_raw>0`，且 receipt 中至少一侧 token 对池净流入>0）；
- 所有经 CH/RPC 键一致、方向为 target token 进池且 WETH 出池的真实 sell Swap。

对每个候选块依次用原始整数仓位做同块 atomic sell guard 与 exact-size quote：

- 首个 `sell_ok=true`、guard recovery ratio `>0.10` 且 exact quote
  `amount_out>0` 的块 X，定为首个可卖机会；cell 以 X 的 exact quote 定价，证据标
  `ATOMIC_GUARDED_EXACT_QUOTE_DELAYED_REFLOW`。这是带 Mint/储备或真实卖出锚点的
  历史模拟报价，不冒充我方真实 fill。
- 真实 sell 只证明该块市场可卖；若该块 exact-size quote 缺失，不按第三方成交比例
  缩放本仓位，继续找后续候选；始终缺 quote 则 unresolved。
- 若退出块 frozen atomic/exact-size evidence 明确为 execution revert/不可卖，事件与
  reserve 覆盖全绿，全区间没有真实 sell，且所有 Mint 候选原子复测均不可卖（包括
  无 Mint 的情形），则标
  `CONFIRMED_CONTINUOUSLY_UNSELLABLE=-100%`。这才算 E4 全区间无恢复。
- rate limit、timeout、未知 revert 类型、缺 receipt、缺 event chunk、CH/RPC 不一致、
  reserve/Transfer 不等或某个 Mint 候选未成功复测，均保留 unresolved；禁止默认全损。

LP pull 后无 Mint、无真实 sell 且全区间覆盖成立时，按冻结机制判为没有恢复边界；
不逐块重复同一状态。若有 Mint，则每一个 Mint 都必须复测，不能只看 cutoff。

delayed-exit 净收益严格沿用原 estimand 的算式：

`net_return = (exact_amount_out_raw - 10^15 - frozen_cost_weth_raw) / 10^15`。

## 4. 有界识别与 alpha/beta

每窗、每个人群均先纳入新确认的 rug 与 delayed exit。若仍有 residual：

- 悲观界：每个 residual 固定为 `-1.0`；
- 主乐观界：每个 residual 固定为该窗、该人群已 resolved cell 的中位数；
- 另报 p75 乐观敏感性，但不用于主分级判据。

所有分母固定为原人群总数。逐窗报告 full-universe 与 KEYSTONE 的 point（若完全
resolved）或 `[pessimistic, median-optimistic]` 区间。

条件 alpha 的保守区间冻结为：

- `alpha_lower = KEYSTONE_pessimistic - full_universe_median_optimistic`
- `alpha_upper = KEYSTONE_median_optimistic - full_universe_pessimistic`

beta 是 full-universe 同窗界；选择 alpha 是 KEYSTONE 减 beta，禁止跨窗或跨龄相减。
同时报告在 KEYSTONE 中 beta/selection-alpha 的带符号占比区间；分母跨零时只报
`UNDEFINED`。

供独立审查的候选分级规则（Codex 不自行定级）：

- `IRON candidate`：三个窗的 `alpha_lower` 均 `>= +0.10`，且事件键一致、reserve
  整数对账、RPC ledger、CH 查询 hash 至少形成两个独立锚点；结论对 residual 的冻结
  悲观/乐观赋值不敏感。
- 否则为 `PINNED candidate`，保留逐窗区间和残余；不得把区间中点冒充完整 EV。

## 5. 安全、时序与可复现

- 只读 ClickHouse 与 prettiest-blue-aura；禁止 orbital；RPC ledger 记录 method、
  block/range、用途、状态、request/response hash，不记录 URL、token 或环境值。
- 不下单、不签名、不广播、不写 live/signals/Redis；executor 必须保持关闭。
- 不训练、不自判 GO；N/A 的外部来源不补造。
- 新脚本必须先校验本文 SHA256 和上述输入 hash，才能查询或计算结果。
- 最终分级由独立审查 agent 逐行决定；无完整锚点不得升 IRON。
