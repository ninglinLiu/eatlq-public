# 资产清单：数据与代码

**用途**：一年后有人（包括我们自己）要重建这套工作流时，只看这一个文件就够。
每项注明**它依赖什么**，特别是**停掉付费 archive RPC 之后它还能不能跑**。

---

# ⚠ 第 0 节：两颗地雷（读别的之前先读这个）

用户已决定「数据全部保留」。但清盘发现**有一张表正在自己删自己**，
还有一张表的名字在骗人。这两条与所有分析结论无关，但会静默损坏资产。

## ~~地雷 1~~ → **假警报，已撤回（2026-09-13 当天查实并撤销）**

> **⚠ 本节的初版结论是错的，而且我据此往生产表里写了 412,781 行冗余数据，已全部撤销。**
>
> **正确结论：那批「孤儿」日志从来没有丢过，TTL 也没有销毁任何独有数据。**
> 完整的失败过程与教训见 `02_METHOD_LESSONS.md` §7——那条教训比原来那条更值钱。
>
> **实测证据**：取 279,461 个孤儿键（`tx_hash` 归一化后）去目标表点查，
> **每一个都已经至少有一行存在**，缺失的键数为 **0**。
> 它们的正确前缀孪生行早就被 MV 正常解码了，另有 `bf180_*` 回填也独立抓到过。
>
> **我为什么会判错**：我用来证明「数据丢了」的那个检验是
> `WHERE (tx_hash, log_index) IN (SELECT tx_hash, log_index FROM 孤儿行)`——
> 拿 **64 字符无前缀**的 `tx_hash` 去比对表里 **66 字符有前缀**的值，
> **这个查询在任何情况下都必然返回 0**。我把「必然为 0」当成了「确认缺失」。
>
> 下面保留 (a)(b) 两小节的**事实描述**（它们是准确的），但**结论已作废**。

`eatlq.raw_evm_logs`（2,213 万行）挂着：

```sql
TTL toDateTime(received_at_ms) + toIntervalDay(180) SETTINGS ttl_only_drop_parts = 1
```

它是**原始采集缓冲区**，9 个物化视图从它解码进各自的 typed 表，而**那些 typed 表都没有 TTL**。
所以对已被解码的 2,172 万行来说，TTL 删掉原始副本**不丢任何信息**——这是正常的热缓存轮转。

**但有 459,496 行从未被解码，对它们来说这张表是唯一副本。** 分两类：

**(a) topic0 少了 `0x` 前缀 —— 414,186 行**

全部 9 个 MV 都用 `WHERE topic0 = '0x…'` 精确匹配。
2026-03-14 → **2026-05-21** 期间有五个采集源（`batch_probe`、`quicknode`、`probe_test`、
`sprint089_gap_backfill`、`discovery_poller`）把 topic0 写成了**不带 `0x`** 的形式，
于是等值过滤**静默地一行都没匹配上**：

| topic0（无前缀） | 事件 | 孤儿行数 | 本该进 | 区块时间范围 | 池数 |
|---|---|---|---|---|---|
| `c42079f9…` | UniV3 Swap | **355,813** | `uniswap_v3_swaps` | 2026-03-31 → 05-21 | **146** |
| `d78ad95f…` | UniV2 Swap | 40,686 | `uniswap_v2_swaps` | 2026-03-31 → 05-21 | **1,254** |
| `b3e27736…` | Aero vAMM Swap | 16,783 | `uniswap_v2_swaps` | 2026-05-18 → 05-21 | 2 |
| （空字符串） | — | 904 | — | 2026-03-14 | 8 |

> **⚠ 此处初版写的「实测确认它们真的没落地：抽 200 行反查命中 0/200」是假阴性，已作废。**
> 那个查询拿无前缀的 64 字符 `tx_hash` 比对表里 66 字符的值，必然返回 0。
> 归一化后重查：**279,461 个键全部已存在，缺失 0 个。**
> 这些无前缀行是**同一条日志的冗余原始副本**，不是未被采集的数据。

**(b) 有一个 topic0 压根没有解码器 —— 45,310 行**
`0x0d3648bd0f6ba8…`，来自**单一地址** `0x8909dc15e40173ff4699343b6eb8132c65e18ec6`
（不在 `census.pools` 里），2026-05-20 起**至今仍在持续写入**。没有任何 MV 认领它。

### 结论：不需要任何处理

TTL 是**合理设计**：`raw_evm_logs` 是原始采集缓冲区，9 个 MV 把它解码进各自的 typed 表，
而**那些 typed 表都没有 TTL**。删掉 180 天前的原始副本不丢任何信息，
因为解码后的内容在下游永久保存。**TTL 应当保留**——它正是防止一张 2,213 万行的热缓冲无界增长的东西。

无前缀的那 414,186 行是冗余原始副本，**它们过期不造成任何损失**。

### 唯一仍然成立的小缺口：(b) 那个没有解码器的 topic0

`0x0d3648bd0f6ba8…`，**45,310 行，单一地址 `0x8909dc15e40173ff4699343b6eb8132c65e18ec6`**
（不在 `census.pools` 里），2026-05-20 起至今仍在写入。**没有任何 MV 认领它**，
所以它确实只存在于带 TTL 的原始表里。

但这一条的性质与初版判断完全不同：**它不是"被过滤器漏掉的已知事件"，而是"没人要的未知事件"。**
要不要留取决于那个合约是什么——**至今未查明**。
在弄清之前，合理的处理是**先查那个地址是什么**，而不是先抢救。

### 一个真实的、与本节无关的好消息

无论上述如何，`census.cl_swaps` 走的是完全独立的链路
（`backfill_cl_swaps.py` 直接 `eth_getLogs`），**不依赖任何 MV 的 topic0 匹配**。
它 366 天零缺口、去重干净、时间戳零漂移的结论**不受任何影响**。
本次事故里它还充当了交叉验证基准，1,006/1,006 tick 精确吻合。

> **这次事故产出的方法论教训见 `02_METHOD_LESSONS.md` §7。**
> 教训不是「写入过滤器会静默漏数据」（那个假设被证伪了），而是
> **「用与被查 bug 相同的错误假设写出来的验证查询，必然确认那个 bug」**。

## 地雷 2：`eatlq.uniswap_v2_swaps` 不是 Uniswap V2 表

```sql
CREATE MATERIALIZED VIEW eatlq.aerodrome_vamm_swaps_mv TO eatlq.uniswap_v2_swaps
```

**Aerodrome vAMM 的成交被写进了这张名叫 uniswap_v2 的表里**，而且**没有 venue 列可以区分**，
只有 `source` 和 `pair_address`。任何把它当作「UniV2 成交量」的查询都是错的。

- 账本里已经因此犯过一次错（把 25,523 个 UniV2 pair 误报成 Aerodrome）。
- **拆分办法**：join `census.v2_weth_pairs` 的 `factory` / `venue` 列。
- **通用规则**：查任何 `_mv` 结尾的对象前先 `SHOW CREATE TABLE`。
  全库 10 个物化视图，9 个指向同名表（无害），**只有这一个指向别人**。

---

# 第 1 节：数据资产

## 1.1 家底

`system.tables` 报 **172 个对象**：**57 张真实存储表**、85 个普通视图、10 个物化视图、1 个字典、1 个 Memory 表。
账本此前列举的表清单是不完整的——`census` 有 11 张、`eatlq` 有 20 张、`edge` 有 4 张从未被记录。

| 库 | 占盘 | 性质 |
|---|---|---|
| `census` | 46.6 GiB | 自建全链真值 + 研究衍生表 |
| `eatlq` | 10.2 GiB | 生产采集链路 |
| `edge` | 814 MiB | 研究用棒线/流量面板 |

## 1.2 皇冠：`census.cl_swaps`

```
ENGINE = ReplacingMergeTree ORDER BY (pool, block_number, log_index)
block_time DateTime MATERIALIZED toDateTime(1778789347 + 2*(block_number - 46000000))
```

| 项 | 值 |
|---|---|
| 行数 | **424,433,536** |
| 区块 | 34,300,000 → 50,085,519 |
| 日期 | **2025-08-17 → 2026-08-17** |
| 池数 | 166,317 |
| **连续性** | **366 天全在，零缺口** |
| **去重** | 380 万行切片上 `count()` == `uniqExact(pool,block,log_index)`，**零重复。查询不需要 `FINAL`** |

**`block_time` 是算出来的，但被证明精确。** 它假设每块恰好 2 秒。
对 `eatlq.block_ts_map` 里 **1,142,539 个真实 RPC 时间戳**逐一比对：
**最小漂移 0 秒，最大漂移 0 秒。** Base 出块是确定性的 2.000000 秒，所以这不是近似，是算术恒等。
（覆盖区间 42.5M–47.1M 块，约中间三分之一；公式锚点落在验证窗内。首尾各约 8M/3M 块是外推。）

**两个真实限制**：
1. **没有 venue 列**，场地必须 join `census.pools`。
2. **元数据覆盖不全**：166,317 个池里 **127,550 个（76.7%）在 `census.pools` 里查不到**
   token / fee / venue，占成交 6.89%。这个洞随时间倾斜（2026-06 后为 0%，越早越大）。

**月度分布平滑，无掉线**（首尾月按设计从 17 日起止）：
2025-08 至 2026-08 每月 1,089 万 – 5,081 万笔，日均 68 万 – 167 万笔。

## 1.3 三个已确认的数据质量问题（全部实测存在）

### (a) tick 溢出坏点 —— 且**污染高度集中，修起来很便宜**
两个哨兵值就是 UniV3 的 tick 边界：
`1.0001^887272 = 3.4025678683638813e38`（MAX_TICK）与 `1.0001^-887272 = 2.9389568101428185e-39`（MIN_TICK）。

| 表 | 行数 | \|小时对数收益\|>10 | 占比 | **受影响池数** |
|---|---|---|---|---|
| `edge.bars_1h` | 149,696 | **0** | 0 | **0**（最大 0.43） |
| `edge.bars_1h_year` | 656,852 | 28 | 0.0043% | 5 |
| `edge.bars_1h_year_50` | 2,070,771 | **797** | 0.0385% | **411 个池里的 38 个** |

> **这是给 `05_FRONTIER.md` 的 F3 的直接礼物**：全部 797 个坏点只来自 **38 个池（9.2%）**。
> 也就是说，**修坏点过滤器不需要一个新的幅度阈值，只需要一张 38 个池的排除名单**。
> 这比账本里担心的「$50 宇宙上 10.8% 的棒线超标、可能在丢真信号」要好得多——
> 那 10.8% 是幅度阈值扫出来的，而真正的坏点是池级的、可以点名的。

同一缺陷传导到原始价格表：`census.pit_hourly` 4,023 行落在 sqrt 比率边界（0.075%），
另有 **6,006 行 `L_med_h` ≤ 0**；`edge.rot_15m2` 500 行（0.0084%，39 个池）。

### (b) 费率快照失效 —— 比账本记的更严重
`census.pools.fee` 的 `resolved_at` 全部落在 2026-08-17/19，是一次性快照。

| venue | 快照与实测不符的 (池,日) 比例 | 一年内改过费率的池 |
|---|---|---|
| `aero_cl` | **11.44%**（6,601 / 57,701） | **45 / 175** |
| `unknown` | 10.86% | 5 / 26 |
| `univ3` | **0%** | **0 / 210**（不可变，是干净对照组） |

误差幅度（aero_cl）：均值 1.05 bps，p99 **26 bps**，**最大 105 bps**。最坏的例子：

| 池 | 快照声称 | 实测最低 | 实测最高 | 不同费率档数 |
|---|---|---|---|---|
| `0x0652202c…` | **4.72 bps** | 3.7 bps | **100 bps** | **42** |
| `0x50f8f7ff…` | 95 bps | 30 | 200 | 36 |
| `0x6cda37dd…` | 75 bps | **5** | 75 | 2 |

**`census.pools.fee` 对 `aero_cl` 一律不可用。** 一律 join `census.pool_fee_daily`。

### (c) 发现流不是面板
`eatlq.discovered_pools` 是**建池注册表**（94,859 行 ≈ 94,854 个池，一池一行），不是时间序列。
对 `source='discovery_poller'` 的池算观测跨度：

| 跨度（天） | 池数 |
|---|---|
| **0** | **8,083** |
| 1 | 554 |
| 2 | 478 |
| 3–10 | 24 |
| 42 / 115 / 149 | 8 |

**9,115 / 9,147 = 99.65% 的池跨度只有 0–2 天，88.4% 只被观测一天。**
这里面是两个完全分开的总体：极少数被持续监测的池，和约 9,100 个一天快照。
**不做过滤直接在这张表上算单池时间序列，结果会被单观测噪声主导。**

## 1.4 费率表的四代血统（每代都在换时间分辨率换池覆盖）

| 代 | 表 | 池数 | 时间分辨率 | 状态 |
|---|---|---|---|---|
| 1 | `census.pools.fee` | 38,767 | **无**（快照） | ❌ 见 1.3(b) |
| 2 | `census.pool_fee_implied` | 286 | **无** | 已被取代 |
| 3 | `census.pool_fee_verified` | 787 | 6 个月度窗口（止于 2026-03） | 覆盖最广但止得早 |
| 4 | `census.pool_fee_monthly` | 513 | 13 个月 | 中间选项 |
| **5** | **`census.pool_fee_daily`** | **411** | **366 天，日度，带 `source` 列** | ✅ **权威** |

**注意这是一个取舍不是单调改进**：每代时间分辨率上升，**池覆盖下降**。
`pool_fee_daily` 的 411 个池恰好等于 `edge.bars_1h_year_50` / `bars_universe_50`
——它是为「50 档研究宇宙」建的。**这 411 个池之外要费率，只能退回第 3/4 代。**

`pool_fee_daily` 的 `source` 只有两个值：`static_immutable`（univ3，合法，因为不可变）
和 `onchain_fee_call`（aero_cl + unknown，实测）。411 × 366 = 150,426 格，填充 95.3%。

## 1.5 可信度背书（这几张表经过独立核对）

| 表 | 核对结果 |
|---|---|
| `census.pit_universe` | 存的 `sum(n_swaps)` = 227,101,754，从 `cl_swaps` 重算 = **227,101,754**，3,030 个池**零不符** |
| `census.aero_emissions` | `source` 列自带完整方法说明（topic0、archive RPC、**0 个失败分块**）；10,354 行、155 个 gauge、**53 个连续周 epoch** |
| `census.aero_gauge_stake` | `method` 列诚实声明 USD 是**严格上界**⇒ 由它算的 APR 是**下界**；每行带 `confidence`（high 1,933 / medium 2,952 / low 2,529）。**下游必须尊重这一列** |
| `edge.flow_1h*` | `ofi` 严格落在 [−1,1]，零非有限值，行数与对应 `bars_*` 精确相等 |
| `edge.eth_usd_1h` | 8,769 行，ETH 1,522.45–4,935.53，零非正值，全年无缺口 |
| `edge.bars_1h` | **全库最干净的棒线，零坏点**（最大波动 0.43）——但只有 71 个池 × 3 个月 |

## 1.6 分层结论

**TIER A —— 原始实测真值，永久保留**
`census.cl_swaps`（皇冠）、`census.pools`（**除 `fee` 列**）、`census.tokens`、
`census.aero_emissions` / `aero_gauge_created` / `aero_gauge_map` / `aero_gauge_stake_samples` /
`aero_minter_weekly` / `aero_rewards_by_epoch`、`census.v2_weth_pairs`（**拆分地雷 2 的钥匙**）、
`census.pool_fee_daily`、`eatlq.block_ts_map`、`eatlq.uniswap_v3_swaps`、
`eatlq.uniswap_v3_collects`（3 个池）、`eatlq.r0b_rpc_position_events`（3 个池，**须过滤 `liquidity>0`，
因为 66% 的 burn 是纯收费操作**）、`eatlq.uniswap_v3_mints`/`_burns`、`eatlq.discovered_pools`、
`eatlq.erc20_transfers`、`eatlq.tx_from_by_hash`、`eatlq.uniswap_v2_swaps`（**拆分后才算 A**）

**TIER B —— 衍生，可从 A 重建**
`census.pit_hourly` / `pit_universe` / `pit_wout` / `pool_fee_monthly` / `aero_gauge_stake` /
`aero_emission_apr` / `aero_pool_epoch_ctx` / `aero_price_hourly` /
`census.v2_hourly` / `v2_daily`（**⚠ 179 天窗口只有 155 天，2026-06 缺 17 天，不是连续序列**）、
`edge.bars_1h_year_50`（**排除那 38 个池**）/ `bars_1h_year` / `flow_1h_year_50` / `rot_15m2` /
`eth_usd_1h` / `bars_universe*`、`eatlq.raw_evm_logs_hourly`（**是 `AggregateFunction` 状态，
必须用 `countMerge`/`sumMerge`，不能用 `count()`**）、`eatlq.uniswap_swaps_clean_mat`

**TIER C —— 单次研究产物，只对某一轮有意义**
`census.c2_pos` / `c3_pos` / `c3x_pos` / `c4_pos` / `lp_pos`（合成 LP 仓位网格）、
`census.v2_pit_cost_25`/`_50`、`census.bf_progress`（回填记账）、
`eatlq.base_*_b2`/`_b3`/`_cha_*` 共 9 张 sprint 级钱包归因表、
`eatlq.pool_features_5m_sidecar_s083`、`edge.bars_1h`/`flow_1h`（最干净但只 71 池 3 个月）

**TIER D —— 可疑 / 已被取代 / 已知错误**

| 对象 | 错在哪 | 用什么替代 |
|---|---|---|
| `census.pools.fee` | aero_cl 上 11.44% 的池-日不符，最大差 105 bps | `census.pool_fee_daily` |
| `census.pit_cost` | **中位数就已污染**：p50 = 6,317 bps，43.7% 的行 >10,000 bps，874 行 >1e18 | `pit_cost_25`/`_50` + `feasible=1` + 上界过滤 |
| `eatlq.raw_evm_logs` | **180 天 TTL，正在自毁**（地雷 1） | 无 |
| `eatlq.uniswap_v2_swaps`（按名字用） | 混着 Aerodrome vAMM（地雷 2） | 先按 `v2_weth_pairs.factory` 拆 |
| `census.pool_fee_verified` | 只到 2026-03，少 5 个月 | `pool_fee_monthly` / `pool_fee_daily` |
| `census.pool_fee_implied` | 无时间维度 | `pool_fee_daily` |
| `edge.rot_15m` | `sp_last` 存成 `String`，缺多列；行数与后继完全相同 | `edge.rot_15m2` |
| `eatlq.uniswap_v2_burns` | **1 行**，采集从未成功 | 无 |
| `eatlq.uniswap_v2_mints` | 12,595 行 / 6 个池 / 止于 2026-05-20 | 无 |
| `eatlq.sender_pnl_summary_mat` / `sender_token_pnl_mat` | **0 行** | 各自的 `_v1` 视图 |
| `default.tmp_dash` | 0 行，Memory 引擎，重启即失 | — |

**保留优先级**：Docker 卷 `/var/lib/docker/volumes/eatlq_clickhouse_data/_data`。
没有它，下面所有 TIER A 代码资产都是空转。

---

# 第 2 节：代码资产

## 2.1 最重要的一件事：原子引擎有**三个家族，互不等价**

账本一直把「原子成交真值引擎」当作一个东西。**它是三个，能力不同，其中一个有已知盲区。**

### 家族 1 —— SwapRouter02 `eth_call` + ERC-20 槽位 state-override（Python，用得最多）

在历史区块上：探测 `balanceOf`/`allowance` 槽位 → 用 `stateDiff` 授予 WETH 与 allowance →
`eth_call` 买入 → 授予 alt 与 allowance → `eth_call` 卖出。QuoterV2 仅作对照。

**四分类标签**（关键在于把「不知道」和「卖不掉」分开，不再一刀切 −100%）：
`SELL_SUCCESS` / `DETERMINISTIC_REVERT` / `ECONOMICALLY_UNSELLABLE`（税≥90%）/ `INFRASTRUCTURE_UNKNOWN`

| 路径 | 行数 | 角色 |
|---|---|---|
| **`/root/base_f03_atomic_r0/src/atomic_engine.py`** | 248 | **最干净的独立实现，首选** |
| `/root/keystone_r2_atomic/src/r2_pipeline.py` | 1,266 | 全流程 + 校准 |
| `/root/base_truth_panel_r0/src/slot_detect.py` | 149 | **升级版槽位探测，必用** |
| `/root/base_arb_edge_r0/src/atomic_verify.py` | 343 | 两腿套利版 |
| `/opt/EATLQ/services/executor/src/swap.py` `simulate_real_sell` | ~170 | **生产用，只在 `latest`，且 fail-open** |

规范：`/root/keystone_r2_atomic/out/atomic_label_spec.md`
校准表：`/root/keystone_r2_atomic/out/atomic_simulator_calibration.csv`

> **⚠ 已知盲区，而且它真实造成过亏损：**
> **state-override 看不见 transfer hook / 卖出授权。** 生产事故：一个闭源蜜罐把所有人禁止卖出，
> 模拟器直接写余额槽位、**绕过了真实的授权分支**，于是买入成功、卖出 revert。
> 这就是 PARADAENGI 那 −0.114 ETH 的机制。
> 根因文档：`/opt/EATLQ/mvp/audits/honeypot_rootcause_20260623/honeypot_rootcause_cn.md`

其它必须记住的坑：
- **盲扫槽位 0–39 对 ERC-7201 / Solady / 代理合约无效**（F03 有 19.4% `INFRASTRUCTURE_UNKNOWN`，
  把 20 扫到 40 **一个都没救回来**）。改用 `slot_detect.py` 的 `eth_createAccessList`：**98.3%，每代币 4.2 次调用**。
- **USDC / FiatToken 不能授予 `MAX_UINT256` 余额**——最高位是黑名单标志。生产用 `max(2×amount, amount+1)`。
- **gas 炸弹蜜罐**：无限 gas 的 `eth_call` 成功，真实 gas（300k–10M）revert。生产上限 `EXECUTOR_SELL_SIM_GAS=3_000_000`。
- **分开买、分开卖不等于来回**；但同池一次 `eth_call` 来回按 CLMM 恒等式**恒等于 2×费率**（见 `01_MARKET_FACTS.md` §A1）。
  真值面板的最终尺子是**单向成交价对 `slot0` 中间价**（`slippage_631.py`）。
- **Aerodrome CL 不走 SwapRouter02**，需按工厂选路由器（`out/router_map.json`，11 个覆盖 94.1%）。

### 家族 2 —— Foundry `AtomicRoundTripCreateB4`（真的跑 `approve()`）
`/opt/EATLQ/scripts/research/base_b4/AtomicRoundTrip.sol`（151 行）
+ `/opt/EATLQ/scripts/research/gl_readonly_rpc.py`

构造函数注入，在**一次**历史 `eth_call` 里 `WETH.deposit` → approve → 买 → approve → 卖。
**代币的 `approve()` 真的执行**，比家族 1 的写槽位更接近真实路径。什么都不部署。

### 家族 3 —— Anvil fork-replay（真 EOA 买入 → 持有 → 卖出）
`/opt/EATLQ/scripts/research/base_lefttail/fork_replay_backtest.py`（430 行）

在入场块 fork，用真实 EOA 做真买真卖。
**这是唯一诚实的蜜罐检验——家族 1 和 2 都会漏掉「买得成、卖出授权失败」。**
慢且贵，但对候选名单做最后一道复核时必须用它。

## 2.2 TIER A 代码：停掉 archive RPC 仍然能跑（只吃 ClickHouse）

| 资产 | 入口 | 干什么 |
|---|---|---|
| **点时成本 / 库存门** | `/root/base_truth_panel_r0/src/pit_cost.py`、`pit_cost_year.py` | 闭式 CLMM 滑点 + 库存可行性。**对 537 次原子实测验证，中位比 1.00–1.10** |
| **分层假设搜索** | `/root/base_dex_search_r0/src/run_all.py` | 76,500 配方 → T1 纸面 vs 随机 → T2 成本容量 → T3 → 美元门。**⚠ T3 没有真调原子引擎**，用的是 35.1% 卖不掉的折价 |
| **仪器硬门** | `/root/preflight/preflight.py` | 退出码非零即禁止报裁决。见 `02_METHOD_LESSONS.md` |
| 费率真值持久化 | `/root/base_fee_tier_r0/src/persist_verified_fees.py` | 建 `pool_fee_*` 系列 |
| ETH 点时价 | `/root/base_truth_panel_r0/src/eth_price.py` | 建 `edge.eth_usd_1h`，零 RPC |
| 研究纪律编译器 | `/root/quant_research_os/pipeline/` | 假设台账、周/月编译、`AUTHORITY_MODEL.md` |
| 预登记原子库 | `/root/base_wildcat_alpha/src/hypotheses.py` | CH 只读 + 变更阻断 SQL + **带集成测试** |
| 均值回归 | `/root/base_deep_edge_r0/src/run_mr2.py` | **注意用 `run_mr2.py`，`run_mr.py` 有索引与对零比较的 bug** |
| A3/A5 预测 | `/root/base_predict_r0/src/run_predict.py` | 动量 vs 订单流，日块 bootstrap |
| LP vs HODL | `/root/base_lp_edge_r0/src/pnl.py` | 费 − IL − 执行成本 |
| 费率档做市 | `/root/base_fee_tier_r0/src/stage3.py` | `--verified-fees` 开关必开 |
| 条件做市 | `/root/base_condmm_r0/src/s01_panel.py` → `s02_test.py` | 设计上零 RPC |
| LP 模拟器 + LVR | `/root/base_lp_research_r0/src/lp_simulator.py`、`lvr.py` | **有单元测试**。P0 未过则任何 LP 结论无效 |
| 拦截影子追踪 | `/root/closed_source_shadow_tracker/tracker.py` | SQLite，唯一一条还在产新数据的线 |

## 2.3 TIER B 代码：需要 archive RPC，停了就跑不了

- **三个原子家族全部**（这是付费 RPC 的唯一实质依赖）
- 真值面板构建器：`backfill_cl_swaps.py`（**只要 `eth_getLogs`，便宜的日志节点就够，不必 archive**）、
  `slot_detect.py`、`discover_routers.py`、`depth_631.py`、`slippage_631.py`、`stability_r2.py`
- 深度探针 `/root/base_depth_census_r0/src/depth_probe.py`
- Aerodrome 排放/贿赂 `/root/base_aero_emissions_r0/src/s01…s18`（**建完表之后 s16/s17 叠加分析是 TIER A**）
- 止损成交 `/root/base_stop_fill_r0`、抛售回收 `base_b20_dump_reclaim_r0`
- 套利原子核验（stage 1–3 是 A，stage 6 是 B）
- 生产执行器的 `latest` 卖出模拟（**只需普通 RPC，不需要 archive**）

## 2.4 RPC 退订对照表

| 手上还有 | 还能做什么 |
|---|---|
| 只有 ClickHouse | 重跑搜索、preflight、均值回归/预测/LP/rotate/condmm、QROS、wildcat、美元门、费率审计 |
| + 便宜的日志节点 | 继续扩 `census.cl_swaps` |
| + 普通（非 archive）RPC | 采集、建池发现、执行器 `latest` 卖出模拟 |
| 只剩磁盘缓存 | 重放 `out/cache_atomic/*.json` 与冻结的 JSON 台账 |
| **没有 archive** | **不能**产生新的原子标签、不能 fork-replay、不能做历史槽位探测、不能补 GL5v3 |

## 2.5 一年后的重建顺序

1. 恢复 ClickHouse 卷。
2. 跑 TIER A 筛子（零 RPC）。
3. **只有某个族通过了纸面 + 容量 + preflight，才去买 archive 机时**。
4. 买了之后，只对**幸存者**跑家族 1（配 `slot_detect.py`，不要盲扫）和家族 3。
5. **不要对整个宇宙做原子标注。** 账本的教训是原子实测是最贵的仪器，
   它的位置在漏斗最底部，不是顶部。

## 2.6 若要精简，这些必须留

**代码**：`base_f03_atomic_r0/`、`keystone_r2_atomic/out/atomic_label_spec.md` + 校准 CSV、
`AtomicRoundTrip.sol` + `gl_readonly_rpc.py`、`fork_replay_backtest.py`、
`base_truth_panel_r0/src/{slot_detect,pit_cost,backfill_cl_swaps,discover_routers}.py`、
`base_dex_search_r0/`、`preflight/preflight.py`、
`HYPOTHESIS_QUEUE.md` + `CUMULATIVE_KILL_LEDGER.md` + QROS `pipeline/` + `global/`、
EATLQ `services/ingestor` + `services/archiver` + `infra/clickhouse/init/`、
`executor/src/swap.py`（**注释里有 gas 炸弹、USDC 高位、fail-open 三条坑，别删**）

**数据**：ClickHouse 卷，尤其 `census.cl_swaps`、`census.pit_cost_*`、
`census.pool_fee_*`、`edge.bars_*`。没有这些，TIER A 全部空转。
