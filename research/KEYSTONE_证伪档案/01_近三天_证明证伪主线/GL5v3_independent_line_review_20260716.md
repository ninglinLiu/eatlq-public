---
task_id: GL5-v3-independent-review
reviewer: independent_agent
date: 2026-07-16
artifact_verdict: PASS
decision_verdict: INCONCLUSIVE
substantive_blockers: 0
research_only: true
go_decision: NOT_MADE
---

# GL5v3 独立逐行终审

## 终审结论

**工件裁定 `PASS`，实质性 blocker 为 0；研究裁定维持 `INCONCLUSIVE`，`GO=NOT_MADE`。**

本次 archive 解析没有完成任务书理想中的“收益点估计”和“rug 真数”，但这是证据不足后的合规结果，不是应被填数掩盖的交付失败：完整 120 笔收益仍有 +1/+5/+15 各 7/5/2 条未决；严格 pool-level rug 仍有 3/1/1 条未决。收益双界和条件 alpha 双界均已大幅收窄且全体大于零，所以 `EDGE_COLLAPSES` 不符合当前历史样本；但冻结规则要求 primary offset `unresolved==0` 才能正式落 `EDGE_SURVIVES`，因此形式状态只能是 `INCONCLUSIVE`。

终审过程中发现并纠正了一个会过度强化结论的问题：最初离线 finalize 把“target WETH 储备高于 5%，但 atomic sell 未成功”事后归作 `CONFIRMED_NOT_COLLAPSED`，这与增量预注册 Rule 5 不一致。最终工件已删除 reserve-only 安全分支，相关腿保留为 `RUG_UNRESOLVED`，主报告、人话版、summary、逐腿账本和测试已统一。纠正后的 rug 界为 +1 `[0%,2.50%]`、+5/+15 `[0%,0.83%]`，不再宣称 0% 真数。

## 逐项核对

| 审查项 | 独立结果 | 裁定 |
|---|---|---|
| 对象身份 | 644 行，F-on selected 120 行；对象为 `2026-06-24_Fon_644_to_120`，没有混入 431/116 的零 F `+56%` 对象 | PASS |
| Arm-0 锚 | `pass=120`、monthly `0.08574380412768878`、MDD `-0.003931798477305182`、fatal `2`、mean return `1.4280241443411608`；70 个叶字段 `all_python_equal=true` | PASS |
| 预注册 | GL5v3 SHA `da489d3f3f4ad8b40ac48849661116264ac1a6e98f5a9835b9cdc60f07882412`；继承 GL5v2 SHA `c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e` | PASS |
| 先后顺序 | prereg 文件 08:40:27Z、sidecar 08:40:33Z；首条 RPC ledger 08:42:38Z。未发现阈值或对象在结果后被改写 | PASS |
| 收益与 rug 分离 | `return_resolved` 与 `rug_adjudication` 独立；未把缺历史仓位出场价自动当 rug，也未把 reserve-only/quote-only 自动当安全 | PASS |
| 收益双界 | 独立逐腿重算与 summary 一致（浮点累加仅末位约 `2e-16` 差异） | PASS |
| baseline/alpha | 644 全 population 同规则；alpha 报双界而非伪点估计 | PASS |
| 右尾 | 三档数值门通过，但 `unresolved_eq_0=false`，三档形式状态均为 `INCONCLUSIVE` | PASS |
| Golden | 5 条固定 hash 样本；F-on 无 collapse 类，类别缺口已披露；所有公开 evidence response hash 均能在 ledger 成功记录中找到 | PASS |
| RPC 安全 | 实绩仅 `eth_call=14024`、`eth_getBlockByNumber=1`；allowlist、生效 endpoint 别名、无 public fallback、无 send/sign/broadcast | PASS |
| 秘密扫描 | 授权 URL 未出现在 prereg、主报告、人话版、代码、测试、summary、逐腿账本、ledger 或 ledger summary | PASS |
| 钱包级边界 | 报告明确限定为 pool/模拟地址层面；未声称策略钱包免受 blacklist、hook、限额、MEV 或竞争 | PASS |
| 回归 | 独立运行 `.venv/bin/pytest -q tests/test_base_gl5v3.py`：`9 passed`；最终 evidence pin 测试 SHA `5af646a4b647a8b89a6a7f92db1d97613a34e00802acde2c0885164db333da8c` | PASS |
| GO/实盘安全 | 所有终态均为 `GO=NOT_MADE`；未发现下单、签名、广播或 live/Redis/signals 写入路径 | PASS |

## 独立重算

### 120 条 F-on

| offset | 分支计数（real / alive / impaired / unresolved） | rug（否认 / 确认 / 未决） | 完整 mean 双界 |
|---:|---:|---:|---:|
| +1 | 31 / 81 / 1 / 7 | 117 / 0 / 3 | `[1.3672943342260926, 1.4770342541250798]` |
| +5 | 34 / 80 / 1 / 5 | 119 / 0 / 1 | `[1.3960784104486632, 1.4881408064429673]` |
| +15 | 38 / 79 / 1 / 2 | 119 / 0 / 1 | `[1.4771676388156305, 1.489564076021217]` |

悲观替代严格为 `-1 - 0.15/50.09425 - 0.02 = -1.0229943556396193`；乐观替代逐腿使用冻结 Arm-0 QV2 return。没有 unresolved 进入 resolved-only 点均值。

### 同龄 644 baseline

| offset | 分支计数（real / alive / impaired / collapse / unresolved） | rug（collapse / non-collapse / unresolved） | baseline mean 双界 |
|---:|---:|---:|---:|
| +1 | 64 / 376 / 26 / 24 / 154 | 24 / 600 / 20 | `[0.22492396928144273, 0.5027130531429405]` |
| +5 | 65 / 354 / 26 / 43 / 156 | 43 / 587 / 14 | `[0.1837616521997999, 0.457376277494632]` |
| +15 | 64 / 322 / 26 / 86 / 146 | 86 / 542 / 16 | `[0.06719320977346327, 0.30840368730131335]` |

按区间差的合法组合 `selected_low-baseline_high` 与 `selected_high-baseline_low`，条件 alpha 为：

- +1 `[0.8645812810831521, 1.2521102848436372]`
- +5 `[0.9387021329540313, 1.3043791542431675]`
- +15 `[1.1687639515143171, 1.4223708662477537]`

三档均不是点识别，但区间符号均为正。报告没有把区间中点包装成“真正 alpha”。

### 右尾

独立读取最终机器结果核对：resolved n 为 113/115/118；去 top-1% mean 为 `1.3187829238187667 / 1.3078189075607554 / 1.3313413608591802`；top-1% 正利润贡献为 `14.13% / 14.03% / 13.57%`；powered rolling 窗为 4/4/2，均过 `-10%` 门；bootstrap terminal p5 为 `233.7589R / 235.0369R / 241.4788R`，MDD p95 为 `1.5388R / 1.5227R / 1.4291R`。唯一形式失败项是 `unresolved_eq_0`。

## RPC ledger 独立核对与缺陷分级

ledger SHA256 为 `2db34b77ab1a604fc7263ce1697c29284056f913738d17bf645b997406c7c6c8`。独立解析得到：

- 14,025 个 logical calls；每个 logical call 的 attempt 1 都有记录。
- 17,034 条 JSONL，13,668 条成功（含 head health check）；逐腿工件的 13,667 条成功 evidence `(request hash,response hash)` 全部能在 ledger 找到。
- 客户端计数器为 18,055 次 HTTP attempt，故有 1,021 条连续 transport-error 的中间失败重试未落 JSONL。
- 漏记原因位于 `Rpc.call` 的 `last_class == "unknown"` 分支：第一次 transport failure 后，后续同 method/同 params 的失败重试不再 `_record`。没有证据表明成功响应、最终 unresolved、方法或参数发生漏记。

该问题违反“每次 attempt 均落账”的预注册工件要求，ledger 必须保持 `complete=false`，不能称为完整 attempt ledger。终审将其分为**明确、非阻断的审计缺陷**，理由是：每个 logical query 的首 attempt 已覆盖；重试使用同一 method/params；所有影响逐腿分类的成功响应 hash 均完整存在；运行时 allowlist 在进入重试循环前执行。它不改变 644/120 分类、收益界、rug 界或 alpha。后续任何 RPC 研究必须先修 logger；不得伪造补回本轮 1,021 行。

## Atomic/exact-size 的适用边界

冻结实现的 atomic 是以 0.0302 ETH 当块新买再卖的路径 guard；历史仓位数量的输出金额来自同块 QuoterV2 exact-size。终审抽核发现，在 archive-resolved selected 腿中，历史 qty 经常大于 atomic 当块新买 qty（+1 为 68/82，median ratio 约 1.87，max 14.36）。因此本轮不能证明策略钱包持有的完整历史 token 数量一定能通过 token 级限额或地址级 hook；QuoterV2 也不是钱包 receipt。

这不是隐藏 blocker，因为该构造在预注册中明确冻结，主报告也把结论限定为 pool-level，并显著保留钱包级 blacklist/hook/限额缺口。它禁止把当前正双界升级为可部署真钱收益或 GO 证据。

## Golden 与证据哈希

冻结 selection 实际为 3 条 `ARCHIVE_ALIVE_ATOMIC_OK`、1 条 `UNRESOLVED`、1 条 `REAL_SELL_CARRY_FORWARD`。F-on 没有 `CONFIRMED_COLLAPSE`，所以无法满足“2 collapse”愿望；实现按 hash 最小值补足并设置 `category_shortfall_disclosed=true`，没有从 baseline 偷换 cohort。5 条 golden 中所有非空 entry/target/lag/atomic/quote response hash 均存在于成功 ledger 记录。

另外，1,696 个成功 atomic probe 的 `pin_block` 全部等于对应 target block；10,141 个成功 `WETH.balanceOf` 返回值可由逐腿十进制值恢复为 32-byte ABI word，其 canonical response SHA 与逐腿工件逐条一致。

## 最终边界与接受意见

接受 GL5v3 工件，接受以下精确表述：

1. 历史 F-on 120 的完整收益方向在冻结悲观/乐观替代下为正，但尚非点识别。
2. 没有确认 selected rug；严格 pool-level rug 率仍为 +1 `[0%,2.50%]`、+5/+15 `[0%,0.83%]`，不是 0% 真数。
3. 完整 population 条件 alpha 区间为正，但不是因果实验或点 alpha。
4. 当前证据强烈支持历史 edge 幸存、排斥 `EDGE_COLLAPSES`，但依预注册形式状态仍为 `INCONCLUSIVE`。
5. pool-level/archive 证据不能证明策略钱包一定卖得掉；不能据此自判 GO。

最终裁定：`PASS`；substantive blockers=`[]`；`GO=NOT_MADE`。
