---
sop_applied: true
alpha_id: AR-GL2-B-SMARTMONEY
family: F3/F4/F7
next_gate: COMPLETE_INVENTORY_FORWARD_SIGNALS_THEN_HUMAN_LINE_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-B：账户归因、FIFO 与历史复制复核

结论候选：归因和链上整数对账工程门通过，但聪明钱复制 merit 仍为
`UNTESTED_ELIGIBILITY_CONTAMINATED`。不能据此判断 Base 聪明钱有或没有 edge，
不训练、不 GO。

## 人群与证据边界

- 资格训练人群：blocks `46,389,458..46,417,105` 的 B3 确定性 1,000 tx
  目标-universe 样本；其中高置信账户 932，不代表 Base 全链。
- 前向人群：紧随其后的 blocks `46,417,106..46,444,753`，冻结 17 pools，
  共 4,355 笔真实 swap。
- 账户是可持续跟踪的交易账户/实体，不声称自然人。EOA mismatch 只在 EOA
  子集比较；智能账户只作参考。
- 复制是历史模拟：被观测买入后，取延迟截止后的不同真实 swap 作为 market
  reference，再施加冻结 30 bps adverse；不是我方真实成交、下单或广播。

## 归因与 FIFO 工程门

|检查|结果|
|---|---:|
|EOA `resolved == tx.from`|859/859，mismatch 0.00%|
|EOA trace golden|73/73|
|FIFO target-token historical `balanceOf` raw delta|20/20 整数相等，5 wallets|
|receipt pool WETH cash leg == swap WETH raw|20/20 整数相等|
|无法闭合 WETH 现金腿|69 tx，保持 unresolved|

Nansen 因无授权 key/credits 记为
`NOT_RUN_MISSING_AUTHORITY_AND_API_KEY`；没有购买或伪造。

## 冻结资格的致命污染

阶段一分布后冻结的阈值机械选出 5 个钱包，但其中 2 个有
`oversell_raw > 0`，说明观察窗之前已有库存，训练期 FIFO PnL 左截断。完整库存的
3 个资格钱包在前向窗没有买入信号；4 条前向信号全部来自这 2 个左截断钱包。
所以冻结主组不能回答“合格聪明钱可复制吗”，也不能在看到结果后把人群偷换为
3 个完整库存钱包。

## 前向逐窗描述（污染组，不作 edge 定论）

|延迟|人群|signals|resolved/unresolved|mean|median|p90|max|top1 removed mean|win rate|
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|+1s|冻结聪明钱污染组|4|4/0|-5.3695%|-4.7688%|-2.2000%|-2.2000%|-6.4260%|0%|
|+1s|无条件 baseline|4|2/2|-9.9924%|-9.9924%|-9.4584%|-9.4584%|-10.5264%|0%|
|+5s|冻结聪明钱污染组|4|4/0|-5.3695%|-4.7688%|-2.2000%|-2.2000%|-6.4260%|0%|
|+5s|无条件 baseline|4|2/2|-9.9924%|-9.9924%|-9.4584%|-9.4584%|-10.5264%|0%|
|+15s|冻结聪明钱污染组|4|4/0|-5.6041%|-4.6631%|-3.3497%|-3.3497%|-6.3556%|0%|
|+15s|无条件 baseline|4|2/2|-10.1241%|-10.1241%|-9.4584%|-9.4584%|-10.7898%|0%|

baseline 有 2/4 因无后续真实退出保持 unresolved，缺失没有记全损。样本只有 4
条，且 smart 与 baseline 的 resolved 比例不同；表中均值只是描述数，不是可识别的
相对 EV。

## 冻结与残余

阶段一分布 SHA256 为
`b80d466a71ea4ed1b9b4e4f59a56485e3b0e426fc0d503483e33bf6f315b8f7b`，
早于第二阶段预注册。第二阶段正文内部时间 `13:48:09Z` 是手工错误；只增勘误已
冻结，权威顺序是正文 mtime `13:50:01`、sidecar `13:51:01`，均早于阶段二代码和
收益结果。下一历史门是让完整库存资格账户实际出现前向信号，并完成可比 baseline
定价；Nansen 仍为外部残余。最终分级只由独立审查 agent 决定。
