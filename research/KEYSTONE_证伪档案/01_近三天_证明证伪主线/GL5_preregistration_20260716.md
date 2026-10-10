---
task_id: GL5
title: 保守 KEYSTONE 可实现出场重定价预注册
date: 2026-07-16
frozen_before_any_gl5_repricing: true
research_only: true
training_allowed: false
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
---

# GL5 预注册：对象身份、停止门与可实现出场规则

本文件在任何 GL5 新 RPC 查询、rug 前瞻扫描、Arm-1 重定价或结果汇总之前冻结。冻结后不得因为结果修改；任何更正必须另建版本并把本轮判为口径漂移。

## 1. Step 0 被审计对象

唯一候选对象是以下冻结链路，禁止拼接或替换字段：

- universe：B5/B8 的 W1–W3 同龄 431 个机会；KEYSTONE 子集 116 个。
- 日期：W1 `[2026-02-27 05:41:15Z, 2026-04-01 00:00:00Z)`；W2 `[2026-04-01 00:00:00Z, 2026-05-01 00:00:00Z)`；W3 `[2026-05-01 00:00:00Z, 2026-06-08 18:29:40Z)`。
- 入场决策：冻结 `entry_block`（t0+10m）；reaudit10 执行偏移 `+1/+3/+5/+10/+15` 块。
- 主出场前沿：入场固定 `entry_block+1`，出场为 `trigger_block+1/+3/+5/+10/+15` 块；GL3/GL1 只正式 pin `+1/+5/+15`。
- trigger：`legacy_exb`；若缺失则 `entry_block+7200`。
- legacy exit 参数来源：硬止损 `-50%`；从峰值回撤 `35%`；最大持有 `7200` 块（约 4h）；单一止盈上限/打印参数为 `10x`，不存在 TP ladder。GL5 不把不存在的 ladder 补进对象。
- 仓位：`0.001 WETH`；每个尝试腿 gas 为 `$0.075`，ETH/USD 用冻结 B3 日收盘。
- 模型：`HistGradientBoostingClassifier(max_iter=300,max_depth=4,learning_rate=0.05,l2_regularization=1.0,min_samples_leaf=20,random_state=0)`；逐窗仅用过去数据训练。
- 选择：`prob <= cut AND fric >= -0.05 AND fric is not null AND regime is not null`；431/116 链路对全部 W1–W3 都使用 full-feature 模型及 cut `0.2808669872465047`，cut 是符合条件训练集 fatal 概率的 60 分位。B5 另报的 general-feature cut `0.29365326821372995` 不属于 431/116 选择对象。
- 旧出场证据优先级：真实卖单；同块 AtomicRoundTrip 成功保护的 exact-size QuoterV2；固定偏移网格上首次恢复；独立确认且截至观察截止无恢复的 LP/transfer rug 才为 `-1`；其余 `UNRESOLVED`。

源锚必须逐字节 pin：

| 路径 | 冻结 SHA256 |
|---|---|
| `scripts/research/base_b8/b8_rpc_evidence.py` | `4d0123dc9aa9290c07342a66e473f836ed5c7221385b6186762a8b7c93920fee` |
| `scripts/research/base_b5/b5_offline_reverify.py` | `22f5c9e787e6a79695f9db4dc9679b10ea6cfd455e9b16ac8c190c86a5e5dc17` |
| `scripts/research/base_lefttail/keystone_live.py` | `626eaca39d43bf52049bb989e4579777a4bf60d1ea7d0e40730944178177a745` |
| `configs/research/base_reaudit10_seconds_preregistration_20260715.json` | `c942e7f286f9dda5cc384f0c91f4e98172d2e6307fd80e66869817580ca2534b` |
| `mvp/diagnostics/base_reaudit10_per_trade_20260715.json` | `65126c50ed5680c8d0363379662fcd88a0d6b339c6af78fbd873cf4ade7bb410` |
| `mvp/diagnostics/base_reaudit10_final_20260715.json` | `7af0f54247956fa069b0d82bb686f0d9c09bdb76da07cf63e9eb21998c45bcb7` |

## 2. F 身份硬门槛

任务问题明确指向“保守 F-on KEYSTONE”，因此候选对象必须在产生 431/116 与 +56% 的选择链路中存在可追溯的显式 F 名单版本和名单 SHA。检查范围是 `prepare_rows()` 及其生成 `selected` 所调用的 B5 选择/特征/训练代码；仅在另一份 644-pool 回测中出现 F 不算同一对象。

严格规则：

1. 必须找到显式 deployer/skeleton/F blacklist 过滤在 116 子集形成之前执行；
2. 必须能 pin 名单条数、版本、SHA 和过滤前后计数；
3. 若任一项不存在，记 `STOP_OBJECT_IDENTITY_MISMATCH`；不得把 247+33、245+33 或任何其他 F 名单事后 graft 到 431/116；不得启动 Arm-1、基线、golden RPC 或新 RPC；报告只能陈述对象身份矛盾与已有 Arm-0 锚。

该停止规则优先于后文所有计算。

## 3. Arm-0 严格复现门槛

从冻结逐腿 JSON 独立聚合，必须同时满足：

- 总行数 `==431`、selected `==116`；逐腿文件行数 `==116`。
- `1:1`：resolved `==113`，unresolved `==3`，evidence `== {ATOMIC_GUARDED_EXACT_QUOTE:107, CONFIRMED_LP_RUG:2, REAL_SELL:4, UNRESOLVED_EXIT:3}`，mean `==0.5663701436516096`，median `==0.2597229826746521`，top1-removed mean `==0.48288116031956935`。
- `1:5`：resolved `==112`，unresolved `==4`，evidence `== {ATOMIC_GUARDED_EXACT_QUOTE:101, CONFIRMED_LP_RUG:3, REAL_SELL:8, UNRESOLVED_EXIT:4}`，mean `==0.5658600160864686`，median `==0.2586536605039931`，top1-removed mean `==0.48133795277529695`。
- `1:15`：resolved `==111`，unresolved `==5`，evidence `== {ATOMIC_GUARDED_EXACT_QUOTE:101, CONFIRMED_LP_RUG:3, REAL_SELL:7, UNRESOLVED_EXIT:5}`，mean `==0.5768599229957799`，median `==0.2731717488361512`，top1-removed mean `==0.49397325802362163`。
- `1:3` 与 `1:10` 也必须与 reaudit10 final 逐字段严格 `==`。

数字容差为绝对 `1e-15`，但测试先做 Python 浮点 `==`；只有序列化/聚合顺序导致末位差且绝对误差不超过 `1e-15` 才可标 `PASS_WITH_SERIALIZATION_TOLERANCE`，不能改 headline。任一失败即 `STOP_ARM0_REPRODUCTION_DRIFT`。

账本名称不得混淆：+56% 点估计是 GL1-01 `PINNED`；GL1-12 `IRON` 是 431/116 的残差双界与 alpha/beta 符号分离。GL5 必须分别复核两者，不能称“+56% 本身为 GL1-12 IRON”。

## 4. 仅在两道门均通过时启用的 Arm-1 规则

### 4.1 延迟与扫描

- 主出场偏移：`+1/+5/+15` 块；完整敏感性：`+1/+3/+5/+10/+15`。
- nominal Base 块时：2 秒；不以 nominal 秒替代实际 block timestamp。
- 真实执行滞后带：目标出场块之后 `15` 块；敏感性 `5/30` 块。
- reserve 主口径：`WETH.balanceOf(pool)@block`；transfer-net 仅作重建并须 golden 与 balanceOf 精确 `==`。
- rug collapse 主阈值：相对入场可观测 WETH reserve 跌幅 `>=95%`；敏感性 `>=90%`、`>=99%`。
- 扫描区间：实际入场执行块至目标出场块加滞后带，端点均包含。首次达到阈值的块为 collapse block。

### 4.2 出场证据优先级

1. 真实 SELL：目标出场块至目标块 `+1` 内，按 `(block_number, transaction_index, log_index)` 取最早一笔 `receipt.status==1` 且 token 输入量 `>=` 反事实持仓量的 decoded sell；用该笔真实 WETH/token 价，不做 VWAP。
2. 若无合格真实 SELL，先做前瞻扫描。collapse 在“最早允许执行块”之前或之上发生，且有完整 archive 覆盖，则按最后一个确认可回收点；若没有正的可回收卖价且确认连续不可卖，gross `=-1`。单独的失败 receipt 只能证明该次失败；必须与 reserve collapse、transfer restriction 或截至固定窗口无恢复之一结合才可定全损。
3. 仅在完整扫描确认直到 `target+lag` 未达 collapse 阈值时，才允许 target block exact-size QV2，且必须同块 AtomicRoundTrip sell-path 成功。使用实际 `amount_out`，不再额外添加固定 haircut；exact-size quote 已内含曲线冲击与池费。
4. atomic revert 但没有完整坍塌/限制/无恢复证据，或任何 archive 区间缺档，均为 `UNRESOLVED`，不强制 `-1`，也不按 quote 成交。

gas：每次已尝试的 entry/exit leg 各 `$0.075`；未实际尝试的恢复点不收费。return 以 `0.001 WETH` 和冻结当日 ETH/USD 换算。

### 4.3 UNRESOLVED 双边界

- 点估计完全排除 UNRESOLVED。
- 乐观界：若旧 exact-size guarded quote 存在，用 Arm-0 该腿 net return；否则用同 cohort/window 已解决 return 的中位数。
- 悲观界：gross `=-1`，再减已确认发生的 attempted-leg gas；不得添加未发生的未来 gas。
- population mean 区间：resolved Arm-1 returns 与上述逐笔悲观/乐观替代后分别求普通算术平均；禁止把未知默认为任一端点的点估计。

### 4.4 三臂与“报价洗白”分解

- Arm-0：冻结 116 入场、旧出场模型。
- Arm-1：同 116 入场、同偏移、新模型。
- 同龄基线：冻结全部 431，完全相同的新模型；不得重新训练或重新选阈值。
- 报价救济腿：Arm-0 evidence 为 `ATOMIC_GUARDED_EXACT_QUOTE`。
- rug 洗白率：报价救济腿中 Arm-1 判为 confirmed collapse/unsellable 的数量除以报价救济腿数。
- 确认存活率：报价救济腿中完整扫描存活且 guarded exact quote 成功的数量除以报价救济腿数。
- 正利润幻觉份额：`sum(max(0, arm0_ret)-max(0, arm1_ret)) / sum(max(0, arm0_ret))`，仅在两臂均 resolved 的同腿上算；另报 unresolved 个数，禁止把它们塞入分子。
- F-on rug exposure：116 子集内至少一个主偏移被 confirmed collapse/unsellable 的 unique pool 数；与单独激进回测的 207 只作外部对照，不合并分母。

### 4.5 右尾判据

逐主 cell 独立检查：pooled mean `>0`；top-1 移除 mean `>0`；所有 `n>=20` 的 rolling 14d/step7d mean `>=-0.10`；至少 2 个 `n>=20` regime 同时 mean `>0` 且相对同龄基线 alpha `>0`；seed `20260715` 的 10,000 路 MC terminal p5 `>100R`；MDD p95 `<=20R`；冻结 OOS mean `>0`。若 primary unresolved `>0`，形式状态必须是 `INCONCLUSIVE`，即使已解决点估计通过其他门。

“幸存”只在 `1:1/1:5/1:15` 的 Arm-1 形式状态均 PASS 且主阈值下正利润幻觉份额 `<=10%` 时成立；“崩塌”在三格中任一 resolved mean `<=0` 或正利润幻觉份额 `>=50%` 时成立；其余为 `INCONCLUSIVE`，不得强迫二选一。

## 5. Golden、RPC 与安全边界

仅在 Step 0 两门通过后，按 `sha256("GL5-golden|" + pool + ":" + offset)` 升序选 5 腿，要求至少 2 个 confirmed collapse/unsellable、1 个 survived quote；类别不足则全纳并记录不足，不替换阈值。独立复核 `balanceOf`、receipt、真实 SELL、atomic。

RPC 只允许环境中已注入的 `EXECUTOR_READ_RPC_URL`，并要求非秘密别名/记录为 `prettiest-blue-aura`；禁止读取 `.env`。允许方法仅 `eth_blockNumber`、`eth_getBlockByNumber`、`eth_getLogs`、`eth_getTransactionReceipt`、`eth_call`。禁止签名、发送、广播、Redis/signals、服务启停与 live config 修改。若 Step 0 停止，新 RPC 调用必须 `==0`。

## 6. 固定产出与裁定边界

无论通过还是停止，都输出 Step 0 机读锚、逐腿状态、主报告、人话版、测试证据、artifact hash 与独立逐行审查。若停止，Arm-1/基线/分解/golden 必须明确 `NOT_RUN_STOP_GATE`，而不是伪造空结果。全程 `GO=NOT_MADE`；Codex 只给证据裁定候选，最终裁定由独立审查 agent 与人类完成。
