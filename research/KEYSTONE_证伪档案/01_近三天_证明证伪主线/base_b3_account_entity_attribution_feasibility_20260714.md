---
sop_applied: true
alpha_id: AR-B3-ACCOUNT-ENTITY-ATTRIBUTION
family: F3/F7
next_gate: EXTERNAL_VENDOR_CROSSCHECK_THEN_RESEARCH_ONLY_PNL_HISTORY
decision_flags:
  training_allowed: false
  live_allowed: false
---

# Base B3 Account/Entity Attribution Feasibility Spike — 结果

## 裁决

**`LOCAL_FEASIBILITY_PASS_EXTERNAL_CROSSCHECK_PENDING`。**

B1/B2 证明失败的是两种通用粗糙归因：把 `tx_from` 当交易者，以及把最终收币方当交易者。它们没有证明 Base 的账户级归因先天不可行。B3 在冻结的新池/WETH 目标 universe 上，以 UserOp execution boundary、协议 adapter、历史账户类型和 trace/balance reconciliation 构成 resolver cascade；五个本地数值门的点估计全部通过。

这只支持以下有限结论：

> 在本轮目标 universe 内，识别一个可持续跟踪、能重建持仓变化并支持 PnL 复算的交易账户/实体，具有明确工程可行性。

它不识别自然人，不声称恢复智能账户 owner，不证明有聪明钱 edge，也不授权 RH1 改用该结果。Nansen 外部交叉验证没有已授权 API key，仍未运行，因此 B3 完整验收尚未关闭。

## 冻结 universe 与样本

- Base blocks `46,389,458–46,417,105`。
- Uniswap V3 / Aerodrome CL，WETH 交易对，swap 时池龄不超过 43,200 blocks。
- 完整 universe：4,339 个唯一交易、17 个池。
- 确定性 protocol/pool/side 分层、块高均匀样本：1,000 tx。
- trace golden：100 tx；账户 inventory `balanceOf(block-1/block)` 对账：同一 100 tx。
- 全程只读 RPC；没有训练、canary 或 live 写入。

## Resolver 输出

| 结果 | 行数 | 占样本 |
|---|---:|---:|
| `ACCOUNT_HIGH_CONFIDENCE` | 932 | 93.2% |
| `ENTITY_MEDIUM_CONFIDENCE` | 68 | 6.8% |

高置信 932 行对应 591 个账户。按执行入口分解：

| 账户/边界类型 | 行数 | 归因依据 |
|---|---:|---|
| 历史 EOA | 859 | 已知协议 adapter + trace root signer |
| 历史 EIP-7702 | 11 | swap block 的 `eth_getCode`，单点只作当时账户类型 |
| ERC-4337 UserOp sender | 62 | `UserOperationEvent` log boundary + sender subtree 包含目标 pool |

路由样本为 Universal Router 775、DEX Router 82、ERC-4337 EntryPoint 62、aggregator 11、Uniswap router 2、未知合约/路由 68。最后 68 行只给 `ENTITY_MEDIUM_CONFIDENCE`，没有用“第一个 recipient”强补。

## 预注册 gates

| Gate | 结果 | 门槛 | 判定 |
|---|---:|---:|---|
| 高置信 precision | 100/100 = 100% | ≥95% | PASS |
| 高置信 coverage | 932/1,000 = 93.2% | ≥65% | PASS |
| 账户 inventory/PnL 可复算 | 95/96 = 98.958333% | ≥80% | PASS |
| 已知共享基础设施误收 | 0/100 = 0% | <1% | POINT-ESTIMATE PASS |
| receipt + UserOp parse latency | p95 0.449186s | ≤2s | PASS |

“共享基础设施误收 0%”只表示：100 笔 golden 中没有 resolved entity 落入预注册的 80 个 B2 疑似共享基础设施地址，direct signer 均为当块 EOA/EIP-7702，UserOp 样本均能把目标 pool call 隔离到唯一 sender subtree。它不是“Base 全链未知共享合约误差为零”的证明；100 个零失败样本也不足以给总体 `<1%` 提供严格的高置信统计上界。

高置信账户最大集中度为 31/932 = 3.32618%，地址是历史 EOA，不是合约钱包坍缩。该数值只作活跃交易者集中度参考，不与 B2 的合约钱包 `<1%` 门混用。

## Inventory/PnL golden

100 笔中 99 笔的目标 token Transfer net 与 `balanceOf(block)-balanceOf(block-1)` 完全一致。96 个唯一账户中 95 个可按账户 inventory delta 与关联 WETH pool swap amounts 建立 PnL 记账基础；gas payer 单独保留。

唯一未救回案例为：

- tx `0x7b5f721103861fd6f224fbe01ce03e0dad0818677ceae8bc91064888eb4ee53c`
- UserOp sender `0x7468055e937a7c4ade0b6936a63d6b2ac6957b4c`
- 该 sender 发起 sell，但本 tx 目标 token 的 Transfer net 和块级余额变化都为 0。

该例继续记为不可复算，没有把后续 recipient、第一跳地址或任意资金接收者硬塞成账户。当前结果只证明目标样本交易的 inventory/PnL 复算可行，不等于已经补齐 591 个账户的完整历史 FIFO PnL。

## 外部交叉验证

- Nansen：`NOT_RUN_MISSING_AUTHORITY_AND_API_KEY`。官方 Smart Money API 支持 Base DEX trades/holdings，但需要认证；本轮没有购买 credits，也没有用供应商结果替代链上 golden。
- GMGN：`PUBLIC_DOCS_ONLY_NOT_GOLDEN`。公开材料不足以审计其 Base 归因算法、覆盖和防前视口径。
- Blockscout：公开能力已核验；本地结果仍由 receipt/UserOperationEvent/trace 独立计算，未把页面标签当真值。

因此下一门是：在获得明确授权和 API key 后，对冻结样本做 Nansen 地址/标签/DEX trade 一致性矩阵；GMGN 只能做展示层抽查。供应商不一致必须按 route family 回溯，不能投票生成 golden。

## 对 RH1 的约束

- `rh1_merit = UNTESTED`。
- 不对 Base 聪明钱下“有 edge”或“无 edge”结论。
- 不训练、不 GO、不进入 canary/live。
- B3 的本地工程可行性不能替代 RH1 缺失的完整历史 FIFO PnL 链上 golden。
- B2 的 11.60% multi-hop/ambiguous 失败结论保持原样；B3 是限定 universe 的新 resolver cascade，不是放宽 B2 门槛。

## 工件

- `mvp/diagnostics/base_b3_account_entity_attribution_20260714.json`
- `mvp/diagnostics/base_b3_entity_attribution_sample_v2_20260714.json`
- `mvp/diagnostics/base_b3_trace_golden_20260714.json`
- `mvp/diagnostics/base_b3_inventory_golden_20260714.json`
- `mvp/diagnostics/base_b3_latency_probe_20260714.json`
- `scripts/wallet_profile_v0/base_b3_entity_attribution.py`
- `scripts/wallet_profile_v0/base_b3_trace_golden.py`
- `scripts/wallet_profile_v0/base_b3_inventory_golden.py`
- `scripts/wallet_profile_v0/base_b3_finalize.py`
- `tests/test_base_b3_entity_attribution.py`

