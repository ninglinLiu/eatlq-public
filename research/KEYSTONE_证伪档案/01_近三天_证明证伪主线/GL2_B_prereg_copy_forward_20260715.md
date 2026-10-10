---
sop_applied: true
alpha_id: AR-GL2-B-SMARTMONEY
family: F3/F4/F7
next_gate: COPY_FORWARD_HISTORICAL_READOUT_THEN_HUMAN_LINE_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-B · 聪明钱资格与历史前向复制预注册（阶段二）

冻结时间：2026-07-15T13:48:09Z。阶段一分布工件已经先生成并冻结：
`b80d466a71ea4ed1b9b4e4f59a56485e3b0e426fc0d503483e33bf6f315b8f7b`
（`mvp/diagnostics/base_gl2_b_stage1_distribution_20260715.json`）。本文 SHA256 必须在任何阶段二收益查询前落盘。

## 人群与资格（冻结）

- 资格训练窗：Base blocks `46,389,458..46,417,105`，B3 确定性 1,000 tx 样本中的高置信账户；不声称代表 Base 全链。
- 历史前向窗：紧随其后的等长 blocks `46,417,106..46,444,753`。资格变量只来自训练窗，禁止用前向窗调阈值。
- 聪明钱账户须同时满足：`n_closed >= 2`、`realized_weth > 0`、`winrate >= 0.50`、`profit_factor >= 1.20`、`top1_profit_share <= 0.75`、`sell_buy_ratio <= 2.0`、`span_blocks >= 1000`。
- 分布冻结时满足者为 5 个；地址由脚本从冻结分布机械派生，不手挑。
- 前向信号只接受这些账户在训练窗 17 个目标池中的真实链上买入。EOA 使用 `tx.from`；若未来出现智能账户，只有唯一 UserOperation sender 边界才允许，不能退回 recipient。

## 现实复制（冻结）

- 延迟分别为 `+1s/+5s/+15s`，从源买入 `block_time` 后计时。
- 我方入场必须是 cutoff 后该池第一笔**不同 tx 的真实 swap**，且成交时间 `>= source_time + delay`；绝不使用源钱包同笔成交、spot 或未来倒填。
- 入场 raw price 使用该后续真实 swap 的 WETH/target 原子数量比，再施加固定 `30 bps` 不利买入修正；证据类型标 `TRUE_SWAP_PLUS_FROZEN_30BPS_ADVERSE`。
- 固定持有 `7200s`；退出取入场后 `>=7200s` 的第一笔真实 swap，施加 `30 bps` 不利卖出修正。没有后续真实成交则 `UNRESOLVED_NO_TRUE_EXIT`，不记 −100%。
- 每个已成交回合成本再扣 `2 * 0.0000215 WETH` gas；notional 固定 `0.001 WETH`。
- baseline 为前向窗相同 17 池的全部非资格账户真实买入，按 SHA256 排序与 smart 已解析样本等量抽取，使用完全相同的延迟、入场、退出、成本与 unresolved 规则。
- 逐延迟报告 `n/resolved/unresolved/mean/median/winrate/p90/max/top1-removed mean`；小样本不得用均值单独下结论。

## 残余与铁律

Nansen=`NOT_RUN_MISSING_AUTHORITY_AND_API_KEY`，不购买、不伪造。本阶段只是有限历史可复制性审计，不训练、不下单、不写 Redis/live/signals，不自判 grade 或 GO。
