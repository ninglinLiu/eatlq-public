# Base LP 研究 R0：结论摘要

**本轮唯一可成立的主结论是：现有数据无法完成手续费真值校准，P0 失败，P1/P2 必须停止。** 没有 Base LP 盈利与否的策略结论。

最关键的证据有三条：

1. Uniswap v3 官方合约把 Burn 本金和 Collect 手续费明确分开；所以 `burn - mint` 不是手续费真值。
2. ClickHouse 中 31 条 Collect、23 个池全部属于 Ethereum mainnet (`chain_id=1`)；Base (`8453`) 为 0 条。GOAL 对 Collect 覆盖的链归属有误。
3. 三个指定 Base 池在 2026-03-14 的 Burn 金额与 v3 本金公式高度吻合，中位相对误差约 `10^-10`，证明 Burn 可校验本金数学，却不能校验手续费。

已完成且可复用的仪器包括：无份额截断的逐笔份额模型、1% 容量违规硬标记、五项摩擦拆分、v3 区间仓位数学、LVR 闭式解及非负/单调自检、校准误差与符号检验。标准化 $500、±15% 的一天容量审计中，三池 `capacity_violation_pct` 均为 0；这只是容量校验，不是收益校准。

下一步不是调策略，而是补齐三个指定 Base 池的 Collect 或历史 fee-growth 真值，随后重跑 P0.2。详见 [`p0_calibration_report.md`](p0_calibration_report.md) 与 [`OPEN_QUESTIONS.md`](OPEN_QUESTIONS.md)。

