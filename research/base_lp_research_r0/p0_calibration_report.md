# P0 仪器建设与校准报告

## 结论先行

**P0.2 未通过，裁决：`STOP — FEE TRUTH ABSENT`。不得进入 P1/P2。**

原因不是模拟误差超过 15%，而是现有 Base 数据中没有独立手续费真值，因而误差和符号检验均不可识别：

1. Uniswap v3 官方事件接口明确说明，`Burn` 不提取手续费；手续费必须经 `Collect` 提取。`burn amount - mint amount` 不是手续费真值。
2. `raw_evm_logs` 中 Collect topic 的 31 条事件全部为 Ethereum mainnet (`chain_id=1`)，Base (`chain_id=8453`) 为 **0 条**。GOAL 所称“2026-03-14 一天、23 个池”的 Collect 数据确实存在，但链归属写错了。
3. 2026-03-14 的实证复核显示，三个指定 Base 池的 Burn 数量与 v3 本金公式吻合；这进一步否定了把 Burn 差额当手续费的做法。

这触发了 GOAL §1 的“若已核实事实错误，立刻停下并报告”和 P0.2 的“达不到就停、不要进 P1”。本报告没有策略收益结论。

## P0.1 份额模型

实现见 [`src/lp_simulator.py`](src/lp_simulator.py)。核心定义为：

```text
L_raw = L_human × 10^((decimals0 + decimals1) / 2)
my_share = L_raw / (L_raw + active_L_raw)
```

代码没有 `MAX_POOL_SHARE`，也没有任何份额截断。任一有效观测 `my_share > 1%` 时，整个样本标为 `CAPACITY_VIOLATION`；原始份额仍保留。摩擦分为 `gas`、入场 swap fee、出场 swap fee、滑点和 rebalance 五项，分别记录。

### 容量审计

为验证实现而预先固定一个非策略样本：2026-03-14、每池 $500、以首笔成交价为中心的静态 ±15% 区间、不再平衡。WETH/AERO 的 $500 通过同一时刻 WETH/USDC 交叉换算为 AERO；所有成交均来自 `chain_id=8453` 去重视图。

| 池 | 有效 swaps | `implied_share_p50` | `implied_share_p99` | `capacity_violation_pct` | 状态 |
|---|---:|---:|---:|---:|---|
| `0xd0b53d...` WETH/USDC 5 bps | 23,174 | 0.005073% | 0.006035% | 0.000% | OK |
| `0x82321f...` WETH/AERO 5 bps | 944 | 0.048157% | 0.063150% | 0.000% | OK |
| `0xb4cb80...` WETH/USDC 1 bp | 24,956 | 0.283540% | 0.345158% | 0.000% | OK |

可复核产物：[`out/p0_capacity_audit.csv`](out/p0_capacity_audit.csv) 与 [`out/p0_data_audit.json`](out/p0_data_audit.json)。这些数字只证明份额/容量仪器按定义工作，**不证明手续费模型已校准**。

## P0.2 真值审计

### 事件语义

Uniswap 官方 [`IUniswapV3PoolEvents.sol`](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol) 对三类事件的定义是：

- `Mint.amount0/amount1`：铸造该流动性所需的 token 数量；
- `Burn.amount0/amount1`：移除流动性产生的本金 token 数量；接口特别注明 Burn 不提取手续费；
- `Collect.amount0/amount1`：实际提取的手续费数量。

核心合约的 [`burn` 与 `collect` 实现`](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol) 也把两者分开：Burn 将移除流动性的本金记入 `tokensOwed`，Collect 才转出欠付 token。故 GOAL 提议的 `burn - cumulative mint` 同时混合不同时间价格下的本金组成、未配对的流动性生命周期和期初存量，不能产生手续费真值。

### 链纯度与 Collect 覆盖

用于核查 Collect 的事件签名为：

```text
Collect(address,address,int24,int24,uint128,uint128)
topic0 = 0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0
```

复核查询：

```sql
SELECT 8453 AS requested_chain_id,
       count() AS rows,
       uniqExact((tx_hash, log_index, address)) AS events,
       uniqExact(address) AS pools,
       min(block_time), max(block_time)
FROM eatlq.raw_evm_logs
WHERE raw_evm_logs.chain_id = 8453
  AND lower(topic0) =
    '0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0';
```

结果为 `chain_id=8453, rows=0, events=0, pools=0`。另以相同查询显式改成 `WHERE chain_id = 1` 做链归属诊断，结果为 `rows=31, events=31, pools=23, 2026-03-14 09:34:59–09:46:11 UTC`。两个查询各自链纯，不使用混链聚合。因而 GOAL 要求的 3 月 14 日独立交叉验证无法在 Base 上执行。

### 去重审计

原始表有重复摄取，研究必须使用 `*_decoded`（其上游为按 `(chain_id,pool,tx_hash,log_index)` 聚合的 `*_deduped`）视图。为避免实时流入导致复现数字漂移，下表统一截于 `2026-08-07 06:00:00 UTC`：

| 池 | swaps 原始/去重 | mints 原始/去重 | burns 原始/去重 |
|---|---:|---:|---:|
| `0xd0b53d...` | 3,613,212 / 2,524,937 | 135,281 / 83,102 | 204,888 / 123,327 |
| `0x82321f...` | 380,043 / 366,270 | 815,095 / 814,717 | 2,161,199 / 2,160,199 |
| `0xb4cb80...` | 5,368,623 / 3,799,826 | 4,137 / 2,366 | 4,753 / 2,764 |

查询口径示例（另两类事件只替换表名）：

```sql
SELECT pool_address, count() AS dedup_events,
       sum(duplicate_rows) AS source_rows,
       min(block_time), max(block_time)
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = 8453 AND removed = 0
  AND block_time <= toDateTime64('2026-08-07 06:00:00', 3, 'UTC')
  AND pool_address IN ('0xd0b53d...', '0x82321f...', '0xb4cb80...')
GROUP BY pool_address;
```

### Burn 本金公式复核

对 2026-03-14 的每个 Burn，取事件前最近的同池 Swap 价格，使用 [Uniswap v3 白皮书 §2](https://app.uniswap.org/whitepaper-v3.pdf) 的区间头寸公式，由 `(L,tickLower,tickUpper,sqrtPriceX96)` 预测 Burn 本金数量。结果：

| 池 | Burn 事件 | 非零 token 边 | 相对误差 P50 | 相对误差 P99 |
|---|---:|---:|---:|---:|
| `0xd0b53d...` | 1,223 | 979 | 3.78e-10 | 2.02e-4 |
| `0x82321f...` | 3,665 | 2,263 | 7.53e-11 | 3.03e-9 |
| `0xb4cb80...` | 135 | 157 | 5.36e-10 | 1.48e-7 |

误差来自整数舍入、用事件前价格近似事件时状态及浮点 TickMath；数量级足以证明 Burn 字段就是本金公式输出。样例见 [`out/p0_burn_principal_samples.csv`](out/p0_burn_principal_samples.csv)。

### 校准门禁结果

| 指标 | 要求 | 实际 | 裁决 |
|---|---:|---:|---|
| 独立 Base 手续费真值 | 必须有 | 0 条 Collect；无 fee-growth 历史 | FAIL |
| 模拟手续费中位相对误差 | <15% | 不可识别 | FAIL |
| 符号检验 | p>0.05 | 不可识别 | FAIL |
| 三个指定池均验证 | 必须 | 0/3 有手续费真值 | FAIL |

[`src/calibration.py`](src/calibration.py) 已实现中位相对误差和精确双侧符号检验，并在缺少独立真值时明确报错；没有用 Burn 代理值伪造一个 p 值。

## P0.3 LVR 实现状态

[`src/lvr.py`](src/lvr.py) 实现 [Milionis et al., arXiv:2208.06046](https://arxiv.org/abs/2208.06046) Example 4 的区间头寸闭式解：仓位在区间内时，瞬时 LVR 为

```text
LVR rate = L × σ² × sqrt(P) / 4
```

离散路径以 `Δlog(P)²` 估计二次变差，逐步累加；区间外增量为 0。单元测试强制每个增量非负、累计值单调不减，并覆盖恒价路径、区间外路径和逐日聚合。

由于 P0.2 已失败，未生成三个池的“fee_income/LVR/IL/fee_minus_LVR”日表：其中 `fee_income` 未经真值校准，继续发布会违反门禁。LVR 代码完成不等于 P0 通过。

## 测试与复现

```bash
cd /root/base_lp_research_r0
python3 -m unittest discover -v
python3 -m src.p0_data_audit
```

第二条命令只做 ClickHouse 只读查询，并且 Base 查询均显式包含 `chain_id = 8453`。本轮没有链上写操作，没有访问私钥，也没有修改 `/opt/EATLQ`。

## 数据能力硬边界（按 GOAL 原样复述）

1. **能做**：从 swap 数据模拟任意假想 LP 仓位的完整损益（手续费份额、IL、LVR、区间穿越）。因为 `liquidity_raw` 给了每笔成交时的活跃 L。
2. **能做（但仅限 7 个池）**：用真实 mint/burn 重建仓位级流动性变化，用于校准模拟器。
3. **不能做**：Loesch 那种钱包级 LP 盈亏分布研究。也**不能做** Aerodrome 的广度研究（只有 6 个 CL 池被发现）。补数据摄取的提案见 H4，本轮**不实施**。

本次审计需要给第 2 条增加限定：mint/burn 能重建流动性变化并校验本金数学，**但在没有 Base Collect 或历史 fee-growth 的情况下不能校准手续费收入**。

## 修复 P0 所需的最小数据

至少需要以下任一种独立真值，覆盖三个指定池与可形成统计检验的时间窗：

- Base 池级 `Collect` 事件完整回填，并正确解码 owner/range/amount0/amount1；或
- 每个区间边界的历史 `feeGrowthOutside{0,1}X128`、池级 `feeGrowthGlobal{0,1}X128` 和 position fee-growth checkpoint；或
- 可证明完整的 PositionManager `Collect`/流动性事件与 tokenId 生命周期，再映射回池和 tick 区间。

数据补齐后必须重新运行 P0.2；在达到 `<15%` 且符号检验 `p>0.05` 前，P1/P2 仍不可启动。
