---
sop_applied: true
alpha_id: AR-B3-ACCOUNT-ENTITY-ATTRIBUTION
family: F3/F7
next_gate: BOUNDED_FEASIBILITY_SPIKE
decision_flags:
  training_allowed: false
  live_allowed: false
---

# Base B3 Account/Entity Attribution Feasibility Spike — 预注册

## 目标

目标不是恢复自然人或原始 EOA，而是识别一个可持续跟踪、可重建持仓和可独立计算 PnL 的交易账户/实体。

B1/B2 只证明 `tx_from` 全局真值和最终收币方 transfer-net 两种万能归因不可用；它们没有检验 UserOp sender、协议 adapter、账户类型分类和 trace 边界，因此不得外推为 Base 账户级归因整体不可行。

## 有限 universe 与预算

- Base chain id 8453；锁定块 `46,389,458–46,417,105`。
- Uniswap V3 / Aerodrome CL。
- 必须为 WETH 交易对，且 swap 时池龄在 0–43,200 blocks（约 24h）之间。
- 完整目标 universe：4,339 个唯一交易、17 个池。
- 确定性分层样本：1,000 个 tx，按 protocol/pool/side 分组并在块高上均匀抽取。
- 只读 RPC；最多 1,000 transaction + receipt + historical code probes；trace golden 上限 100。
- 不购买 Nansen credits；没有已授权 API key 时只记录 `NOT_RUN_MISSING_AUTHORITY`。

## Resolver cascade

1. `UserOperationEvent`：按 receipt log index 将 swap 归入下一个 UserOp 边界，取 indexed `sender` 为账户。
2. 协议 adapter：EntryPoint、Universal Router、SwapRouter、Aerodrome、0x/1inch/OKX、solver/settlement、direct pool。
3. 账户分类：EOA、EIP-7702、自有智能账户/Safe/机器人、共享 router/vault/solver/CEX/归集、未知合约。
4. trace/state-diff golden：不允许用 transfer-net 或第一个 recipient 补缺。
5. 输出枚举：`ACCOUNT_HIGH_CONFIDENCE`、`ENTITY_MEDIUM_CONFIDENCE`、`SHARED_INFRA`、`AMBIGUOUS`、`UNATTRIBUTABLE`。

## 冻结 gates

- 高置信 precision ≥95%（独立 trace/state-diff golden）。
- 目标 universe 高置信 coverage ≥65%。
- ≥80% 高置信账户可重建买卖和 PnL。
- 错误共享合约率 <1%。
- 解析延迟满足策略窗口；本轮冻结为单 receipt 拉取+解析 p95≤2s（占既有 10s 跟随延迟预算的 20%），只作离线可行性门，不自设 live GO。

决策：全部通过才 `FEASIBLE_CONTINUE`；coverage 30–65% 且 precision 通过则 `HIGH_CONFIDENCE_MINORITY_ONLY`；加入 UserOp/adapters/classifier 后仍低于30%或 precision 不过则 `ENGINEERING_ECONOMICS_FAIL`。无论结果如何都不判策略 edge、不训练、不 GO。

## 外部交叉验证

Nansen 官方 Smart Money API 支持 Base DEX trades/holdings，但需要付费认证；只能作为交叉验证，不能作为 golden。GMGN 公开材料只证明产品展示聪明钱标签，算法和 Base 覆盖不透明。Blockscout 明确索引 Base UserOperations、AA wallets、internal transactions/raw trace，可用于独立核验。DEX Screener 不作为账户归因源。

## 执行回执（2026-07-14）

- 本地五个数值门点估计全部通过：precision 100%、coverage 93.2%、账户可复算 98.958333%、已知共享基础设施误收 0%、解析 p95 0.449186s。
- 完整结果：`reports/base_b3_account_entity_attribution_feasibility_20260714.md`。
- 裁决：`LOCAL_FEASIBILITY_PASS_EXTERNAL_CROSSCHECK_PENDING`；Nansen 未获授权/API key，完整验收未关闭。
- `rh1_merit=UNTESTED`；training/live 均保持 false。
