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

# GL1 项目终局真相账本（候选分级，待审查 agent 逐行终审）

权威口径：秒级区块偏移；缺失保留未知；真成交优先，否则明确写原子守卫报价；只有五证据确认且不恢复的 rug 才记全损；真储备/真持仓；WETH 计价；全宇宙和 KEYSTONE 子集绝不混写。**本轮新增 RPC=0，GO=NOT_MADE。**

## 总账

| ID | 主张 | 分级 | 权威口径下答案 | 历史为何翻转 | OPEN 怎么定 |
|---|---|:---:|---|---|---|
| GL1-01 | 历史秒级 KEYSTONE 选中样本有正的回放 edge<br><small>KEYSTONE selected; 0.001 WETH; frozen WETH gas/cost model; entry +1; exit +1/+5/+15 blocks; real sell first, else guarded atomic quote; confirmed rug=-100%; unresolved retained</small> | **PINNED** | {"formal_status": "INCONCLUSIVE because unresolved != 0", "frontier": {"1:1": {"evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 107, "CONFIRMED_LP_RUG": 2, "REAL_SELL": 4, "UNRESOLVED_EXIT": 3}, "mean": 0.5663701436516096, "median": 0.2597229826746521, "n_resolved": 113, "n_total": 116, "n_unresolved": 3, "top1_removed_mean": 0.48288116031956935}, "1:15": {"evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 101, "CONFIRMED_LP_RU… | 30-minute vs seconds; missing-as-loss vs unresolved; proxy vs evidence hierarchy | — |
| GL1-02 | KEYSTONE 真钱净 edge<br><small>same strategy and 0.001 WETH, but actual entry+exit fills, gas and broadcast latency</small> | **OPEN** | "没有真钱往返，报价不能单独证明成交利润" | historical replay was repeatedly phrased as realized tradability | RM1 one-arm one-roundtrip in comparable shallow pool; human arm only |
| GL1-03 | 历史出场延迟前沿<br><small>entry +1 fixed; exit +1/+5/+15 blocks; same evidence hierarchy</small> | **PINNED** | {"interpretation": "2–30 seconds range did not decay monotonically; this does not prove future latency tolerance", "plus_1": {"evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 107, "CONFIRMED_LP_RUG": 2, "REAL_SELL": 4, "UNRESOLVED_EXIT": 3}, "mean": 0.5663701436516096, "median": 0.2597229826746521, "n_resolved": 113, "n_total": 116, "n_unresolved": 3, "top1_removed_mean": 0.48288116031956935}, "plus_15": {"evidence": {"… | t0+600/1800s feature or exit horizon was mislabeled as execution latency | — |
| GL1-04 | 历史入场延迟前沿<br><small>entry +1/+5/+15 blocks; exit +1 fixed</small> | **PINNED** | {"plus_1": {"evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 107, "CONFIRMED_LP_RUG": 2, "REAL_SELL": 4, "UNRESOLVED_EXIT": 3}, "mean": 0.5663701436516096, "median": 0.2597229826746521, "n_resolved": 113, "n_total": 116, "n_unresolved": 3, "top1_removed_mean": 0.48288116031956935}, "plus_15": {"evidence": {"ATOMIC_GUARDED_EXACT_QUOTE": 107, "CONFIRMED_LP_RUG": 2, "REAL_SELL": 4, "UNRESOLVED_EXIT": 3}, "mean": 0.53048808… | entry feature window and transaction inclusion delay were conflated | — |
| GL1-05 | 历史 KEYSTONE 信号频率是爆发型而非稳定均匀<br><small>116 selected signals only; natural UTC calendar weeks/months; zero weeks included</small> | **PINNED** | {"active_days": 27, "daily_count_histogram_on_active_days": {"1": 12, "2": 7, "4": 1, "8": 1, "9": 1, "12": 1, "13": 2, "15": 1, "16": 1}, "daily_timeline_nonzero": [{"date": "2026-03-01", "n": 13}, {"date": "2026-03-02", "n": 15}, {"date": "2026-03-03", "n": 8}, {"date": "2026-03-04", "n": 2}, {"date": "2026-04-01", "n": 9}, {"date": "2026-04-02", "n": 12}, {"date": "2026-04-03", "n": 13}, {"date": "2026-04-04", … | 131/217 trade counts from different samples were divided into a misleading average | — |
| GL1-06 | 未来信号频率<br><small>future unseen Base market under frozen selector</small> | **OPEN** | "历史 distribution cannot identify future arrival rate" | backtest counts were presented as a monthly forecast | more forward time; report weekly/monthly distribution and zero intervals, never only mean |
| GL1-07 | 容量/尺寸对历史报价 edge 的侵蚀<br><small>0.001/0.003/0.010 WETH; n=76 quote-success cohort; QuoterV2 incremental impact only</small> | **PINNED** | {"curve": [{"evidence": "base mixed real-sell/guarded-quote + quote-only size delta", "median_incremental_return_impact": 0.0, "projected_seconds_net_return": 0.5663701436516096, "quote_n": 76, "size_weth": 0.001, "worst_incremental_return_impact": 0.0}, {"evidence": "quote-only size delta; not a realized 0.003 WETH fill", "median_incremental_return_impact": -0.0002176521683803223, "projected_seconds_net_return": … | old USD capacity extrapolated survivor quotes into fills and stable frequency | — |
| GL1-08 | 现实每月盈利<br><small>future count × size × actual fills; WETH native</small> | **OPEN** | {"mechanical_scenarios": [{"historical_model_net_weth": 0.0005663701436516097, "notional_weth": 0.001, "signals_per_month_assumption": 1, "warning": "机械情景，不是未来月盈利预测；未含真钱执行误差。"}, {"historical_model_net_weth": 0.0028318507182580484, "notional_weth": 0.001, "signals_per_month_assumption": 5, "warning": "机械情景，不是未来月盈利预测；未含真钱执行误差。"}, {"historical_model_net_weth": 0.005663701436516097, "notional_weth": 0.001, "signals_pe… | $4.3k/$28k combined optimistic frequency, USD, quote, survivor and size assumptions | RM1 realized roundtrips plus forward frequency; then multiply distributions, not point means |
| GL1-09 | QuoterV2 reasonable quote掩盖原子严重不可卖的假阳性率<br><small>fresh 1,265-pool probe; 2-minute measured disease cases n=306; exact atomic roundtrip comparator</small> | **PINNED** | {"disagreement": 0, "identification_warning": "unknown prevents claiming universal zero", "n": 306, "unknown_disease": 2, "wilson95": [0.0, 0.012398593345761188]} | survivor exit snapshot 1/224 and unbiased entry disease question were different estimands | — |
| GL1-10 | W2 全人群高度有毒<br><small>full W2 n=408; atomic sell failure at 2m/10m</small> | **IRON** | {"ten_minute": [311, 408, 0.7622549019607843], "two_minute": [304, 408, 0.7450980392156863]} | full W2 toxicity was confused with selected KEYSTONE W2 returns | — |
| GL1-11 | KEYSTONE 选中的 W2 历史秒级表现为正<br><small>selected W2; +1/+1 blocks; resolved n=60; unresolved 1</small> | **PINNED** | {"alpha_delta": 0.30517340036285995, "mean": 0.5407906579197636, "top1_removed_mean": 0.46901919800344766, "unresolved": 1} | ecological fallacy: full W2 was substituted for selected subset | — |
| GL1-12 | 无条件瞎买的完整经济 EV<br><small>all 431 candidates; +1/+1; same evidence hierarchy</small> | **OPEN** | {"resolved_only_mean": 0.013701194529929463, "resolved_only_n": 285, "unresolved": 146, "warning": "resolved-only mean is not the full-universe EV"} | -68% imputed missing as loss; +1.37% conditions on resolved; neither identifies full EV | historically settle 146 unresolved using five-evidence hierarchy without default loss |
| GL1-13 | 五证据 rug 裁定<br><small>262 delayed-rug candidates; mechanics + pre/post atomic + real sell + later recovery + cutoff</small> | **IRON** | {"CONFIRMED_LP_RUG": 235, "TEMPORARY_UNSELLABLE": 1, "UNRESOLVED": 26, "confirmed_share": 0.8969465648854962, "unresolved_not_loss": true} | quote failure/label was previously treated as rug without recovery and mechanics proof | — |
| GL1-14 | B3 2-minute acceleration signal density<br><small>frozen 3,331 pools; fresh token; true pool-era transfers/reserves/holders</small> | **IRON** | {"W1": 17, "W2": 0, "W3": 0, "seconds_reprice_measured_W1": 15, "seconds_unresolved_W1": 2} | proxy 64 signals and true 17 signals were mixed; V2 rows were coerced into V3 | — |
| GL1-15 | D5 对历史 KEYSTONE 选中样本的收益边际<br><small>all +1/+3/+5/+10/+15 entry-exit matrix cells</small> | **IRON** | {"blocked": 0, "mean_delta": 0.0, "safety_role": "still mandatory pre-entry guard; zero historical marginal != useless in future"} | a mandatory safety gate's role was confused with observed incremental alpha | — |
| GL1-16 | forward LP monitor historically rescued losses<br><small>24 measured forward cases; block-level archive monitor</small> | **PINNED** | {"prewarning_among_real_loss": 0, "prewarning_total": 2, "real_loss": 4, "rescue_proven": 0} | theoretical pending-mempool possibility was stated as archive-proven rescue | — |
| GL1-17 | 当前冻结天气门能挑对时机<br><small>causal exogenous derived gate; 55 OOS eligible days; compare same-open-share random gates</small> | **PINNED** | {"beats_random_p95": false, "open_days": 2, "selected_trades_on_open_days": 0} | 102 calendar envelope, 40 eligible repair days, regime labels and post-hoc W1/W2/W3 were mixed | — |
| GL1-18 | 任意可能的天气门都无效<br><small>all future causal exogenous gates</small> | **OPEN** | "one frozen gate failed; universal claim is not identified" | failure of one threshold was over-generalized | new preregistered external metric with rolling/OOS and same-work random comparator |
| GL1-19 | RH 聪明钱可复制 alpha<br><small>point-in-time attributed EOAs, true holdings, FIFO PnL, costed exits</small> | **OPEN** | "UNTESTED; attribution and independent FIFO/on-chain golden blocked" | recipient/transfer proxy and repeated wallet activity were treated as true copyable holdings | historical: close B2 EOA mismatch <=2% and independent FIFO golden |
| GL1-20 | RM1 禁广播框架与硬停在干跑中生效<br><small>fixed block 42,702,284; 0.001 WETH shallow pool; live_broadcast disabled</small> | **IRON** | {"broadcast": false, "read_only_rpc_attempts": 15, "real_tx_hashes": [], "send_calls": 0, "signing": false} | dry-run pipeline proof and realized execution evidence were not separated | — |
| GL1-21 | 当前可以作 GO<br><small>all GL1 evidence</small> | **IRON** | {"go": "NOT_MADE", "live_allowed": false, "self_go_allowed": false} | positive historical edge was mistaken for authorization | — |
| GL1-22 | 102 天、40 天和 55 天分别是什么<br><small>UTC calendar envelope vs original weather eligibility intersection vs corrected OOS eligibility</small> | **PINNED** | {"calendar_dates_inclusive": 102, "corrected_oos_eligible_days": 55, "exact_elapsed_days": 101.53362268518518, "meaning": "102 is an envelope, 40 was a rule intersection, neither means 102 fully observed trading days", "original_eligible_days": 40, "original_segments": [["2026-04-17", "2026-05-13", 27], ["2026-05-27", "2026-06-08", 13]], "requested_end": "2026-06-08T18:29:40Z", "requested_start": "2026-02-27T05:41… | calendar dates, active pool dates, feature-complete dates and OOS dates were all called 'days' | — |
| GL1-23 | 长期日历时间上的 alpha 衰减速度<br><small>future performance decay under a frozen selector, distinct from 2–30 second execution latency</small> | **OPEN** | {"historical_observation": "later rolling windows became sparse and weaker; overlapping windows and selection history do not identify a stable decay law", "not_same_as": "seconds latency frontier"} | seconds-scale execution decay and months-scale market drift were treated as one decay question | more forward/OOS calendar time under frozen selector; report non-overlapping windows and arrival distribution |

## 历次量化主张（只记录当时怎么说，不拿旧文档给当前答案背书）

| 主张 | 日期 | 当时数值 | 当时口径 | 出处 |
|---|---|---|---|---|
| KEYSTONE edge | 2026-06 | n=131, mean +59.7%, median +30.5%, win 68%, fatal 3.8% | single holdout; old sell simulator/exit assumptions | `docs/keystone-go-live.md` |
| KEYSTONE edge | 2026-06 | n=131, hold4h mean +65.2% | same holdout; exit-upgrade replay | `mvp/audits/keystone_exit_upgrade/` |
| KEYSTONE edge | 2026-06 | n=217, mean +71.6%, median +37.5% | monthly walk-forward; old proxy execution | `scripts/research/base_lefttail/_set217_forkreplay.json` |
| KEYSTONE edge | 2026-06 | n=131, 2% haircut mean +54.2%, median +27.2% | old holdout; fixed 2% haircut | `scripts/research/base_lefttail/_risk_rebacktest_report.json` |
| KEYSTONE edge | 2026-07-14 | KEYSTONE full -4.61%; W1 +16.06%, W2 -18.48%, W3 about -100% | 30-minute exit; missing/adverse treated conservatively, B5 | `mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json` |
| KEYSTONE edge | 2026-07-15 | +1/+5/+15 exit mean +56.64%/+56.59%/+57.69%; top1-removed +48.29%/+48.13%/+49.40% | entry +1 block; seconds; real sell first then guarded atomic quote; confirmed rug only; unresolved 3/4/5 retained | `mvp/diagnostics/base_reaudit10_final_20260715.json` |
| blind-buy baseline | 2026-07-14 | mean -68.02% | B5 30-minute/missing-as-adverse convention | `mvp/diagnostics/base_b5_keystone_cha_reverify_20260714.json` |
| blind-buy baseline | 2026-07-15 | resolved 285/431, mean +1.37%, unresolved 146 | reaudit +1 block; unresolved excluded from point estimate | `reports/reaudit_10_suspect_conclusions_result_20260715.md` |
| QuoterV2 disagreement | 2026-07-14 | historical exit snapshots: 1/224 unique pools | survivor/known-exit sets; not unbiased instant-hp prevalence | `mvp/diagnostics/base_b4_arm_a_20260714.json` |
| QuoterV2 disagreement | 2026-07-15 | 0/306 measured 2m disease cases; Wilson 95% upper 1.24%; unknown disease 2 | unbiased fresh probe; QV2-reasonable vs atomic severe disagreement | `mvp/diagnostics/base_reaudit10_final_20260715.json` |
| W2 toxicity | 2026-07-15 | 2m 304/408=74.51%; 10m 311/408=76.23% | full W2 cohort; atomic roundtrip sell failure | `mvp/diagnostics/base_reaudit10_final_20260715.json` |
| KEYSTONE W2 | 2026-07-15 | resolved n=60 mean +54.08%; conditional alpha +30.52pp; unresolved 1 | KEYSTONE-selected W2; +1/+1 block seconds | `mvp/diagnostics/base_reaudit10_final_20260715.json` |
| rug prevalence | 2026-07-14 | delayed rug 262: LP-pull 260, transfer-toggle 2 | B4 candidate label; coarse historical class | `mvp/diagnostics/base_b4_arm_a_20260714.json` |
| rug adjudication | 2026-07-15 | confirmed LP rug 235; temporary unsellable 1; unresolved 26 | same 262 candidates; five-evidence/no-recovery adjudication | `mvp/diagnostics/base_reaudit10_final_20260715.json` |
| capacity | 2026-06 | fixed $100 about $4,330/month; dynamic ceiling about $28k/month | old survivor proxy, USD/gas/frequency assumptions | `scripts/research/base_lefttail/capacity_curve_report_20260623.md` |
| capacity | 2026-07-14 | 0.003/0.010 WETH median incremental impact -0.0218pp/-0.0979pp; n=76 each | QuoterV2 diagnostic only; not realized fills | `mvp/diagnostics/base_b8_honest_execution_20260714.json` |
| weather gate | 2026-07-14 | derived gate appeared to revive/kill depending data coverage | 102-day envelope but only 40 eligible days in early repair | `reports/keystone_revival_professional_20260714.md` |
| weather gate | 2026-07-15 | opens 2/55 OOS days; KEYSTONE n=0; does not beat same-work random gate | frozen causal exogenous gate; seconds exit fronts | `mvp/diagnostics/base_reaudit10_final_20260715.json` |
| B3 acceleration | 2026-07-14 | true signals W1=17, W2=0, W3=0 | 3,331-pool frozen universe; fresh tokens; true reserve/holder | `mvp/diagnostics/base_2min_accel_b3_v1_20260714.json` |
| RH smart-money | 2026-07-14 | UNTESTED | EOA attribution mismatch and FIFO/on-chain golden blocked | `mvp/diagnostics/base_rh1_smartmoney_20260714.json` |

## 频率时间分布（禁止用单一均值代替）

自然周（含零周）：`2026-02-23=13, 2026-03-02=25, 2026-03-09=0, 2026-03-16=0, 2026-03-23=0, 2026-03-30=50, 2026-04-06=0, 2026-04-13=3, 2026-04-20=5, 2026-04-27=6, 2026-05-04=6, 2026-05-11=5, 2026-05-18=2, 2026-05-25=1`。

周计数直方图（信号数→周数）：`{0: 4, 1: 1, 2: 1, 3: 1, 5: 2, 6: 2, 13: 1, 25: 1, 50: 1}`；月份：`[{'month': '2026-03', 'n': 38}, {'month': '2026-04', 'n': 61}, {'month': '2026-05', 'n': 17}]`；有信号日计数直方图：`{1: 12, 2: 7, 4: 1, 8: 1, 9: 1, 12: 1, 13: 2, 15: 1, 16: 1}`。

## 容量与现实月收益

| 尺寸 WETH | 报价 n | 相对 0.001 的中位收益侵蚀 | 最坏侵蚀 | 诊断性历史净收益 | 证据限制 |
|---:|---:|---:|---:|---:|---|
| 0.001 | 76 | 0.00% | 0.00% | 56.64% | base mixed real-sell/guarded-quote + quote-only size delta |
| 0.003 | 76 | -0.02% | -0.14% | 56.62% | quote-only size delta; not a realized 0.003 WETH fill |
| 0.010 | 76 | -0.10% | -0.65% | 56.54% | quote-only size delta; not a realized 0.010 WETH fill |

旧的 `$4,330/月`、`$28k/月` 不是权威答案。现实月收益仍为 OPEN；机器 JSON 给出 1/5/10/20/38/61 笔与三种尺寸的机械 WETH 情景，明确不是预测，也不做无依据 USD 换算。

## 锚点

每条 claim 的完整文件 SHA256、说明和 OPEN 解决方法均在机器 JSON；本 Markdown 不重复隐藏长 hash。分级为 Codex 候选，终审权保留给审查 agent。
