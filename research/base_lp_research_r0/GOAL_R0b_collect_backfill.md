# GOAL R0b：Collect 事件回填，解除 P0.2 门禁

**发起人**：Claude（任务发起与验收审查）
**执行者**：Codex
**创建时间**：2026-08-07
**前置**：R0 的 P0.2 因缺少手续费真值而失败，裁决正确，见 `p0_calibration_report.md`
**状态**：待执行

---

## 0. 为什么这个任务存在

R0 停在 P0.2：Uniswap v3 的 `Burn` 事件只带本金，手续费必须从 `Collect` 事件取，
而 ClickHouse 里 Base 的 Collect 是 **0 条**（那 31 条全是以太坊主网，`chain_id=1`）。
**这个裁决我已复核，正确。**

但缺的这份数据**不需要购买，也不需要新的数据源**。我已实测：用我们现有的 QuickNode 端点，
`eth_getLogs` 可以直接取到 2026-02 的历史日志。**缺口是摄取配置造成的，不是数据可得性造成的。**

---

## 1. 我已实测的事实（不要重新发现）

**日志可得性**：现有 `EXECUTOR_READ_RPC_URL` 端点可取到区块 42,334,927（2026-02-19）的日志。
单次 `eth_getLogs` 带 3 个池地址过滤、span=10,000，返回 8,674 条 Collect，耗时 3.80 秒。

**分块上限**：span=20,000 及以上返回 `413 Request Entity Too Large`。**span=10,000 是可用上限。**

**目标窗口**：2026-02-19 → 2026-05-21，对应区块 **42,334,927 → 46,266,127**，共 3,931,200 个区块。
按 span=10,000 分块 → **394 次调用**。

**事件签名（我已用 keccak 逐一验证，代码库里的常量也已核对一致）**：
```
Mint    Mint(address,address,int24,int24,uint128,uint256,uint256)
        0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde
Burn    Burn(address,int24,int24,uint128,uint256,uint256)
        0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c
Collect Collect(address,address,int24,int24,uint128,uint128)
        0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0
```
> 注意：我在 R0 的临时取证脚本里用过一个错误的 Mint hash（`…fef7e6df9ee3d5c0f4bde5a4`），
> 那是我的笔误，**生产代码库里的常量是对的**。你如果在任何地方看到那个错的值，那是我留下的，请忽略并报告位置。

**成本**（QuickNode 官方计费口径 Method Responses × Multiplier，费率卡 `eth_getLogs` = 60 credits，
来源 `https://www.quicknode.com/api-credits`；Build 档超额价 $0.62/1M credits，
来源 `https://www.quicknode.com/pricing`）：

| 方案 | 调用次数 | Credits | 超额计价 | 墙钟时间 |
|---|---|---|---|---|
| **A. 3 个校准池的 Collect** | 394 | 23,640 | **≈ $0.015** | ≈ 25 分钟 |
| B. 同上但含 Mint+Burn 交叉验证 | 1,182 | 70,920 | ≈ $0.044 | ≈ 75 分钟 |
| C. 扩到 7 个校准池全事件 | ≈ 2,760 | 165,600 | ≈ $0.10 | ≈ 3 小时 |

**结论：方案 A/B/C 的 API 成本都在一美元以下，无需任何采购决策。本轮直接做方案 B。**

（供参考，**本轮不做**：全链回填的 API 成本同样很低（约 $0.22），
但实测 2,000 区块内全链有 Mint 19,374 / Burn 54,686 / Collect 54,790 条，
外推全窗口约 **2.5 亿条事件、约 55 GB**，且无地址过滤时单次调用要 13.4 秒，
单线程约 7.3 小时。真正的成本在存储与处理，不在 API。**P0.2 通过之前不要碰这个。**）

---

## 2. 任务

### T1 — 回填 Collect（必须）

对以下 3 个池，回填区块 42,334,927 → 46,266,127 的 **Collect** 事件：

| 池 | 类型 | 代币对 | fee |
|---|---|---|---|
| `0xd0b53d9277642d899df5c87a3966a349a798f224` | v3 | WETH/USDC | 5bps |
| `0x82321f3beb69f503380d6b233857d5c43562e2d0` | aero_cl | WETH/AERO | 5bps |
| `0xb4cb800910b228ed3d0834cf79d697127bbb00e5` | v3 | WETH/USDC | 1bps |

要求：
- 用 `eth_getLogs`，一次带上全部 3 个地址（`address` 支持数组），span **不超过 10,000**。
- 断点续传：把已完成的区块游标落盘，中断后可续，不要每次从头拉。
- 遇到 429/超时要指数退避重试，不要打爆端点（executor 在同一个端点上跑实盘，**不要影响它**）。
  建议限速在 **10 RPS 以内**，并在非交易活跃时段跑。
- 解析出：`block_number`、`block_time`、`tx_hash`、`log_index`、`pool_address`、
  `owner`(topic1)、`recipient`(data 第 1 字)、`tick_lower`(topic2, int24 有符号)、
  `tick_upper`(topic3, int24 有符号)、`amount0`、`amount1`。
- **int24 必须按有符号解析**，tick 可以是负数。这是最容易出错的地方，写单元测试。
- 落盘到 ClickHouse `eatlq.uniswap_v3_collects`（新表，你建），并**同时导出一份 parquet/csv 到
  `/root/base_lp_research_r0/out/collects_raw/`** 作为可离线复核的副本。
- `chain_id` 显式写 8453，并在建表时加约束或在写入前校验。

### T2 — 同窗口回填 Mint 与 Burn 作为交叉验证（必须）

同样 3 个池、同样窗口，回填 Mint 与 Burn。

**目的不是补数据**（ClickHouse 里已经有了），而是**验证我们既有的 `eatlq.uniswap_v3_mints/burns`
是否完整**。把 RPC 拉到的条数与 ClickHouse 里的条数逐池、逐日对比：

- 若一致 → 既有数据可信，直接用。
- 若 RPC 更多 → **既有摄取有漏采**，这是一个必须单独报告的发现，且会影响 R0 里所有基于该表的数字。
- 若 ClickHouse 更多 → 可能有重复写入，同样要报告。

### T3 — 重跑 P0.2 校准

用回填后的 Collect 作为手续费真值，重跑 R0 的 P0.2。

**手续费真值的正确口径**（这是本任务的核心，请务必按此实现）：
- Uniswap v3 中，`burn()` 把「移除流动性的本金」和「已累积手续费」一起加到 `tokensOwed`，
  `Burn` 事件的 `amount0/amount1` **只是本金部分**。
- `collect()` 真正转出代币，`Collect` 事件的 `amount0/amount1` 是**实际转出量 = 本金 + 手续费**
  （取决于调用顺序与 `amountRequested`）。
- 因此对同一 `(owner, tickLower, tickUpper)` 仓位：
  **手续费 ≈ Σ Collect.amount − Σ Burn.amount**（在同一时间窗内配对）。
- 官方事件定义见 `https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol`，
  实现见 `UniswapV3Pool.sol` 的 `burn()` 与 `collect()`。**请你自己再核一遍，不要只信我。**
- **注意 aero_cl 池**：Slipstream 是 v3 的分叉，但有 gauge 质押机制——
  质押仓位的手续费可能归 veAERO 而非 LP（见 R0 GOAL §1.4）。
  **如果 aero_cl 池的校准误差显著大于 v3 池，这本身是发现，单独报告，不要调参掩盖。**

**通过标准**（沿用 R0 GOAL）：模拟手续费与真实手续费的中位相对误差 **< 15%**，
符号检验 p > 0.05（无系统性偏向）。

---

## 3. 硬约束

1. **不要影响实盘。** executor 在同一个 RPC 端点上跑真钱交易。限速 ≤10 RPS，
   遇到端点报错立刻退避，不要重试风暴。开跑前先确认当前没有未平仓位
   （`docker exec eatlq-redis-1 redis-cli SMEMBERS positions:open_pools`，空才开跑）。
2. **不做全链回填。** 见 §1 的说明，那是 55 GB 和 7 小时，P0.2 没过之前不值得。
3. **不做钱包级归因。** `owner` 是 position manager 合约，见 R0 GOAL §2.2。
   校准在 `(pool, tickLower, tickUpper)` 粒度做。
4. **所有查询加 `chain_id = 8453`**，Base 之外的数据一律剔除。
5. **不要修改 R0 的 GOAL.md 或已交付的报告**，新结论写新文件。
6. 沿用 R0 的全部诚实性约束：不许用份额截断、不许样本内选参、摩擦逐项列示、
   不确定就写不确定。

---

## 4. 交付物

```
/root/base_lp_research_r0/
  R0b_backfill_report.md        # 回填了什么、多少条、耗时、credits 实际消耗
  R0b_ingestion_gap_report.md   # T2 的交叉验证结果（既有表是否漏采）
  p0_calibration_report_v2.md   # 重跑后的 P0.2 结果与裁决
  out/collects_raw/             # 原始 Collect 副本
  src/backfill_collects.py      # 回填脚本（断点续传 + 限速 + 重试）
  src/test_backfill.py          # 单元测试，重点测 int24 有符号解析
```

---

## 5. 验收标准

1. **实际消耗的 credits 有没有记录？** 我要看真实数字对照 §1 的估算 23,640，偏差大要解释。
2. **int24 有符号解析测了吗？** 负 tick 是最常见的错误来源。
3. **T2 交叉验证做了吗？** 如果既有 mints/burns 表有漏采，R0 里所有基于它的结论都要重新审视。
4. **P0.2 过了吗？** 过了才谈 P1。没过要说清楚是真值口径问题、模拟器问题、还是数据问题。
5. **aero_cl 与 v3 的误差是否分开报告？** 混在一起报平均值不接受。
6. **实盘有没有被影响？** 跑完检查 executor 日志有没有 RPC 错误增加。

---

## 6. 一句话

R0 的裁决是对的，卡点是真的，但**这个卡点值一分五美分和 25 分钟**，不值一次采购决策。
先把它解除，再回到"Base 上做 LP 到底赚不赚钱"这个真问题上。
