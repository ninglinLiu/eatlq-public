---
sop_applied: true
alpha_id: AR-GOLDEN-TRUTH-LEDGER
family: F1/F4/F6/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL1 冲突根因诊断表

| 冲突主张 | 历次表面翻转 | 真正变动的口径 | 统一裁定 |
|---|---|---|---|
| KEYSTONE 活/死 | +59.7/+71.6 → -4.61 → 秒级 +56.64 | 执行延迟；缺失处理；证据优先级；样本配置 | 这些是不同估计量；当前历史秒级=PINNED，真钱=OPEN |
| 瞎买 -68%/+1.37% | -68.02 → resolved-only +1.37 | 缺失=-100% vs unresolved 排除 | 两者都不是 431 全体 EV；完整 EV=OPEN |
| QuoterV2 诚实/不诚实 | 1/224 vs 0/306 | 幸存者退出集 vs 无偏入场疾病集 | 不同人群/问题，不是直接矛盾 |
| W2 有毒/KEYSTONE W2 赚钱 | 304/408 不可卖 vs selected n60 +54.08% | 全 W2 vs KEYSTONE 子集 | 全域毒性和选择器避毒可同时成立 |
| rug 262/235 | 粗标签 262 vs confirmed 235+temporary1+unknown26 | 标签 vs 五证据分类/恢复检查 | 只有 235 可确认全损 |
| 天气门有效/无效 | 102天/40天/55 OOS天 | 日历外壳、资料齐备交集、修复后 OOS eligibility | 当前冻结门未胜随机；不能外推所有门 |
| 容量 $28k/未知 | 旧 USD 动态上限 vs 0.001–0.010 WETH quote curve | 频率、USD、gas、幸存者、报价=成交 | 报价尺寸侵蚀已钉；实现容量和月钱 OPEN |
| 速度能否救 rug | 理论抢跑 vs archive rescue=0/4 | pending 可观测性 vs canonical block hindsight | 历史没有救援证据；mempool 可能性仍 OPEN |
| D5 有用/零边际 | 安全必需 vs blocked=0 | 防护职责 vs 已选样本增量收益 | 两句可同时成立 |
| alpha 衰减/未衰减 | 2–30秒不衰减 vs 后期窗口稀疏/弱 | 毫秒延迟曲线 vs 日历时间漂移 | 短延迟历史稳定；未来/长期衰减 OPEN |
| RH 聪明钱 alpha | 早期转账/钱包代理正面叙事 vs UNTESTED | 接收地址 vs EOA/真持仓/FIFO | 未闭合归因前不得作 edge 结论 |

结论：大多数所谓‘翻烧饼’不是同口径复算不稳定，而是问题、分母、时间、缺失处理或成交证据换了却没写。今后任何脱离 cohort、block offset、evidence class、missing policy、size/gas 的数字，视为没有结论。
