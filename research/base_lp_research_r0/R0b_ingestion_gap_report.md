# R0b 既有摄取缺口报告

## 结论

既有 Base Mint/Burn decoded 表在目标窗口内存在**严重漏采**。RPC 数据比 ClickHouse
多 **1,744,873 条 Mint** 和 **5,432,817 条 Burn**；反向 anti-join 为 0，说明旧表中的
记录都能在 RPC 回填中找到，没有发现 ClickHouse 独有事件或重复/异源事件。

因此，R0 中任何依赖既有 `uniswap_v3_mints_decoded` 或
`uniswap_v3_burns_decoded` 完整性的数量、流动性路径或结论都必须重新审视。本轮校准使用
RPC staging 数据，不把不完整旧表当作完整真值，也没有修改旧表。

## 总量对比

窗口为 `chain_id=8453`、区块 `42,334,927` 至 `46,266,127`，`removed=0`。

| 事件 | RPC 唯一事件 | ClickHouse decoded 唯一事件 | RPC 独有 | ClickHouse 独有 | 旧表覆盖率 |
|---|---:|---:|---:|---:|---:|
| Mint | 2,645,058 | 900,185 | 1,744,873 | 0 | 34.03% |
| Burn | 7,719,107 | 2,286,290 | 5,432,817 | 0 | 29.62% |

覆盖率只是 `ClickHouse / RPC` 的窗口总量比，不代表随机抽样率；缺口在池和日期上并不均匀。

## 按池的事件主键差集

差集键为 `(chain_id,pool_address,tx_hash,log_index)`，用双向 `LEFT ANTI JOIN`
计算。ClickHouse 24.12 在这些 LowCardinality 视图上的 `EXCEPT` 曾返回不满足集合基数
恒等式的结果，因此最终审计不采用 `EXCEPT`。

| 池 | RPC Mint | CH Mint | RPC 独有 Mint | CH 独有 Mint | RPC Burn | CH Burn | RPC 独有 Burn | CH 独有 Burn |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `0xd0b53d9277642d899df5c87a3966a349a798f224` | 135,999 | 83,102 | 52,897 | 0 | 206,594 | 123,327 | 83,267 | 0 |
| `0x82321f3beb69f503380d6b233857d5c43562e2d0` | 2,504,687 | 814,717 | 1,689,970 | 0 | 7,507,266 | 2,160,199 | 5,347,067 | 0 |
| `0xb4cb800910b228ed3d0834cf79d697127bbb00e5` | 4,372 | 2,366 | 2,006 | 0 | 5,247 | 2,764 | 2,483 | 0 |

每一行都满足 `RPC = CH + RPC独有`，而 `CH独有=0`。

## 逐日检查

三个池、91 个 UTC 日，共比较 273 个池日：

| 事件 | 比较池日 | 不一致池日 | RPC 更多 | CH 更多 | CH `block_time IS NULL` |
|---|---:|---:|---:|---:|---:|
| Mint | 273 | 216 | 216 | 0 | 490 |
| Burn | 273 | 214 | 214 | 0 | 769 |

日级比较对 RPC 使用同一确定性 Base 区块时间映射；ClickHouse 的空时间行单独计数，
没有静默归入某一天。结果显示缺口覆盖窗口的大部分日期，并非单日偶发问题。

## 影响与处置

- 旧 Mint/Burn 表不能用于本窗口的完整事件计数或仓位流动性重构。
- P0.2 v2 改用 `eatlq.r0b_rpc_position_events` staging 表的 RPC 回填事件。
- 新 Collect 表不受旧 decoded 表缺口影响，事件主键唯一性已单独验证。
- 本报告只确认缺口及其规模；没有推断上游漏采的具体软件根因，也没有覆盖或修复旧表。

机器可读结果在 `out/r0b_ingestion_comparison.json`，逐池逐日在
`out/r0b_ingestion_comparison.csv`；验证逻辑在 `src/validate_ingestion.py`。
