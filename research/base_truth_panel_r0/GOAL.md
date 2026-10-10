# GOAL：Base 真值面板 R0

**开轮**：2026-08-17  
**起因**：`base_depth_census_r0` §6 证明现有面板是 `discovery_poller` 新池发现流，单池观测窗 0–2 天，全库只有 4 个 v3 池有连续覆盖。**过去 30 轮能问什么问题，是被这个数据生成过程决定的，不是被研究者选择的。**  
**性质**：仪器轮。**本轮不产生任何 alpha 结论，不做回测，不出候选。**  
**最高允许状态**：`INSTRUMENT_FACT`

LIVE_TRADING = FORBIDDEN。NO_SIGNING。NO_BROADCAST。见 `NO_LIVE.lock`。

---

## 1. 这一轮要还的债

| 债项 | 现状 | 本轮做到哪 |
|---|---|---|
| 单池观测窗 0–2 天 | 任何需要 >72h 单池历史的假设结构性不可测 | **拉一段不依赖 poller 的完整历史** |
| Aerodrome 全盲 | vAMM 只订阅 2 个 pair，两个工厂零日志 | Slipstream(CL) 与 UniV3 共用 topic0，**一次回填同时覆盖** |
| 宇宙大小未知 | 面板 60 天见 607 个 v3 WETH 池 | 用真值数出到底有多少池 |
| 池寿命未知 | 面板说 2 天，链上抽样 4/6 仍在成交 | **第一次真实测量池寿命分布** |

---

## 2. 方法（先冻结，跑完不改）

### 2.1 取数口径

不按地址过滤，**只按 topic0 拉整条链**：

```
topic0 = 0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67
       = Swap(address,address,int256,int256,uint160,uint128,int24)
```

这个签名 Uniswap V3 与 Aerodrome Slipstream 共用，所以一次回填两个场地都进来，且**不受任何注册表覆盖影响**——池不需要被"发现"过就会出现。

- 窗口：block **46,000,000 → 链头**（2026-05-14 20:09:07 → 2026-08-17，约 94.6 天）
- 分块：10,000 块／次（QuickNode 付费档上限；413 时自动二分）
- 落地：`census.cl_swaps`，`ReplacingMergeTree ORDER BY (pool, block_number, log_index)`
- 续跑：`census.bf_progress` 记已完成分块，可随时 kill 重启

### 2.2 区块时间解析推导（已验证）

Base 出块间隔实测在 46M→50,085,435 五个采样点上**恒为 2.000000 s/block，误差 0 秒**，故：

```
block_time = 1778789347 + 2 * (block_number - 46000000)
```

作为 MATERIALIZED 列。这省掉 400 万次 `eth_getBlockByNumber`。

### 2.3 成本（已按官方表核算）

QuickNode 对 **Base 是「所有方法一律 20 credits」**（`quicknode.com/api-credits/base`；只有 Advanced APIs 2x、Large Calls 4x 例外，`eth_getLogs` 不在其中）。

| 项 | 量 |
|---|---|
| 分块数 | 409 |
| **credits** | **≈ 8,180**（免费档月配额 1,000 万的 0.08%） |
| 下载量 | ≈ 77 GB |
| 落盘 | ≈ 9.3 GB（实测 2.4x 压缩） |
| 墙钟 | ≈ 60 min（4 并发） |

搜索结果里的「255 credits」是 Ethereum 的表，「60 credits」是第三方 rate card，**都不适用于 Base**。

---

## 3. 本轮必须回答的问题（只问事实，不问 alpha）

1. **真实宇宙有多大**：94.6 天里有多少个不同的 CL 池成交过？对比面板的 607。
2. **池寿命的真实分布**：首末成交跨度的分位数。面板说 0–2 天，真值说多少？
3. **有多少池活过 72 小时**：这个数字直接决定「成熟币」类假设是否可能存在。
4. **多少池活过 7 天 / 30 天**：决定中长周期假设的宇宙大小。
5. **Aerodrome Slipstream 占多少**：按工厂区分 UniV3 与 Slipstream，各多少池、多少成交。
6. **面板漏了什么**：真值池集合减去面板池集合，漏掉的那些池的成交量分布是什么——漏掉的是垃圾还是好东西。

---

## 4. 明确不做

- 不做任何 PnL、不做回测、不做信号搜索。本轮只建面板、只数事实。
- 不用本轮数据给任何旧结论"平反"。旧结论的改判由独立的回溯审计负责，且必须逐条给出参数证据。
- 不推论 Base 有 alpha。宇宙变大不等于有 edge——`base_depth_census_r0` 已实测 8 月最活跃 meme 币 $50 来回 210 bps，过路费问题不因宇宙变大而消失。
- 不碰 live。

---

## 5. 产物

| 文件 | 内容 |
|---|---|
| `src/backfill_cl_swaps.py` | 回填器（可续跑） |
| `census.cl_swaps` | 真值成交表 |
| `census.bf_progress` | 续跑检查点 |
| `out/backfill.log` | 运行日志（含实际 credits） |
| `reports/R0_TRUTH_PANEL.md` | 上面 6 个问题的答案 |
