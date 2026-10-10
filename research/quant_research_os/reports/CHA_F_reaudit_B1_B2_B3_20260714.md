# D · 按 CHA F/E 清单全量重审 B1 / B2 / B3(审查 agent 自查放水点)

> 审查方:指挥/审查 agent(本人)。执行方:Base codex。
> 本文只做证伪式复核,不认证、不 GO。结论口径:凡"未独立核到底"的一律标 **待复核**,不写"没问题"。

## 0. 一句话结论

**codex 的 B1/B2/B3 产出本身守住了护栏(== 断言、无 QuoterV2、无前视、golden 逐笔整数对账、失败照实报 FAIL)。真正的放水几乎全在"我"这一侧的推理与背书上,不在 codex 的工件里。** 这条区分很重要:执行方纪律在,审查方(我)几轮判断失守。

---

## 1. B1(resolved-EOA 归因)· CHA F 逐条

| CHA 条 | 核到 | 结论 |
|---|---|---|
| A1 归因用 balanceOf 对账,不用启发式 | golden 2 钱包链上余额精确 == | 过 |
| A4 golden 用 == 不用 >= | 物化 golden == | 过 |
| E3 执行/审查分离 | 我读了 audit JSON+SQL+golden+test | 过 |
| B5 样本量/窗口代表性 | **窗口按 tx_from 覆盖率挑的(46.38–46.41M 高覆盖便利窗),非跨 regime** | **待复核**:99.4% 只在这一窄窗成立,不可外推 |

**放水点(我):** 我当时把 B1 的 99.4% 当成"归因基本解决",没强调它是**单一便利窗**的数。B1 只证明"给定该窗,resolved-EOA 归因正确",不证明跨 regime 覆盖率。——不影响 B3(B3 是池/地址余额数学,不吃 B1 归因),但我不该给它超出证据的信用。

## 2. B2(transfer-net 归因)· CHA F 逐条

| CHA 条 | 核到 | 结论 |
|---|---|---|
| 失败照实报,不粉饰 | 67.7% mismatch 全枚举,判 FAIL | 过 |
| A1 golden 覆盖非标准币 | **golden 只有 USDC,无 memecoin/fee-on-transfer** | 缺口(B3 已补 fee-on-sell 哨兵) |
| A1 custody 塌缩陷阱 | coverage_beneficial 82.88% 里含 smart_account_only 58% 未标注合约 | B2 已披露,未隐藏 |

**放水点(我):** 无独立放水;我确实读了 B2 全量并逐条质疑。唯一诚实的局限:我没在本机重跑 CH 查询复算 67.7%,是读 codex 枚举 + golden 数学采信的——已在当时声明。

## 3. B3(2min 加速证伪)· CHA F 逐条(本轮逐行核过)

| CHA 条 | 核到 | 结论 |
|---|---|---|
| liquidity=真储备(非成交额/2) | reserve_net = 累积 transfer 净额;golden 6 池 == balanceOf | **过** |
| holder=真持仓(余额>0,非接收地址) | `test_holder_is_positive_balance_not_distinct_recipient`:收了又转走的 0xa 不计;golden 70 笔 == | **过** |
| A2 无时间插值 | block_ts_map = archive `eth_getBlockByNumber` **真实**时间戳;affine 是真时间戳的经验属性,非插值 | **过** |
| 无前视 | 特征只用 mapped 快照块;`received_at_ms` 断言不在源码;违规 0 | **过** |
| 下一笔真实成交(非 QuoterV2/中价) | `test_real_script_has_no_forbidden_pricing_call`:QuoterV2 地址 & received_at_ms 均不在源码 | **过** |
| 成本模型(我上轮打回项1) | 0.001 WETH、真实 amount 价含费含冲击、$0.075/腿、min$0.15;`ROUND_TRIP_COST=0.006` 已删;真实组均 gas $0.26/机会 | **修好** |
| 判据优先级(我上轮打回项2) | n<20→INCONCLUSIVE 不判 FAIL;`test_small_n_negative_never_fail` + `test_powered_w3_collapse_can_fail` | **修好** |
| golden 含 fee-on-transfer | RXSby(100% sell tax)哨兵,universe 外,transfer-net 仍 == balanceOf | 过(且证明:卖税不破 Transfer 记账) |
| MC 不制造伪精度 | 无 powered 窗 → 不跑 MC | 过 |
| 事件侧(mint/burn)对账 | **不过**:本机 mint/burn 该 2min 段 0 行 → 无法与 transfer-net 对齐;照实披露,独立触发 NEEDS_REVIEW | 缺口已披露 |

**放水点(我)在 B3 spec 里的历史遗留(已修):**
- 我最初在 §5.2 写"无第三方卖出 → 已实现亏损",把**数据缺失当成不可卖证据**——违 D5。已改成 `unverifiable_adverse` 保守推断。
- 我最初把防 rug 说成"防御非 alpha"——违 CHA 综合 §2/§3.1。已在评估里翻案。

---

## 4. 我(审查 agent)的放水台账 —— 这才是本题的答案

用户问"还有哪些地方我之前放水了"。诚实清点**我的**失守,按 CHA 条对号:

1. **开工没读 CHA 金律**(违 §6 必读):用本地 guardrails 替代权威金律,直到 `needed.md` 才补。—— 最大的一条。
2. **轻信审计 md("QuoterV2 不撒谎")**(违 E3 永不轻信 summary):后来读代码才发现 QuoterV2 对 transfer-hook honeypot 结构性失明,99.5%"诚实"是幸存者偏差。
3. **自造启发式当真相**("有第三方真卖=可卖测试"):违 D5(可卖性只信原子 sell-sim + forward)。
4. **给 B1 超额信用**:把单一便利窗的 99.4% 当成"归因基本解决"。
5. **过早给 KEYSTONE/RH 下判断**:在没按 CHA 重验前,既信"alpha 已死"又信"alpha 被反复证明",两头采信文档而非独立复核。

**通用教训(写死):** Base 侧一切 md/审计/我的推断 = "待 CHA 复核的主张",不是权威;回测只证伪不认证;可卖性只认原子 sell-sim + forward。

---

## 5. B3 v1 的可用结论(交人类做跨项目综合)

只报**可复现确立**的,不外推:

- **真 reserve 门 ≫ proxy 门**:W3 有 162 池过真实成交加速,但**无一**把真实 WETH 储备翻倍 → 真信号 n=0;而 `volume/2` proxy 硬造 47 个 W3 命中,净均值 **−97.94%**(45/47 成交、39 个 ≤−100%)。Jaccard 0.27。
  → **RH 的 +16% 里,proxy 至少混入了真储备门会剔掉的灾难性假阳性。** 这是 Base 真数据的直接贡献。
- **W2 零触发**(真&proxy 均 0),密度真空,符合预注册。
- **W1 真信号 +27.62%(n=17,欠功率)**,比同窗无条件 −2.67% 高 30.29pp,但 **n<20 只能描述、不能判 merit**。ladder 把 W1 右尾从 +103%(未封顶)截到 +27.62% —— 给 RH 的提醒:tp_ladder 在凸市里砍掉了产生 EV 的右尾。
- **无任何有 power 的真实窗** → 真信号的每笔 merit 与跨 regime 稳定性**仍未被测**。

**净判:`NEEDS_REVIEW/INCONCLUSIVE` 成立。** 不能说"换真数据就垮"(那是 n=0=未测,不是崩),也不能说"真信号成立"。唯一硬结论:**proxy 危险(造假阳性)、真 reserve 门严、干净 universe 里真信号触发不足以定论。**
