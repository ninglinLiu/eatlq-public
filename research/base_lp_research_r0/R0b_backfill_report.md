# R0b 回填报告

## 裁决摘要

T1 和 T2 的 RPC 回填已完成。窗口为 Base `chain_id=8453` 的区块
`42,334,927` 至 `46,266,127`（两端都包含），三个事件各发出 394 次
`eth_getLogs`，每次同时过滤三个池、跨度不超过 10,000 区块。全部请求串行、
限速不超过 2 RPS；1,182 个响应均一次成功，重试与 RPC 错误均为 0。

Collect 共回填 **7,719,230** 条，事件主键无重复，已写入
`eatlq.uniswap_v3_collects`，并保存 394 份 gzip CSV 离线副本到
`out/collects_raw/`（约 298 MiB）。

## 回填数量

| 池 | 类型 | Collect | Mint | Burn |
|---|---:|---:|---:|---:|
| `0xd0b53d9277642d899df5c87a3966a349a798f224` | v3 5 bps | 207,258 | 135,999 | 206,594 |
| `0x82321f3beb69f503380d6b233857d5c43562e2d0` | aero_cl 5 bps | 7,506,962 | 2,504,687 | 7,507,266 |
| `0xb4cb800910b228ed3d0834cf79d697127bbb00e5` | v3 1 bp | 5,010 | 4,372 | 5,247 |
| **合计** |  | **7,719,230** | **2,645,058** | **7,719,107** |

Collect 表以 `(chain_id,pool_address,block_number,tx_hash,log_index)` 排序，使用
`ReplacingMergeTree(ingested_at)`，并有 `CHECK chain_id = 8453`。最终检查为
7,719,230 行、7,719,230 个唯一 `(tx_hash,log_index)`、0 条错误链记录。
`tick_lower < 0` 的记录有 212,266 条，证明负 tick 路径实际被覆盖。

区块时间使用窗口起点的确定性时间锚点 `2026-02-19 00:00:01 UTC` 和 Base
两秒出块间隔生成。用现有 `block_ts_map` 的 1,086,765 个区块交叉验证，时间戳
不一致数为 0，最大偏差为 0 ms。

## 运行、断点与离线副本

| 事件 | 完成分块 | 日志数 | JSON-RPC 响应 | 重试/错误 | 墙钟时间 |
|---|---:|---:|---:|---:|---:|
| Collect | 394 | 7,719,230 | 394 | 0 / 0 | 1,641.87 秒（27.36 分） |
| Mint | 394 | 2,645,058 | 394 | 0 / 0 | 964.94 秒（16.08 分） |
| Burn | 394 | 7,719,107 | 394 | 0 / 0 | 1,326.88 秒（22.11 分） |
| **合计** | **1,182** | **18,083,395** | **1,182** | **0 / 0** | **3,933.69 秒（65.56 分）** |

每个事件的检查点都已推进到 `next_block=46,266,128`；每完成一个分块就落盘，
中断后只会继续未完成区间。Collect 离线副本为 394 份；Mint/Burn 的 788 份
交叉验证副本保存在 `out/rpc_backfill/`（合计约 503 MiB）。

Collect 实际耗时比 25 分钟估计高 9.5%，原因是该窗口实际返回 771.9 万条日志，
响应解析和压缩写盘占用了不可忽略的时间。方案 B 总耗时 65.56 分钟，低于 75 分钟估计。

## Credits 与成本

这里记录的是应用实际观察到的成功 Method Responses 乘任务指定费率，并非读取账户
账单：

| 项目 | 响应 | 单次 credits | credits | 按 $0.62 / 1M 估算 |
|---|---:|---:|---:|---:|
| Collect | 394 | 60 | 23,640 | $0.0146568 |
| Mint + Burn | 788 | 60 | 47,280 | $0.0293136 |
| **方案 B 合计** | **1,182** |  | **70,920** | **$0.0439704** |
| 历史 `positions()` 快照诊断 | 183 `eth_call` | 20 | 3,660 | $0.0022692 |
| **本轮观测总计** |  |  | **74,580** | **$0.0462396** |

Collect 的 23,640 credits 与任务估算完全一致。计费口径参照
[QuickNode API credits](https://www.quicknode.com/api-credits)；金额按任务指定且在运行时复核的
[Build 超额价格](https://www.quicknode.com/pricing)计算。

## 正确性与实盘安全

- `int24` 按 256 位 topic 的二补码有符号解析；单元测试覆盖 `-1`、最小/最大 int24、
  sign-extension 以及真实负 tick 解码。
- 三个事件 topic 使用生产代码中的正确签名；未发现任务说明中的错误临时 Mint hash。
- 所有查询和写入显式限定 `chain_id=8453`，且未做全链回填。
- 开跑前和结束后 Redis `positions:open_pools` 均为空。回填期间执行器日志中的
  `429`、timeout、rate-limit、RPC error、connection error 计数均为 0。

可复核材料：`out/backfill_state/`、`out/collects_raw/`、`out/rpc_backfill/`，以及
`src/backfill_collects.py` 和 `src/test_backfill.py`。
