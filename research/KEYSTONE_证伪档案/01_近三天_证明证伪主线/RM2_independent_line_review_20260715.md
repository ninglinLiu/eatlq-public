---
sop_applied: true
alpha_id: AR-RM2-CAPSTONE-ADAPTER
family: F1/F4/F6/F7
next_gate: HUMAN_ONLY_PHASE1_DEEP_POOL_ARM__SEPARATE_PER_TRADE_REVIEW_REQUIRED
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# RM2 独立逐行终审

## 裁决

`ADAPTER_REVIEW_PASS_DISABLED_ONLY`。

该裁决只绑定适配器 SHA256 `e4ab54ec55ff305e11f4db29fd2373d8071ae2125b1455553da6b50459fba485` 与禁用证明 SHA256 `0251658a5bc0d080167a3bb54b465d44ef0830ffecd14fdc54a725900ec6cccc`。它表示当前禁用态证据与源码中的 fail-closed 结构通过审查；**不表示 GO、可部署、真实节点已接受交易、真实成交已验证或 KEYSTONE 真钱 edge 已关闭**。任何未来 ARM 仍须由人类针对具体交易另行制作、复核并亲自执行。

## Part 1：封账事实汇总

- 专业版只聚合 GL1/GL2/GL3 与 reaudit10 的封账事实，没有重算或改级。431 同龄候选、116 个冻结 KEYSTONE、entry+1 与 trigger+1/+5/+15、0.001 WETH、真卖优先/否则标注 atomic guarded quote、残余有界的口径均写清。
- “瞎买”三窗约 `-31%` 有界、KEYSTONE 约 `+52%～+57%`、条件 alpha 下沿超过 `+83pp`，以及撤销 411 个新增 rug 裁定仍不推翻方向，均锚到 GL3 result `e42ac92d…32a9` 与独立审查 `658155a8…fdb5`；权威总账 SHA256 为 `99f70bda…635e`。
- W2 全域风险、KEYSTONE-selected W2 小岛、QuoterV2 已测 Wilson 95% 上界 1.2399%、爆发型频率、rug 五证据、D5/forward monitor/天气门的限定历史结果均保留原 scope 与直接 hash。
- OPEN 表没有把真钱净 edge、未来频率、现实月收益、月尺度衰减、RH 聪明钱或天气门篮子伪装成已关闭。`IRON ≠ GO ≠ 可部署`、报价不等于 fill、`live_allowed=false` 与 `self_go_allowed=false` 明示。
- 人话版按“知道什么 / 不知道什么 / 为什么反复变卦 / 下一步”解释；明确历史收益主要来自当时链上模拟，并未由我方真钱买卖兑现。0.001 WETH 只描述本金风险，另列实际 gas，深池阶段也没有外推薄新池收益。

## Part 2：八类结构性硬门

|检查|终审结果|源码约束|
|---|---|---|
|仓库外人类密钥|PASS|ARM 与私钥文件必须绝对路径、仓库外、非 group/world 可读；ARM 禁止 secret 字段；只能二选一读取人类文件或间接环境变量，拒绝 executor 复用；缺源在构造 `ChainIO` 前拒绝。可变 `bytearray` 仅 best-effort 清零，报告未再声称 Python 进程无副本。|
|逐笔 ARM|PASS|环境 `ARM_TRADE_ID` 必须非空且逐字符等于 ARM `trade_id`；ARM、CLI 与当前 adapter/prereg hash 三重等号；review approval/ticket 必填；state 与所有 reservation marker 均绑定同一 trade id。|
|全量 `eth_call`|PASS|entry 对最终 unsigned tx 模拟且要求正输出；approval 对最终精确额度 tx 模拟；flatten 先探测，再写入确定性 97% 最低输出，并对最终将广播的 tx 二次精确模拟。revert/非正输出均不进入签发。|
|D5|PASS|只接受 pinned `AtomicRoundTripCreateB4` creation bytecode；离线重编译长度/hash 相等；constructor 的 target/fee/amount 与 entry 逐整数绑定；block 必须 `latest`，D5 到 entry 模拟漂移不超过 1 block；买卖、整数等号与 recovery `>0.10` 缺一即拒。|
|固定尺寸|PASS|ARM、CLI、entry calldata `amountIn` 与 native `tx.value` 均严格等于 `1000000000000000`；Router 当笔 wrap 为 WETH；entry 最低输出不得低于同次模拟的 97%。|
|旧仓与 one-shot|PASS|entry 前 target 链上余额、state 仓位和 target allowance 必须为零；entry state 用 `O_EXCL` 先占位；同一 state 无法第二次 entry。ARM 固定一笔 roundtrip，正常为 entry、exact-balance approval、flatten 三个连续 nonce 广播。|
|RM1 kill-switch|PASS|RM1 prereg 及全部 source hash 每次加载复核；冻结 posture、D5、模拟、30 秒 inclusion、旧仓、第二 roundtrip、停止新 entry、flatten intent 与最终余额 `==0` 均为运行时硬等号。receipt status 必须为 1。|
|急停 flatten|PASS|即使 entry 在广播后、state 数量回写前崩溃，flatten 也以链上真实 target 余额为权威；approval 仅授权该真实余额；flatten 从零额模板动态生成同一真实余额，最终 receipt 后余额必须为零。|

## 唯一签发边界与崩溃窗

- 源码只有一个 `sign_and_send` 定义与一个 `send_raw_transaction` 调用点；禁用路径不构造 `ChainIO`。
- entry、approval 与 flatten 都在签名前创建 `O_EXCL` reservation。进程在 reservation 后、send 前或 send 后、hash 回写前崩溃时，自动重放均被 marker 拒绝。
- 独立检查了 marker-only 窗口：`load_bound` 以 marker 为权威，核 trade id，并持久化 `killed=true`、`flatten_required=true`、`reject_new_entry=true`；只有链上 target 余额最终 `==0` 后才清除 killed/reason。若 approval marker 已存在但 allowance 不足，系统只允许人类介入，不重发 approval。
- approval 与 flatten 使用 ARM 中 consecutive nonce 的零额模板，运行时只生成精确余额额度；正常 roundtrip 三笔真实 gas 均须进入 `realized_net_weth`，手册已明确收集三笔 hash/receipt/block/latency/allowance 与 gas。

## 禁用证明与离线复核

- 机器禁用证明：28/28 场景全部拒绝，`sign_calls=0`、`send_calls=0`、`real_tx_hashes=[]`，且没有 `ChainIO`/RPC。
- 本审查在 ARM、私钥与 RPC 四类 authority 输入全部为空、executor/strategy 容器均不存在时，内存复算同一禁用证明并确认唯一 send boundary。
- 项目回归证据为 RM1 12 + RM2 19 = 31/31。本审查另独立运行 30 个不创建密钥材料的测试并全部通过；为遵守审查铁律，唯一未重跑的是使用公开虚构十六进制串构造 32-byte buffer 的清零夹具，该夹具源码已逐行审阅。`py_compile`、四份 RM2 报告 SOP 检查与 `git diff --check` 均通过。

## 保留边界

- 未运行 `execute` 或 `emergency-flatten`，未连接 RPC，未签名、未广播；因此 nonce/fee、真实节点接受、mempool、真实 30 秒 inclusion、真实 approval/flatten 与余额归零仍未验证。
- D5 与 entry 模拟允许最多 1 block 漂移；广播前后状态仍可能变化，3% 最低输出只限制价格恶化，不能消除失败、抢跑或 rug。
- approval 后若 token 余额因 rebase/外部变化而改变，当前精确余额 flatten 可能 revert 或留下残余；最终 `balance==0` kill-switch 会阻止把它写成成功，但需要人类处置。
- Python/签名库可能保留不可控的密钥副本。未来只能用隔离进程、仓库外人类密钥与逐笔 ARM；本终审不授权任何一次具体交易。

最终：blockers 为空；`GO=NOT_MADE`，`training_allowed=false`、`live_allowed=false`、`self_go_allowed=false`。
