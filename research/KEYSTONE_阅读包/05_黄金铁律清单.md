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

# GL1 黄金铁律清单（IRON + PINNED，候选终审版）

下列事实仅在各自行内口径成立；不允许删掉口径后泛化。

- **GL1-01 · PINNED · 历史秒级 KEYSTONE 选中样本有正的回放 edge**：KEYSTONE selected; 0.001 WETH; frozen WETH gas/cost model; entry +1; exit +1/+5/+15 blocks; real sell first, else guarded atomic quote; confirmed rug=-100%; unresolved retained。答案见机器账本；主要翻转根因：30-minute vs seconds; missing-as-loss vs unresolved; proxy vs evidence hierarchy

- **GL1-03 · PINNED · 历史出场延迟前沿**：entry +1 fixed; exit +1/+5/+15 blocks; same evidence hierarchy。答案见机器账本；主要翻转根因：t0+600/1800s feature or exit horizon was mislabeled as execution latency

- **GL1-04 · PINNED · 历史入场延迟前沿**：entry +1/+5/+15 blocks; exit +1 fixed。答案见机器账本；主要翻转根因：entry feature window and transaction inclusion delay were conflated

- **GL1-05 · PINNED · 历史 KEYSTONE 信号频率是爆发型而非稳定均匀**：116 selected signals only; natural UTC calendar weeks/months; zero weeks included。答案见机器账本；主要翻转根因：131/217 trade counts from different samples were divided into a misleading average

- **GL1-07 · PINNED · 容量/尺寸对历史报价 edge 的侵蚀**：0.001/0.003/0.010 WETH; n=76 quote-success cohort; QuoterV2 incremental impact only。答案见机器账本；主要翻转根因：old USD capacity extrapolated survivor quotes into fills and stable frequency

- **GL1-09 · PINNED · QuoterV2 reasonable quote掩盖原子严重不可卖的假阳性率**：fresh 1,265-pool probe; 2-minute measured disease cases n=306; exact atomic roundtrip comparator。答案见机器账本；主要翻转根因：survivor exit snapshot 1/224 and unbiased entry disease question were different estimands

- **GL1-10 · IRON · W2 全人群高度有毒**：full W2 n=408; atomic sell failure at 2m/10m。答案见机器账本；主要翻转根因：full W2 toxicity was confused with selected KEYSTONE W2 returns

- **GL1-11 · PINNED · KEYSTONE 选中的 W2 历史秒级表现为正**：selected W2; +1/+1 blocks; resolved n=60; unresolved 1。答案见机器账本；主要翻转根因：ecological fallacy: full W2 was substituted for selected subset

- **GL1-13 · IRON · 五证据 rug 裁定**：262 delayed-rug candidates; mechanics + pre/post atomic + real sell + later recovery + cutoff。答案见机器账本；主要翻转根因：quote failure/label was previously treated as rug without recovery and mechanics proof

- **GL1-14 · IRON · B3 2-minute acceleration signal density**：frozen 3,331 pools; fresh token; true pool-era transfers/reserves/holders。答案见机器账本；主要翻转根因：proxy 64 signals and true 17 signals were mixed; V2 rows were coerced into V3

- **GL1-15 · IRON · D5 对历史 KEYSTONE 选中样本的收益边际**：all +1/+3/+5/+10/+15 entry-exit matrix cells。答案见机器账本；主要翻转根因：a mandatory safety gate's role was confused with observed incremental alpha

- **GL1-16 · PINNED · forward LP monitor historically rescued losses**：24 measured forward cases; block-level archive monitor。答案见机器账本；主要翻转根因：theoretical pending-mempool possibility was stated as archive-proven rescue

- **GL1-17 · PINNED · 当前冻结天气门能挑对时机**：causal exogenous derived gate; 55 OOS eligible days; compare same-open-share random gates。答案见机器账本；主要翻转根因：102 calendar envelope, 40 eligible repair days, regime labels and post-hoc W1/W2/W3 were mixed

- **GL1-20 · IRON · RM1 禁广播框架与硬停在干跑中生效**：fixed block 42,702,284; 0.001 WETH shallow pool; live_broadcast disabled。答案见机器账本；主要翻转根因：dry-run pipeline proof and realized execution evidence were not separated

- **GL1-21 · IRON · 当前可以作 GO**：all GL1 evidence。答案见机器账本；主要翻转根因：positive historical edge was mistaken for authorization

- **GL1-22 · PINNED · 102 天、40 天和 55 天分别是什么**：UTC calendar envelope vs original weather eligibility intersection vs corrected OOS eligibility。答案见机器账本；主要翻转根因：calendar dates, active pool dates, feature-complete dates and OOS dates were all called 'days'

审查 agent 可降级或改级；Codex 不自我背书、不作 GO。
