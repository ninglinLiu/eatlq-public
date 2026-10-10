---
sop_applied: true
alpha_id: AR-GL1-GOLDEN-TRUTH-GL3
family: F0/F1/F4/F7
next_gate: PRESERVE_GL1_12_HISTORICAL_BOUND_AND_RESOLVE_OTHER_OPEN_ITEMS
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL1 黄金事实账本 · GL3 增量更新

本文件不覆盖原 GL1/GL2；它只按独立审查结果机械替换 GL1-12。独立审查最终定级 `GL1-12=IRON`；`GO=NOT_MADE`。

|主张|GL3 最终分级|冻结历史答案|残余与限制|
|---|---|---|---|
|GL1-12|IRON|全宇宙 beta +1/+5/+15=[-32.41%,-30.08%]/[-32.99%,-30.99%]/[-32.96%,-30.98%]；条件 alpha=[+84.41%,+88.94%]/[+83.83%,+86.91%]/[+83.36%,+86.44%]|仍有 full 15/14/14、KEYSTONE 2/1/1 个未决；CH Mint/Burn 解码不完整，RPC ledger 元数据不完整；历史报价不是实盘成交，未判 GO。|

全账本分级计数：`{'IRON': 7, 'OPEN': 6, 'PINNED': 10}`。GL2 基线 SHA256=`e9447d626f5607647da21b2446e9c339e8143d172fc86b47fdb234ecc6bce6d6`；GL3 独立审查 SHA256=`658155a81f8db5f89f7f6045026008fe8735ca21526abd7da9d868b29203fdb5`。

完整逐行理由见 `reports/GL3_independent_line_review_20260715.md`；机器账本见 `mvp/diagnostics/base_gl1_golden_truth_ledger_gl3_20260715.json`。不训练、不下单、不签名、不广播、不修改 live/signals/Redis。
