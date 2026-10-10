---
sop_applied: true
alpha_id: AR-GL4-LIVE-RECON
family: F0/F1/F4/F7
next_gate: HUMAN_LINE_ACCEPTANCE_AND_GL1_LEDGER_INTEGRATION_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
  substantive_blocker: false
---

# GL4 独立逐行终审

终审结论：**核心账与裁定通过，附四项不改变方向的限制；`substantive_blocker=false`，`GO=NOT_MADE`。** 我没有修改主体账本、预注册、实现、测试、GL1 或 registry，也没有新增 RPC 调用。

## 独立复算结果

|项目|独立结果|终审|
|---|---:|---|
|窗口内 outgoing tx|73|PASS|
|真实成功 swap 腿|11 = 8 BUY + 3 SELL|PASS|
|累计买入本金|0.169919978298458736 WETH|PASS|
|严格 receipt 闭环 PnL|-0.000011451891750166 WETH；0/3 胜|PASS|
|最坏闭环|-0.000004994235519901 WETH|PASS|
|失败卖出|48 次 status=0；gas 0.000163143764 WETH|PASS，均不是成交|
|5 个未卖仓经济 PnL|-0.160224410337641023 WETH|PASS，均保持 `cash_realized=null`|
|receipt 闭环 + 末窗 executable mark|-0.160235862229391189 WETH|PASS，不能改称 realized|
|RPC ledger|360 行；357 ok / 3 quote revert；仅 5 种只读方法|PASS|
|回归测试|8/8|PASS|

逐笔用 raw `tx.value`、`Withdrawal`、`gasUsed × effectiveGasPrice`、approval gas 和失败 receipt gas 重算，浮点账本与整数/Decimal 重算差小于 `2e-17 WETH`。11 条 accepted legs 全部具备 `status=1 + Swap + wallet-relevant transfer`；73 条 outgoing 中不存在被 hardcode 名单漏掉但带 Swap event 的成功交易。三个 RPC error 是三笔 PARADAENGI 的末窗 exact-path `eth_call` revert，不是失败成交，也不是 sell receipt。

## 逐项审查

|被审项|结果|裁定 / limitation|
|---|---|---|
|预注册与 SHA|PASS|SHA=`6cff…7b7`；quote、approval、status=0、dry-run 均不能进 fill|
|全量语料 manifest|PASS with limitation|633 文件的固定树、指定 sprint、`mvp/runs` 与 realized-PnL 路径无缺项，逐文件 SHA 复核通过；manifest 证明全字节读取，不等于机械证明人类理解了每一行；602 文件平行通读没有单独路径 sidecar|
|钱包与 fill 完整性|PASS with limitation|对语料明确的两个钱包穷尽；不能证明不存在未披露第三钱包|
|严格 realized 账|PASS|只认 3 个真实 SELL receipt；approval gas 已分配；3 笔全负|
|未卖仓标记|PASS|5 仓仍完整持币且池 WETH 坍塌≥99%；JOTCHUA/BOT 保留正的末窗报价，未默认全损；三 PARA 只因显式 exact-path revert 记零|
|quote 是否冒充 fill|PASS|末窗报价只出现在 mark；无 sell receipt 的 `cash_realized_pnl_weth=null`|
|gas / USD oracle|PASS|成功腿、approval、失败 attempt 分离；Chainlink `updatedAt` 均不晚于对应 receipt timestamp|
|开盘/暂停/重启时间线|PASS with limitation|主要节点有白纸锚；6/29 后诚实标 UNKNOWN；6/24 F 扩容的精确时刻缺失|
|实际配置逐段 pin|PASS|现已拆出初始 `2 deployers + 1 skeleton`、后续 `245+33+2`、6/25 historical F OFF + trail .25；初始 skeleton 删除/重建转折仍只能 bounded/contested|
|“策略没变过”|FAIL 该原主张|entry pickle 在已核区间可稳定，但 F、trail、size、E 与 risk gates 改过；可执行策略显然改变|
|aggressive 207 rugs|PASS with qualification|原报告白纸写明 207 rug 盈利是 in-sample 核心上界；06-29 真亏反驳其 forward/live 外推，但该池不属于历史 207，不能单凭一笔推翻 207 行的历史算术|
|归因 waterfall|PASS with limitation|序列化恒等式 exact；rug principal-minus-mark 为 -0.160056815171 WETH，未知执行/regime/择时留 residual；但冻结预注册写的是严格 realized unit，主体 D 实际是补充 economic-mark waterfall。严格 realized 的五个未卖 rug 不能进 realized rug 桶|
|+56 对质|PASS with taxonomy correction|不同模型、size、exit、gates、日期，故未推翻冻结历史 estimand；却已推翻把它说成真钱/前瞻 edge 的外推。约 +56% frontier 属 GL1-01 PINNED；GL1-12 IRON 是区间/符号分离，不是“+56 已真钱实现”|
|可复现与 tests|PASS with limitation|SHA 和 8/8 `assertEqual` 通过；RPC JSONL 只存 request/response hash，离线可复算账本字段，但要独立再生响应 hash 仍须只读重跑|
|安全边界|PASS|只接受进程注入的 `prettiest-blue-aura` endpoint，不读真实 `.env`；无 send/sign/broadcast、Redis、executor 或 signals 写路径|

## 最终 grade

|GL1 条目|最终 grade / outcome|严格 scope|
|---|---|---|
|GL1-01|**PINNED，保留**|历史 KEYSTONE selected frontier；含 guarded atomic/quote 与 unresolved；非真钱 realized|
|GL1-02|**PINNED / FAIL_AT_OBSERVED_TINY_LIVE_SCALE**|本月实际部署的 live/canary 配置与两个证据钱包；3 个严格闭环全负，5 个未卖坍塌仓另作经济标记。原先“同策略、0.001 WETH”精确 forward estimand 仍未测试，但这不能把已观察实盘改判 PASS|
|GL1-12|**IRON，保留但强制限缩**|431/116 冻结历史区间与 alpha/beta 符号分离；非 live、非 realized、非 forward guarantee|
|建议新增 GL1-24|**PINNED**|“两证据钱包实盘真实战绩”：11 腿；8 买本金 0.169919978298458736 WETH；3 闭环严格 realized `-0.000011451891750166 WETH`；5 未卖仓的补充经济标记使总经济 PnL `-0.160235862229391189 WETH`。两个数必须分栏，绝不能合称 realized|

因此，+56% 的终审表述是：**没有被 GL4 推翻为历史算术错误；被强制限缩为有界历史 estimand，任何真钱或前瞻解释都已在观察尺度失败。** aggressive 的 207-rug 结论只能保留为其原数据上的 in-sample 算术，forward/live 证据级别为 **OPEN / NOT-GO-EVIDENCE**。

## 被审工件 SHA256

|工件|SHA256|
|---|---|
|preregistration|`6cff51454d40b7623cfcd3cf2ed2e10ac461019c67a88e4c921a33547e4ad7b7`|
|live truth ledger|`2dde9b71f8cb4ab18d3787bd1c991ef0777b1e3d35da384f47d1c93275148064`|
|corpus manifest|`51a87dbcd8c36a1271d027718d62692ef42b4a5d46fc3fa0bc2590e8fae69f2c`|
|RPC summary|`2aa21f5a8abc8eb95a521cfdd9e8307d71b08c76fd9866af54537db4a3ca6238`|
|RPC full JSONL|`76fb9ecbc59b784caffb6a7836f6a42906d64ab7d9bb1d5cfce814448b4f267f`|
|GL4 主报告|`5095dcf988ccaccbd5dc1e0a17c8951b5fb583f6138bda4ce082aca815e55546`|
|人话版|`eab43458afa29db5756b0287e00d1c236144173a50c148dd4151c6a9716bc515`|
|reconciliation script|`518df723858e8ab386957a8e075f7694a23e8a5755556dafe205e50c9c9d6fa2`|
|render script|`0e326e43ef48d0a1e7b0b50106346727f518e333651fe7f43b52c88ea1f2109f`|
|regression test|`7ce76aee83945819c427e9af1a617169b86a238434476c41e38c3b4223338899`|
|GL3 GL1 ledger|`99f70bdaec1bb2ac076c52aead9b35078678b301ece88ed38c90211a0790635e`|

终审不授权 GO：`training_allowed=false`、`live_allowed=false`、`self_go_allowed=false`、`go_decision=NOT_MADE`。最终接受权仍在人类逐行审查。
