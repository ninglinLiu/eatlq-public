# GOAL R3：Morpho Blue 核心市场 — 健康度路径与清算回避（不是清算赛跑）

**发起人**：Claude（任务发起与验收审查）
**执行者**：Codex
**创建时间**：2026-08-15
**状态**：待执行（**门禁：R0 P0 必须 PASS**）
**工作区**：`/root/base_morpho_credit_r0/`
**前置**：`p0_instrument_report.md` 为 PASS。R2 的预言机画像若存在应复用，但不是硬前置。R0 若未做事件回填，本轮大概率 UNDERPOWERED——那是合法裁决，不是让你去全链扫清算。

---

## 0. 给执行者的先决约束（不可协商）

1. **本轮研究借款人状态机，不研究如何当清算人。** 禁止：清算机器人、抢跑、flashloan 清算、gas 竞价、私有中继、LIF 利润回测、“如果我们先到能赚 5%”。官方已经写了先到先得。那是金矿，禁止。
2. **禁止任何真实资金操作。** `NO_LIVE.lock`。禁止 `liquidate` calldata 上链。本地 fork 若用来**验证公式**，不得接 signer，不得保存能广播的 raw tx。
3. **禁止在没有仓位级数据时编造钱包分布。** 没有事件、没有 `position()` 抽样，就不要写“聪明借款人会补仓”。
4. **禁止把回避率写成交易策略。** 本轮最高有用产出是：健康度如何走、谁在 HF 接近 1 时补抵押/还款、清算发生前的可观察状态、以及这些状态能不能当 **F03 的否决器**（否决器也要到 R4 才检验）。本轮只建状态，不跟现货。
5. **禁止未上架市场、禁止 HERMES、禁止把 Aave/Moonwell 混进来“增加样本”。** 主市场只有 `cbBTC/USDC` 86%。
6. **禁止样本内选参当结论。** 禁止修改 GOAL。诚实优先。数字带来源。
7. **禁止碰 `/opt/EATLQ` live。** 禁止改 R0 仪器公式来让路径更平滑。

---

## 1. 已核实的事实基线

### 1.1 机制（官方，已核实）

清算页：`https://docs.morpho.org/learn/concepts/liquidation/`  
`IMorpho.sol` v1.0.0：`https://github.com/morpho-org/morpho-blue/blob/v1.0.0/src/interfaces/IMorpho.sol`

```
HF = COLLATERAL_VALUE_IN_LOAN_TOKEN * LLTV / BORROWED_AMOUNT
可清算 ⇔ HF < 1
LIF(0.86) ≈ 1.05
允许部分清算，也可一次清到 100%
坏账留在本市场，不向其它市场摊
预言机价同时决定“能不能清”和“清的时候换多少”
```

官方还写了：

- 价格恢复且 HF 回到 1 以上，则不再可清算，借款人保留仓位
- 清算人是无许可外部人；协议自己不清
- `Market` / `Position` 的应计警告与 R0 相同：生字不含自 `lastUpdate` 的利息。**HF 必须用应计后的 borrow assets。** 用生字会系统性高估 HF。

`liquidate` 的官方警告（你必须读函数注释）：

- `seizedAssets` 与 `repaidShares` 只能一个非零
- 超额会 underflow
- 攻击者可以抢先小额 repay 让别人的清算交易 underflow

这些警告解释了为什么清算赛跑是专业场。本轮不进入该场。

### 1.2 本轮唯一科学问题

> 在核心市场上，借款仓位的健康度是一个有记忆的状态机，还是一到压力就瞬时死亡？具体：HF 进入危险带之后，下一状态更常是（补抵押 / 还款 / 被清算 / 价格恢复）中的哪一种？这些转移是否稳定到可以在样本外复述？它们对 $1,000 供给者的坏账风险有没有可测量的含义？

零假设：**危险带之后的转移是价格路径的函数，没有独立的行为结构；对 $1,000 供给者，坏账事件稀少到估计不稳。**

### 1.3 为什么这不是金矿，以及为什么仍值得做

不是金矿：我们不赚 LIF。值得做：

- 供给者的真正风险是坏账，不是 4% APY 的波动
- Wildcat S02 把“参与广度”当否决器。健康度路径是借贷版的否决器原料
- 官方隔离意味着：这个市场的坏账不会被 Aave 式资金池稀释，也因此更值得单独看

---

## 2. 数据现实

仓位级数据是本轮的稀缺资源。按这个顺序找，找到就停在够用的那一层，不要为了“更全”去扫全链。

**层 A — 官方 API 仓位（优先）**

读 `https://docs.morpho.org/developers/api/morpho/` 的 positions 部分。GraphQL 是否能按 `marketId + chainId=8453` 列出借款仓位、HF、collateral、borrow？能则落盘 `out/r3_positions_api.parquet`，并抽 20 个地址用 `eth_call position(id, user)` 交叉。对不上就信链，标 API 偏差。

**层 B — 核心市场事件重建**

若 R0 P0.4 已有 `SupplyCollateral` / `WithdrawCollateral` / `Borrow` / `Repay` / `Liquidate`：

- 按 `(user, market)` 重建仓位时间线
- `onBehalf` 才是仓位主人，`caller` 可能是 bundler / manager。**归因用 onBehalf。** 搞反就是 LP 那次把 Position Manager 当巨鲸。

若 R0 没做事件：本轮允许**只对核心市场、最近 30 天**补这五类事件。规则同 R0：span 先试 10,000，≤10 RPS，断点续传，记 credits。超过 30 天必须先写估算再停。

**层 C — 都没有**

裁决 `DATA_INSUFFICIENT`。可以用 R2 的假想仓做敏感度附录，但主文不得假装有真实借款人。

禁止：为了凑清算样本去拉 2024 年以来全市场 Liquidate；禁止把其它抵押物的清算并进来。

---

## 3. 研究阶段

### P0 复核

确认 R0 PASS、应计函数可复用、事件 topic 已与 Basescan 对过（若用层 B）。抄进报告开头。

### 预登记 `out/r3_preregister.json`

```
danger_band          = HF ∈ (1.00, 1.05]     # 进入危险
deep_danger          = HF ∈ (1.00, 1.02]
horizon_hours        = [1, 4, 12, 24]
next_state           = {top_up, repay, liquidated, recovered, still_danger, disappeared}
top_up               = SupplyCollateral > 0 且 Borrow 净增加 ≤ 0
repay                = Repay > 0
liquidated           = 出现 Liquidate（无论部分或全部）
recovered            = HF 回到 > 1.10 且期间无 Liquidate
train / holdout      = 70/30 时间切分，按“进入危险的时刻”而不是按日历行
min_borrow_usd       = 1000                 # 低于此的仓位当尘埃，单独计数但不进主表
exclude_if_api_only_and_no_chain_check = true
```

**H0：** holdout 上，进入 `danger_band` 后 24h 内，`liquidated` 与 `recovered` 的比例不显著不同于“只由同期抵押物价变化解释的模拟仓”（模拟仓：冻结借款人行为，只让价格走，看 HF 会不会自己穿越 1）。

**H1（行为）：** 真实仓的 `top_up + repay` 显著高于冻结行为的模拟仓。这说明有人在回避。

**H2（供给者）：** 观察期内核心市场出现坏账（Liquidate 后仍留 borrow 且 collateral=0）的次数、金额、占供给的 bps。对 $1,000，期望坏账 ≤ 同期供给利息。这是供给者基准，不是策略。

H2 的利息用 R1 的供给 APY 积分或 4.12% 年化的预登记常数，写死一种。

### P1 — 宇宙与快照

- 有多少独立 `onBehalf` 有非零 collateral 或 borrow
- 借款 USD 分布：p50/p90/p99/max
- HF 分布：<1、1–1.02、1.02–1.05、1.05–1.15、>1.15
- 尘埃仓占比
- API 与链抽样的 HF 中位相对误差

HF < 1 的仓若在快照里大量存在：要么清算滞后（值得描述），要么你用了未应计的 borrow（仪器错误）。先排除仪器。

### P2 — 转移矩阵（主分析）

以“进入 danger_band 的时刻”为事件。同一仓位在未离开危险带时不重复计数。holdout 上报告：

```
                top_up  repay  liquidated  recovered  still_danger  disappeared
from danger
from deep_danger
```

每个格子：计数、比例、借款 USD 加权比例。**分母是进入事件，不是期末存活仓。** 被清算的必须留在分母。这是 LP R0 H3 的同一条诚实规则。

对照：冻结行为模拟仓（只用预言机价或现货价路径重算 HF）。两种价格都有就都做，没有现货就只用预言机，并写明“回避相对的是预言机路径，不是真实兑付价”。

检验：对 `top_up+repay` 的比例做预登记的二项或 bootstrap，α=0.01。只在 holdout 报。

### P3 — 清算事件的描述（不是利润）

若有 Liquidate 事件，只允许这些列：

- 时间、borrower、repaidAssets、seizedAssets、badDebtAssets
- 清算前最后一个 HF（若能重建）
- 距进入 danger 的时间
- 是否部分清算后仓位仍在

禁止列：清算人地址的 PnL、gas 出价、捆绑 swap、MEV 标签当“可复制策略”。清算人地址可以做**计数**（多少唯一 caller），用来说明“这是不是已经被少数人垄断”。垄断本身是“不要去做清算人”的证据，不是邀请。

### P4 — 对 $1,000 供给者的坏账算术

```
bad_debt_usd_total
bad_debt_bps_of_supply
interest_usd_on_1000_over_same_window
net = interest - bad_debt_share
implied_share_p50 / p99 / capacity_violation_pct
```

坏账份额 = `1000 / supply_usd_at_event`，按事件加权。供给是 $1.48B 时，$1,000 分到的坏账几乎一定是尘埃——**把这句话算出来，不要用感觉。** 若窗口内坏账为 0，写 0，不要用 Moonwell 2026-02 的 oracle 事故来吓自己；那是另一个协议。来源若要引用：`https://nomoslabs.io/archive/moonwell-2026`，只作为“隔离市场为什么重要”的外部对照，不进本市场数字。

### P5 — 失败模式

1. `caller` 当仓位主人
2. 未应计 HF
3. API 仓位是最新快照，没有进入危险的时间，于是你用截面冒充路径
4. 把 vault 聚合仓当成一个“超级借款人”的行为（vault 是策展人，行为不是自然人）
5. 危险带阈值事后改成能出显著结果的值
6. 清算样本 n=3 还报 p 值
7. 写出清算执行步骤

---

## 4. 交付物

```
r3_health_paths_report.md
out/r3_preregister.json
out/r3_positions_api.parquet      # 若有
out/r3_events_core/               # 若本轮补了事件
out/r3_transition_holdout.csv
out/r3_liquidation_desc.csv       # 描述，无 PnL
out/r3_supplier_bad_debt.csv
src/r3_*.py
src/test_r3_hf_accrual.py         # 应计后 HF vs 生字 HF
```

裁决词：

- `DATA_INSUFFICIENT` — 无仓位、无事件
- `REFUTED` — holdout 上行为项不存在，转移由价格解释
- `DESCRIPTIVE_ONLY` — 有转移表，但样本或制度不够做检验
- `UNDERPOWERED`
- `MECHANISM_ONLY` — H1 成立（有人回避）但 H2 对 $1,000 无经济意义
- `SHADOW_CANDIDATE` — 本轮原则上不给这个标签，除非你同时证明回避状态能在不交易借贷的情况下改变现货决策；那件事属于 R4

---

## 5. 验收标准

1. 第一句裁决。报告里找不到“清算利润”“LIF 年化”“如何抢第一笔”。
2. HF 用了应计后借款。测试里有生字 vs 应计的对照。
3. 归因用 `onBehalf`。
4. 被清算的仓在分母里。
5. 有 `implied_share_*` 与坏账算术。
6. 预登记在先。
7. 主市场只有一个。
8. 没碰 live，没改 GOAL，没写执行器。

---

## 6. 明确禁止的事

- 禁止任何清算执行设计，包括“仅教育目的”的逐步调用顺序。
- 禁止给清算 bot 仓库加功能、禁止部署官方社区 bot。
- 禁止用 Moonwell / Aave 清算史代替 Morpho 核心市场。
- 禁止把 vault 策展人再配置解释成散户补仓。
- 禁止为了增加 Liquidate 样本而扩大到全部 Base 市场。

---

## 7. 起步建议

1. 先问 API 能不能列出核心市场借款仓。能，先做层 A 快照与交叉，再决定要不要拉事件。
2. 先算 HF 分布。若几乎没有人在 1.05 以下，转移矩阵会空，UNDERPOWERED 是正确的。
3. 写测试：同一仓，应计前后 HF 谁更大。应计后 HF 应该更差或相等，不应更好。

---

## 8. 一句话

5% 的清算奖金是别人的金矿。我们要的是：危险带之后人往哪走，以及 $1,000 的供给者会不会在隔离市场里吃到坏账。路径有记忆，是发现；路径只是价格，也是发现。去抢清算，不是。
