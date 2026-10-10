# Base-RH1（已执行）· 在 Base 复现 RH「聪明钱跟单」证伪方法

> 状态：**`merit=UNTESTED / NO_GO`。** B2-v3 holder concentration 与 subgraph golden 均失败，子图救回已回滚，beneficial coverage 回到 82.88%；且 RH1 FIFO PnL 缺独立链上 golden。既有短窗选择器输出仅保留为失效诊断，不得用于“有/没有 edge”结论。
> 人类指令（2026-07-14）：完成任务；现实跟单延迟估计 **10 秒以内**。本版据此冻结主延迟为 10 秒，并由审查 agent 冻结资格阈值，不再先看 RH 结果分布调参。
> 目标：在 **Base/EATLQ** 上复现 RH 的聪明钱选择、point-in-time/OOS、延迟跟买和真实成交退出方法；吸收 RH router 误归因与成交幻觉教训。回测只证伪不认证，不下单/签名，不自判 GO。
> 完整执行报告：`/opt/EATLQ/reports/base_rh1_smartmoney_falsification_20260714.md`。

## 执行边界与当前阻断（2026-07-14）

- 一手执行仓库为 `/opt/EATLQ`，链为 Base（chain_id=8453）；RH 合集 `/root/rh链聪明钱0714.md` 只提供方法与失败教训，不作为 Base 结果数据。
- Base 钱包主口径使用 B1 高覆盖块窗的 `tx_from_by_hash.tx_from`，不用已知错误的 `swap.sender`，也不把 recipient/sender fallback 冒充高置信钱包。
- 执行只读本地 ClickHouse，RPC calls=0；新增 research 脚本、测试、JSON/CSV、报告与 hash ledger，未触碰 live。
- 受限点不是“找不到 RH 工作区”，而是 Base 全历史高置信钱包归因覆盖不足：当前可严谨执行的 tx.from 窗只有约 15.4 小时/28 个 WETH 土狗池。

## 0. 策略假设(要被证伪的东西)

"识别一批历史上反复赚钱的钱包(聪明钱);当他们买入某新币时,我延迟跟买;按阶梯/时限卖出。"预期:蹭到聪明钱的选币能力,拿到正超额。

## 1. 两个头号防自欺(不堵死,结论全假)

### 1.1 聪明钱名单的因果性(反幸存者偏差)
- 名单必须**逐时点(point-in-time)构建**:在决策时刻 T,一个钱包能否算"聪明",**只能用它在 T 之前已平仓交易的真实盈亏**,严禁用 T 之后的信息、严禁用"全历史最终赢家"名单。
- 预注册资格规则(冻结):最小已平仓交易数、最小胜率/盈亏、最小时间跨度;**剔除内部人**(池部署者/早期注资地址/貔貅操盘手——他们不是"聪明",是庄)。
- 对照:**随机钱包跟单**(同数量、同活跃度的随机地址),真聪明钱必须显著跑赢随机名单,否则"聪明"是噪声。

### 1.1.1 冻结的逐时点资格规则（不得看结果后修改）

在每个信号决策时刻 `T`，只使用 `[T-90d, T)` 内、且退出成交已在 `T` 前确认的记录。钱包须同时满足：

1. 已平仓独立 `wallet-token episode >= 20`；同币多次加减仓按一个 episode，采用 FIFO 重建已实现 PnL。
2. 首末已平仓 episode 跨度 `>= 30d`，且活跃交易日 `>= 10`。
3. 扣除真实 gas、DEX/协议费、token tax 与可观测冲击后：累计 realized PnL `> 0`、profit factor `>= 1.20`、胜率 `>= 55%`。
4. 90 天窗口前后各半段的 realized PnL 均 `>= 0`，防止只靠一次旧暴击入选。
5. 任何一项成本缺失按 adverse 补入；钱包与 token 间若存在无法解释的转入/转出、跨钱包库存或归因歧义，该 episode 不得算盈利，主口径按 0/亏损较保守者处理，并单列敏感性。
6. 排除：token/pool deployer、初始 LP/注资者、与 deployer/初始 LP 同资金簇者、公开交易前持币者、可识别做市/路由/聚合/托管合约、貔貅/抽池操盘关联地址。排除关系也只能用 `T` 前已知证据。

资格是每个 `T` 动态重算的布尔状态，不生成一份全期静态名单。资格规则本次直接冻结，**不先扫 RH 收益分布再挑阈值**；如导致样本不足，只能报 `INCONCLUSIVE`，不得降门槛补样本。

> Base 数据现实：链上 swap 有数月历史，但通过 golden 的 `tx.from` 高覆盖归因只覆盖约 15.4 小时。因此 30 天规范资格臂当前为 `BLOCKED_ATTRIBUTION_HISTORY`。本轮另按 RH 原 selector battery 跑短窗探索臂；短窗只能提前证伪/描述，不能满足 §4 PASS。

### 1.1.2 冻结的随机钱包对照

- 每个 `T` 从当时未满足聪明钱资格、但有可归因历史的地址中做 1:1 最近邻匹配；匹配变量为过去 90 天的已平仓 episode 数、活跃日、最近一次交易距 T、成交额和单笔 notional 中位数。
- 不匹配未来收益、token 身份或信号后的行为。每个聪明钱包取 100 组随机匹配，seed 固定为 `20260714`。
- 报 matched balance；任一标准化差异绝对值 `>0.10`，该对照不合格，结论降为 `INCONCLUSIVE_CONTROL_MISMATCH`。

### 1.2 延迟税(跟单的真正杀手)
- 你只能在聪明钱买入 tx **已上链且可观测之后**才行动。必须建模**真实检测+执行延迟**:跟买成交价 = 你延迟之后的**下一笔真实成交价**(你买在他已经推高的价上)。
- 必须并列报三条:**聪明钱自己的已实现收益**、**你延迟跟买后的收益**、两者之差 = **延迟/滑点税**。若延迟税吃光 edge,策略即证伪。
- 扫延迟敏感性:0 块 / 1 块 / N 块 / 观测到即下一笔,看 edge 随延迟怎么衰减。

### 1.2.1 冻结的延迟与信号去重

- 主口径：源钱包买入所在区块/slot 已被 RH 数据源确认并可观测的时刻 `observed_at` 后 **10 秒**，取严格晚于 `observed_at+10s` 的下一笔同池真实成交作为跟单入场锚。若只有链上区块时间、没有真实观测时间，则用 `source_block_time + 10s`，并明确标为对现实延迟偏乐观的上界。
- 敏感性：`0 block`、`1 block/slot`、`5s`、`10s`（主）、`30s`、`60s`，以及“观测到后下一笔真实成交”。`0 block` 只作不可实现上界，不得混入主结论。
- 同一 token 在持仓未结束前只允许一笔策略仓位；多个合格钱包连续买入只记为 crowding/共识特征，不重复叠仓。主信号取该 token 的首个合格钱包买入。
- “新币”冻结为该 token 首个公开池首笔真实成交后 24 小时内；旧币新池单列，不混入主判据。

## 2. Base 真实字段

- 真实成交序列(非报价/中价)、真实入场/出场价。
- 可卖性/rug:沿用 D5 原子 sell-sim + 事后真值(adverse 保守),按 RH 链蜂窝/抽池机制核对。
- 时间锚:RH 真实区块/成交时间,无前视。
- 聪明钱盈亏:由 RH 真实成交重建每钱包已平仓 PnL(point-in-time)。

### 2.1 Base schema 回执要求

执行脚本必须提交一张 `逻辑字段 -> 数据库.表.真实列 -> 覆盖范围 -> NULL率 -> 时间语义` 映射表，至少覆盖：

- swap：chain/venue、block/slot、block_time、tx_hash、log/instruction index、pool、token0/1、方向、amount in/out 原始整数、decimals、trader/beneficial owner 及其归因来源；
- pool/token：token、pool、deployer、创建 tx/slot/time、初始 LP/注资地址、quote token、费率；
- wallet inventory：转账、swap、期初库存、部分平仓与 gas payer，用于 FIFO realized PnL；
- execution：源 tx 何时被数据源观测、确认深度、目标 size 下的 gas/priority fee、DEX fee、token tax、冲击；
- sellability/rug：D5 原子买→卖模拟输入输出、固定 block/slot、事后池储备/抽池真值及未知标签；
- reproducibility：数据快照时间、RPC endpoint 别名（不得泄密）、请求/响应 hash、查询 hash、代码 commit/hash。

映射未交、钱包只能看到 router 而无法解 beneficial owner、或历史成交序列不完整时，停在 `BLOCKED_SCHEMA/ATTRIBUTION/COVERAGE`，不得静默换代理。

### 2.2 成交、PnL 与退出定义（冻结）

- 钱包本人收益：源钱包该 episode 的真实 FIFO realized PnL / 实际投入成本；未在样本截止前平仓的 episode 不进入资格计算，但进入“信号后结果”时按截止可卖真值 adverse 结算。
- 跟单收益：固定投入 `1R`，入场按 §1.2.1 的下一笔真实成交价，再加目标 size 的方向性冲击；退出为 `+30%/+60%/+120%` 各卖 1/3，未成交余量在 60 分钟后首笔真实可卖成交退出。
- 每档 TP 只有目标触发后出现可容纳该 size 的真实卖向成交才算兑现；只看到价格越线不算成交。
- 60–120 分钟内没有可验证卖出、原子 sell-sim 失败、或池储备塌陷，主口径按 `-100%` adverse；只能标 `unverifiable_adverse`，不能反推“已证明是蜜罐”。另报最后真实可卖价敏感性。
- 同时报告不封顶、60 分钟统一退出变体，防止阶梯砍掉右尾。
- 成本逐腿扣真实 gas/priority fee、DEX fee、token tax 与 size impact；缺项 adverse，不得统一写一个乐观百分比。

## 3. 验证框架

- 跨 regime(仿 B3/B6 的滚动子窗,别压单段)、forward/OOS(早段定资格规则,后段只验)。
- 成本:真 gas + 费 + 冲击;缺证据 adverse。
- 容量与拥挤:聪明钱被很多人跟时的容量衰减 / 自我踩踏,披露机会数与容量。

### 3.1 冻结的时间切分与统计单位

- 时间顺序 60%/20%/20% 切为资格规则校准段、validation、最终 OOS；本版资格阈值已经冻结，因此最早 60% 只用于建立滚动历史，不得再调阈值。
- 在 validation+OOS 内按自然月或固定 28 天切不重叠子窗，并用事前可得的市场成交额、发行数、基准币趋势标注 regime；不得按策略收益挑窗。
- 统计单位为去重后的 `token-position`，不是每个钱包信号；同 token 的相关信号不得冒充独立样本。
- `n>=20` 才称 powered 子窗（沿用本系列约定）；`n<20` 只描述并标 `INCONCLUSIVE_SMALL_N`。
- 除均值外，报告中位数、胜率、PF、收益分位、top1 去除、cluster bootstrap（按钱包和 token 双层聚类）及 100-trade 路径 MC；但不以中位数替代右尾判据。

## 4. 预注册右尾判据(冻结,禁改)

跟单版**通过**需同时满足（收益均为扣全成本后的 `R`）：

1. 10 秒主口径 pooled mean `>0`，且移除单笔最大赢家后仍 `>0`；
2. 每个 powered 子窗 mean `>= -0.10R` 且 alpha（相对同窗无条件新币基线）`>=0`；
3. 至少 2 个 powered regime 同时 mean `>0` 且 alpha `>0`；
4. 10 秒跟单 mean 高于 100 组匹配随机钱包结果的 95 分位；
5. 10 秒延迟税后 mean `>0`，且不是只有 `0 block` 不可实现上界为正；
6. 100-trade cluster-bootstrap/MC（10,000 次，seed=`20260714`）累计终值 `p5>0R`，MDD `p95<=20R`；
7. 最终 20% OOS 段 mean `>0`、alpha `>0`，且 top1 移除后 mean `>0`。

任一**有足够样本且可验证**的硬门不过 → `FAIL`。若缺 schema/归因/覆盖，或关键比较 `n<20` → `INCONCLUSIVE/BLOCKED`，不得把“没测成”写成证伪。

## 5. 必须产出

- 聪明钱 point-in-time 名单构建法 + 反幸存者校验。
- 三收益对照(聪明钱本人/延迟跟买/延迟税)+ 延迟敏感性曲线。
- 跟单版 vs 随机名单 vs 无条件,跨滚动子窗 + OOS;判据逐项 + PASS/FAIL。
- golden、回归测试(==)、RPC/hash ledger、可复现命令。

### 5.1 固定交付路径与最小验收

Base/EATLQ 侧可按仓库结构调整目录，但须在回执中列真实路径，至少产出：

- `rh1_preregistration.json` + SHA256（包含本文件所有冻结参数）；
- `rh1_schema_mapping.md`、`rh1_coverage.json`、`rh1_rpc_hash_ledger.jsonl`；
- `rh1_wallet_eligibility_point_in_time.parquet`、`rh1_signals.parquet`、`rh1_trades.parquet`；
- `rh1_results.json`、延迟曲线 CSV/图、最终中文报告；
- golden fixture 与回归测试，精确断言：`feature_time < signal_time`、资格交易的 `exit_confirmed_at < T`、entry row 是延迟阈值后的首笔真实成交、同 token 未重叠持仓、随机匹配 seed/数量相等、不可卖样本未被删除。

报告必须逐项列 §4 七道门的数值、布尔值和证据路径；不得只写总裁决。

## 6. 铁律

纯回测/只读,不下单/签名/广播/改 live,不自判 GO,可复现;数据缺失一律保守 adverse。Base schema 不适配处**照实回报,不硬凑**。

---

## 7. 人类拍板项：已处理

1. “聪明钱”资格阈值：由本版 §1.1.1 直接预注册，**不先扫结果分布**。
2. 跟单延迟：依据人类“10 秒以内”估计，冻结 **10 秒为主口径**；仍须用 Base 真实观测日志校验。若实测 p50/p95 高于 10 秒，主报告必须追加实测 p50/p95 口径，且最终裁决取二者中更 adverse 的结果。

## 8. 当前执行裁决

`Base-RH1 canonical merit = UNTESTED`。

原因不是缺 RH 工作区，而是 Base 通过 golden 的高置信 `tx.from` 历史只覆盖块 46,389,458～46,417,105：57,426 笔原始 swap、18,840 笔 WETH/非 quote-token 可用成交、3,404 钱包、28 池，时间约 15.4 小时。

## 9. Base 执行结果

### 9.1 RH 主选择器为何没有信号

训练切点前 2,484 钱包中：364 个有≥1闭合 episode，105 个有≥2，35 个同时已实现盈利；但加入 RH `top1_profit_share<50%` 去暴击门后为 **0**。因此 `deconcentrated top-30` 没有可选钱包，10 秒主跟单、matched-random、延迟税三线都无合法样本。

这是 `INCONCLUSIVE_NO_ELIGIBLE_WALLETS`，不是收益 FAIL；但它直接复现了 RH 的核心教训：短窗“聪明钱”利润全靠单笔暴击，去集中后名单消失。

### 9.2 RH 五选择器电池（10 秒、真实成交、24h新池）

| selector | wallets | n | mean | median | win | 去top1 mean |
|---|---:|---:|---:|---:|---:|---:|
| abs_pnl | 30 | 1 | −2.42% | −2.42% | 0% | — |
| winrate(closed≥15) | 1 | 0 | — | — | — | — |
| median_pnl | 30 | 4 | −53.98% | −55.67% | 0% | −71.16% |
| deconcentrated | 0 | 0 | — | — | — | — |
| risk_adjusted | 30 | 1 | −2.42% | −2.42% | 0% | — |

旧探索臂曾产生 n≤4 的负向输出；由于归因硬门和 FIFO PnL golden 均未通过，这些数字现只作为失效诊断留档，**不构成负 edge 证据**。

### B2 连带铁律（2026-07-14 更新）

B2-v3 曾把 beneficial coverage 提高到 92.250897%，但 80 个疑似共享合约排除后只剩 47.809355%；新增 5 个多跳 archive golden 仅 1/5 对上，故全部子图救回已回滚。回滚后 coverage=82.877094%、ambiguous=11.604500%。因此 RH1 仍不得改用 B2；即使未来 B2 通过，RH1 也必须先为 FIFO realized PnL 补独立链上 golden，才允许进入 merit 检验。

在这两个条件同时满足前，禁止对 Base 聪明钱下“有 edge”或“没有 edge”结论；`merit=UNTESTED`，不 GO。

### 9.3 交付

- 报告：`/opt/EATLQ/reports/base_rh1_smartmoney_falsification_20260714.md`
- 脚本：`/opt/EATLQ/scripts/research/base_rh1_smartmoney.py` + `base_rh1_smartmoney_core.py`
- 结果：`/opt/EATLQ/mvp/diagnostics/base_rh1_smartmoney_20260714.json`
- trade-level：`/opt/EATLQ/mvp/diagnostics/base_rh1_smartmoney_trades_20260714.csv`
- ledger：`/opt/EATLQ/mvp/diagnostics/base_rh1_hash_ledger_20260714.json`
- 测试：`python3 -m unittest -v tests.test_base_rh1_smartmoney`，6/6 通过。

最终行动：不 GO、不恢复 Route-A、不用 fallback 身份硬凑数。若继续，只扩高置信 `tx.from` 历史后按同一冻结逻辑重跑。
