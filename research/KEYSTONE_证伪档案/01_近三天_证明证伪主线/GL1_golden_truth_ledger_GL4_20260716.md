---
sop_applied: true
alpha_id: AR-GL1-GOLDEN-TRUTH-GL4
family: F0/F1/F4/F7
next_gate: HUMAN_ACCEPTANCE_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL1 黄金事实账本 · GL4 实盘增量

独立终审已落：`GL1-02=PINNED / FAIL_AT_OBSERVED_TINY_LIVE_SCALE`，新增 `GL1-24=PINNED`；`GL1-01=PINNED` 与 `GL1-12=IRON` 保留但强制限定为历史、非实盘、非前瞻。`GO=NOT_MADE`。

|条目|最终 grade|答案|严格边界|
|---|---|---|---|
|GL1-02 · KEYSTONE 真钱净 edge|PINNED / FAIL|3 个 receipt 闭环严格 realized `-0.000011451891750166` WETH，0/3 胜|实际配置/规模与 .001 WETH GL3 estimand 不同；不同不能把负实盘改判 PASS|
|GL1-24 · 实盘真实战绩|PINNED|11 成交腿；8 买本金 `0.169919978298458736` WETH；5 未卖仓末窗 mark 后总经济 `-0.160235862229391189` WETH|未卖仓 cash-realized=null，economic mark 不得叫 realized|
|GL1-01 · +56 frontier|PINNED 保留|冻结历史 selected frontier|quote/guarded atomic，非 live realized|
|GL1-12 · 历史 alpha/beta 区间|IRON 保留|431/116 冻结区间与符号分离|不是 +56 已真钱实现，不是 forward guarantee|

全账本分级计数：`{'IRON': 7, 'OPEN': 5, 'PINNED': 12}`。GL3 基线 SHA256=`99f70bdaec1bb2ac076c52aead9b35078678b301ece88ed38c90211a0790635e`；GL4 truth SHA256=`2dde9b71f8cb4ab18d3787bd1c991ef0777b1e3d35da384f47d1c93275148064`；独立审查 SHA256=`9d53d6191e7e705bc107574041617ff7b3c5a994fd5ed575beb0cc43a6f42785`。

最终对质：+56 没有被证明为历史算术错误，但真钱/forward 外推已失败；aggressive 207-rug 只保留样本内算术，forward/live=`OPEN / NOT_GO_EVIDENCE`。

机器账本：`mvp/diagnostics/base_gl1_golden_truth_ledger_gl4_20260716.json`。独立审查：`reports/GL4_independent_line_review_20260716.md`。不训练、不下单、不签名、不广播、不修改 live/signals/Redis。
