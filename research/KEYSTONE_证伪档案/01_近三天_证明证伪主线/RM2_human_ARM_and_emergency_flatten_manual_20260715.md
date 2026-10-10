---
sop_applied: true
alpha_id: AR-RM2-CAPSTONE-ADAPTER
family: F4/F6/F7
next_gate: INDEPENDENT_REVIEW_BEFORE_ANY_HUMAN_COMMAND
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# RM2 人类逐笔 ARM 与一键急停手册

## 当前状态

**现在不得执行本页任何真钱命令。** 当前没有 ARM、没有私钥、没有广播；适配器必须先由独立审查 agent 逐行通过，然后由人类为某一笔交易单独制作仓库外 ARM 包并亲自执行。Codex 不接触私钥、不设置 `ARM_TRADE_ID`、不运行 execute/flatten。

本次待审源码：`scripts/research/base_rm2/rm2_guarded_adapter.py`，SHA256=`e4ab54ec55ff305e11f4db29fd2373d8071ae2125b1455553da6b50459fba485`。RM1 冻结 SHA256=`8a8d8dfa5f302c1111c30645f633b3151ddccf4160b07e77e7a1bac9ea17f6f0`。

## 两阶段，只能按顺序

### 阶段一：深池成熟币，只证管道

人类选一个 WETH 计价、成熟且容易退出的 Uniswap V3 深池，单独 arm 一笔 `0.001 WETH` 往返。它只回答：同笔报价能否被真实 entry/exit 兑现、真实滑点/gas/广播到上链延迟是多少、flatten 是否归零。深池结果是执行摩擦的安全下界，**不能证明 KEYSTONE 薄新池的历史 +52%～+57% 可以兑现**。

### 阶段二：真实 KEYSTONE 薄新池，才验 GL1-02

只有出现真实、按冻结 selector 当时产生的 KEYSTONE 信号后，人类才另做一个全新的 arm，仍只下 `0.001 WETH`。必须重新过 D5、同笔精确模拟、旧仓检查和逐行参数审查。真实 receipt/log/balance delta、gas、延迟和报价差才回答“KEYSTONE 真钱净 edge”；单笔本金最坏约损失 `0.001 WETH`，通常为几美元量级，另有实际 gas。一次结束立即停，不自动做第二笔。

## 仓库外材料

1. ARM JSON、one-shot state 文件和私钥文件都必须使用绝对路径并位于 `/opt/EATLQ` 外；ARM/私钥权限必须是 `0600`。
2. ARM JSON **不得含** private key、seed、mnemonic 或任何 secret 字段。私钥只允许二选一：
   - 人类在执行时设置 `RM2_PRIVATE_KEY_FILE=/secure/...`，文件在仓库外且 `0600`；或
   - 人类设置 `RM2_PRIVATE_KEY_ENV_NAME=<另一个由人类控制的环境变量名>`，密钥值只存在那个外部环境变量。不得复用 `EXECUTOR_PRIVATE_KEY`。
3. ARM JSON 固定字段至少包括：唯一 `trade_id`、RM1 prereg hash、最终 reviewed adapter hash、`review_approval=HUMAN_REVIEW_AGENT_APPROVED`、非空 review ticket、钱包、target token、`notional_weth_wei=1000000000000000`、`max_roundtrips=1`、`mode=KEYSTONE|SHAKEDOWN`、`shakedown_n_blocks`、`rpc_endpoint_alias=prettiest-blue-aura`、仓库外 state path、D5 deploy-less `eth_call`、entry unsigned tx、零额 approval unsigned template 与 flatten unsigned tx。三者 nonce 必须连续；KEYSTONE 的 `shakedown_n_blocks` 必须为 null；SHAKEDOWN 必须由人类明确填整数 `[1,7200]`。
4. entry/flatten 只接受白名单 SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481` 的 `exactInputSingle`。适配器会解码并硬校验 token 方向、recipient、fee、`amountIn` 与 `amountOutMinimum>=精确模拟输出的97%`；entry 的 calldata `amountIn` 和 native `tx.value` 都必须等于 `1000000000000000`，由 Router 当笔 wrap 成 WETH，不需要隔离钱包预先给 WETH allowance。D5 只接受冻结 AtomicRoundTrip creation bytecode、与 entry 完全相同的 target/fee/amount 和 `latest`，D5 到 entry 模拟最多漂移 1 block。
5. 新 token 退出前需要 allowance。适配器不允许无限授权：从 ARM 的 `approve(router,0)` 模板在链上余额已知后生成 `approve(router,真实全余额)`，精确模拟后单次广播并复核 allowance。flatten 也必须是 `amountIn=0, amountOutMinimum=0` 的冻结模板；代码按真实余额先探测输出，再确定性写入 97% 底线并对最终 tx 精确模拟。approval 与 flatten 各自受 30 秒超时和一次性 reservation 约束。
6. RPC URL 只在人类执行环境通过 `RM2_RPC_URL` 提供；host 必须精确为 prettiest-blue-aura 的 Base QuickNode host、chain id=8453，artifact 不记录 URL。

## 人类逐笔 entry 接口

以下只是经审查后的人类接口约定，不是本轮执行命令。`<...>` 必须由人类填入，不能由 Codex 代填或运行：

```text
ARM_TRADE_ID=<这一笔唯一trade_id> \
RM2_PRIVATE_KEY_FILE=/secure/<human-key-file> \
RM2_RPC_URL=<human-injected-prettiest-blue-aura-url> \
python3 /opt/EATLQ/scripts/research/base_rm2/rm2_guarded_adapter.py execute \
  --arm-file /secure/<RM2_ONE_SHOT_ARM.json> \
  --require-prereg-sha256 8a8d8dfa5f302c1111c30645f633b3151ddccf4160b07e77e7a1bac9ea17f6f0 \
  --require-adapter-sha256 e4ab54ec55ff305e11f4db29fd2373d8071ae2125b1455553da6b50459fba485 \
  --max-roundtrips '==1' \
  --notional-weth-wei '==1000000000000000' \
  --live-broadcast HUMAN_EXPLICIT
```

任一等号、hash、review ticket、trade id、私钥来源、钱包、router、D5、模拟、余额或 state 不匹配，必须拒绝且不发送。entry 发送前会原子创建 state reservation；即使进程随后崩溃，该 arm 也不能自动重放。

## 一键急停 / 所有出场共用的 flatten 接口

当 KEYSTONE/SHAKEDOWN 出场规则触发、超时需要处置或人类要求急停时，先停止任何新 entry，再由同一个人类 arm 调用：

```text
ARM_TRADE_ID=<同一笔trade_id> \
RM2_PRIVATE_KEY_FILE=/secure/<human-key-file> \
RM2_RPC_URL=<human-injected-prettiest-blue-aura-url> \
python3 /opt/EATLQ/scripts/research/base_rm2/rm2_guarded_adapter.py emergency-flatten \
  --arm-file /secure/<RM2_ONE_SHOT_ARM.json> \
  --require-prereg-sha256 8a8d8dfa5f302c1111c30645f633b3151ddccf4160b07e77e7a1bac9ea17f6f0 \
  --require-adapter-sha256 e4ab54ec55ff305e11f4db29fd2373d8071ae2125b1455553da6b50459fba485 \
  --reject-new-entry '==true' \
  --simulate-exact-tx '==true' \
  --target-balance-after '==0'
```

顺序固定：拒绝新仓 → 校验 state trade id → 读真实余额（它覆盖崩溃前未回写的 state 数量）→ exact-balance approval 精确模拟/原子占位/发送/复核 → 按真实余额构造 flatten 探测 → 写入 97% 最低输出 → 对最终 tx 精确 `eth_call` → 签名前原子创建 flatten reservation → 人类适配器签名/发送 → 最多等待 30 秒 → receipt 后重读余额 → 必须 `==0`。一个 arm 的一笔往返通常有 entry、approval、flatten 三个广播；任一模拟或发送失败不得写成已平仓，reservation 一旦存在便不得自动重放，state 保持 killed/flatten-required，交回人类人工处置。

## 每笔完成后的人类核对

- entry、exact-balance approval、flatten 三笔各自的 tx hash、receipt、block、广播到上链延迟、allowance 前后值、真实 token/WETH balance delta、gas/priority fee；`realized_net_weth` 必须扣掉三笔总 gas，不能漏掉 approval 成本。
- 决策→广播→上链→成交的时间戳；广播到上链超过 30 秒必须标记。
- entry/exit 真成交与各自同块报价的差；报价不得冒充 fill。
- target 余额严格为 0，state 为 consumed；第二个 entry 必须使用新的 trade id、新 ARM、新 state 和新逐行审查。
- 深池只判断管道；只有真实 KEYSTONE 薄池样本才能更新 GL1-02。任何结果仍由人类与审查 agent 决定，不自动 GO。
