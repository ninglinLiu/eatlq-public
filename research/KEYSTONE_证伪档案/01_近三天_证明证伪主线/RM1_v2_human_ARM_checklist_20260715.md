---
sop_applied: true
alpha_id: AR-KEYSTONE-RM-CALIB
family: F4/F6
next_gate: HUMAN_ONLY_ARM_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# RM1 v2 人类 ARM / 急停清单

当前状态：**未 ARM、不可广播、不含签名器**。预注册 SHA256：`8a8d8dfa5f302c1111c30645f633b3151ddccf4160b07e77e7a1bac9ea17f6f0`。

## 人类逐项确认后才可制作一次性 arm 包

- [ ] 接受预注册 SHA256 完全一致；任何参数变化先生成新预注册、新 SHA256。
- [ ] 指定隔离钱包地址与人工保管的签名方式；不得写入 artifact、仓库或普通环境文件。
- [ ] RPC 身份确认是 prettiest-blue-aura/Base 8453；artifact 只记端点代号，不记 URL。
- [ ] Router 白名单只含 Uniswap SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481`。
- [ ] Chainlink Base ETH/USD 标准 feed 人工复核：`0x71041dDdad3595F9CEd3DcCFBe3D1F4b0a16Bb70`；仅看板使用。
- [ ] 模式选 `KEYSTONE` 或 `SHAKEDOWN`。若 SHAKEDOWN，人工明确填写 `N`，范围 `[1,7200]`；本轮 dry-run 的 `N=1` 不是真钱默认。
- [ ] 标的必须是 WETH 计价池；按人类最新裁定，首笔应选同条件浅池并重新做 D5。
- [ ] unsigned 买单和 unsigned 卖/flatten 单逐笔保存 calldata hash，并在对应 block 做完整 `eth_call`；任何 revert 都不广播。
- [ ] D5 必须 buy/sell/整数一致全绿且 recovery ratio `>0.1`。
- [ ] 链上确认不存在旧仓；本 arm 的计数器为 0；notional 精确等于 `1000000000000000 wei WETH`。
- [ ] 人类明确承认：一次 arm 只允许一笔往返，开仓或平仓后立即耗尽，第二笔需要新 arm。

## 一键 arm 的安全接口约定

本交付**故意不提供可广播命令**。人类后续广播适配器只能接受一个脱离仓库保存的一次性 arm 包，且启动前必须运行：

```text
human-arm-adapter --arm-file /secure/RM1_ONE_SHOT_ARM.json \
  --require-prereg-sha256 8a8d8dfa5f302c1111c30645f633b3151ddccf4160b07e77e7a1bac9ea17f6f0 \
  --max-roundtrips ==1 --notional-weth-wei ==1000000000000000 \
  --live-broadcast HUMAN_EXPLICIT
```

适配器必须把 `==` 条件作为运行时相等断言；若尚无经审查的 `human-arm-adapter`，不得用现有 executor 顶替，也不得 ARM。

## 一键急停 / flatten 的接口约定

```text
human-arm-adapter emergency-flatten --arm-file /secure/RM1_ONE_SHOT_ARM.json \
  --reject-new-entry ==true --simulate-exact-tx ==true --target-balance-after ==0
```

顺序不可改变：立即拒绝新仓 → 读取真实 token 余额 → 生成全量退出 calldata → `eth_call` 精确模拟 → 人类适配器广播 → receipt/log 对账 → token 余额必须 `==0`。若模拟失败，不得假装已平仓；记录 `UNRESOLVED` 并由人类处置。

## 每笔结束后交回人类

- [ ] 保存决策、广播、上链、receipt 时间戳和 block/hash。
- [ ] 真成交数量来自 receipt/log/balance delta，不来自报价。
- [ ] 计算 WETH 原生真实净额；USD 仅用对应块 Chainlink 看板值。
- [ ] 应用 3%、30 秒、−5pp、unresolved 四个旗标。
- [ ] 仓位余额 `==0`，arm 已耗尽，框架停止；GO 仍由人类决定。
