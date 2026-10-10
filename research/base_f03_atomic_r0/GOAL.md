# GOAL：F03 赛马 — atomic 成交真值回测

**发起人**：用户  
**执行者**：Cursor Grok  
**创建时间**：2026-08-17  
**工作区**：`/root/base_f03_atomic_r0/`  
**只读源**：wildcat 面板与影子、greenfield/keystone atomic 原语、ClickHouse、archive RPC  
**不改**：`/root/base_wildcat_alpha`、`/root/keystone_r2_atomic`、`/root/base_greenfield_directional_v0`、`/opt/EATLQ` live

---

## 0. 硬约束

1. **主账是路由器 `eth_call`，不是 QuoterV2，不是 tick。** Quoter 只准坐对照列。tick 的 `fwd_ret_4` 只对照。
2. **买：SwapRouter02 `exactInputSingle` + WETH balance/allowance state-override。** 卖：代币 balance/allowance override 后再走路由器。口径对齐 V1.1。
3. **禁止真实资金。** `NO_LIVE.lock` 缺失则退出。
4. **不重写 F03，不换宇宙来让结果变好看。** 不能做空的腿标 `NOT_EXECUTABLE`，不许改去找更顺的池。
5. **现货只能买 alt。** 必须先映射“纸面多/空 tick”到“买哪一个币”，映射在看结果之前锁死。
6. **最高状态仍是 SHADOW_CANDIDATE。** 本轮即使 atomic 为正也不授权实盘。
7. **预登记先写进 `out/preregister.json`，再跑成交。**

---

## 1. 本轮只回答

在冻结的 F03 深挖阈值下，**现货账户用 WETH 按自己的尺寸能成交的那一侧**，4 小时后原子买卖的净 WETH 期望是多少？和 tick 价差是不是同一回事？

不回答：月赚 1000、信贷正交、翻向机制、做空腿的纸面收益。

---

## 2. 可执行映射（预登记，不许事后改）

V3 tick 上涨 = token0 相对 token1 变贵。用 WETH 买 alt：

| 纸面 F03 | 池结构 | 现货动作 | 标签 |
|---|---|---|---|
| HIGH | token1 = WETH | 买 token0 | `LONG_TICK` |
| LOW | token0 = WETH | 买 token1 | `SHORT_TICK_VIA_ALT` |
| 其他（含 HIGH 且 token0=WETH、USDC 池、无 WETH 腿） | — | 不做 | `NOT_EXECUTABLE` |

主样本 = replay 里全部 `LONG_TICK`（HIGH 且 WETH 是 token1）。  
次样本 = `SHORT_TICK_VIA_ALT` 按月分层抽 60 条（种子 8453）。  
4 条影子只分类 + 能映射的才跑，n=4 不作统计。

---

## 3. 冻结执行参数

```
router              = SwapRouter02 0x2626664c2603336E57B271c5C0b26F421741e481
quoter              = QuoterV2     0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a  # 对照
amt_in              = 16666666666666666   # $50 @ 3000，执行器帽
entry               = 信号小时最后一笔 swap 的块 + 1
exit                = [t+4h, t+5h) 最后一笔 swap 的块；若无，则 entry_block + 7200
fee                 = 该池 fee() 在进场块的链上值，不用 meta.fee_bps
gas_debit           = 0.0002 WETH 来回（成功路径）
amountOutMinimum    = 0（与既有 atomic 模拟一致）
retry_unknown       = 卖出 INFRASTRUCTURE_UNKNOWN 时 +0,+5,+30,+150 块
calibration_smoke   = keystone 校准表 1 个 SUCCESS + 1 个 RUG；失败则停
```

PnL（V1.1 纠正口径）：

| 状态 | 计入 |
|---|---|
| NOT_EXECUTABLE / 无样本 | 不进主分母 |
| ENTRY_TX_REVERT | −买 gas |
| BUY 成功 + SELL_SUCCESS | weth_out/amt_in − 1 − 0.0002 |
| BUY 成功 + revert/税≥90% | −1.0 − gas |
| INFRASTRUCTURE_UNKNOWN | NULL，排除，单独报 |

Quoter 买卖价差只对照。禁止用它当主裁决。

---

## 4. 裁决词

- `ATOMIC_LABEL_NOT_TRUSTWORTHY` — 校准冒烟失败
- `NOT_EXECUTABLE_FOR_SPOT` — 可映射的 LONG_TICK 太少
- `UNDERPOWERED` — 成交成功 n < 30
- `TICK_NOT_ATOMIC` — tick 为正/有价差，atomic 期望 ≤ 0 或被失败成交吃掉
- `ATOMIC_EDGE_IN_WETH` — holdout 式时间后 30% 上，成交成功净期望 > 0 且填成率不是个位数（仍不部署）
- 任何正结果最高仍是 `SHADOW_CANDIDATE`

---

## 5. 明确禁止

- 禁止把 HIGH 且 WETH=token0 的池硬买 alt（那是在做反）
- 禁止用 120 bps 假设替换本轮 atomic
- 禁止 LIVE_GO
- 禁止改 F03 阈值
- 禁止把 Quoter −31% 的 USDC/AERO 冒烟当本轮主账（那一池本轮是 NOT_EXECUTABLE）
