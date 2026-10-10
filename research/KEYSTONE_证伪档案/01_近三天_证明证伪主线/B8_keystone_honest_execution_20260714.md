---
sop_applied: true
alpha_id: AR-B8-KEYSTONE-HONEST-EXEC
family: F4/F6/F7
next_gate: HUMAN_EVIDENCE_REVIEW_THEN_INDEPENDENT_FORWARD_SHADOW
decision_flags:
  training_allowed: false
  live_allowed: false
---

# B8 KEYSTONE 诚实可执行口径重验

> 纯回测/固定区块只读；不下单、不签名、不广播、不改 live；不作 GO。B7 事后 +57.67% 未用于 PASS。

## 三档延迟与同口径基线

| 延迟 | 口径 | n resolved | unresolved | mean | median | top1移除 | fatal | alpha vs baseline |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 0m | primary | 116 | 0 | +52.55% | +23.80% | +44.35% | +2.59% | +30.96% |
| 0m | unconditional | 431 | 0 | +12.05% | +0.00% | +7.03% | +3.71% | — |
| 30m | primary | 116 | 0 | +20.68% | +2.69% | +12.09% | +31.90% | +20.45% |
| 30m | unconditional | 431 | 0 | +2.50% | +0.00% | -2.49% | +13.23% | — |
| 60m | primary | 116 | 0 | -2.98% | -17.19% | -12.51% | +45.69% | +10.82% |
| 60m | unconditional | 431 | 0 | -3.27% | +0.00% | -9.36% | +17.17% | — |

## 三档延迟 OOS

| 延迟 | 口径 | n | unresolved | mean | median | top1移除 |
|---:|---|---:|---:|---:|---:|---:|
| 0m | primary | 28 | 0 | +17.47% | -6.96% | +6.21% |
| 0m | unconditional | 168 | 0 | -1.78% | +0.00% | -5.39% |
| 30m | primary | 28 | 0 | +9.73% | -8.07% | +0.02% |
| 30m | unconditional | 168 | 0 | -5.63% | +0.00% | -9.00% |
| 60m | primary | 28 | 0 | +2.48% | -8.04% | -4.95% |
| 60m | unconditional | 168 | 0 | -6.71% | +0.00% | -9.67% |

## 主口径 30 分钟：跨 regime

| regime | primary n | mean | top1移除 | baseline mean | alpha | unresolved |
|---|---:|---:|---:|---:|---:|---:|
| W1 | 38 | +70.89% | +56.58% | +14.11% | +56.78% | 0 |
| W2 | 61 | -4.62% | -11.14% | -6.27% | +1.65% | 0 |
| W3 | 17 | -0.78% | -11.78% | -7.46% | +6.68% | 0 |

## 出场证据分布

| 延迟 | 无入场/空仓 | 真成交 | 原子回退+10%折扣 | 原子失败后真实恢复 | 真 rug | unresolved |
|---:|---:|---:|---:|---:|---:|---:|
| 0m | 3 | 84 | 27 | 0 | 2 | 0 |
| 30m | 3 | 45 | 31 | 0 | 37 | 0 |
| 60m | 3 | 33 | 27 | 0 | 53 | 0 |

## 先不装门，再测派生门

不装门的 30 分钟诚实版见首表。天气门仅用校准期风险代理分布的第 35 分位，冻结数值为 **61.4161%**；OOS 开工 2/55 天。

| OOS 口径 | n | mean | top1移除 |
|---|---:|---:|---:|
| eligible 不装门 | 28 | +9.73% | +0.02% |
| 派生门 | 1 | -107.24% | — |
| 同开工天数随机门 p95 | — | +175.25% | — |

## 容量与 helper 地址风险

QuoterV2 仅作容量诊断：0.003 WETH 相对 0.001 WETH 的单位报价冲击中位数 -0.02%；0.01 WETH 为 -0.10%。这些报价没有进入收益。

实际入场 Transfer recipient 的确定性抽样 n=20；可从该 recipient 向池 transfer=20，失败=0。这只排查地址特异性 transfer 限制，不等价真实钱包已完成 swap。

## 预注册右尾

冻结哈希 `27b08babc8ba5ad15c104fec6830d469f91ad166592fbe1d82b7a37203e6ec05`。逐项：`{'pooled_mean_gt_0': True, 'pooled_top1_removed_mean_gt_0': True, 'every_powered_rolling_window_mean_ge_minus_10pct': False, 'at_least_2_powered_regimes_positive_mean_and_alpha': False, 'mc_terminal_p5_gt_100R': True, 'mc_mdd_p95_le_20R': False, 'heldout_oos_mean_gt_0': True, 'primary_delay_nonzero': True, 'primary_unresolved_eq_0': True}`。证据状态 **FAIL**；GO 决策 **NOT_MADE**。n<20 的 W3 只描述。

## SOP/守卫复核

1. `FAILED_NO_7D_ALPHA` 仍有效，本报告不 supersede。
2. label freeze 仍为 1800 秒、ROUND_TRIP_COST=0.006、pool types=v2/v3；B8 是独立研究 estimand，没有改 label。
3. FeatureStore canonical 名称及 `unique_buyer_entropy_5m` additive 字段仍一致。
4. `smart_money`/`pool_score` 执行边界仍与 contract 一致；P4 静态测试通过。
5. executor 的 chain_id、dead-letter、pending_sell 路径仍在；本任务未启动 executor。
6. paper readiness 当前不是全绿：`.env.example` 比 readiness allowlist 多 10 个键，且检测到既有 dual-ingestion 与两条未授权 watchdog cron；本任务未修改这些运行态。数据查询均使用 `FINAL`，避免把重复摄入行直接重复计数。
7. P5 monitoring schema 静态测试通过；未训练、未生成 promotion。
8. 已复用 B5 swaps、B6 weather、B7/AtomicRoundTrip，而非另造收益源。
9. artifact 未写入 RPC URL、SEND 或私钥。
10. 没有用模型指标或 B7 +57.67% 作 supersession。

## RPC、golden 与复现

RPC ledger：`{'api_credits': 72780, 'hard_cap_attempts': 6000, 'http_attempts': 3639, 'logical_calls': 3417, 'methods': {'eth_blockNumber': 1, 'eth_call': 2325, 'eth_getBlockByNumber': 1293, 'eth_getTransactionReceipt': 20}}`。固定区块 hash、请求/响应 hash 与 5 个整数 golden 均在 JSON。30 分钟真-rug 的最短向后观察为约 8.18 天，中位约 63.79 天；定义仍严格限于冻结截止块，不外推到永远。

```bash
cd /opt/EATLQ
python3 scripts/research/base_b8/b8_extract_future_swaps.py
# export only the approved FORK_ARCHIVE_RPC in the execution environment
python3 scripts/research/base_b8/b8_rpc_evidence.py
python3 scripts/research/base_b8/b8_honest_execution.py
python3 -m unittest tests.test_base_b8_honest_execution
```
