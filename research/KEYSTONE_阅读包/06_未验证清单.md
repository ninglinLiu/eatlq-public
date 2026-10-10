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

# GL1 未验证清单（按对挣钱的重要性排序）

| 优先级 | ID | 还不知道什么 | 定案方式 | 类型 |
|---:|---|---|---|---|
| 1 | GL1-02 | KEYSTONE 真钱净 edge | RM1 one-arm one-roundtrip in comparable shallow pool; human arm only | 需极小额真钱（RM1） |
| 2 | GL1-08 | 现实每月盈利 | RM1 realized roundtrips plus forward frequency; then multiply distributions, not point means | 需极小额真钱 + 更多时间 |
| 3 | GL1-06 | 未来信号频率 | more forward time; report weekly/monthly distribution and zero intervals, never only mean | 需更多时间数据 |
| 4 | GL1-23 | 长期日历时间上的 alpha 衰减速度 | more forward/OOS calendar time under frozen selector; report non-overlapping windows and arrival distribution | 需更多时间数据 |
| 5 | GL1-12 | 无条件瞎买的完整经济 EV | historically settle 146 unresolved using five-evidence hierarchy without default loss | 历史可解 |
| 6 | GL1-18 | 任意可能的天气门都无效 | new preregistered external metric with rolling/OOS and same-work random comparator | 历史可解 + OOS |
| 7 | GL1-19 | RH 聪明钱可复制 alpha | historical: close B2 EOA mismatch <=2% and independent FIFO golden | 历史可解 |

任何 OPEN 都不得用最坏值偷偷填成亏光，也不得用最好报价偷偷填成成交。
