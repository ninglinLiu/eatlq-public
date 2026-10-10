# KEYSTONE / B6 RPC 与 API credits 执行前预算

日期：2026-07-14  
状态：PRE-FLIGHT ONLY；本预算阶段 RPC=0。未启动服务、未签名、未广播、未改 live。

## 1. 计费事实

- 当前批准 archive 端点供应商：QuickNode Base（endpoint secret omitted）。
- QuickNode 当前公开口径：Base 普通 RPC method 为 20 API credits/attempt；Trace/Debug 等 advanced API 为 2 倍 chain multiplier，本计划不使用。
- Base `eth_getLogs`：付费计划单请求上限 10,000 blocks；预算直接按 10,000-block chunk，不再使用会被 413 拒绝的 43,200-block chunk。
- B4 实测 retry 放大：4,204 attempts / 3,756 logical = 1.1193。普通 `eth_call` 预算按 1.12；大范围 logs 保守按 1.10–1.25。
- QuickNode 当前公开套餐：Build 含 80M credits，超额 $0.62/1M；更高套餐单价更低。实际剩余额度无法从普通 JSON-RPC 得知，须由账户 dashboard 确认。

## 2. 批次 E：116 池执行证据闭合

### E1 推荐：完全可计量的 pinned `eth_call`

四个冻结检查点：entry、exit trigger、trigger+约30min、trigger+约60min。每池：

- pool fee：1 call（已有可信缓存则跳过，但预算不扣）；
- 每检查点 AtomicRoundTrip：1 call；
- 每检查点 QuoterV2：1 call；
- 合计：1 + 4×2 = 9 logical calls/pool。

| 子批 | 池数 | logical calls | 预计 attempts(×1.12) | 预计 credits | attempts 硬帽 | credits 硬帽 |
|---|---:|---:|---:|---:|---:|---:|
| E1a 一次性校准探针 | 20 | 180 | 202 | 4,040 | 250 | 5,000 |
| E1b 43 个证据缺口池 | 43 | 387 | 434 | 8,680 | 600 | 12,000 |
| E1c 20 个已有成交对照 | 20 | 180 | 202 | 4,040 | 250 | 5,000 |
| **E1 推荐合计** | **63 unique / 83 lane rows** | **747** | **838** | **16,760** | **1,100** | **22,000** |

若将116池全部四点复测：1,044 logical，约1,169 attempts，约23,380 credits；建议总硬帽1,500 attempts / 30,000 credits。

限制：同块 AtomicRoundTrip 能检验真实 transfer path、税与当块可买可卖，但不能在历史canonical state中保存“entry时买入、exit时仍由同钱包持有”的反事实仓位。因此它能大幅缩小43笔的不确定性，不能单独证明真实历史持仓一定成交。

### E2 不纳入首批：Anvil fork本地交易

现有 `fork_replay_backtest.py` 每个检查点启动一次fork并在本地签测试交易；上游 archive 调用由Anvil动态产生，旧artifact没有upstream method ledger。粗估每fork 100–500 standard RPC：

- 43池×4点=172 forks；约17,200–86,000 attempts；约344k–1.72M credits。
- 116池×4点=464 forks；约46,400–232,000 attempts；约0.93M–4.64M credits。

该范围过宽，且当前铁律禁止签名；所以首批不执行。未来若另行批准，只能先接本地计量proxy，5池探针，upstream硬帽10,000 attempts / 200k credits，再决定是否扩。

## 3. 批次 W：B6天气数据修复

### W1 工厂创建事件完整扫描

冻结窗加35天暖机约为2026-01-23至2026-06-09，Base约5.9M blocks。按四个批准factory、10,000 blocks/chunk：

- 约590 chunks/factory；
- 约2,360 logical `eth_getLogs`；
- 约2,596–2,950 attempts；
- 约51,920–59,000 credits；
- 硬帽3,500 attempts / 70,000 credits。

敏感性：每增加/减少一个factory，约增减590 logical calls、约11,800–14,750 credits。

### W2 UTC日期映射

优先检查QuickNode Base log response是否稳定带 `blockTimestamp`：≤4 calls，≤80 credits。若存在，W2无需新增块头调用。

若不存在，则对约137个UTC日界做二分定位。最坏按每界23个 `eth_getBlockByNumber`：

- 最多3,151 logical；
- 约3,309 attempts（×1.05）；
- 约66,180 credits；
- 硬帽3,600 attempts / 72,000 credits。

不建议直接补47,809个创建块时间：那会消耗约956,180 credits（未含retry），而天气日计数只需要日期边界。

### W3 补足因果honeypot代理

不做14,877 fresh-V3全量census。先按固定hash对当前缺口周分层抽样，使每个7日滚窗至少有30个候选；预计额外600–800池，每池fee+Atomic+QV2=3 calls：

- 1,800–2,400 logical；
- 约2,014–2,686 attempts；
- 约40,280–53,720 credits；
- 硬帽3,000 attempts / 60,000 credits。

若unknown仍>5%，停止回报，不自动扩全量。全14,877池census的单独上界约44,631 logical、约49,956 attempts、约999k credits；不在本次默认授权建议内。

### W批总计

| 情形 | 预计 credits | 硬帽 |
|---|---:|---:|
| logs自带可靠timestamp | 92k–113k | 130k |
| 另需UTC日界块头 | 158k–179k | 202k |
| 保守总帽（含少量golden/重跑余量） | — | **250k** |

## 4. 推荐执行顺序与总帽

1. E1a 20池：5k credits硬帽；先校准429/5xx/unknown与attempt倍率。
2. E1b+E1c：E批累计22k credits硬帽。
3. W1前4个chunks仅作返回结构与timestamp探针：≤80 credits。
4. W1全量：70k credits硬帽。
5. 仅在W1覆盖自检通过后做W2/W3；W批累计250k credits硬帽。

推荐本轮总硬帽：

- E1：22k credits；
- W：250k credits；
- 总计：**272k API credits**，即13,600个Base standard RPC attempts等价。

按Build超额价仅约$0.17，但这是额度等价，不代表会产生账单；如果月包仍有余额，边际账单为$0。272k约占Build月包80M的0.34%，占Free 10M的2.72%。

## 5. 强制熔断

- 每次HTTP attempt计费，不只计logical success；attempt ledger实时落盘。
- 禁止JSON-RPC batch；只允许单请求并发，W1≤4 workers，E1≤8 workers。
- 连续5次429/5xx、attempt/logical>1.35、任一阶段触及硬帽、返回块hash不一致、日志区间缺口或unexpected method，立即停止。
- allowlist仅：`eth_blockNumber`、`eth_getLogs`、`eth_getBlockByNumber`、`eth_call`。
- 禁止：`eth_sendRawTransaction`、任何签名、Trace/Debug、服务启动/重启、live配置修改。
- 所有结果记录block number/hash、request/response hash、logical/attempt/credits三本账。

## 6. 结论

推荐的E1+W修复不是API成本问题：合理硬帽272k credits，远低于常见月包。主要风险是数据定义、重试失控和把Atomic同块结果误称为真实持仓成交。先跑5k-credit E1a探针并回报，再决定是否继续，是信息价值最高的顺序。
