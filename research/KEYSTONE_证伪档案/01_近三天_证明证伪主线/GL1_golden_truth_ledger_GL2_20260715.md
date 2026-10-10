---
sop_applied: true
alpha_id: AR-GL1-GOLDEN-TRUTH-GL2
family: F0/F1/F3/F4/F6/F7
next_gate: RESOLVE_REMAINING_OPEN_ITEMS_UNDER_FROZEN_RULES
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL1 黄金事实账本 · GL2 增量更新

本文件不覆盖原 GL1；它以独立逐行终审为准，机械替换 GL1-12/18/19 的答案、锚点和残余。三条均维持 OPEN，GO 未作出。

|主张|GL2 分级|本轮可固定事实|仍缺什么|
|---|---|---|---|
|GL1-12|OPEN|resolved-only 三窗 beta=3.31%/3.51%/3.94%，条件 alpha=56.53/57.66/58.23pp；不能外推完整 431|Pre-freeze the authoritative trigger estimand and prove E4 non-recovery over the whole interval for residual rows, or provide valid partial-identification bounds without imputation.|
|GL1-18|OPEN|5 门中 2 个进入 OOS，0 个稳定胜；其中 1 个无功效，3 个未测试|Build the three missing point-in-time exogenous sources and obtain powered OOS exposure for pool creation, then rerun the already-frozen comparator rules.|
|GL1-19|OPEN|EOA 0/859 mismatch、target/WETH golden 20/20；前向 4 条全来自左截断钱包，merit 未测试|Qualify complete-inventory wallets only, obtain a powered forward sample and a comparably resolved unconditional baseline, then rerun the frozen delayed-copy replay.|

全账本分级计数：`{'IRON': 6, 'OPEN': 7, 'PINNED': 10}`。原账本 SHA256=`60d515fb4566fa348259f7f3260cee9b7c8aec1e8e5947e0e6d8fceeea16727e`；独立审查 SHA256=`179af06333d03bcebe4d7e87adb20cea1faf9353f0d3042aff5b985c6266cf87`。

完整逐行理由见 `reports/GL2_independent_line_review_20260715.md`；机器账本见 `mvp/diagnostics/base_gl1_golden_truth_ledger_gl2_20260715.json`。不训练、不下单、不签名、不广播、不修改 live/signals/Redis。
