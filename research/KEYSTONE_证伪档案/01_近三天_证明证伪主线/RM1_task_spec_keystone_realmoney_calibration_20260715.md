---
sop_applied: pending
alpha_id: AR-KEYSTONE-RM-CALIB
family: F1/F4/F6/F7
next_gate: BUILD_AND_DRYRUN_ONLY__HUMAN_ARM_PER_TRADE
decision_flags:
  training_allowed: false
  live_allowed: false        # 真实广播永远等人类单独 arm，codex 不得自翻
  self_go_allowed: false
---

# RM1（v2）· 执行链路真钱跑通（一笔一研究）

> 审查/指挥 agent 下发，人类已裁定四处歧义并重定目的。
> codex 只做三件事：**搭框架 → 干跑(live_broadcast=DISABLED) → 预注册**，然后停手交回人类。
> 真实发单/签名/广播永远是人类单独 arm 的独立动作，**且一次 arm 只放一笔**；codex 不自判 GO、不翻 live_allowed、不启停 executor。违者作废。

## 0. 目的（已被人类掰正 —— 重点变了）

不再是「攒 30 笔 KEYSTONE 信号做统计验证」。原因：历史长周期实盘证明 **KEYSTONE 信号极稀、且新池容易被 rug 打断，等信号来验证不现实**。
**本轮真正目的 = 把我们整条真实 execution 链路端到端跑通**：选标的 → 全量 eth_call 模拟 → D5 原子可卖门 → 广播 → 真实成交 → 记录「真成交 vs 同块报价」偏差 + 真实延迟 → 按规则出场 → 平仓对账。
**交易一笔、研究一笔**：每次人类 arm 只放一笔真钱往返，跑完全量记录、交回人类逐笔研究，人类满意再 arm 下一笔。没有批量、没有总预算。

**为避免等稀有信号 / 避免被 rug**：链路跑通阶段用**我们自选的、已知深流动性的成熟币**(非新池)做标的，随时可跑，不依赖 KEYSTONE 信号触发。

### 审查保留（写死，防日后过度声称）
深池安全币上量到的报价-成交偏差是**下界**：它能证明"管道通、延迟真、止损/模拟门真生效"，**但不能证明 reaudit10 的 +56%**（那活在薄新池、含抢跑/三明治）。链路跑通 = 对 +56% 的**必要不充分**验证；薄池真成交是后续独立一步。

## 1. 估计量（预注册冻结，禁止看结果改）

全部**以 WETH 原生计价**（不需要也不许猜 USD）。每笔真钱往返记录：

- `entry_slippage_pp` = 真实买入成交价 vs 决策块 QuoterV2 精确报价
- `exit_slippage_pp`  = 真实卖出成交价 vs 出场块 QuoterV2 精确报价
- `realized_net_weth` = 真实(卖出所得 − 买入成本 − 真实 gas − 真实 priority fee)，WETH
- `quote_return`      = 用报价复算的同笔收益（reaudit10 口径）
- `gap = realized_net_return − quote_return` ← **核心产出**
- 真实延迟：`决策时间戳 → 广播 → 上链块 → 成交` 的 ms/块分布；落在 reaudit10 容忍的 2–30 秒前沿内还是外。

## 2. 参数裁定（人类已定，写死）

- **下单尺寸 = 0.001 WETH**（严格镜像 reaudit10 冻结 `base_reaudit10_seconds_preregistration_20260715.json` 的 0.001 WETH；远低于旧 KEYSTONE 的 $100/0.033WETH；也在人类可接受区 0.001–0.01 WETH 的下沿，最安全）。不许放大。
- **ETH/USD 源**：核心估计量**全程 WETH 原生**，不做 USD 换算。仅人类看板需要 USD 时，**读 Base 链上 Chainlink ETH/USD 预言机 @对应 block**取真值，**严禁硬编码 3000 之类猜数**。
- **出场规则**：采用**经过广泛回测的真实 KEYSTONE 出场规则**：`硬止损 −50%` + `距峰值回撤 35%` + `最长持有 7200 块(≈4h)`。**E-window 1800s 明确排除**（回测标注"未回放"，不许自造）。codex 须从冻结的 KEYSTONE 配置**读到确切参数并引用来源**；读不到唯一值就停下问，不许猜。
  - 链路跑通阶段(深池成熟币)还提供一个 `SHAKEDOWN` 短固定持有出场模式(默认 +N 块、可配置)，纯为快速闭环证明管道；真实 KEYSTONE 出场规则也要实现，供日后真信号验证用。两种模式都进预注册冻结。

## 3. 入场前强制安全门

- 每笔入场前必须过 **D5 原子买-卖往返模拟**(eth_call)通过才允许；不过则跳过、记 `SKIP_UNSELLABLE`，不下真钱。
- 每笔真实 tx 广播前必须先 `eth_call` 全量模拟；模拟 revert/异常则不广播。
- 只碰 WETH 计价、白名单 router/DEX；不加杠杆；私钥隔离，artifact 绝不含私钥/RPC URL。

## 4. 规模与止损（人类已改：无总预算，一笔一 arm）

> 人类否决了 $1000 总预算 / 30 笔批量。改为**逐笔**：

- **`PER_ARM = 恰好 1 笔真钱往返`**。agent 未经新的人类 arm，**绝不开第二个仓位**；开仓/平仓后即停、交回人类。
- 单笔最大风险 = 该笔 notional(0.001 WETH，现货买入，亏损上限即本金 + gas)，天然极小，无需累计预算。
- kill-switch(运行时硬校验)保留：模拟 revert、D5 不过、超时未成交、出场后仓位未清零、或检测到已有未平仓位时**拒绝开新仓** → 自动停并如实记录。
- **无 `MAX_TRADES`/`TOTAL_BUDGET`**：节奏由人类逐笔 arm 决定（"还有很多待研究"）。

## 5. 预注册判据（做之前冻结；分"管道二值门"与"逐笔观测旗标"，非批量统计裁决）

**管道二值门(每笔必须全绿，否则算管道 bug)：**
- 广播前必 eth_call 模拟，revert 即不发 —— 干跑须证明会拦。
- D5 原子门未过即不下真钱、记 SKIP。
- 入场在广播后如实记录实际成交块与延迟。
- 出场按规则完成，仓位平到 0，无悬挂 token。
- kill-switch 各触发条件在干跑里逐一证明会触发。
- 已有未平仓位时拒绝开新仓。

**逐笔观测旗标(冻结阈值，供人类逐笔研究；不做批量 PASS/FAIL)：**
- `|entry_slippage_pp|` 或 `|exit_slippage_pp|` > **3.0%** → 旗标"此尺寸/池报价不可靠"。
- 真实"广播→上链"延迟 > **30 秒(≈15 块)** → 旗标"超出前沿预算"。
- `gap = realized_net_return − quote_return` < **−5.0pp** → 旗标"执行吃掉 edge"。
- 深池标的出现 `unresolved`(应恒有真成交) → 旗标"管道 bug"。

**研究说明(非门)**：要在**薄池真成交**上对 +56% 下任何分布结论前，需累计足量薄池样本；本轮深池样本只证管道，不外推。**禁止看结果改尺寸/阈值/止损**，任何调参作废重来。

## 6. 交付物（本轮 codex 只到"干跑+预注册"为止）

1. 链路框架代码（选标的 → 全量模拟 → D5 门 → 广播接口(本轮 DISABLED) → 报价配对记录 → 真实延迟埋点 → 真实 KEYSTONE 出场规则 + SHAKEDOWN 模式 → 平仓对账 → kill-switch 运行时硬校验）。
2. **干跑报告**：`live_broadcast=DISABLED` 下用 archive/最近块把整条链路走通，证明管道正确、各 kill 条件会触发、绝不误发单。
3. 预注册文件 + SHA256（冻结尺寸/出场规则两模式/判据阈值/止损语义/Chainlink 源地址）。
4. **给人类的 ARM 清单**：一页纸，列明真跑前要人类确认的项(钱包、RPC、router 白名单、Chainlink feed 地址、SHAKEDOWN 的 N)，以及"怎么一键 arm 放一笔 / 怎么一键急停 flatten"。
5. 回归测试（模拟 revert 拦截、D5 拦截、报价配对、延迟计算、单笔限制、kill-switch，断言用 ==）。

## 7. 铁律（不可协商）

- 本轮**不发任何真实 tx**：`live_broadcast=DISABLED`，executor/strategy 保持关闭。
- 不自判 GO；`live_allowed`/`self_go_allowed` 恒 false；一次人类 arm 只放一笔。
- 纯只读 + 干跑；链上调用固定 block、记 block hash 与请求/响应 hash；RPC 用 prettiest-blue-aura（orbital 已死）。
- 缺证据不默认全损；报价永不单独充当成交证据；WETH 原生计价，USD 只从链上预言机读。
- 一切可复现，产出 hash。

## 8. 审查 agent 终审保留

干跑+预注册回来后，审查 agent 逐行审：报价对照是否真同块配对、延迟埋点量的是不是真实栈、kill-switch 是不是运行时硬校验、"一笔一 arm"是否真被代码强制。**真钱 arm 的最终建议由审查 agent 出、由人类逐笔拍板，codex 不自我背书。**
