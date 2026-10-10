---
sop_applied: true
alpha_id: AR-RM2-CAPSTONE-ADAPTER
family: F4/F6/F7
next_gate: INDEPENDENT_LINE_REVIEW_THEN_HUMAN_ONLY_ARM
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# RM2 守卫广播适配器：禁用态结果

## 裁决边界

适配器源码已经包含一个真实签名/`eth_sendRawTransaction` 末端，但本轮没有进入该末端。运行时没有私钥来源、没有 `ARM_TRADE_ID`、没有 ARM 文件、没有签名、没有广播，也没有真实交易 hash。源码 SHA256=`e4ab54ec55ff305e11f4db29fd2373d8071ae2125b1455553da6b50459fba485`；禁用证明 SHA256=`0251658a5bc0d080167a3bb54b465d44ef0830ffecd14fdc54a725900ec6cccc`。

## 结构性路径

真实 entry 的唯一顺序是：仓库外、0600 ARM 文件及 reviewed adapter hash → 仓库外密钥来源存在 → 环境 `ARM_TRADE_ID` 精确相等 → `HUMAN_EXPLICIT` → RM1 全部冻结常量/kill-switch 无漂移 → KEYSTONE/SHAKEDOWN 模式及后者 N∈[1,7200] → 外部一次性状态未占用、无旧仓且 target allowance 为零 → 精确 host 的 prettiest-blue-aura/Base 8453 → D5 固定 AtomicRoundTrip creation-bytecode hash、target/fee/amount、`latest`，且与 entry 模拟相差不超过 1 block → 同一笔 `SwapRouter02 exactInputSingle` 全量 `eth_call` 且输出为正 → calldata `amountIn` 与 native `tx.value` 均严格为 0.001 WETH，使用 Router 的 WETH wrap 支付路径，不依赖隔离钱包预先授权 WETH；`amountOutMinimum` 不低于模拟输出 97% → 原子创建 outside-repo one-shot reservation → 人类密钥派生地址与 ARM 钱包一致 → 唯一签名/发送末端。

急停 flatten 的唯一顺序是：立即拒绝新 entry → 校验 state `trade_id`，即便 entry 在链上成交后、状态回写前崩溃，也先以链上真实余额为权威 → 从冻结零额模板生成 `approve(router,真实全余额)`，精确模拟、原子占位、广播并复核 allowance → 再从冻结零额 flatten 模板按真实余额生成探测 tx，并确定性写入模拟输出的 97% 为最终 `amountOutMinimum` → 对这笔最终 tx 再做一次精确 `eth_call` → 签名前原子创建 flatten reservation → 同一 arm 只完成这一笔往返 → 每个广播各自 30 秒 inclusion kill-switch → 链上余额必须 `==0` → arm consumed。一个 roundtrip 正常最多是 entry、exact-balance approval、flatten 三个广播；approval/flatten 任一步失败均不继续，模拟失败不会被写成已经平仓，签名前占位后崩溃也只允许人类处置而不自动重放。

## 禁用态逐门拒绝

|守卫|禁用证明|固定拒绝原因|
|---|---|---|
|仓库外私钥来源|PASS|`EXTERNAL_PRIVATE_KEY_SOURCE_MISSING`|
|逐笔 ARM 未设/不匹配|PASS|`ARM_TRADE_ID_UNSET` / `ARM_TRADE_ID_MISMATCH`|
|人类广播确认|PASS|`HUMAN_EXPLICIT_BROADCAST_CONFIRMATION_MISSING`|
|全量交易模拟|PASS|`PREBROADCAST_ETH_CALL_FAILED`|
|D5 原子买卖|PASS|`SKIP_UNSELLABLE_D5_FAILED`|
|尺寸严格 0.001 WETH|PASS|`NOTIONAL_MUST_EQUAL_0_001_WETH`|
|已有仓/外部仓/arm 已用|PASS|`OPEN_POSITION_EXISTS` / `EXTERNAL_ONCHAIN_POSITION_EXISTS` / `PER_ARM_ONE_ROUNDTRIP_EXHAUSTED`|
|30 秒超时/出场残仓|PASS|`BROADCAST_TO_INCLUSION_TIMEOUT` / `EXIT_POSITION_NOT_ZERO`|
|flatten 有仓、精确模拟、精确数量|PASS|`NO_OPEN_POSITION_TO_FLATTEN` / `FLATTEN_PREBROADCAST_ETH_CALL_FAILED` / `FLATTEN_AMOUNT_MUST_EQUAL_ACTUAL_BALANCE`|

机器证明共 28 个拒绝场景，覆盖 entry、post-send 与 flatten 的参数/状态硬门；`all_rejected=true`、`sign_calls=0`、`send_calls=0`、`real_tx_hashes=[]`。布尔夹具仅用于在不创建/读取任何密钥的情况下触发下游守卫；禁用证明没有构造 `ChainIO`，也没有做 RPC 调用。D5 initcode/参数、approval、state 崩溃窗和精确 RPC host 另由离线回归逐项断言。

## 回归测试

`tests/test_rm2_guarded_adapter.py` 当前 19/19 通过；与 RM1 的 12 项回归合跑为 31/31。覆盖 RM1 源 hash、kill-switch 完整等号、逐 entry 守卫、D5 initcode 由 pinned source 实际编译后 hash 相等、D5 严格 `>0.10` 与 latest/参数绑定、KEYSTONE/SHAKEDOWN 模式、WETH 输入 native value 严格等于 notional、3% 模拟滑点底线、entry/approval/flatten 原子占位、marker-only 崩溃后 fail-closed state 合成、链上余额救援、state trade-id 绑定、仓库外密钥的可变 32-byte buffer 尽力清零、外部绝对路径、精确 RPC host、RPC 前拒绝、30 秒/残仓、唯一 send boundary，以及 exactInputSingle 的 token/recipient/amount 整数解码。

## 尚未证明

- 没有签名或真实 RPC send，因此没有证明真实节点接受、nonce/fee 合理、receipt 能在 30 秒内返回或真实余额能归零。
- Python 与签名库可能在内部产生不可控的密钥副本；代码只保证自己持有的可变 `bytearray` 在使用后尽力清零，不能声称进程内绝无残留。因此仍必须使用隔离进程和人类仓库外密钥。
- 没有证明深池成交能外推 KEYSTONE 薄新池，也没有证明 GL1-02 真钱净 edge。
- 代码必须由独立审查 agent 逐行批准；之后仍只能由人类制作仓库外 ARM/密钥并亲自运行。本报告不提供 GO。

当前姿态：executor/strategy 关闭；不修改 live/signals/Redis；`live_allowed=false`、`self_go_allowed=false`、`GO=NOT_MADE`。
