# B20 dump-reclaim R0 — 预注册

冻结时刻：2026-08-19，在看任何 Arm 收益之前。  
仪器：ClickHouse 选时 + 与 `scripts/research/base_lefttail/fork_replay_backtest.py` 相同的 **anvil fork 区块真实买卖** + 决策块/出场块 QuoterV2 持有期定价。  
不是 V4。V4 roundtrip 仍 blocked，本轮只测 **B20 的 Uniswap V3 WETH 腿**。

## 假设

B20 开盘常在前 10 分钟砸出左尾形态，Keystone 的 `p_fatal` 会扔掉；若价格在 1 小时内回到开盘附近，再买，短持有，能抓到假死后的反抽，且 fork 买卖能成交。

## 宇宙（PIT）

1. `discovered_pools`：`pool_type=v3`、`has_weth_leg=1`、token0 或 token1 以 `0xb200` 开头。
2. Uni fee ∈ {100, 500, 3000, 10000}（`fee_bps ∈ {1,5,30,100}`）。
3. 每个 token 只留 **最早创建** 的一条 V3 WETH 池。
4. `t0` = 该池在 `uniswap_v3_swaps_decoded`（chain 8453）的首笔 swap。
5. 前 600s 成交笔数 `ns_pre ≥ 5`。

## Arm A（策略，本轮唯一官方臂）

- 前 600s：`early_ret ≤ ln(0.80)`（相对 t0 ≤ −20%）。
- 之后在 (600s, 3600s] 内，**第一笔**使 `ret_from_t0 ≥ ln(0.95)`（回到开盘 −5% 以内）的 swap 为入场。
- `entry_block` = 该笔的 `block_number`。超时无恢复 = 不交易。
- 出场（只看入场之后的 swap，因果）：
  - 硬止损：相对入场 `≤ ln(0.70)`（−30%）
  - 回撤：相对持仓峰值跌破 25%（`lp - peak ≤ ln(0.75)`）
  - 最长 1800 blocks（约 1h）
- 仓位：0.02 ETH（与现货实盘 `EXECUTOR_SIZE_MIN_ETH` 对齐）。

## 入场闸（fork 之后才知道，但仍是 PIT）

在 `entry_block` fork，新 EOA 真买真卖：

- 买不进：不交易（`dead`）
- 买进卖不出：记 `instant_hp`，实现 −100%
- 同块来回滑点 `< −10%`：不交易（摩擦闸）

## 官方收益尺子

对通过摩擦闸的入场：

1. QuoterV2：`entry_block` 用 0.02 WETH 买 token，再在 `exit_block` 把该数量卖回 WETH。  
   `qv2_net = weth_out / 0.02e18 − 1`
2. 在 `exit_block` 再做一次 fork 真买真卖。若卖不出：覆盖为 **stranded −100%**（不用 QV2 正数）。
3. Gas：入场 fork 的买 gas + 出场 fork 的卖 gas，按该块 `gasPrice` 折 ETH，从净收益里扣。

## 仪器勘误（策略参数未改）

本地 anvil 1.7.1 在 B20 上 `OpcodeNotFound`（预编译是 2026-06-25 Beryl 才上的）。  
官方买卖尺子改为与 B4/B8 相同的 **archive `eth_call` + `AtomicRoundTripCreateB4` 钉在 entry/exit block**。仍是「在该块对新 EOA 真走 SwapRouter02 买+卖」，不广播。QuoterV2 持有期定价不变。

## 不做的事

- 不用 `p_fatal`，不放宽 Keystone 阈值。
- 不测 V4 / B20Factory 发现（本轮宇宙仍来自已发现的 V3 池）。
- 不改 live `.env`、不写 Redis、不广播。
- 不在看完结果后改 DUMP/RECOVER/出场参数（改了必须标成 R1，另开预注册）。

## 怎么杀

- 官方臂 `n_entered < 8`：`UNDERPOWERED`，不当作可上线证据。
- 计入 hp/stranded 后中位数 `qv2_net_gas ≤ 0`：假设失败。
- 胜率主要靠单只（如 Basecat）撑起：`CONCENTRATED`，不是可推广策略。
