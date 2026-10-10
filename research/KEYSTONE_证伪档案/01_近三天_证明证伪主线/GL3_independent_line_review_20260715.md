---
sop_applied: true
alpha_id: AR-GL3-CLOSE-GL12
family: F1/F4/F7
review_role: independent_read_only
final_grade: IRON
go_decision: NOT_MADE
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL3 · GL1-12 独立逐行终审

本审查只读取 GL3 预注册、实现、机器结果、RPC/ClickHouse 证据、测试以及 GL1/GL2 锚点；没有修改预注册、机器结果、实现、GL1 账本或 registry。最终裁决：**`GL1-12=IRON`**。这只钉住冻结历史人群与冻结秒级 estimand 下的结论：无条件瞎买的 EV 严格为负，而 KEYSTONE 相对同窗同龄全宇宙的选择 alpha 下沿仍为正；**不判 GO，不等于真钱 edge，也不授权训练、canary 或 live。**

## 逐项裁决

|审查项|裁决|独立核对结果|
|---|---|---|
|预注册时序与 SHA|PASS|正文 SHA256=`fdbeccb0a4a1b17ccda0bb3e8779608804d051a8e61f89d4021cd5fc5b88a0bf`，sidecar 一致；文件时间 14:32:19/14:32:27 UTC，早于 inventory 14:49:32、pool evidence 14:51:40、result 14:55:43。文件时间只能证明工件顺序，不能证明人的认知状态，但未发现结果先于冻结的工件证据。|
|estimand 同源|PASS|431/116 人群固定；逐 cell 独立断言 `target_block=trigger_block+N`，N=1/5/15，0 个偏差；机器口径为 `entry_block+1`，没有 `b0+2/+6/+16`。|
|残余人口|PASS|冻结 GL2 的 149/152/153 个 unresolved 精确进入裁定；最终只机械改写 411 个原 unresolved cell，原 resolved cell 未变。|
|全区间事件门|PASS WITH LIMITATION|3,336 个连续、每片不超过 5,000 blocks 的 RPC `eth_getLogs` 分片全部成功；411 个确认 cell 自各自 target 至 cutoff 的 raw Mint/Burn/Swap 事件数均为 0。9 个池出现 CH/RPC 缺口；相关 25 个 cell 保持 unresolved。另有同池 +5/+15 两 cell 的唯一缺失 Burn 已发生在各自 target 之前，逐 cell 后区间键集合相等，因此没有错误降损。|
|CH 解码表系统性缺口|PASS AS CONSERVATIVE INPUT, NOT AS INDEPENDENT COMPLETENESS ANCHOR|全扫描中 CH 只有 11,287 个 Swap，Mint/Burn 均为 0；RPC 有 12,231 Swap、146 Mint、393 Burn，`RPC-CH=944/146/393`。因此 CH 空集不能独立证明没有 Mint/Burn。实现没有把全局不等强补为一致，并在逐 cell 有冲突时留 unresolved；411 个 `-100%` 的全区间事件完整性实际依赖 raw RPC，不把 CH 当第二个完整索引。结果报告的“全区间双源”措辞偏强，应按本段限制理解。|
|真储备整数门|PASS|625/625 checkpoint 的 WETH/target `balanceOf(b)-balanceOf(b-1)` 与同块原始 Transfer net 整数相等；每个确认 cell 均含 target 与 cutoff checkpoint，0 个 pool data gap。该门验证检查点和事件边界，不声称逐块读取每一个中间 balance。|
|411 个 `-100%` 门|PASS|逐 cell 重算：411/411 均为退出块 atomic measured 且 `sell_ok=false/recovery_ratio=0`、exact-size quote 明确 execution revert、raw RPC 后区间无 Mint/真实 sell、reserve 门全绿、cutoff 可取。上游五证据 E1 mechanism flag 为 407 true/4 false；4 个边界项仍有前置 Burn/Collect/receipt 边界、同仓位严格失败与后区间 raw 无 Mint/Swap，不违反冻结的“无 Mint 即无 LP 回流边界”规则。没有单凭 cutoff 报价定损。|
|回流与 delayed exit|PASS/NOT EXERCISED|所有覆盖全绿的 cell 均无候选恢复块；有候选或证据缺口的路径不会强补，代码显式加入 `candidate_retest_required_but_not_executed` 并保留 unresolved。本次没有 delayed-exit 定价样本，故该价格路径未被数据实跑验证。|
|缺失保守处理|PASS|43 个 cell 继续 unresolved，`net_return/exit_weth_raw` 保持 null；原因包括 event-key mismatch 25、exit atomic 不满足严格失败 39、quote 不满足明确 revert 36（可重叠），没有把数据缺口默认成全损。|
|有界 EV|PASS|独立从 431/116 行复算：残余悲观=-1，主乐观=各 cohort/window resolved median，p75 只作敏感性；分母固定。全宇宙三窗区间分别 `[-32.41%,-30.08%]`、`[-32.99%,-30.99%]`、`[-32.96%,-30.98%]`，均严格小于 0。|
|alpha/beta 算法|PASS|逐窗使用 `KEYSTONE_pessimistic-full_median_optimistic` 与 `KEYSTONE_median_optimistic-full_pessimistic`；三窗 alpha 区间为 `[84.41%,88.94%]`、`[83.83%,86.91%]`、`[83.36%,86.44%]`，下沿均远高于预注册 +10pp 门。beta 是负的全宇宙区间，故 KEYSTONE 的正历史结果不是搭乘正 beta；JSON 的占比按带符号口径输出。|
|结论对 411 裁定敏感性|PASS|额外把 GL3 新确认项全部退回 GL2 unresolved 后，冻结同样 bounds 的全宇宙乐观界仍为 -1.03%/-0.99%/-0.74%，KEYSTONE 悲观界仍为 +54.33%/+52.84%/+52.38%。因此即使不采纳 411 个 `-100%`，核心符号与 alpha 下沿仍成立。|
|RPC ledger 与只读姿态|PASS WITH LIMITATION|5,211/5,211 evidence request hashes 与 ledger 一一对应；方法仅 `eth_getLogs` 3,961、`eth_call` 1,250，无 send/sign/broadcast，endpoint alias 为 prettiest-blue-aura。ledger 行本身没有预注册要求的 block/range/purpose 字段；这些字段只能通过同一 request hash 回连 inventory/pool evidence 恢复。审查将其记为可复核但 schema 不完整，不视为 GO 证据。|
|回归测试|PASS|`python3 -m unittest tests/test_base_gl3_gl12.py -v`：7/7 通过；另做了 target、原行不变性、411 门、bounds、ledger 反向引用的独立断言，均为 0 mismatch。|

## 最终分级与边界

`IRON` 的对象是一个已经被有界识别钉住的历史事实，而不是精确点估计：在冻结的 431 个同龄 Base 候选、0.001 WETH、`entry+1 / trigger+1,+5,+15` 和冻结证据层级下，无条件瞎买的 EV 上界仍为负，KEYSTONE 相对全宇宙的条件 alpha 下界仍显著为正。残余 15/14/14 个 full-universe cell 与 2/1/1 个 KEYSTONE cell 不跨越结论边界。

限制仍需保留：CH Mint/Burn 解码表在本区间没有完整性，不能作为“无事件”的独立真值；RPC ledger 元数据 schema 不完整；atomic/exact quote 是历史模拟证据，不是我方真实成交；本轮没有恢复候选，所以 delayed-exit 分支未实跑；结论没有未来/OOS、真钱成交或 GO 含义。`GO=NOT_MADE`。

## 审查锚点

- 预注册：`fdbeccb0a4a1b17ccda0bb3e8779608804d051a8e61f89d4021cd5fc5b88a0bf`
- GL3 result：`e42ac92d29e75fa1ad4fbd12e25365a1672f4aeeef50f6edffa84bd1fe5a32a9`
- event inventory：`0d2f5d0479b0271097be723d4b28c61b452126304d5c5a6d11c3ed7dd2f38550`
- pool interval evidence：`3425cea3a93671eb4e610f71bbc8cf7fc60ce8a48a6babeced863f4f375c716f`
- full RPC ledger：`125c172eebffe81198692aad4b49818e98e43a51bc8354f5654b25724b70d3d4`
- frozen GL2 universe：`966cf597c837070a40a3cce9c2defcdf11f25230c97adb93c69892bb62c5f197`
- reaudit10 core：`e62e0f37bb6a2756256795bfaa0d5f9677b73e5e8d0836370ebf1afbdbbd5e2d`

