---
sop_applied: true
alpha_id: AR-GL2-B-SMARTMONEY
family: F3/F4/F7
next_gate: COPY_FORWARD_HISTORICAL_READOUT_THEN_HUMAN_LINE_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-B 阶段二预注册只增勘误

原预注册正文所写 `2026-07-15T13:48:09Z` 是手工记录错误，不作为时序证据，也不回写已冻结正文。权威冻结时序只认文件系统：正文 mtime `2026-07-15 13:50:01.102826534 +0000`，其 SHA256 sidecar mtime `2026-07-15 13:51:01.540868055 +0000`；两者必须早于阶段二代码、查询和结果。

术语勘误：cutoff 后的后续真实 swap 只作为 `TRUE_SWAP_MARKET_REFERENCE_SIMULATED_FILL`，经冻结 30 bps adverse 修正形成历史模拟成交价；它不是我方真实 fill，不代表下单、签名、广播或链上成交。其余阈值、切分、延迟、退出、成本和 unresolved 规则不变。
