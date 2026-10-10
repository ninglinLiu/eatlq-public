# 未决问题

## 阻断 P0 的问题

1. **如何取得 Base 手续费真值？** 当前库没有 Base Collect。需要决定是从部署/首次所需区块完整回填池级 Collect，还是从 archive RPC 回放 fee-growth。前者数据量较直观；后者还需重建 tick crossing 和 position checkpoint。
2. **Aerodrome Slipstream 的 Collect/fee-growth 语义是否与 Uniswap v3 完全一致？** 2026-03-14 的 Burn 本金公式吻合，但这不能回答未质押 LP、gauge 与 veAERO 之间的手续费归属。需要以 Aero/Slipstream 对应版本的官方合约源码和真实 Collect 为准。
3. **摄取为何把 mainnet Collect 与 Base mint/burn 叙述混在一起？** 31 条 Collect 仅覆盖 mainnet 约 11 分钟，应审计订阅配置和历史回填任务；本轮依约未修改 `/opt/EATLQ`。

## 因 P0 门禁而未研究

- Base 近 30 天 LP 基准率、分层分布与文献对照；
- H1–H4 的样本外假设检验；
- Aerodrome 与 Uniswap 当前 TVL/成交量口径冲突；
- Aerodrome 广度回填的成本提案。

这些不是“负结论”，而是未执行。只有 P0.2 取得独立手续费真值并通过 `<15%` 中位相对误差与 `p>0.05` 符号检验后才能继续。

