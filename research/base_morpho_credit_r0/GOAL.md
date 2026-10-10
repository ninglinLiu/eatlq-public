# GOAL：Base 链 Morpho Blue 隔离信贷 — R0 仪器建设与真值校准

**发起人**：Claude（任务发起与验收审查）
**执行者**：Codex
**创建时间**：2026-08-15
**状态**：待执行
**工作区**：`/root/base_morpho_credit_r0/`
**前置**：无。本文件是本系列唯一可以先跑的任务。R1–R4 全部门禁依赖本轮 P0。

---

## 0. 给执行者的先决约束（不可协商）

这些不是建议，是硬约束。违反其中任何一条，本轮结果作废。

1. **禁止在 P0 通过之前做任何策略研究、假设检验、收益回测或“先看看有没有 alpha”。** P0 是仪器校准。上一轮 Uniswap v3 LP 回测产出 +3279% 假结果，根因就是跳过仪器；方向性五个月的价值主要来自证伪，不是来自找到金矿。
2. **禁止任何形式的真实资金操作。** 不下单、不签名、不广播、不 supply / withdraw / borrow / repay / liquidate / flashLoan、不授权、不碰私钥、不改 `/opt/EATLQ` 下任何 live 配置或 executor。本轮只允许：官方只读 API、`eth_call`、`eth_getLogs`、本地 ClickHouse 只读、本地 fork 模拟（若做，必须无 signer）。工作区根目录已有 `NO_LIVE.lock`；所有入口必须检查它，缺失或被删则直接退出。
3. **禁止做清算机器人、清算回测、清算抢跑模拟、MEV 拍卖、私有交易中继。** 官方文档写明 Morpho 清算是先到先得、可演变为 gas war。那是金矿，不是本系列的对象。本轮甚至不准把“清算利润”写成研究问题。
4. **禁止把名义供给 APY 当成策略收益。** 2026-08-15 核心市场供给大约 4.1%。那是底，不是 alpha。任何把 4% 年化包装成“我们发现了收益”的句子，我会直接打回。
5. **禁止研究未上架垃圾市场。** GraphQL 必须带 `where: { chainId_in: [8453], listed: true }`。未上架的 HERMES/USDC 一类市场在 2026-08-15 出现过利用率 100%、APY 400%–3000% 的数字。那是诱饵，不是样本。
6. **禁止用份额上限、容量截断、样本内选参、未扣摩擦的净值来美化任何数字。** 若某样本在 $1,000 名义本金下对单市场供给份额超过 1%，标记 `CAPACITY_VIOLATION` 并从主结论剔除，同时单独报告剔除比例。不许 `min(share, cap)`。
7. **诚实优先于结论。** 数据不够就写“数据不足以回答”。不许用更弱代理凑答案。不确定的外部数字必须带可复核来源：官方文档给 URL，链上给 `eth_call`/日志查询，API 给完整请求与返回哈希或落盘副本。
8. **禁止修改本 GOAL.md。** 异议写进 `OPEN_QUESTIONS.md`。禁止改 R1–R4 的 GOAL 文本。禁止改 `/root/base_wildcat_alpha/` 与 `/root/base_lp_research_r0/` 里已经交付的报告。
9. **所有链上查询必须锁定 `chain_id = 8453`。** Morpho Blue 在以太坊与 Base 上可以是同一 CREATE2 地址。混链会把主网市场当 Base。
10. **本轮不回答“能不能赚钱”。** 本轮只回答：“仪器是否校准、核心市场状态能否被独立复现、历史面板是否可复核。”

---

## 1. 已核实的事实基线

以下事实我在 2026-08-15 逐条核实过。**不要重新发现，也不要在没有新证据的情况下推翻。** 若执行中发现任何一条是错的，立刻停下并报告——那比完成任务更重要。

### 1.1 为什么做这个场，而不是再做一个 AMM

我们刚结束的判断（发起人，2026-08-15）：

- Uniswap v3 在 Base 上已经被我们挖过方向性、LP、wildcat。LP 的 R0 停在 P0.2：Base Collect 为 0，`burn - mint` 不是手续费真值。见 `/root/base_lp_research_r0/FINDINGS.md`。
- Wildcat 唯一影子候选是 F03，成熟远期结果为 0。见 `/root/base_wildcat_alpha/FINAL_WILDCAT_REPORT.md`。J.3 已经点名：需要一块非 AMM 面板（桥 + 借贷利率/清算）。
- Base 上钱不在 Uni V3。DefiLlama 当日：链 DeFi TVL 约 $4.61B；Morpho Base TVL 约 **$3.27B**；Uniswap 全版本约 $358M；Aerodrome 约 $250M。来源：`https://defillama.com/chain/Base`、`https://defillama.com/protocol/morpho`。
- Aerodrome 30 日成交约 $10.5B，是 DEX 主场，也是排放金矿。本系列**不去做 Aero**。
- Base 永续 30 日名义量约 $6B，但全链 OI 约 $14M。量/仓位比不对，不作为主场。
- 本系列只做 **Morpho Blue 隔离信贷**，核心市场是 **cbBTC/USDC 86% LLTV**。这不是金矿：核心供给约 4.1%。有意思的是结构。

### 1.2 官方合约与核心市场（已核实，请你再核一遍）

官方地址页：`https://docs.morpho.org/get-started/resources/addresses`  
官方接口：`https://github.com/morpho-org/morpho-blue/blob/v1.0.0/src/interfaces/IMorpho.sol`  
官方 API：`https://docs.morpho.org/developers/api/morpho/`  
清算：`https://docs.morpho.org/learn/concepts/liquidation/`  
IRM：`https://docs.morpho.org/learn/concepts/irm` 与 `https://docs.morpho.org/get-started/resources/contracts/irm`  
预言机：`https://docs.morpho.org/learn/concepts/oracle`

Base（chain 8453）官方部署：

| 合约 | 地址 | 来源 |
|---|---|---|
| Morpho Blue | `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb` | 官方地址页 Base 段，Basescan 链接 |
| AdaptiveCurveIRM | `0x46415998764C29aB2a25CbeA6254146D50D22687` | 同上。**不要用以太坊的 `0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC`** |
| ChainlinkOracleV2 Factory | `0x2DC205F24BCb6B311E5cdf0745B0741648Aebd3d` | 同上 |

核心市场上架配置，来自 2026-08-15 官方 GraphQL `marketById`（你必须重跑并比对）：

```
marketId     0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836
lltv         860000000000000000          # 86% WAD
irm          0x46415998764C29aB2a25CbeA6254146D50D22687
oracle       0x663BECd10daE6C4A3Dcd89F1d76c1174199639B9
loan         USDC  0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913  decimals=6
collateral   cbBTC 0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf  decimals=8
```

当日 `listed: true` 且按 `SupplyAssetsUsd` 降序的前三个真实市场（未上架 HERMES 已排除）：

| 市场 | 供给 USD | 借款 USD | 利用率 | 供给 / 借款 APY |
|---|---:|---:|---:|---:|
| cbBTC / USDC 86% | 1,479,968,224 | 1,284,933,079 | 0.8682 | 0.04121 / 0.04761 |
| USDe / USDC 91.5% | 353,110,020 | 318,158,008 | 0.9010 | 0.03449 / 0.03836 |
| WETH / USDC 86% | 84,322,924 | 73,448,857 | 0.8710 | 0.04133 / 0.04759 |

> `cbBTC/USDC` 单市场供给已经大于 Base 上 Uniswap + Aerodrome 的 TVL 之和。$1,000 名义本金对它的份额约 `6.8e-7`，容量不是本轮的敌人。容量敌人是你把未上架垃圾市场算进样本。

### 1.3 必须写进仪器的官方公式（已核实）

**LTV / Health Factor**（官方清算页）：

```
LTV = BORROWED_AMOUNT / COLLATERAL_VALUE_IN_LOAN_TOKEN
COLLATERAL_VALUE_IN_LOAN_TOKEN = COLLATERAL_AMOUNT * ORACLE_PRICE / ORACLE_PRICE_SCALE
ORACLE_PRICE_SCALE = 10^36
HEALTH_FACTOR = COLLATERAL_VALUE_IN_LOAN_TOKEN * LLTV / BORROWED_AMOUNT
可清算 ⇔ HEALTH_FACTOR < 1  ⇔  LTV > LLTV
```

**LIF**（官方清算页）：

```
LIF = min(1.15, 1 / (0.3 * LLTV + 0.7))
86% LLTV ⇒ LIF ≈ 1.05
整个 LIF 归清算人，协议不抽成。
```

本轮实现 LIF 只为校验文档，**禁止**用它算“清算利润策略”。

**APY**（官方 IRM 页）：

```
borrowAPY = exp(borrowRate * 31536000) - 1
supplyAPY = borrowAPY * utilization * (1 - fee)
utilization = totalBorrowAssets / totalSupplyAssets
```

官方示例：`borrowRate` 每秒 `1512768697`（1e18 缩放）⇒ borrow APY 4.89%，利用率 85%、fee=0 ⇒ supply APY 4.15%。你的单元测试必须复现这两个数。

**AdaptiveCurveIRM**（官方 IRM 技术页）：

- 目标利用率 `u_target = 0.9`
- 曲线陡度 `CURVE_STEEPNESS = 4`：利用率 100% 时瞬时借款利率约为目标处的 4 倍；利用率 0% 时约为 1/4
- 自适应：利用率持续高于目标，曲线上移；低于则下移。利用率 100% 维持 5 天，`r_90%` 大约翻倍（最大速度）
- `rateAtTarget` 有上下界：约 0.1%/年到 200%/年
- `borrowRate()` 会写存储；只读必须用 `borrowRateView()`

**Market 结构体的官方警告**（`IMorpho.sol`）：

- `totalSupplyAssets` / `totalBorrowAssets` **不含**自 `lastUpdate` 以来应计利息
- `totalSupplyShares` **不含**自上次应计以来 feeRecipient 的新增份额
- 因此：API 的“当前 APY / 当前总量”与 `eth_call Morpho.market(id)` 的原始字**本来就会差一截应计**。P0 必须把这个差拆开，不许把应计差当成 API 造假。

**Oracle**（官方预言机页）：

- `price()` 返回 1 单位抵押物以贷款物计价的价格，缩放需覆盖小数差
- 市场上线后预言机地址不可改
- 清算与健康度**只认这个预言机**，不认 Uniswap tick，不认 Coinbase 现货

### 1.4 事件签名（我未当作已核实 —— 你必须从源码核）

`IMorpho.sol` v1.0.0 正文**没有**事件定义。事件在 `EventsLib.sol`。请从

`https://github.com/morpho-org/morpho-blue/blob/v1.0.0/src/libraries/EventsLib.sol`

自己抄事件、自己算 topic0，写进 `out/event_topics.json`，并在报告里贴源码行号。我本地用“`Id` 当成 `bytes32`”算过一组 keccak，**那组数不是已核实事实**。`Id` 是用户定义值类型，ABI 编码等于 `bytes32`，但你必须用官方事件字面量再算一遍。算完与 Basescan 上任意一笔真实 `Supply`/`Borrow`/`Liquidate` 对一下 topic0，三笔都对才算过。

IRM 的 `BorrowRateUpdate(Id,uint256,uint256)` 在 `morpho-blue-irm` 仓库，同样自己核。

### 1.5 数据源边界（已核实）

| 源 | 能给什么 | 不能给什么 |
|---|---|---|
| Morpho GraphQL `https://api.morpho.org/graphql` | 上架市场枚举、USD、历史 `historicalState`（HOUR/DAY…） | 列表查询里**没有** `historicalState`；必须 `marketById` |
| Morpho REST `https://api.morpho.org/v0/blue/markets/8453:<id>` | 单市场 config / state / apy history（固定 lookback） | 没有列表端点 |
| `eth_call` Morpho.`market` / `idToMarketParams` / `position` | 链上真值 | 不含自 `lastUpdate` 的应计；没有 USD |
| `eth_call` oracle.`price()` | 清算用价格 | 不是现货，不是 TWAP，除非该预言机内部是 |
| `eth_getLogs` Morpho + IRM | 事件级供给/借款/清算/利率更新 | 贵、慢；本轮只允许对核心市场做有界回填 |
| 本地 ClickHouse `eatlq` | V3/V2 swap、部分 raw logs | **没有** Morpho 语义表。`MARKET_MAP.md` 已写 lending 缺席 |
| Wildcat F03 面板 | 现货池小时方向流 | 本轮只读，不改，不重训 |

官方自己写了：部分 historical 查询“需要 backfill，未 backfill 的在 sandbox 标 deprecated”。若 HOUR 级历史是空的，写 `DATA_INSUFFICIENT`，不要用 DAY 级冒充 HOUR。

---

## 2. 数据现实（本轮最重要的约束）

我没有在 ClickHouse 里建过 Morpho 表。你开工第一件事是证实这句话，而不是假设它。

允许的本地只读探查：

```bash
docker exec eatlq-clickhouse-1 clickhouse-client -q "SHOW TABLES FROM eatlq"
docker exec eatlq-clickhouse-1 clickhouse-client -q "SELECT count() FROM eatlq.raw_evm_logs WHERE chain_id=8453 AND lower(address)=lower('0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb')"
```

三条可能：

- A. raw logs 里已经有 Morpho 地址的足够事件 → 本轮以解码为主，API 做交叉验证。
- B. raw logs 很少或没有 → 本轮以官方 API 历史为主，核心市场补一段有界 `eth_getLogs`。
- C. 两者都不够 → **停在 P0，写 DATA_INSUFFICIENT**，不要用日频三个点画“微观结构”。

`eth_getLogs` 硬限制（沿用 LP R0b 的实测，你必须再测一次）：

- 现有只读 RPC 在 span≥20,000 时可能 413。先用 span=10,000 试，以你自己的实测为准。
- 限速 ≤10 RPS，指数退避。executor 与这个端点可能共用，**不要影响实盘**。开跑前若能检查 `docker exec eatlq-redis-1 redis-cli SMEMBERS positions:open_pools`，非空就改到非交易时段或换只读端点。
- 只回填 **一个** 市场：`cbBTC/USDC` 的 Morpho + 其 IRM。WETH/USDC、USDe/USDC 本轮只用 API。
- 断点续传，游标落盘。credits / 调用次数 / 墙钟必须记下来。

禁止：全链 Morpho 回填、全市场清算日志、vault 全量、奖励 APR、积分。

---

## 3. 研究阶段

### P0 — 仪器建设与校准（门禁。不通过不许进 R1–R4，也不许在本文件里做 P1）

**目标**：造一套经过链上真值校验的只读仪器。本轮没有策略结论。

#### P0.0 工作区与 fail-closed

- 所有代码放 `/root/base_morpho_credit_r0/src/`
- 每个可执行入口第一件事：检查 `NO_LIVE.lock`，否则 `sys.exit(2)`
- 禁止导入任何 executor、signer、web3 写方法。RPC 封装只允许 `eth_call`、`eth_getLogs`、`eth_blockNumber`、`eth_getBlockByNumber`
- 单元测试必须覆盖：缺 lock 时入口失败

#### P0.1 官方配置复现

重跑并落盘：

1. GraphQL `markets(first: 50, orderBy: SupplyAssetsUsd, orderDirection: Desc, where: { chainId_in: [8453], listed: true })`
2. `marketById` 核心三市场
3. REST `GET https://api.morpho.org/v0/blue/markets/8453:0x9103c3b4e834476c9a62ea009ba2c884ee42e94e6e314a26f04d312434191836`
4. REST `.../state`
5. `eth_call` `idToMarketParams(id)`、`market(id)`、oracle.`price()`、IRM.`borrowRateView`

交付 `out/p0_config_snapshot.json`。必须逐字段对比：loan、collateral、oracle、irm、lltv。任一不一致 → 停，写发现，不要“以 API 为准继续”。

对 `market()` 原始字：按官方警告做应计利息校正后再和 API 的 supply/borrow 比。校正公式必须来自 Morpho 源码 `_accrueInterest`，不要发明。中位相对误差门槛：**总量 < 1%，利用率 < 50 bps，APY 方向一致**。做不到就报告差从哪里来（应计、USD 计价、fee、时间戳错位），不要调参压误差。

#### P0.2 公式仪器

实现并单测，全部对照官方文档数字：

| 测试 | 期望 |
|---|---|
| 官方 IRM 示例 `ratePerSecond=1512768697` | borrow APY ≈ 4.89%，85% util、fee=0 ⇒ supply APY ≈ 4.15% |
| 官方清算示例：借 150k USDC、2 cbBTC、oracle 使 1 cbBTC=100000 USDC、LLTV 86% | LTV 75%，HF ≈ 1.1467 |
| LIF(0.86) | ≈ 1.05；LIF 上限 1.15 |
| 利用率 0 / 0.9 / 1.0 的曲线倍数 | 0.25× / 1× / 4× `r_90%`（在自适应项冻结时） |
| WAD、6 位 USDC、8 位 cbBTC、1e36 oracle | 显式单位测试，禁止静默 float |

禁止在生产路径用 float 累加份额。展示层才允许 float。

#### P0.3 历史面板（核心市场）

对 `cbBTC/USDC` 拉官方历史：

- REST `apy/history?lookback=ninety_days` 与 `one_year`（若有）
- GraphQL `historicalState`：`supplyAssetsUsd`、`borrowAssetsUsd`、`supplyApy`、`borrowApy`、`utilization`（若字段存在；不存在就写不存在）
- `interval: HOUR` 先试 14 天；空则退到 `DAY` 并在报告里把分辨率降级写死

同时抽 **同一时间戳** 的现货代理，只为以后 R2 做对齐，本轮不分析：

- 本地 ClickHouse 里 Base WETH/USDC 或 cbBTC 相关池的 tick / 成交，`WHERE chain_id=8453`
- 若没有 cbBTC 池，写“现货腿本轮不可用”，不要用 ETH 价冒充 BTC 价

输出 `out/core_market_hourly.parquet`（或 csv）+ `out/panel_coverage.md`：

必须出现的数：`n_rows`、`start`、`end`、`resolution`、`pct_missing`、`source`、`duplicate_keys`、`api_vs_chain_checked_at`。

#### P0.4 有界事件回填（仅当 P0.3 的 HOUR 面板缺口 > 30%，或你要给 P0.1 做事件级交叉）

只允许核心市场 + 其 IRM，窗口由你实测成本后预登记，**默认不超过最近 30 天**。超过必须先写进 `OPEN_QUESTIONS.md` 说明 credits 与条数估算，然后停，等审查。不要先拉再解释。

解码字段至少：`block_number`、`block_time`、`tx_hash`、`log_index`、`topic0`、`market_id`、事件名、资产/份额原始字。`Id` / 有符号整型按 ABI 解。落盘 `out/events_core/`，`chain_id` 写死 8453。

#### P0.5 上架宇宙普查（描述性，不是策略）

对 `listed: true` 的 Base 市场出一张表：市场 id、抵押/贷款符号与地址、lltv、oracle、irm、供给、借款、利用率、APY。标记：

- `CORE`：cbBTC/USDC 86%
- `SECONDARY`：USDe/USDC、WETH/USDC
- `SMALL`：供给 < $50M
- `IDLE`：借款 = 0 或抵押为 null（idle 市场）
- `EXCLUDE_UNLISTED`：根本不应出现

本轮禁止对 SMALL / IDLE 做任何收益计算。

#### P0 通过标准（必须同时满足）

1. `idToMarketParams` 与 API 五元组完全一致。
2. 应计校正后，核心市场总量相对误差 < 1%，利用率误差 < 50 bps。
3. §1.3 全部单元测试通过。
4. 核心市场历史面板至少有一种可用分辨率，缺口与来源写明。
5. 事件 topic0 与 Basescan 三笔真实交易一致（若做了 P0.4；没做则在报告写“未做事件回填，R3 将 UNDERPOWERED”）。
6. 报告里没有策略收益、没有清算 PnL、没有 4% APY 被写成 alpha。

**达不到就停。** 写清楚是 API 空、RPC 限流、应计公式错、还是混链。不要进本文件的任何“顺手看一眼利率”。

### P1 — 本文件没有 P1

利率、预言机基差、健康度、F03 正交分别是 R1–R4。本文件写 P1 我会当越权。

---

## 4. 交付物清单

全部放 `/root/base_morpho_credit_r0/`：

```
GOAL.md                         # 本文件，不要修改
NO_LIVE.lock                    # 已存在，不要删
README.md                       # 已存在，不要改执行顺序
p0_instrument_report.md         # P0 主报告：先写裁决 PASS/FAIL，再写证据
OPEN_QUESTIONS.md               # 你没能回答的，以及为什么
FINDINGS.md                     # 一页纸。本轮只允许仪器结论，不许策略结论
src/
  no_live.py                    # lock 检查
  morpho_rpc.py                 # 只读 eth_call / getLogs
  morpho_api.py                 # GraphQL + REST，带落盘
  formulas.py                   # LTV / HF / LIF / APY / 应计
  panel.py                      # 历史面板
  test_*.py
out/
  p0_config_snapshot.json
  event_topics.json
  core_market_hourly.parquet
  listed_universe.csv
  panel_coverage.md
  events_core/                  # 若做了 P0.4
```

---

## 5. 验收标准（我会怎么审查）

按这个顺序，一条不过就打回：

1. **有没有策略句子？** 有就打回。本轮不是“Morpho 能不能赚钱”。
2. **`NO_LIVE.lock` 检查是否在每个入口？** 我会抽一个入口删 lock 看它死不死。
3. **五元组链上 vs API 一致吗？** 不一致还继续往下写，作废。
4. **应计有没有按源码拆开？** 拿 `market()` 生字直接减 API，是仪器错误。
5. **官方示例数复现了吗？** 4.89% / 4.15% / HF 1.1467 / LIF≈1.05。
6. **未上架市场进样本了吗？** 进了就打回。
7. **HOUR 空的时候有没有用 DAY 冒充微观结构？** 有就打回。
8. **有没有清算 PnL、排放 APR、积分、Aero、永续？** 有就打回。
9. **外部数字有来源吗？** 没有来源的 TVL/APY 我当没写。
10. **R1 能不能只靠你的面板开工？** 这是 P0 的唯一产品。面板不能用，R1 我会拒收。

---

## 6. 明确禁止的事

- 禁止修改本 GOAL.md 与 R1–R4 GOAL。
- 禁止改 `/opt/EATLQ`、禁止碰 live 配置与私钥。
- 禁止链上写、禁止本地构造 `liquidate`/`supply` calldata 并发送。
- 禁止把 DefiLlama 营销口径（例如 Morpho 博客 “$5B deposits”）与 TVL 混用。博客：`https://morpho.org/blog/5b-on-base-is-the-new-day-one-for-onchain-finance`。本系列用 DefiLlama TVL + 官方 API。
- 禁止用“业界普遍认为”“大家都知道”替代证据。
- 禁止把 `/root/v3lp_backtest/` 的 0.30% 档结果、任何旧方向性成绩、F03 的未成熟影子当作本轮证据。
- 禁止给 R1–R4 预写结论。

---

## 7. 起步建议

1. 先读官方三页：清算、IRM、API historical。再读 `IMorpho.sol` 里 Market/Position 的 Warning 注释。
2. 用核心 `marketId` 打通：REST config → REST state → `eth_call market` → 应计校正 → 误差表。一个市场通了再拉宇宙。
3. GraphQL 字段名以 schema 为准。我 2026-08-15 用的是 `marketId` 不是 `uniqueKey`；`uniqueKey` 会直接报错。
4. 历史查询必须走 `marketById`，不要走 `markets { items { historicalState } }`。
5. 现货腿能对齐最好，对齐不了就明确写缺，把 R2 标成可能 UNDERPOWERED。不要编一条 BTC 价。

---

## 8. 一句话说明本轮为什么值得做

我们在 AMM 上证明了很多东西不成立，但那是因为我们一直站在同一类市场上。Morpho Blue 的核心市场比 Base 上整个 Uniswap 更深，收益结构却是利率、利用率、预言机健康度，不是 tick 与 LVR。本轮最可能的结果是：API 与链能对上，HOUR 面板有缺口，仪器能用但很丑。那也是好结果——它决定 R1–R4 该不该开，而不是再让我们在未校准的表上寻找金矿。
