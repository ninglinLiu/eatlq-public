# GOAL R2：Morpho Blue 核心市场 — 预言机价对现货的基差与可交易性

**发起人**：Claude（任务发起与验收审查）
**执行者**：Codex
**创建时间**：2026-08-15
**状态**：待执行（**门禁：R0 P0 必须 PASS**）
**工作区**：`/root/base_morpho_credit_r0/`
**前置**：`p0_instrument_report.md` 裁决 PASS。R1 不是前置；R1 若已出面板可复用时间索引，但本轮问题独立。

---

## 0. 给执行者的先决约束（不可协商）

1. **禁止在仪器未通过时开工。** 复用 R0 的只读 RPC、公式、lock 检查。
2. **禁止任何真实资金操作。** 禁止套利交易、禁止清算、禁止在 DEX 上下单“验证基差”。只允许 `eth_call` 报价（若做，必须是 `eth_call` 模拟，不许 `eth_send*`）。
3. **禁止把预言机滞后写成金矿。** 清算人已经在做这件事。本轮问的是：基差是否存在、是否大到超过成本、是否在 $1,000 上可成交、是否只是喂价心跳。默认答案是“不可成交”。
4. **禁止用 tick 当可成交报价。** Wildcat 已经写过：tick close 不是 executable quote。本轮若只用 tick，基差结果最多算 PROXY，主裁决不得优于 `INCONCLUSIVE`。
5. **禁止样本内选参。** 基差阈值、平滑窗口、延迟块数在训练窗定，holdout 报。
6. **禁止研究未上架市场、禁止用 ETH 价冒充 cbBTC 价。** 现货腿必须是 cbBTC/USDC 或 BTC/USDC 在 Base 上的真实池。没有这样的池 → 本轮 `DATA_INSUFFICIENT`，不要用 WETH/USDC 当“相关资产”凑一篇。
7. **禁止修改 GOAL。** 诚实优先。所有数字带来源。
8. **最高状态 SHADOW_CANDIDATE。** 本轮即使基差漂亮，也不构成实盘授权。

---

## 1. 已核实的事实基线

### 1.1 机制（官方，已核实）

预言机页：`https://docs.morpho.org/learn/concepts/oracle`  
清算页：`https://docs.morpho.org/learn/concepts/liquidation/`  
接口：`IOracle.price() → uint256`，1 单位抵押物的贷款物价，缩放覆盖小数差。Morpho 文档与清算公式使用 `ORACLE_PRICE_SCALE = 10^36`。

核心市场（2026-08-15 `marketById`）：

```
oracle = 0x663BECd10daE6C4A3Dcd89F1d76c1174199639B9
cbBTC  = 0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf   # 8 decimals
USDC   = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913   # 6 decimals
LLTV   = 86%
```

官方原话要点（你必须自己再读一遍，不要只信我的转述）：

- 清算发生在**当时的预言机价**，不是“真实市场价”
- 先到先得，无拍卖
- 86% LLTV 的 LIF ≈ 1.05
- 预言机可 revert，从而在某些市况下卡住 borrow / withdrawCollateral / liquidate
- 市场上线后预言机不可更换

因此本轮的经济对象不是“BTC 方向”，而是：

```
basis = oracle_cbBTC_in_USDC - spot_cbBTC_in_USDC
```

以及这个差是否大到：改变健康度、改变清算触发、或在现货上可被 $1,000 触及。

### 1.2 本轮唯一科学问题

> 核心市场预言机价相对 Base 上可观察现货，是否存在持续、可预登记、样本外仍在的基差过程？若存在，它是喂价更新频率造成的无经济意义噪声，还是大到足以改变健康度/清算状态？在 $1,000、延迟 1–2 块、双边报价之后，有没有正的可成交期望？

零假设：**基差的中位数接近 0，尾部被更新间隔解释，扣成本后不可成交。**

### 1.3 明确不是本轮的问题

- 利率拐点（R1）
- 借款人补仓行为（R3）
- 当清算人（金矿，禁止）
- 跨链 CEX 基差（没有 CEX 数据就不要编）
- Uniswap V3 LP 手续费

---

## 2. 数据现实

### 2.1 预言机腿（必须）

每个分析时点取：

```
eth_call oracle.price()
```

换算成“1 cbBTC 值多少 USDC”时，**单位必须写进测试**：

```
# 先读预言机合约与官方缩放，不要盲目套 1e36
# 若确认是 Morpho 标准：
usdc_per_cbbtc = price * 10^cbBTC_decimals / (10^36 * 10^USDC_decimals) 的变形
```

你必须用一笔已知块的 `price()` 与当时公开 BTC/USD 粗值交叉，数量级对不上就停。2026-08-15 附近 BTC 不是 $100 也不是 $10,000,000。数量级错是仪器死亡，不是发现。

对历史：优先 `eth_call` 带 `blockNumber` 回溯。RPC 若不支持 archive，写明，改用：

- 预言机合约自身的更新事件（你必须从该预言机源码找出 Feed 地址与事件）
- 官方 API 是否暴露 oracle 历史（没有就写没有）

不要用 API 的 token USD 价冒充 `oracle.price()`。那是另一条喂价。

### 2.2 现货腿（必须是 cbBTC，否则停）

在本地 ClickHouse 与 `discovered_pools` 里找 Base (`chain_id=8453`) 上：

- token = `0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf`
- 对端优先 USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- 场内优先 Uniswap v3 / 已有 swap 表里成交最多的池

若找不到 cbBTC/USDC 池：

1. 找 cbBTC/WETH，再经 WETH/USDC 合成。合成必须显式，延迟与误差单独列。主裁决降为 PROXY。
2. 若 cbBTC 任何池都没有 → **DATA_INSUFFICIENT，本轮结束**。不要用 WBTC、BTCB、cbETH。

可成交性：Wildcat J.1 要求过买卖双边报价。本轮至少对 holdout 里 |basis| 最大的 30 个时点做只读 `eth_call` 报价（若路由可知），记录：买 $250/$1000、卖 $250/$1000、失败码、gas。做不到就承认现货腿仍是 tick 代理。

### 2.3 对齐

预言机更新是离散的，swap 是离散的。对齐规则预登记：

- 默认：每个预言机更新块 T，取 T 及 T+1、T+2 的现货（若有）
- 另做：每个小时末，取该小时最后一次预言机价 vs 最后一次可观察现货
- 禁止：把一天一条的预言机和一小时一条的 tick 线性插值后当高频基差

---

## 3. 研究阶段

### P0 复核

抄 R0 裁决、oracle 地址、现货池地址（你新发现的）进报告开头。现货池必须给出：地址、fee、swap 行数、时间覆盖、`chain_id=8453` 过滤证明。

### 预登记 `out/r2_preregister.json`

```
train_end / holdout_start     = 70/30 时间切分
basis_unit                    = USDC_per_cbBTC 与 bps 两列都留
noise_threshold_bps           = 5          # 小于此当心跳噪声
econ_threshold_bps            = 30         # 低于此不谈可成交
delay_blocks                  = [0, 1, 2]
notional                      = [250, 1000]
cost_proxy_bps                = [10, 30, 80]
oracle_stale_seconds          = 你从该预言机心跳实测后再填，不许先填 3600 再看数据
```

**H0：** holdout 上 |basis| 的中位数 < 5 bps，且 95 分位 < 30 bps。  
**H1（机制，非交易）：** 基差在预言机更新瞬间收敛，更新间隔内随现货漂移。这只说明心跳，不说明可交易。  
**H2（经济）：** 在 delay=1 或 2 块、30 bps 成本、双边报价之后，按基差方向做 $1,000 纸面现货对敲的 holdout 净值 ≤ 0。

H2 的默认预期是成立（即不可交易）。你要证伪的是“有金矿”，不是证明“有金矿”。

### P1 — 预言机画像（必须）

对 `0x663BECd10daE6C4A3Dcd89F1d76c1174199639B9`：

- 读代码或 bytecode 元数据：它是 Chainlink 路由、Redstone、固定、还是包装器？
- 底层 feed 地址、heartbeat、deviation threshold（有文档就引用，没有就从更新事件实证）
- 更新间隔分布：p50/p90/p99 秒
- `price()` 是否曾 revert（eth_call 抽样）
- 与官方 `ORACLE_PRICE_SCALE` 是否一致

交付 `out/r2_oracle_profile.md`。这是本轮最有价值的非交易产出。

### P2 — 基差描述

训练+holdout 都画，但结论只看 holdout：

- basis 的分布、自相关、与 |现货收益| 的关系
- 按“距上次预言机更新的时间”分层的 |basis|
- 若 H1 在描述层成立（间隔越长 |basis| 越大，更新后变小），写清楚这是心跳，不是 alpha

### P3 — 对健康度的含义（纸面）

用 R0 公式，在每个时点算：

```
HF_oracle = f(oracle_price)
HF_spot   = f(spot_price)     # 仅当现货单位已校准
```

问：有多少仓位会在“只用现货”时已跌破 LLTV，但预言机仍显示健康——或相反？

**本轮没有仓位表就不要编。** 若 R0/R3 还没有仓位面板，只用一个**假想标准仓**：LLTV 86%、当前 LTV 80% / 84% / 85.5% 三档，看基差要多大才能把 HF 推过 1。这是敏感度，不是实盘。

假想仓的意义：若 30 bps 基差才能把 85.5% LTV 推过 86%，则基差对清算状态几乎无影响；若 5 bps 就够，才值得 R3 跟。

### P4 — 可成交性（只读）

对 holdout 中 |basis| 最大的 30 个时点（预登记：按训练窗分位数选，不按 holdout 偷看改规则）：

- 记录当时 basis、延迟 0/1/2 块后的现货
- 若能 `eth_call` 报价：买/卖 $250/$1000 的失败与冲击
- 纸面 PnL = 基差收斂 − 成本档 − 冲击
- 失败率单独报

主裁决看 H0 与 H2。H1 只当机制注释。

### P5 — 失败模式

1. 现货池是薄 meme 池，tick 乱跳，基差是现货垃圾不是预言机
2. 合成路径（cbBTC→WETH→USDC）把两段噪声加成“基差”
3. 预言机是 USD feed 再除 USDC feed，USDC 脱锚会被算进 BTC 基差
4. archive 缺失导致你用“当前价 + 旧现货”错位对齐
5. 把清算奖金 5% 加进现货基差策略（禁止）
6. 样本只有一次 BTC 急跌

---

## 4. 交付物

```
r2_oracle_spot_report.md          # 第一句裁决
out/r2_preregister.json
out/r2_oracle_profile.md
out/r2_basis_panel.parquet
out/r2_holdout_tests.csv
out/r2_quote_sample.csv           # 若做了 eth_call 报价
src/r2_*.py
src/test_r2_units.py              # 价格单位、1e36、8/6 位
```

裁决词：

- `DATA_INSUFFICIENT` — 没有 cbBTC 现货腿，或不能 eth_call 历史价且无更新事件
- `REFUTED` — holdout 基差在噪声带内，或 H2 显示不可成交
- `HEARTBEAT_ONLY` — H1 成立但 H2 失败。好结果
- `UNDERPOWERED`
- `SHADOW_CANDIDATE` — 仅当 holdout 上 delay≥1 块、30 bps、双边报价后净值仍显著为正，且现货腿不是 PROXY。默认不信

---

## 5. 验收标准

1. 第一句是裁决。
2. 现货腿资产地址是 cbBTC，或你停在 DATA_INSUFFICIENT。
3. 价格单位测试在，数量级与公开 BTC 价同阶。
4. tick 若当现货，主裁决不得优于 INCONCLUSIVE/HEARTBEAT_ONLY。
5. 没有清算机器人、没有用 WETH 冒充 BTC。
6. 假想仓敏感度与真实仓位没有被混写。
7. 预登记在先。
8. 没碰 live，没改 GOAL。

---

## 6. 明确禁止的事

- 禁止发送任何 swap。
- 禁止设计“预言机更新前抢跑清算”的流程、伪代码、gas 策略。
- 禁止把 CEX BTC 当 Base 现货，除非你同时给出 CEX 数据源与延迟模型；本轮默认没有 CEX。
- 禁止为了让基差变大而换更薄的池。
- 禁止修改预言机地址“试试另一个市场”。主市场只有一个。

---

## 7. 起步建议

1. 先 `eth_call price()` 当下值，换算成 USDC/cbBTC，和任意公开 BTC 价对数量级。这一步错，后面全废。
2. 再在 ClickHouse 搜 cbBTC 地址。搜不到就写 DATA_INSUFFICIENT 收工，不要展开。
3. 读预言机合约，找出 feed 与 heartbeat，比拉 90 天 tick 更重要。

---

## 8. 一句话

清算认预言机，交易认现货。两者之间的缝看起来像金矿，通常只是 Chainlink 心跳。本轮的工作是把缝量出来，并诚实地说 $1,000 够不够付滑点。大多数时候不够。
