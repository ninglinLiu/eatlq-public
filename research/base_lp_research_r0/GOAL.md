# GOAL：Base 链 LP（流动性提供）赛道可行性研究 — R0

**发起人**：Claude（任务发起与验收审查）
**执行者**：Codex
**创建时间**：2026-08-07
**状态**：待执行

---

## 0. 给执行者的先决约束（不可协商）

这些不是建议，是硬约束。违反其中任何一条，本轮结果作废。

1. **禁止在 P0 通过之前做任何策略研究。** P0 是仪器校准，不是热身。上一轮 LP 回测之所以产出了一个 +3279% 的假结果，正是因为跳过了仪器校准。
2. **禁止使用任何形式的"份额上限"来掩盖容量违规。** 见 §1.2。如果模拟出的池内份额高得不现实，必须**报错并标记该样本**，不许静默截断成一个"看起来合理"的数。
3. **禁止用样本内表现选参数然后报告同一样本的收益。** 任何参数选择必须在训练窗完成，报告必须在从未触碰的窗口上做。
4. **禁止把"未扣摩擦的收益"作为主结论。** gas、swap 手续费、滑点、rebalance 成本必须逐项计入并单独列示。
5. **诚实优先于结论。** 如果数据不足以回答某个问题，写"数据不足以回答"，不要用更弱的代理指标凑一个答案。本项目过去五个月的价值主要来自证伪，不是来自找到 alpha。
6. **不下任何真实交易，不动任何私钥，不碰 `/opt/EATLQ` 下的 live 配置。** 本轮是纯离线研究。
7. **所有外部数字必须带可复核来源。** 引用论文给 arXiv/DOI，引用文档给 URL，引用链上数据给查询语句。

---

## 1. 已核实的事实基线

以下事实我已经逐条核实过。**不要重新发现，也不要在没有新证据的情况下推翻。** 但如果你在执行中发现其中任何一条是错的，立刻停下并报告——那比完成任务更重要。

### 1.1 学术基线（已核实）

**LVR（Loss-Versus-Rebalancing）** — Milionis, Moallemi, Roughgarden, Zhang，arXiv:2208.06046，初版 2022-07-31，现行版 2024-05-27
- LVR 是 LP 的**主要逆向选择成本**：池子价格陈旧，被信息更充分的套利者摘取。
- LVR 是**非负、单调不减、可预测**的运行成本。瞬时值 = 价格方差 × 池内边际流动性的缩放积。
- **套利者的累计利润恰好等于 LVR。**
- LVR ≠ 无常损失。IL 本质是"loss-versus-holding"，不是真正的运行成本；LVR 才是。
- **结论：LP 要盈利，手续费收入必须超过 LVR。** 这是本轮研究的中心不等式。

**Impermanent Loss in Uniswap v3** — Loesch et al.，arXiv:2111.09192
- 17 个池，覆盖 43% TVL，2021-05-05 → 2021-09-20
- 手续费收入 $199.3M，IL $260.1M → **IL 相当于手续费的 130%**
- LP 整体若直接持币会多赚 $60.8M
- **49.5% 的 LP 收益为负**
- USDC/WETH 0.05% 池：1,057 个钱包（62%）亏钱，平均 −$18,245；648 个（38%）赚钱，平均 +$2,251

**Automated Market Makers: Toward More Profitable Liquidity Provisioning Strategies** — ACM，DOI:10.1145/3672608.3707833
- 700 天 Uniswap v3 历史数据
- 平均每仓位 IL −3.8%；**超过 50% 的 LP 总收益落在 −1% 到 +1% 之间**；49.5% 仓位为负
- 论文给出的缓解方向：**相关性高的代币对、更大的区间宽度、更长的持仓时长**

**Risks and Returns of Uniswap V3 Liquidity Providers** — ACM，DOI:10.1145/3558535.3559772
- **集中流动性会让 IL 增长得更快**
- 但作者认为在恰当条件下 v3 LP 可以盈利（对 Loesch 的方法学提出了批评）

> **三个独立来源 + 我们自己的回测数据一致指向：区间越窄，结果越差。**
> 我们自己的 WETH/USDC 0.05% 结果：±2% 带宽 −43.36%，±15% 带宽 −8.05%，单调关系清晰。
> 这**直接反驳**了社交媒体上流行的"窄区间单边 LP"打法。执行者必须把这条当作零假设去检验，而不是当作要去证明的目标。

### 1.2 我们自己上一轮 LP 回测的状态（已核实，含一个必须修的 bug）

代码在 `/root/v3lp_backtest/`，数据在其 `data/` 子目录。

**可用的部分**：`WETH/USDC 0.05%` 全部配置，总收益 −7.95% 到 −60.98%，对照 HODL −5.86%。
经我复核，该池的未封顶份额约 **0.96%**，`MAX_POOL_SHARE = 0.05` 未触发，**结论成立**。

**污染的部分**：`WETH/USDC 0.30%` 全部配置，报出 +175% 到 +3695% 总收益、最高 10356% APR。
经我复核，该池的未封顶份额约 **28.15%**，`MAX_POOL_SHARE = 0.05` **持续 binding**。
也就是说：模型假设一笔 $500 本金能拿走该池 5% 的全部手续费，连续 134 天。
**这个 +3279% 完全由那行 `min(my_share, 0.05)` 生成，不是数据里的东西。**

相关代码位置：`backtest_v3_lp.py` 第 101–104 行。

> 这是本项目已经犯过第二次的同一类错误。第一次是 `EXECUTION_ALPHA_PROBE` 的 $550k 幻觉——同样是容量约束被违反后产出的假阳性。
> **执行者必须把"容量违规检测"做成一等公民，而不是一个护栏参数。**

### 1.3 Uniswap v3 range order 机制（已核实，来自官方文档）

来源：Uniswap 官方支持文档 `https://support.uniswap.org/hc/en-us/articles/20980601560717`、
开发者文档 `https://developers.uniswap.org/docs/sdks/v3/guides/managing-liquidity/range-orders`、
v3 白皮书 `https://app.uniswap.org/whitepaper-v3.pdf` §2.1

- range order = 在当前价格**之外**放置单边流动性，近似限价单。
- **关键：区间外的单边仓位不赚任何手续费，直到价格进入区间。**（官方原文：*"Range orders are out of range, single-sided, and do not earn fees until the current price of the pool moves into the set range."*）
- 价格穿越区间后仓位被完全换成另一种代币，**此时必须撤出**；否则价格穿回来会把这笔交易反向做掉（v3 白皮书 §2.1 明确写了这一点）。
- 区间宽度有下限（受 tickSpacing 约束）；价格在区间内时订单可能只被部分执行。

> 这条机制决定了单边 range order 的收益结构和双边 LP **完全不同**：
> 它不是"赚手续费"，而是"用一个限价单换取穿越期间的手续费 + 承担被反向做掉的风险"。
> 建模时必须分开处理，不能套用双边 LP 的公式。

### 1.4 Base 链场地现实（已核实，但有一处冲突待你复核）

- **Aerodrome 是 Base 的主场，不是 Uniswap。** Slipstream（其 v3 式集中流动性 AMM）上线后，Aerodrome 占 Base DEX 成交量 **>60%**，是第二名的三倍以上。
- Aerodrome LP 的收益结构与 Uniswap **本质不同**：LP 可以选择**质押进 gauge 拿 AERO 排放**，或者**不质押拿交易手续费**——两者二选一，不能兼得。质押仓位的手续费归 veAERO 持有者。
  来源：`https://aerodrome.finance/docs`
- AERO 排放率约 **10.9% 年化**（2026-04 官方数据）。
- 2026-07：Aerodrome 与 Velodrome 合并为 Aero，MetaDEX03 架构，Slipstream V3 内嵌 MEV 拍卖（MEV internalization）。

> **待你复核的冲突**：TVL 数字两个来源不一致——web3wagmi 指南称 2026-05 约 $12 亿，Blockworks 数据面板称约 $2.4 亿（并称较 2024-12 峰值 $8.8 亿大幅下降）。
> 这两个数差 5 倍。**请你独立核实当前 Base 上 Aerodrome 与 Uniswap 的实际 TVL 与成交量份额**，给出来源与口径。这个数字直接决定 §3 里场地选择的优先级。

---

## 2. 数据现实（我已实测，这是本轮最重要的约束）

全部位于 ClickHouse `eatlq` 库（容器 `eatlq-clickhouse-1`）。

| 资产 | 实测状态 | 对本研究的意义 |
|---|---|---|
| `uniswap_v3_swaps` | **12.77M 行，9,125 个池，2025-10-01 → 2026-08-07（仍在流入）**。字段含 `sqrt_price_x96_raw`、`liquidity_raw`（成交瞬间的活跃 L）、`tick_raw`、`amount0/1_raw` | **可用，且足够。** 每笔 swap 自带活跃流动性 L，因此份额可以逐笔精确计算，不需要 mint/burn |
| `uniswap_v3_mints` | **仅 23 个池**，2026-02-19 → 2026-05-20（已停止） | 不足以构成研究 universe，**但其中 7 个池足以做校准真值**，见下 |
| `uniswap_v3_burns` | **仅 30 个池**，同上 | 同上 |
| Collect 事件（`raw_evm_logs`） | **仅 2026-03-14 一天，23 个池** | 基本不可用，手续费需从 burn 的 amount 差额推算 |
| 近 30 天活跃池 | **3,016 个池，1.80M 笔 swap** | 这是可用的工作 universe |
| `discovered_pools` | v2 57,358 / v3 20,197 / aero_cl 6 / aero_vamm 4，含 `fee_bps`、`token0/1_symbol`、`created_block` | 池子元数据可用 |
| **链纯度** | `chain_id` 8453（Base）占 99.998%；混入了 21 条 mainnet mint 与 24,441 条 mainnet swap | **所有查询必须加 `WHERE chain_id = 8453`**，否则会混入以太坊主网池（例如 `0x88e6a0c2...` 是主网 USDC/WETH） |

### 2.1 校准数据集（这是本轮最有价值的意外发现）

mint + burn + swap **三者齐全**的池共 16 个，其中 8 个只有 1–2 条 mint（均在 2026-03-14），是噪声。**真正可用的是这 7 个**：

| 池 | 类型 | 代币对 | fee | mints | burns | swaps |
|---|---|---|---|---|---|---|
| `0x82321f3beb69f503380d6b233857d5c43562e2d0` | **aero_cl** | WETH/AERO | 5bps | 815,095 | 2,161,199 | 380,041 |
| `0xdfe5f275020def30993f042174fc2d335678b626` | **aero_cl** | AERO/cbBTC | 27bps | 148,544 | 446,945 | 295,206 |
| `0xd0b53d9277642d899df5c87a3966a349a798f224` | v3 | WETH/USDC | 5bps | 135,281 | 204,888 | 3,613,181 |
| `0xbe00ff35af70e8415d0eb605a286d8a45466a4c1` | **aero_cl** | USDC/AERO | 100bps | 28,575 | 85,787 | 62,131 |
| `0xb4cb800910b228ed3d0834cf79d697127bbb00e5` | v3 | WETH/USDC | 1bps | 4,137 | 4,753 | 5,368,433 |
| `0x4c36388be6f416a29c8d8eee81c771ce6be14b18` | v3 | WETH/USDbC | 5bps | 1,299 | 5,332 | 449,154 |
| `0x10648ba41b8565907cfa1496765fa4d95390aa0d` | v3 | — | — | 372 | 581 | 4,834 |

**三个 aero_cl 池是 Aerodrome Slipstream 池，而且 swap 与 mint/burn 齐全。**
这推翻了"我们对 Aerodrome 完全是瞎的"这个说法——我们对 Aerodrome 的**广度**是瞎的（只有 6 个 CL 池被发现），
但对这 3 个池的**深度**极好：完整的仓位生命周期 + 成交流，覆盖 2026-02-19 → 2026-05-20 整整三个月。
考虑到 Aerodrome 占 Base 成交量 >60%，**这 3 个池是本轮最有价值的数据资产**。

### 2.2 一个必须知道的建模陷阱

Mint/Burn 事件里的 `owner` 字段**不是真实 LP**。
实测：`0x82321f3b...` 池 81.5 万次 mint 全部归属单一 owner `0x827922686190790b37229fd06084350e74485b72`，
经链上查询确认该地址是合约，`name()` 返回 **`Slipstream Position NFT v1`**、`symbol()` 返回 `AERO-CL-POS`
——**它是 Aerodrome 的 NonfungiblePositionManager，不是某个巨鲸。** Uniswap v3 同理。

**因此**：
- **钱包级 LP 归因不可能**（需要 NFT tokenId → owner 映射，即 `IncreaseLiquidity`/`DecreaseLiquidity` 事件或 ERC-721 Transfer，我们两者都没有）。
- **仓位级重建仍然可行**：mint/burn 携带 `tick_lower_raw` / `tick_upper_raw` / `liquidity_raw`，
  可以在 **(pool, tickLower, tickUpper)** 粒度上重建流动性的进出。
- **P0.2 的校准必须在 (池, tick 区间) 粒度做，不要试图做钱包级。** 谁试图做钱包级归因，谁就会重蹈 sprint083 的覆辙。

**由此推出的三条硬边界，请在报告里原样复述**：

1. **能做**：从 swap 数据模拟任意假想 LP 仓位的完整损益（手续费份额、IL、LVR、区间穿越）。因为 `liquidity_raw` 给了每笔成交时的活跃 L。
2. **能做（但仅限 7 个池）**：用真实 mint/burn 重建仓位级流动性变化，用于校准模拟器。
3. **不能做**：Loesch 那种钱包级 LP 盈亏分布研究。也**不能做** Aerodrome 的广度研究（只有 6 个 CL 池被发现）。补数据摄取的提案见 H4，本轮**不实施**。

---

## 3. 研究阶段

### P0 — 仪器建设与校准（门禁阶段，不通过不许进 P1）

**目标**：造一个**经过真值校验**的 LP 仓位模拟器。上一轮失败的根因就是这一步被跳过了。

**P0.1 重写份额模型**
- 逐笔计算 `my_share = my_L / (my_L + active_L)`，单位换算必须显式验证（v3 的 `liquidity` 是原始整数 L，人类单位 L 与之相差 `10^((dec0+dec1)/2)`，`backtest_v3_lp.py` 第 30 行已有正确写法，请复用并写单元测试）。
- **删除 `MAX_POOL_SHARE` 截断。** 改为：计算真实份额，若任一时刻 `my_share > 0.01`（1%），把该样本标记为 `CAPACITY_VIOLATION` 并从主结论中剔除，同时单独报告有多少样本因此被剔除。
- 输出必须包含：`implied_share_p50`、`implied_share_p99`、`capacity_violation_pct`。**这三个数不出现的结果一律不接受。**

**P0.2 用真值校准（关键，不可省略）**
- 用 §2.1 表里**成交量最大的 3 个池**：`0xd0b53d92...`（v3 WETH/USDC 5bps，361万笔 swap）、
  `0x82321f3b...`（aero_cl WETH/AERO 5bps）、`0xb4cb8009...`（v3 WETH/USDC 1bps，537万笔 swap）。
  先用 `0xd0b53d92...` 打通，再验证另外两个。
- 从 mint/burn 事件在 **(pool, tickLower, tickUpper)** 粒度重建流动性的净进出时间序列。
  **不要试图做钱包级归因**——`owner` 是 position manager 合约，见 §2.2。
- 用你的模拟器对同一 tick 区间、同一时间窗，模拟该区间应得的手续费与仓位价值变化。
- **对比模拟值与链上真实值**：真值来自 burn 事件的 `amount0/1_raw` 减去该区间累计 mint 的 `amount0/1_raw`
  之后的差额（这个差额即为该区间赚到的手续费 + 价格变动导致的组成变化，需要用同期 tick 路径拆开两者）。
  Collect 只有一天，不能作为主要真值来源，但 2026-03-14 那天可以拿来做一次独立交叉验证。
- **通过标准**：模拟手续费与真实手续费的中位相对误差 **< 15%**，且无系统性偏向（符号检验 p > 0.05）。
- **若 aero_cl 池的误差显著大于 v3 池**，说明 Slipstream 的手续费机制与 Uniswap v3 有差异
  （例如 gauge 质押仓位的手续费归 veAERO 而非 LP，见 §1.4），
  **这本身就是一个重要发现，必须单独报告，不要靠调参把误差压下去。**
- **达不到就停下来报告，不要进 P1。** 报告里写清楚误差来自哪里。

**P0.3 实现 LVR 度量**
- 按 arXiv:2208.06046 的闭式解实现 LVR。
- 对每个池子，逐日输出：`fee_income`、`LVR`、`IL`、`fee_minus_LVR`。
- **自检**：LVR 必须非负且单调不减。若出现负值或下降，是实现错了，修完再往下走。

**P0 交付物**：`p0_calibration_report.md` + 模拟器代码 + 单元测试。

---

### P1 — Base 链 LP 基准率测定

**目标**：回答"在 Base 上做 LP，基础概率是什么"。**这是本轮最有价值的产出，即使 P2 全部证伪。**

在近 30 天活跃的 3,016 个池上（或按流动性/成交量筛出的可行子集，筛选规则必须预先写死并说明）：

**P1.1** 对每个池，用 P0 的模拟器测算一个标准化被动 LP 仓位（固定名义本金、固定区间宽度档位、不做主动管理）的：
- 手续费收入 vs LVR vs IL
- 对照 50/50 HODL 的超额收益
- 扣除 gas 与 rebalance 成本后的净值

**P1.2** 给出分布，不是均值。至少报告：
- 净收益为负的池子占比（**与 Loesch 的 49.5% 对照**）
- IL / 手续费 比值的分布（**与 Loesch 的 130% 对照**）
- 按池子类型分层：稳定对 / 主流对 / memecoin 对；按 fee tier 分层

**P1.3** 检验文献的三个缓解方向在 Base 2026 数据上是否成立：
- 相关性高的代币对更好？
- 区间越大越好？（我们自己的数据说是，请在更大 universe 上确认）
- 持仓时长越长越好？

**P1 交付物**：`p1_base_rate_report.md`，含分布图与分层表。

---

### P2 — 可证伪假设检验

**只有 P1 完成后才做。** 每条假设必须写成"如果 X 则 Y，否则证伪"的形式，并**预先**登记判定阈值。

按优先级：

**H1（最高优先）：窄区间单边 range order 在高波动池上优于宽区间双边 LP**
- 这是社交媒体上最流行的打法（书签 [445] [534] [510]），也是我们唯一没测过的口径——上一轮只测了 WETH/USDC 稳定主流对。
- 零假设取自文献与我们自己的数据：**窄区间更差**。
- 必须按 §1.3 的 range order 机制单独建模：区间外零手续费、穿越后必须撤出、被反向做掉的风险要计入。
- 判定：在从未触碰的时间窗上，净收益（扣全部摩擦）中位数是否显著优于同池同期宽区间双边 LP。

**H2：fee tier 选择存在可利用的系统性差异**
- 同一代币对存在多个 fee tier（我们的 `discovered_pools` 有 `fee_bps`）。`/root/v3lp_backtest/analyze_fee_tier_arbitrage.py` 有前期探索，先读它，判断是否可复用。
- 判定：控制住成交量与波动率后，某个 fee tier 的 `fee_minus_LVR` 是否稳定为正。

**H3：新池早期存在有限时间窗的高费率窗口**
- 我们对新池的数据覆盖是最好的（discovery_poller 6,871 个池）。
- 注意：这是我们最容易自欺的方向——新池同时也是 rug 最密集的地方。**必须把归零/撤池的池子计入分母**，否则就是幸存者偏差。
- 判定：把 rug 计入后，早期窗口的 `fee_minus_LVR` 期望是否为正。

**H4：Aerodrome Slipstream 上，"拿手续费"与"拿 AERO 排放"两条路的风险调整后收益存在系统性差异**
- 这是 Base 独有的结构，Uniswap 上不存在，文献里也没有。§1.4 已核实：LP 二选一——
  质押进 gauge 拿排放（手续费归 veAERO），或不质押拿手续费（无排放）。官方排放率约 10.9% 年化。
- 我们有 §2.1 的 3 个 aero_cl 池的完整仓位与成交数据，**可以直接算出"不质押拿手续费"这条路的真实收益**。
- 另一条路（排放）需要 AERO 的历史价格与 gauge 分配数据，属于外部数据，**需要你去取并注明来源**。
- 关键陷阱：**排放 APR 以 AERO 计价，AERO 会跌。** 必须用排放发放当时的价格逐笔计价并考虑变现滑点，
  不许用"名义排放 APR"直接和手续费 APR 相加或比较。
- 判定：扣除 LVR 与摩擦后，两条路的净收益分布是否显著不同；是否存在稳定占优的一方。

**H5：补齐 Aerodrome 广度数据的成本与预期收益评估**
- 我们只发现了 6 个 aero_cl 池，而 Aerodrome 占 Base 成交量 >60%。这个缺口有多大、补起来要多少钱和时间？
- 出一份提案（工厂地址、事件签名、回填区块范围、RPC 调用量估算、存储估算），**本轮不实施**。

**P2 交付物**：每条假设一份 `p2_H{n}_result.md`，含预登记的判定阈值、实际结果、裁决（PROVEN / REFUTED / INCONCLUSIVE / UNDERPOWERED）。

---

## 4. 交付物清单

全部放在 `/root/base_lp_research_r0/` 下：

```
GOAL.md                      # 本文件，不要修改
p0_calibration_report.md     # P0 校准报告（含真值对比）
p1_base_rate_report.md       # P1 基准率报告
p2_H1_result.md ...          # P2 各假设结果
src/                         # 代码
  lp_simulator.py            # 核心模拟器
  lvr.py                     # LVR 实现
  test_*.py                  # 单元测试
out/                         # 数据产物（CSV/JSON）
FINDINGS.md                  # 总结：一页纸，先写结论再写证据
OPEN_QUESTIONS.md            # 你没能回答的问题，以及为什么
```

---

## 5. 验收标准（我会怎么审查）

我会按以下顺序检查，任何一条不过就打回：

1. **P0.2 真值校准做了吗？** 没做的话，后面所有数字我都不看。
2. **`implied_share_p50 / p99 / capacity_violation_pct` 三个数在不在？** 不在就是重蹈 `MAX_POOL_SHARE` 覆辙。
3. **有没有出现异常漂亮的收益数字？** 任何超过 100% 年化的结果，我会直接去查它的容量假设和份额分布。上一轮的 +3279% 就是这么发现的。
4. **摩擦扣了吗？** gas、swap fee、滑点、rebalance 成本，逐项列示，不是一个总数。
5. **样本外了吗？** 参数在训练窗选，结论在保留窗报。
6. **分母对不对？** 特别是 H3，rug 掉的池子有没有留在分母里。
7. **不确定的地方有没有说不确定？** 我更愿意看到"数据不足以回答"，而不是一个用弱代理凑出来的答案。

---

## 6. 明确禁止的事

- 禁止修改本 GOAL.md。有异议写进 `OPEN_QUESTIONS.md`。
- 禁止改动 `/opt/EATLQ` 下任何文件，禁止碰 live 配置与私钥。
- 禁止执行任何链上写操作。本轮只读。
- 禁止把 `/root/v3lp_backtest/` 里 0.30% 档的结果作为任何结论的依据（已证明是伪影）。
- 禁止在报告里出现没有来源的外部数字。
- 禁止用"业界普遍认为""大家都知道"这类措辞替代证据。

---

## 7. 起步建议

1. 先读 `/root/v3lp_backtest/backtest_v3_lp.py`（尤其 26–36 行的单位换算注释和 99–104 行的份额计算），理解上一轮做对了什么、做错了什么。
2. 再读 `/root/v3lp_backtest/analyze_fee_tier_arbitrage.py`、`analyze_oneleg_reversion.py`、`analyze_momentum_breakout.py`，判断有无可复用部分。
3. 用一个池子（建议选 mint/burn/swap 三者齐全的其中一个）把 P0 全流程打通，再扩展。
4. ClickHouse 查询示例：
   ```bash
   docker exec eatlq-clickhouse-1 clickhouse-client -q "SELECT ... FROM eatlq.uniswap_v3_swaps WHERE pool_address='0x...' AND block_time BETWEEN ... ORDER BY block_number, log_index"
   ```
   注意 `liquidity_raw`、`sqrt_price_x96_raw`、`tick_raw`、`amount0_raw`、`amount1_raw` 都是 **String**，需要显式转 `Int256`/`UInt256`，注意负数（swap 的 amount 有符号）。

---

## 8. 一句话说明本轮为什么值得做

我们已经用五个月证明了**方向性预测**在这条链上不成立（KOL 信号、跟单、大单吸筹、动量、均值回归全部证伪）。
LP 是一个**结构上不同的收益来源**：它不要求你预测价格方向，而是要求你判断"手续费收入能否覆盖 LVR"。
这是一个我们从未系统检验过的问题，而且它有闭式解、有学术基线、有我们已经具备的数据。

但请记住基准率：**文献一致显示约一半的 LP 亏钱，IL 通常超过手续费。**
本轮最可能的结果是证伪。如果结论是"Base 上被动 LP 的期望是负的"，那也是一个好结果——它会省下我们后面所有的时间。
