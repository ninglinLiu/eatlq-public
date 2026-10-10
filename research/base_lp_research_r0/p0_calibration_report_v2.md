# P0.2 校准报告 v2

## 裁决

**P0.2 未通过，停止，不进入 P1。**

通过门槛为中位相对误差 `<15%` 且双侧符号检验 `p>0.05`。两个 v3 池合并后的
中位相对误差为 **39.04%**，`p=0`；Aero Slipstream 单独为 **96.16%**，
`p=1.90e-8`。两类池均未通过，Aero 明显更差，因此没有把它与 v3 混合后调参掩盖。

## 官方语义复核与真值口径

[Uniswap v3 事件接口](https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)
明确区分 Burn 与 Collect；[池实现](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol)
显示 Burn 把移除流动性的本金加入 `tokensOwed`，Collect 再把请求额与
`tokensOwed` 的较小值实际转出。因此同一 `(owner,tickLower,tickUpper)` 的基本真值是
累计 `Collect - Burn`，但必须处理窗口边界和部分领取。

实际实现采用以下保守规则：

1. 每个仓位的首个 Collect 只作为清账锚点，不计分，以清除窗口前已欠的本金或费用。
2. 锚点以后，Burn 本金跨多个部分 Collect 结转；只有累计 Collect 超过尚未领取本金的
   余额才作为费用。
3. 未匹配的窗口末尾本金、非正费用 token side、失效的流动性路径不计分。
4. 干净的领取 episode 最后按 `(pool,tickLower,tickUpper,token)` 聚合，避免把 position
   manager 的 `owner` 当作钱包，也避免高频部分领取产生伪独立样本。

一个直接证据说明不能机械使用全窗口总和：v3 5 bps 池的一个仓位在窗口内先 Mint，随后
很快 Burn+Collect，期间没有 Swap，但 `Collect-Burn` 仍包含约 102,979,642 单位 token0
和 254 单位 token1；这是窗口前遗留的 `tokensOwed`，首个 Collect 锚点会将它排除。

## 窗口边界与 Aero 特殊机制

对两个标准 v3 池，在终点区块 `46,266,127` 通过 Multicall3 查询
`positions(bytes32)`，再减去窗口内净 Mint/Burn，重构起点流动性。共查询 27,172 个仓位键，
183 个 `eth_call` 响应、0 次重试；3,176 个键的重构起点流动性非零。

| 池 | 快照键 | 起点非零 | 起点为负 | 终点非零 |
|---|---:|---:|---:|---:|
| v3 5 bps | 23,130 | 2,538 | 0 | 2,077 |
| v3 1 bp | 2,576 | 167 | 0 | 143 |
| aero_cl 诊断 | 1,466 | 471 | 129 | 389 |

Aero 的 129 个负起点不是可接受的标准 v3 边界重构结果。官方 Slipstream
[Position 库](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/libraries/Position.sol)
仍以 `(owner,tickLower,tickUpper)` 为键，但
[CLPool](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/CLPool.sol)
的 gauge stake/unstake 会在 NFT 与 gauge 仓位间移动流动性而不发出对应 Mint/Burn，且 staked
路径的费用处理不同。因此快照修正只用于两个 v3 池；Aero 只在窗口内观察到 Mint 后开始计分，
并单独报告。这也是 Aero 误差更大的合理机制解释，但不是对全部误差的完整因果证明。

## 模拟器与样本

模拟费用为每次 Swap 的正向输入额乘费率，再乘仓位流动性占事件所报 active liquidity 的
比例；只有最终事件 tick 位于区间内时计入。跨 tick Swap 只能使用事件的最终 tick 和 active
liquidity，这是当前近似的重要残余误差源。实现也修复了旧查询中的列别名遮蔽问题：此前
active liquidity 被错误读成 0，v2 使用表限定列名。

最终按 range/token 聚合后有 24,432 个正真值观测，其中 v3 23,686 个、Aero 746 个。
符号检验在样本量不大时用精确二项检验，大样本用等价的稳定实现；ties 不进入正负计数。

## 结果

| 池/分组 | range-token 观测 | 中位相对误差 | 模拟高于真值 | 模拟低于真值 | ties | 双侧符号检验 p | 通过 |
|---|---:|---:|---:|---:|---:|---:|---:|
| v3 5 bps | 22,634 | 38.25% | 7,205 | 15,428 | 1 | 0 | 否 |
| v3 1 bp | 1,052 | 59.72% | 275 | 777 | 0 | 5.50e-56 | 否 |
| **v3 合计** | **23,686** | **39.04%** | **7,480** | **16,205** | **1** | **0** | **否** |
| **aero_cl** | **746** | **96.16%** | **296** | **450** | **0** | **1.90e-8** | **否** |
| 全部（仅供参考） | 24,432 | 42.11% | 7,776 | 16,655 | 1 | 0 | 否 |

误差符号以 `模拟值 - 真值` 定义。所有分组都以模拟偏低为主，且偏向具有统计显著性。

## 失败诊断

这次失败不再是“没有 Collect 真值”：Collect 已完整回填，Mint/Burn 也改用同窗口 RPC 数据，
并处理了首个 Collect、部分领取和 v3 起点仓位。当前结果表明剩余的主要问题在模拟器与可识别性：

- 单个 Swap 事件的最终 tick/active liquidity 无法精确表示跨 tick 路径上的逐段费用分配；
- Collect 的请求上限使一次事件不能直接标记全部已累积费用，虽已用本金结转降低偏差，仍有
  episode 边界不透明性；
- Slipstream gauge 的 stake/unstake 与 staked-fee 语义不由标准 Mint/Burn 流完整表达；
- 旧 Mint/Burn 摄取严重漏采，说明 R0 的旧表派生数字不可继续沿用，不过 v2 已绕开该缺口。

没有做份额截断、样本内选参或为通过门槛而调整 Aero。要继续推进，需要能逐 tick 重放 Swap
和显式建模 Slipstream gauge 的独立模拟器改进；在此之前，P1 的收益结论没有合格的手续费
校准基础。

机器可读汇总在 `out/p0_calibration_summary_v2.json`，range/token 观测在
`out/p0_calibration_range_observations_v2.csv`，边界快照在
`out/position_liquidity_boundaries.csv`，实现为 `src/p0_calibrate_v2.py`。
