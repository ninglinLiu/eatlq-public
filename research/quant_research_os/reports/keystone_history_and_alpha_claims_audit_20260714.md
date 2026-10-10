# KEYSTONE 历史天数与“有 alpha”文档谱系审计

日期：2026-07-14  
性质：只读研究复核；未调用 RPC，未启动服务，未改 live，未作 GO 判断。

## 一、先纠正“102 天 / 40 天”

### 1. “102”从哪里来

B6 把冻结 B3 universe 的两个精确时刻：

- `2026-02-27T05:41:15Z`
- `2026-06-08T18:29:40Z`

先各自截成 UTC 日期，再用 `while d <= end` 逐日生成 `2026-02-27` 至 `2026-06-08`，首尾都算。因此：

- 日期标签数：102；
- 精确时间跨度：101 天 12:48:25，即 101.5336 个 24 小时；
- 所以“102”是**被时间区间触及的 UTC 日历日期数**，不是 102 个完整 24 小时，也不是 102 个连续有市场观测的交易日。

这两个边界来自 B3 被 `block_ts_map` 卡住的 3,331 池 universe：首池恰在 2 月 27 日 05:41:15，末池在 6 月 8 日 18:29:39。若定义是“触及的日期”，首尾包含没有 off-by-one；原报告写成 “102 continuous calendar days” 会误导。

### 2. “40”从哪里来

B6 的 `eligible` 不是“资料齐全”的一般定义，而是以下四个程序条件的交集：

1. 从第一条发行观测起已满 35 天；
2. 前置 28 天发行基线分母非零；
3. 过去 7 天内 B4 fresh-V3 探针 `n >= 20`；
4. ETH 的 D-8 与 D-1 收盘价都存在。

交集恰为 40 个日期，仅有两段：

| 可判日期段 | 天数 |
|---|---:|
| 2026-04-17 至 2026-05-13 | 27 |
| 2026-05-27 至 2026-06-08 | 13 |
| 合计 | 40 |

62 个不可判日期的条件计数会重叠：历史暖机不足 36 天、发行比率为空 9 天、近 7 日 V3 探针不足 52 天、ETH 收盘缺失 8 天。按互斥主因看：前 36 天先被 35 天暖机挡掉；暖机后另有 26 天因探针少于 20 被挡掉（4 月 4–16 日与 5 月 14–26 日各 13 天）。

### 3. 更关键的数据覆盖问题

所谓 `issuance_created_ms` “从 2026-01-23 开始的发行历史”，其时间戳 multiset 与 B3 的 3,331 池 `created_ms` 完全相同，实际最早是 2026-02-27，不是 1 月 23 日。原因是查询虽然写了 1 月 23 日下界，但仍 inner join 到未扩展的 `block_ts_map`，所以更早的 discovered pools 没有进入结果；payload 却无条件写了 `issuance_start=2026-01-23`。

实际 3,331 池只落在 49 个有池日期，102 日期中 53 天为零；主要空洞为：

| 空洞 | 天数 |
|---|---:|
| 2026-03-03 至 2026-03-30 | 28 |
| 2026-04-02 至 2026-04-12 | 11 |
| 2026-05-11 至 2026-05-19 | 9 |
| 2026-05-21 至 2026-05-25 | 5 |

B4 探针又只覆盖 1,265 个 fresh V3 池、47 个创建日期。因此：

- 102 是**稀疏批次数据的时间外包络**；
- 40 是**冻结稀疏代理按门槛可判的日期**；
- 两者都不能表述成“102 天连续市场资料里只有 40 天齐全”。

发行比率把映射空洞当成“当天没有新池”，会产生 0、数百乃至近千的异常比率。B6 的零开门和 FAIL 是当前预注册 adverse 规则下的机械输出，但这套数据不足以证明“真实市场天气门无效”；至少发行天气量的构造效度不成立。

另有一处规格不一致：预注册 JSON 写 `minimum_history_days=28`，代码实际使用且公式实际需要 35 天（近 7 天加此前 28 天）。代码的 35 天较保守，但规格字段应更正。现有测试没有断言 102、40、真实历史首日、连续覆盖率，或发行数组是否独立于 B3 universe。

## 二、历史 alpha 文档应怎样计数

为避免只搜策略名漏掉同一缓存的中文报告，本审计用了两层检索：第一层直接搜 `keystone`，得到 30 份；第二层再搜 `base_lefttail/_labeled_universe/_realgas_replay/p_fatal/delayed_rug/左尾`，取并集后为 54 份，再补入同目录两份直接 PnL/覆盖报告，共 **56 份候选文档**。其中有些是实现记录、预注册或异市场研究，逐份在下文标出，不把它们冒充支持票。

真正给出 KEYSTONE 正 PnL 数字的核心或二级报告约 11 份；它们也不是 11 次独立验证。多数重复使用同一批 131 holdout、217 walk-forward、644 labeled universe、`_priced_live.json` 和 QuoterV2/fork 缓存，只是研究不同门、仓位或出场变体。

## 三、8 份核心正收益证据逐份复核

| 文档 | 当时的正面结论 | 对在哪里 | 错/不足在哪里 | 当前定位 |
|---|---|---|---|---|
| `docs/keystone-go-live.md` | holdout 322→131；mean +59.7%、median +30.5%、WR 68%、fatal 3.8% | TRAIN 定阈值后套 TEST；进场特征在入场前；top1 share 低 | 出场是 QuoterV2/价格路径意义的可报价值，不是按真实方向、容量与时限成交；无缺失 adverse、跨 regime 全门和同口径无条件基线 | 证明同代理口径的筛选能力；不证明可交易绝对正 EV |
| `mvp/audits/keystone_exit_upgrade/verdict_cn.md` | 同 131 中 hold4h mean +65.2%，多种出场仍为正 | 正确发现更紧出场会砍右尾，且没有虚称新规则胜出 | 完全继承 131 的样本和定价口径；是出场法比较，不是独立 alpha 验证 | 相对出场研究有效，绝对收益结论不独立 |
| `mvp/audits/keystone_exit_upgrade/v1_walkforward_cn.md` | Feb–Jun pooled n=217，mean +71.6%、median +37.5% | expanding walk-forward 比单切分更强；文档诚实暴露 May 中位 −2.2%、Jun n=1 | 池化掩盖 regime 异质性；仍使用 Quoter/路径收益；没有真实成交缺失 adverse | 证明模型并非只在一次切分有效，同时已给出 regime 崩坏预警 |
| `scripts/research/base_lefttail/risk_rebacktest_report.md` | n=131，2% haircut 后 mean +54.2%、median +27.2%、月化 +158.5% | 测了 haircut、fatal multiplier 与资金压力 | 阶梯仓位、复利/串行组合模拟不等于 live；月化放大；仍继承 131 可报价收益和幸存条件 | 压力测试有用，月化不能当可实现预期 |
| `mvp/audits/backtest_recompute_20260623/backtest_recompute_cn.md` | 固定 $100、217 笔，总回报 +1453%、单笔均值约 +67% | 正确钉死 131 与 217 身份、收益/MDD 分母和并发口径；主动披露最佳单 +1229% 主导 | 仍把 Quoter/路径 ret 当可实现 PnL；最佳单移除后脆弱；并发最坏可清零 | 算术和身份纠错对，绝对 PnL 外推错 |
| `scripts/research/base_lefttail/gate_sweep_report_20260623.md` | 644 标注集，净月化 +21.9%；depth≥3 后 +21.2%、fatal 2.9%→1.8% | 相同尺子下门槛相对比较有效；正确指出 MIN_PRE、收紧 p_fatal、社会证明并非好旋钮 | p_fatal 对 644 全样本推断；绝对月化本身已标“偏乐观”；没有闭合真实成交和选择缺失 | 深池门的相对风险作用可信，绝对 alpha 不可信 |
| `scripts/research/base_lefttail/capacity_curve_report_20260623.md` | 固定 $100 月利润 $4,330；动态容量天花板约 $28k/月 | 多档报价揭示了滑点非线性和容量拐点 | 文档自己承认是“我方不改变世界”的乐观上界；幸存池、同源报价和样本选择使绝对利润失真 | 容量形状可参考，利润高度/天花板不能承诺 |
| `mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest_cn.md` | live 门样本内月化 +8.6%；F 黑名单挡 262/262 rug | 配置一致性和 QV2/fork 小样本交叉有价值；明确标注 F 是样本内上界 | F deployer 正是从同一 262 rug 挖出，再挡同样本，100% 拦截是循环验证；幸存子集 fork 一致不等于无偏 universe 一致 | 配置回放对，未来防护与正收益外推错 |

## 四、其余谱系文档逐份定位

以下逐份列出，防止把“方法通过”“防线通过”或“二手复述”误计为独立 alpha 票数。

| 文档 | 实际证据与对错分析 |
|---|---|
| `docs/KEYSTONE_CROSS_MARKET_AGENT_BRIEF.md` | 复述 Base n=131/+60% 与 WF217；明确说跨链不得假定复现。作为交接对，作为新增证据不是。 |
| `docs/keystone-manual-runbook.md` | 操作手册；不产生收益证据。把它当 alpha 验证是类别错误。 |
| `mvp/audits/alert_and_position_stress_20260622/D_position_stress_lookahead_cn.md` | 正确证明特征/时序无直接前视，并指出 live 训练无 holdout、组合相关尾部高；“无前视”不等于“有收益”。 |
| `mvp/audits/alert_and_position_stress_20260622/B_alert_coverage_cn.md` | 告警覆盖审计；只证明部分运行安全路径，不证明 alpha。 |
| `mvp/audits/disaster_left_tail_audit_20260622/blacklist_mintable_validation_cn.md` | n=131 上过滤器 0 删单、收益不变；正确结论是纯保险/历史 no-op，不是新增正收益。 |
| `mvp/audits/disaster_left_tail_audit_20260622/disaster_left_tail_audit_cn.md` | 防线框架审计，正确指出 −50% 触发不保证成交、真实可滑到 −95%；引用 131 只作背景，反而暴露执行尾部。 |
| `mvp/audits/disaster_left_tail_audit_20260622/onchain_truth_reconciliation_cn.md` | 纠正钱包归属并确认当前 executor 没有策略 swap；是对“已有 live alpha”的反证。 |
| `mvp/audits/execution_mev_latency_audit_20260622/execution_mev_latency_audit_cn.md` | 正确指出无 KEYSTONE 预测↔成交账本、gas/MEV/延迟未闭合、Quoter 同源盲区；是风险审计，不是收益验证。 |
| `mvp/audits/exit_logic_lookahead_20260624/report_cn.md` | 正确证明出场循环无直接前视，并以 real-gas 发现 167 个 rug 赢家中 57% 距撤池不足两分钟；错在该 167 本身是“rug 且盈利”的条件样本，不能证明总体 EV。 |
| `mvp/audits/exit_logic_lookahead_20260624/report_smartexit_cn.md` | 正确证伪“巧刀可免费降 fatal”：现有特征分不开，显著降 fatal 必须砍超过半数右尾；是负面约束。 |
| `mvp/audits/exit_logic_lookahead_20260624/trail_frontier_cn.md` | train/holdout 与 bootstrap 做得对；发现相邻 trailing 阈值不可区分、30min 硬顶才有真差异。样本仍是 167 rug-winner + 95 clean-winner 的条件拼接，不是无条件收益。 |
| `mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md` | 会话汇编，重复 131/217/644 和风险讨论；不应作为独立实验计票。 |
| `mvp/audits/go_live_session_20260624/go_live_session_full_cn.md` | 同上，是决策过程记录，不是新数据。其价值是保留当时 caveat。 |
| `mvp/audits/honeypot_rootcause_20260623/honeypot_rootcause_cn.md` | 关键反证：state override/Quoter 可绕过真实 transfer 授权分支，历史 5.1% fatal 系统性偏低；正确推翻“报价可卖=真钱包可卖”。 |
| `mvp/audits/keystone_origin_search_20260625/origin_search_cn.md` | 正确还原 131、217、PF9.34 的来源，并指出更早真实成本负 EV 证据；是证据谱系索引，不是新增 alpha。 |
| `mvp/audits/multi_chain_feasibility/validation_plan_cn.md` | 尚未执行的验证计划；不能支持任何已存在 alpha。 |
| `mvp/audits/stress167_20260624/conversation_round_stress167_cn.md` | real-gas 确认 167 个已选 rug-winner 的准点利润，同时证明对卖出延迟极敏感；条件化样本不能升级为总体正 EV。 |
| `mvp/audits/aggressive_live_rebacktest_20260625/aggressive_live_rebacktest_cn.md` | 报 314 笔、月化约 $1,755、fatal 1%，但 207 笔是已知 delayed rug；报告自己标为 644 已知结局的 in-sample 上界，月度“补全”还把少数有池日线性外推，6 月 n=0 又用 5 月速率外推。相同幸存子集上 QV2=fork 是对的，绝对/月化外推不成立。 |
| `mvp/audits/aggressive_live_rebacktest_20260625/alpha_decay_decomposition_cn.md` | 正确拆出 edge 质量先坏、机会后消失：rug mean 1 月 +127%→4 月 +10%，5 月 rug 为 0，clean 也转负。它是旧正结论失效的前置证据，不是新支持。 |
| `mvp/audits/aggressive_live_rebacktest_20260625/depth_ge3_share_monthly_cn.md` | 正确说明 5 月深池占比断崖至 8.5%；是供给/regime 证据，不独立证明收益。 |
| `mvp/audits/aggressive_live_rebacktest_20260625/ex_top1_monthly_pnl_cn.md` | 声称去每月 Top1 后仍约 $1,274/月；计算仍建立在同一乐观成交/补全体系，说明“不是只靠每月第一名”但不能证明可成交绝对 EV。 |
| `mvp/audits/exit_logic_lookahead_20260624/conservative_portfolio_audit_cn.md` | 报 $500 钱包约 $1,122/月，并明确称回测上界、真实成交 0；正确纠正“47%复利”其实只是利润/钱包比例。正收益高度仍继承幸存子集与代理尺子。 |
| `mvp/audits/dimension_reconcile_20260624/qv2_vs_fork_reconciliation_cn.md` | 224 个幸存池上 99.5% 一致、共同可卖池滑点中位 −0.60%。这个子集结论对；其样本条件正是“能进入历史定价集合”，不能据此排除 H0a/缺失池上的结构盲区。B4 后来把两者分开。 |
| `mvp/audits/fastrug_survivorship_20260623.md` | 正确揭示 644 构建有两层幸存偏差，并把威胁量级从“3 个”纠正为 45 个全链确认、20 个过 p_fatal；支持风险存在，不支持正 EV。 |
| `mvp/audits/honeypot_defense_v2_BC_20260623.md` | 644 多点 replay 正确发现 delayed rug 与 flip 时序；“旧回测只漏 1 个”的结论只对 p-gated 候选和既有 replay 可测集合，不能当无偏 instant-hp 患病率。 |
| `mvp/audits/cluster_blacklist_fp_validation_20260624/skeleton_false_positive_validation_cn.md` | 正确推翻“89% 模板覆盖即可全拉黑”：安全零误杀骨架只覆盖 38.2%；是防线精度研究，不是 alpha。 |
| `mvp/audits/funding_cluster_poc_20260624/deployer_funding_cluster_cn.md` | deployer 样本内零误杀、funder 不宜直接封禁的工程结论合理；仍是同 262 rug 的样本内防线，不是未来收益证据。 |
| `mvp/audits/golive_aggressive_20260625/report_cn.md` | 上线/配置与回滚记录；引用前述正回测但没有新增历史样本。后续真实 PnL 不能由这份操作报告证明。 |
| `reports/B5_keystone_cha_reverify_20260714.md` | 更强识别标准下的核心反证：真实 decoded swap 方向/容量/时限、缺失 adverse、冻结成本后 full pooled −4.61%，top1 移除 −13.95%；但相对无条件仍 +51.67pp。 |
| `reports/keystone_revival_professional_20260714.md` | B6 原报告；机械 0/40 与八门 FAIL 可复现，但把 102 写成连续日、把发行历史写成 1/23 起均不准确，天气量构造效度不足。 |
| `reports/keystone_revival_人话版_20260714.md` | 对 B5 的“相对会挑、绝对仍亏”解释正确；B6 天数/资料齐全叙述继承了专业版覆盖错误。 |
| `scripts/research/base_v4/_phase5_handoff_report.md` | Base V4 Initialize 进场无正 EV，明确 `v4_alpha_candidate_no`；不能支持 V3 KEYSTONE，只是异市场负证据。 |
| `scripts/research/sol_keystone/_keystone_solana_handoff_report.md` | Sol TEST 剪左尾 −3.4% vs 基线 −4.3%，仅相对少亏；executable −8.0%。支持“排序可能迁移”，反对“绝对正 alpha 迁移”。 |
| `scripts/research/sol_keystone/_sol_autopsy.md` | 旧 Sol 线死因审计，结论是可部署策略离线也无边；负证据。 |
| `mvp/audits/disaster_left_tail_audit_20260622/live_universe_vs_backtest_alpha_cn.md` | 关键反证：真实 17/17 全是 AERO，与 131 微盘池重叠 0%；说明所谓 alpha 从未由当时 live universe 实际交易验证。 |
| `mvp/audits/honeypot_defense_P0_20260623.md` | 防貔貅实现/历史漏检审计；历史“只漏 1/131、1/217”受上游筛选与同源代理约束，不能估计无偏患病率。B4 后来确认 H0a 仍 INCONCLUSIVE。 |
| `mvp/audits/honeypot_defense_impl_EFdepth_20260623.md` | 工程实现并复述 +21.9%→+21.2%、fatal 2.9%→1.8%；不是独立收益样本。 |
| `scripts/research/base_v4/_v3_v4_autopsy_report.md` | V3/V4 对照性尸检；可帮助解释边为何不迁移，但不新增 V3 KEYSTONE 正收益证据。 |

### 关键词命中但不构成 KEYSTONE 正收益证据的其余文件

| 文档 | 定位 |
|---|---|
| `mvp/audits/alert_and_position_stress_20260622/fixes_changelog_cn.md` | 告警/仓位修复记录；无新增收益样本。 |
| `mvp/audits/disaster_left_tail_audit_20260622/config_fix_changelog_cn.md` | live 风控配置变更记录；引用回测出单量，不验证收益。 |
| `mvp/runs/run-004-patch-g2-pool-invariant-feature-redesign.md` | 更早特征工程运行记录；不属于 131/217 的执行闭合证据。 |
| `reports/B4_quoterv2_vs_atomic_sellsim_preregistration_20260714.md` | 预注册文件，只定义检验，不是结果。 |
| `reports/B4_v2_arm_a_20260714.md` | 揭示 delayed LP-pull 与幸存结构；是后验纠偏，不是正收益主张。 |
| `reports/B4_v2_arm_b_20260714.md` | 无偏 fresh-V3 探针发现 confirmed instant-hp 24.19%、W2 74.51%；是旧盲区的反证。 |
| `scripts/research/base_deepsurvivor/_deepsurvivor_handoff_report.md` | 另一条深池/长存活策略，结论 `deep_edge_no`；不能给 KEYSTONE 投支持票。 |
| `scripts/research/base_v4/_caution_policy_draft.md` | V4 风控草案；不是 V3 KEYSTONE 收益证据。 |
| `scripts/research/base_v4/_phase4_handoff_report.md` | V4 阶段交接；异研究对象。 |
| `scripts/research/sol_grad_audit/_pump_lowcost_phase_c_readout.md` | Solana pump 研究，结论右尾不可交易；异市场负证据。 |
| `scripts/research/sol_grad_audit/_pump_tail_realgas_replay_plan.md` | 尚未执行/阶段计划，不是结果。 |
| `scripts/research/sol_grad_audit/_pump_tail_realgas_replay_readout.md` | Solana real-gas 结果，不能证明 Base KEYSTONE。 |

## 五、统一结论：以前到底“对”了什么，“错”了什么

### 对的部分

1. **排序/剪左尾能力真实存在于现有样本。** B5 更严口径下，无条件 pooled 为 −68.02%，friction-only 为 −50.06%，KEYSTONE full 为 −4.61%；相对无条件改善约 +63.41pp，B5 报告按其冻结对照给出的条件 alpha 为 +51.67pp。模型确实比乱买少亏很多。
2. **研究流程并非全是前视作弊。** 多份审计证明入场特征在 signal 前、训练/测试按时间切、出场 trailing 使用 running peak。旧结果不是简单的未来函数 bug。
3. **若只比较同一代理尺子，部分相对结论成立。** 例如深池门比 MIN_PRE/社会证明更能降低浅池 fatal，紧硬顶会用右尾换安全，May/regime 衰减确实存在。
4. **旧文档其实留下了正确预警。** May 崩盘、最大赢家主导、Quoter 乐观、F 黑名单样本内循环、167 个赢家贴近撤池、live 与 backtest universe 零重叠，都在仓库里写过。

### 错的部分

1. **把“价格路径上可报价”升级成“我们真实可成交”。** QuoterV2 能算池数学，不等于真钱包能通过 token transfer hook，也不保证按真实买卖方向、容量、时限成交。
2. **对缺失证据不够 adverse。** 旧口径往往只在有报价/有路径/幸存池上算收益；B5 将无入场或无合格卖出证据按 −100% 并扣成本后，绝对均值翻负。
3. **把相对 alpha 和绝对正 EV 混为一谈。** 从 −68% 改善到 −4.61% 是很强的选择能力，但仍不是可独立做多的正期望。
4. **没有让 regime、top-winner 和 OOS 风险拥有否决权。** W1 +16.06%，W2 −18.48%，W3 欠功率且约 −1%；top1 移除后 pooled −13.95%。旧池化正数不能覆盖这些失败。
5. **把重复缓存的多份报告误当多次独立验证。** 131、217、644 反复出现，本质是同一证据树上的分支，不是独立样本投票。

因此，最精确的历史修正不是“以前所有 alpha 都是假的”，也不是“B5 把旧 217 算术推翻了”，而是：

> 旧研究大体证明了 KEYSTONE 在一个乐观、同源的报价/路径估值体系中有显著的相对排序和剪左尾能力；它没有证明在真实方向、容量、时间限制、执行成本和缺失 adverse 下具有可交易的绝对正 EV。B5 保留了前一结论，推翻了后一升级。

## 六、复现入口

```bash
cd /opt/EATLQ

# 直接名称命中文档
rg -l -i 'keystone' --glob '*.md' \
  --glob '!**/node_modules/**' --glob '!**/.venv/**' . | sort

# B6 日期与覆盖来源
sed -n '45,125p' scripts/research/base_b6/b6_regime_gate.py
jq '.weather_by_day, .data_coverage' \
  mvp/diagnostics/base_b6_keystone_regime_gate_20260714.json

# B5 严格执行口径
jq '.performance, .preregistered_right_tail' \
  mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json
```

本审计没有修改 B6 代码或既有报告；以上是证据审计与口径更正。
