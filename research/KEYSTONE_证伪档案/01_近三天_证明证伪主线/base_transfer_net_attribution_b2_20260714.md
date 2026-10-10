---
sop_applied: true
alpha_id: AR-B2-TRANSFER-NET
family: F3/F7
next_gate: HOLDER_COLLAPSE_AND_SUBGRAPH_REPAIR
decision_flags:
  training_allowed: false
  live_allowed: false
---

# Base B2-v3 持币口径三关核验

**总裁决：`NEEDS_REVIEW_HOLDER_COLLAPSE_AND_SUBGRAPH_GOLDEN_FAIL`。三关未全过，验收口径不重定向，不 GO；RH1 不得使用 B2，merit=`UNTESTED`。**

## 关卡 A：合约钱包集中度 — FAIL

冻结规则：对 `contract_wallet/eip7702` 账户，`distinct_tx_from_count >= 20`，或其 swap 量进入全体 resolved 账户前 0.1%，即列入 `suspected_shared_infra` 研究排除名单。

- resolved 账户：6,600；前 0.1% 阈值：volume rank ≤7。
- 命中疑似共享合约：**80 个**。
- 排除前 beneficial：52,976 / 57,426 = **92.250897%**。
- 排除 80 个账户后：27,455 / 57,426 = **47.809355%**。
- 最大剩余账户：`0x66a0...e650`，1,149 swaps，占调整后 beneficial **4.185030%**。
- 通过条件要求 coverage≥90% 且单账户<1%；两项均失败。

疑似地址未混入已有“已知基础设施”权威 manifest，而是进入独立、可审计的研究排除名单 `configs/research/base_holder_suspected_shared_infra_20260714.json`。该名单已用于上述 coverage 反事实重算，避免把启发式标签伪装成已确认实体。

## 关卡 B：第三方 EOA 持币人 — DESCRIPTIVE_ONLY

严格复现任务书定义后的总体为 2,050 行、159 个 resolved EOA：

| 桶 | 行数 | 占比 | 账户数 |
|---|---:|---:|---:|
| `self_second_wallet` | 529 | 25.804878% | 33 |
| `deposit_sink` | 1,162 | 56.682927% | 24 |
| `one_off` | 93 | 4.536585% | 93 |
| `adverse_unclassified` | 266 | 12.975610% | 9 |

最后 9 个账户由同一 feeder 重复打入、自己不签名、也无后续同 token 卖出；它们不符合指定三桶中的任何一个，因此 adverse 留空，不强塞进 `deposit_sink` 或 `one_off`。

描述性含义：至少 56.68% 的行表现为多 feeder 归集，持币口径确有把签名者与集中持币地址折叠/拆分的身份风险。本关不据此改归因。

## 关卡 C：多跳子图 golden — FAIL，已回滚

从 `transfer_subgraph` 且交易内 swap_rows>1 的候选中，先排除关卡 A suspects，再按区块排序作五分位均匀抽样。5 个独立 archive `balanceOf` 全窗边界均已精确固化：

- 1/5 与物化 `net_delta_raw` 相等；
- **4/5 不相等**；
- 按任务书，整批 `transfer_subgraph` 救回逻辑标记 `FAIL_ROLLBACK_REQUIRED`。

代码保留纯诊断实现，但 `TRANSFER_SUBGRAPH_GOLDEN_APPROVED=False`；重建后可信 `transfer_subgraph` 行精确为 0。beneficial coverage 回落为 **82.877094%**，ambiguous 回到 **6,664 / 57,426 = 11.604500%**。

## 验收与测试

三关没有全过，所以“golden + 集中度干净 + ambiguous≤3%”新验收**不生效**：

- holder concentration：FAIL；
- subgraph golden：FAIL；
- post-rollback coverage：82.877094% <90%；
- post-rollback ambiguous：11.604500% >3%；
- infra leak：0。

执行：

```bash
EATLQ_RUN_B2_RPC=1 EATLQ_RUN_B2_CH=1 \
  python3 -m unittest -v tests.test_base_transfer_net_attribution
```

结果：**18/18 PASS**。测试包含原 3 个边界 golden、5 个均匀 adverse archive 边界、五例回滚物化断言、账户分类、覆盖、EOA 参考指标与 infra=0。

机器证据：

- `mvp/diagnostics/base_transfer_net_b2_audit.json`
- `mvp/diagnostics/base_transfer_net_b2_holder_v3_audit_pre_rollback.json`
- `configs/research/base_holder_suspected_shared_infra_20260714.json`
- `mvp/diagnostics/base_transfer_net_b2_test_evidence_20260714.json`
- `mvp/diagnostics/base_transfer_net_b2_hash_ledger_20260714.json`

`eth_getCode` 仍是 end_block 单点快照，不能替代逐 swap 的 EIP-7702 时点证明。禁止据本结果判断 Base 聪明钱有或没有 edge。
