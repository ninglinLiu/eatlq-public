---
sop_applied: true
alpha_id: AR-GL2-A-UNIVERSE-EV
family: F1/F4/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-A 预注册：431 个同龄候选的五证据完整宇宙 EV

## 冻结时点与研究边界

本文件在读取、聚合或重算 431 个候选逐笔收益以及 146 个 unresolved 的逐笔证据之前冻结。GL1 账本已经公开的汇总数（431、285 resolved、146 unresolved、KEYSTONE 116 及其既有汇总）只用于定义待解决问题，不用于改变下列规则。冻结后阈值、证据优先级、分母、窗口和缺失处理均不得因结果好坏而修改；如实现发现规则不可执行，只能把对应行保留 `UNRESOLVED` 并记录原因，不能改规则救结果。

纯历史、只读重核；不下单、不签名、不广播、不写 Redis/live/signals，不启动 executor，不训练，不作 GO。若需要 RPC，只允许仓库既有 `prettiest-blue-aura` 只读端点，产出不含 URL/凭据的调用 ledger。

## 冻结人群与时间轴

- **完整宇宙**：GL1-12 已冻结的 431 个同龄候选；不得因能否定价、池是否存活、是否被 KEYSTONE 选中而删行。每个候选只出现一次，身份键沿用上游冻结键。
- **KEYSTONE 子集**：同一 431 人群、同一事件窗口中由既有冻结选择器选中的行；不得重跑或调选择阈值。它只是完整宇宙的子集，不另换日期、池龄或候选来源。
- **计价**：原生 WETH，名义买入 0.001 WETH；gas/成本沿用 GL1 秒级回放的冻结 WETH 成本模型。不得换算 USD。
- **事件时点**：候选首次可观测事件所在块为 `b0`；现实入场为 `b0+1`，只能使用截至 `b0` 已知的信息决定选择。
- **退出窗口**：逐窗固定为入场块后的 `+1/+5/+15` 块，即 `b0+2 / b0+6 / b0+16`。主比较为 `+1/+1`；三窗必须分别报告，禁止用一列平均替代。
- **持仓**：以 `b0+1` 的精确原子买入模拟/链上可验证余额变化得到真实 token 整数持仓；禁止用 transfer recipient、固定 token 数或现价反推持仓。无法得到入场持仓则该窗为 `UNRESOLVED_ENTRY`，不记损失。
- **同窗同龄比较**：每个窗口的 KEYSTONE 均值与完整宇宙均值必须来自同一候选来源、同一 `b0`、同一入场/退出偏移、同一证据层级、同一成本和同一 unresolved 政策。

## 冻结证据优先级

每个候选、每个退出窗只允许以下互斥最终类别，按顺序取最高级可用证据：

1. `REAL_SELL`：目标账户或可证明持有同一 token 的链上账户，在该目标退出块或之后的首个合资格真实 swap 卖出；使用真实 token 输入整数和真实 WETH 输出整数。若不能证明成交对应本策略持仓规模，仅把真实成交用于证明“可卖/恢复”，金额仍由下一层同块原子报价确定，类别标为 `ATOMIC_GUARDED_EXACT_QUOTE_REAL_SELL_CONFIRMED`。
2. `ATOMIC_GUARDED_EXACT_QUOTE`：在固定历史块，对 `b0+1` 买得的真实整数持仓执行单次原子 round-trip sell simulation/quote，要求调用成功、输出整数非零、池/路由/token 一致，并记录该证据是原子报价而非成交。批量、`latest`、spot/reserve 比值、首个 transfer recipient 均不得代替。
3. `CONFIRMED_NONRECOVERING_RUG`：只有下列“五证据”规则全部满足才记 `-100%`；即使缺少报价也不能自动落入本类。
4. `UNRESOLVED_*`：证据冲突、历史状态/RPC不可得、持仓不可得、路由语义不明或未满足五证据时保留未知；不进入点估计分子，也不进入已定价分母，但始终保留在 431 总分母的计数里。

真实成交优先于原子报价；报价永远显式标注、永远不声称为 fill。任何较晚证据只可用于裁定退出能否成交、恢复或 rug，不得回灌 `b0` 的 KEYSTONE 选择。

## 146 个 unresolved 的冻结五证据裁定

对 GL1 已知 146 行逐行、逐窗收集以下字段；不得只按 pool/token 批量贴标签而跳过行级检查。

### E1 机制（mechanism）

必须存在可复核链上机制证据，且发生在入场之后：

- LP rug：目标池真实 WETH 储备或可用流动性相对入场前最后可用块下降至少 95%，并由 Mint/Burn/Collect/transfer/trace 中至少一个直接资金动作解释；单纯低成交量、价格下跌、quote revert 不算机制。
- transfer-toggle/honeypot：同一真实持仓在机制前可原子卖、机制后确定性 revert，并有 token bytecode/state/call trace 或后续相同输入族交易的确定性失败支持；仅第三方标签不算。
- 其他机制不得自行新增为“确认 rug”；只能记 `UNRESOLVED_MECHANISM_OTHER` 并披露。

### E2 前后原子可卖（pre/post atomic sellability）

对 `b0+1` 买得的真实 token 整数持仓，在机制前最近可用历史块和每个目标退出块执行完全相同的原子卖出调用。必须保存 block、input/output 整数、success/revert 分类和路由。确认 rug 要求机制前成功且机制后目标窗失败或经济输出归零；历史状态缺失、RPC错误或输入不一致均为未知，不得当失败。

### E3 后续真成交（later real trade）

扫描目标退出块之后至冻结截止的同池/同 token 真实卖出 swap，校验 swap 方向、token 实际转入和 WETH 实际转出。任何合资格后续真实卖出都证明该时点可卖，并优先作为恢复/可卖证据；router 自转、mint/burn、transfer-only、买单和零输出不算卖出。

### E4 恢复（recovery）

从首次不可卖/储备坍缩之后扫描到冻结截止。出现任一合资格真实卖出，或对同一真实持仓规模原子卖出重新成功且输出非零，即 `RECOVERED_OR_TEMPORARY`，禁止记 `-100%`。恢复发生在目标退出窗之后时，该窗仍按当时可得证据定价/未知，但 pool-level rug 类别不得写“永久不恢复”。

### E5 截止（cutoff）

证据扫描截止固定为 GL1 历史包的请求终点 `2026-06-08T18:29:40Z`（对应块由只读 RPC/现有块时间索引确定并写入结果）；不得为单个池延长到今天或因为后来恢复而改变截止。若候选太接近截止，不能同时获得目标 `+15` 块及合理恢复观察，则相应窗保留 `UNRESOLVED_RIGHT_CENSORED`。

### 确认全损的合取规则

仅当 `E1=true AND E2.pre=true AND E2.post_failure=true AND E3.no_later_real_sell_through_cutoff=true AND E4.no_recovery_through_cutoff=true AND E5.observation_complete=true` 时，最终类别为 `CONFIRMED_NONRECOVERING_RUG`，净收益严格 `-1.0`。任一项 false 或 unknown 均不得记全损。机制成立但后来恢复记 `RECOVERED_OR_TEMPORARY`；无法确定则保留具体 `UNRESOLVED_*`。

## 冻结收益与比较公式

- 已定价单笔净收益：`(exit_weth_integer - entry_weth_integer - frozen_cost_weth_integer) / entry_weth_integer`；确认且未恢复 rug 固定 `-1.0`。所有整数原值写机器结果，浮点仅作展示。
- **含确认 rug 版**：纳入 `REAL_SELL`、两类 guarded atomic quote 和 `CONFIRMED_NONRECOVERING_RUG`；残余 unresolved 不插补。
- **排确认 rug 版**：从上述已定价行中仅排除确认 rug，明确这是 survivor-conditioned 对照，不是完整经济 EV。
- 每窗报告：`n_total=431`、证据类别计数、`n_resolved`、`n_confirmed_rug`、`n_unresolved`、均值、中位数、分位数 `p05/p25/p50/p75/p95`、胜率、总净 WETH；KEYSTONE 子集同表。
- 条件 alpha：`mean(KEYSTONE resolved under window/evidence policy) - mean(full-universe resolved under same window/evidence policy)`。同时报告 resolved 覆盖差；若任一人群仍有 unresolved，结论标为部分识别，不把点估计伪称 431 完整 EV。
- beta 搭乘量（只作同口径分解）：`beta_component = full_universe_mean`；`selection_alpha_component = KEYSTONE_mean - full_universe_mean`；恒等式 `KEYSTONE_mean = beta_component + selection_alpha_component`。另报告占 KEYSTONE 均值的带符号比例，但 KEYSTONE 均值接近零时比例记 `NA`。
- rug/排rug两版均逐窗做同一分解；不得拿一个窗口的 KEYSTONE 减另一个窗口的全宇宙。

## 冻结输出与失败规则

- 机器 JSON 必须含全部 431 行的稳定键、三窗结果、五证据字段、整数金额、证据来源/块号、未决原因、RPC错误与 cohort 标识；可另用行级 JSONL，主 JSON 给 hash。
- RPC ledger 只写 method、block/tx 类别、attempt/success/error counts、endpoint alias=`prettiest-blue-aura`，绝不写 URL/token。
- 报告必须把点估计、识别区间/残余未知和 survivor-conditioned 排rug版分开；不得用“完整 EV”称呼仍有 unresolved 的点估计。
- 任何因 RPC/archive/本地证据不足无法落地的行继续 unresolved；不得用 `-1`、`0`、第一个 recipient、最新块报价或最优报价补齐。
- 本任务只给供审查 agent 逐行定级的候选事实，不自行把 GL1-12 升为 PINNED/IRON，不作 GO。

