---
sop_applied: true
alpha_id: AR-GL3-CLOSE-GL12
family: F1/F4/F7
next_gate: PRESERVE_HISTORICAL_IRON_WITHOUT_GO
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL3 人话版增量摘要

这次把 GL1-12 真正收口了：在完全同源的 entry+1、trigger+1/+5/+15 秒级口径下，瞎买 431 个同龄候选的完整宇宙 EV 即使用对剩余未知最乐观的主赋值，三窗仍约为 -30% 到 -31%；KEYSTONE 冻结 116 个子集则约为 +52% 到 +57%，所以它不是搭了正 beta 的顺风车，而是在负 beta 背景上留下至少 +83pp 的条件 alpha。全区间事件与真储备重核把 411 个残余确认成持续不可卖，另有 43 个 cell 因数据或严格失败证据不足继续保持 unresolved，没有默认全损。独立审查最终给 `GL1-12=IRON`，但这只钉住冻结历史有界事实：ClickHouse 的 Mint/Burn 解码覆盖和 RPC ledger 元数据仍有限，历史原子报价也不是真实成交；因此 `GO=NOT_MADE`，不训练、不进 canary/live。
