---
sop_applied: true
alpha_id: AR-GL4-LIVE-RECON
family: F0/F1/F4/F7
next_gate: INDEPENDENT_LINE_REVIEW_THEN_NO_GO_LEDGER_INTEGRATION
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL4 · 实盘真相账本：receipt 重建 2026-06-10→2026-07-13

> 纯历史、只读。没有下单、签名、广播、启动 executor、写 signals/Redis 或修改 live/canary；`GO=NOT_MADE`。报价只作未卖库存的显式经济标记，绝不冒充成交。

## 一句话给人

这个月两个真钱钱包一共实际买入 0.169920 WETH，按每笔买入块上的链上 ETH/USD 约 $288.16；其中两组 USDC 受控烟测和一组 NEXIO 真正卖回，三组严格已实现合计亏 0.000011451892 WETH（约 $0.03），0/3 获胜。其余 JOTCHUA、BOT 和三笔 PARADAENGI 没有卖出 receipt，代币仍完整躺在钱包，但池 WETH 到窗口末都坍塌至少 99%；按窗口末同尺寸链上可执行报价（PARADAENGI 三笔明确 revert 为零）经济净值合计约亏 0.160235862229 WETH / $271.51，8 个已判定入口 0 胜。所以真金亏损的主因不是已观测到的报价—成交滑点，而是 rug/撤池真实兑现，加上 F、trail、仓位规模、模型与 GL3 估计量并不一致；+56% 还能保留为冻结历史样本的报价型有界事实，却不能再叫真钱或前瞻 edge，GL1-02 在这个小样本实盘尺度应判 FAIL。

## 口径先于结果

预注册 SHA256=`6cff51454d40b7623cfcd3cf2ed2e10ac461019c67a88e4c921a33547e4ad7b7`。冻结规则要求 `dry_run=false/等价武装状态 + Base tx hash + status=1 receipt + 钱包资产移动` 四项同时满足；approval、status=0、模拟、报价和 dry-run 都不是成交。严格已实现只认成功卖出 receipt；未卖库存另列链上可执行经济标记。

机器全量清单读取 633 个文件 / 71,817,534 bytes，指纹 `498d86564ba8366cd14406ff1d943f63e0e5c67b0dce97064a736d6096c3bfe2`；独立只读复核另以较窄去重口径完整读取 602 个文件 / 71,452,869 bytes，指纹 `75bc593077b909c95702253a959bc722781bb7c55e30dd20a2f6ebb2304c8ae1`。两者都不是抽样。

## A. 真金战绩

|指标|receipt 结果|
|---|---:|
|成功真实 swap 腿|11（买 8 / 卖 3）|
|累计真实买入本金|0.169919978298 WETH / $288.16|
|排除两组蓝筹烟测后的真钱/自然 canary 本金|0.163919978298 WETH / $277.97|
|严格闭环已实现 PnL|-0.000011451892 WETH / $-0.03|
|闭环胜率 / 最坏闭环|0/3 = 0.00% / -0.000004994236 WETH|
|未卖但链上确认流动性坍塌|5 仓；本金 0.161419978298 WETH；窗口末可执行标记 0.001363163127 WETH|
|receipt + 窗口末可执行经济 PnL|-0.160235862229 WETH / $-271.51|
|全部已判入口胜率 / 最坏单笔经济结果|0/8 = 0.00% / -0.057001012225 WETH|
|BOT 失败卖出|48 次，均 status=0、不是成交；烧 gas 0.000163143764 WETH|
|全部交易相关真实 gas|0.000173370994 WETH / $0.30|

### 逐入口结果

|入口|段|买入 receipt|真 in / 买入块 USD|真 out 或窗口末精确卖出标记|真实 gas|PnL|裁定|
|---|---|---|---:|---:|---:|---:|---|
|JOTCHUA|fee100_fishing_canary|`0xd585074d…447e4681`|0.002666666667 / $4.67|0.000272821951 WETH (MEASURED_QUOTE_NOT_FILL)|0.000000739088|-0.002394583804 WETH / $-4.18|UNSOLD; STRANDED_LIQUIDITY_COLLAPSE|
|BOT|fee100_fishing_canary|`0xea6a3062…06ffb74e`|0.014553311632 / $24.98|0.001090341176 WETH (MEASURED_QUOTE_NOT_FILL)|0.000164194389|-0.013627164844 WETH / $-23.33|UNSOLD; STRANDED_LIQUIDITY_COLLAPSE|
|NEXIO|legacy_canary_roundtrip|`0x553fcd47…8412ba82`|0.002500000000 / $4.37|0.002497501503 WETH receipt|0.000001984227|-0.000004482724 WETH / $-0.01|CLOSED_REALIZED_LOSS|
|USDC|manual_bluechip_smoke|`0xd925db0d…2ad2547f`|0.003000000000 / $5.21|0.002997000109 WETH receipt|0.000001994344|-0.000004994236 WETH / $-0.01|CLOSED_REALIZED_LOSS|
|PARADAENGI-1|keystone_natural_live|`0x84078a4b…d6134a01`|0.057000000000 / $98.53|0.000000000000 WETH (EXPLICIT_EXECUTION_REVERT)|0.000000824372|-0.057000824372 WETH / $-98.53|UNSOLD; STRANDED_LIQUIDITY_COLLAPSE|
|PARADAENGI-2|keystone_natural_live|`0x8ceef47a…0b5989be`|0.057000000000 / $98.07|0.000000000000 WETH (EXPLICIT_EXECUTION_REVERT)|0.000001012225|-0.057001012225 WETH / $-98.07|UNSOLD; STRANDED_LIQUIDITY_COLLAPSE|
|USDC|signal_path_controlled_smoke|`0x062e8c92…bf6430c6`|0.003000000000 / $4.97|0.002999822323 WETH receipt|0.000001797256|-0.000001974933 WETH / $-0.01|CLOSED_REALIZED_LOSS|
|PARADAENGI-3|keystone_aggressive_live|`0x84fb93fc…a566ecf1`|0.030200000000 / $47.37|0.000000000000 WETH (EXPLICIT_EXECUTION_REVERT)|0.000000825092|-0.030200825092 WETH / $-47.37|UNSOLD; STRANDED_LIQUIDITY_COLLAPSE|

### 11 条成功成交腿（receipt 原值）

|UTC|方向|标的|tx|block|ETH/WETH in|ETH/WETH out|token amount|gas WETH|链上 ETH/USD|
|---|---|---|---|---:|---:|---:|---:|---:|---:|
|2026-06-18T13:27:13Z|BUY|JOTCHUA|`0xd585074d…447e4681`|47499943|0.002666666667|0.000000000000|863.98553565|0.000000739088|$1,749.73|
|2026-06-21T12:29:21Z|BUY|BOT|`0xea6a3062…06ffb74e`|47627807|0.014553311632|0.000000000000|90146.69258755|0.000000771589|$1,716.40|
|2026-06-22T08:33:43Z|BUY|NEXIO|`0x553fcd47…8412ba82`|47663938|0.002500000000|0.000000000000|594452.45527363|0.000000811050|$1,746.02|
|2026-06-22T08:36:15Z|SELL|NEXIO|`0xb8468729…34a9e155`|47664014|0.000000000000|0.002497501503|594452.45527363|0.000000893109|$1,746.02|
|2026-06-22T17:54:23Z|BUY|USDC|`0xd925db0d…2ad2547f`|47680758|0.003000000000|0.000000000000|5.21242200|0.000000831082|$1,738.02|
|2026-06-22T17:54:33Z|SELL|USDC|`0xa755efd0…8e81e39b`|47680763|0.000000000000|0.002997000109|5.21242200|0.000000828552|$1,738.02|
|2026-06-23T04:12:11Z|BUY|PARADAENGI-1|`0x84078a4b…d6134a01`|47699292|0.057000000000|0.000000000000|6069678.74367983|0.000000824372|$1,728.58|
|2026-06-23T04:51:35Z|BUY|PARADAENGI-2|`0x8ceef47a…0b5989be`|47700474|0.057000000000|0.000000000000|6795989.79561892|0.000001012225|$1,720.49|
|2026-06-23T18:04:15Z|BUY|USDC|`0x062e8c92…bf6430c6`|47724254|0.003000000000|0.000000000000|4.97152400|0.000000830710|$1,658.15|
|2026-06-23T18:06:19Z|SELL|USDC|`0xaefbb39f…f09be091`|47724316|0.000000000000|0.002999822323|4.97152400|0.000000966546|$1,655.09|
|2026-06-29T01:10:37Z|BUY|PARADAENGI-3|`0x84fb93fc…a566ecf1`|47953045|0.030200000000|0.000000000000|1885152.45765959|0.000000825092|$1,568.43|

**账本纠错。** `live_ledger.csv` 只有 JOTCHUA 一行且写 `amount_out=0`；实际 receipt 收到 863.9855 个代币。`canary_executor.py` 随后因 `tokens<=0` 把仓位从 state 丢掉，没有 SELL。BOT 的 48 次卖出全部 status=0。早期审计称当前 executor nonce=0/no swaps 只是在 6 月 22 日早时点成立，不能覆盖其后钱包历史。

## B. 开盘 / 暂停 / 重启

|时间 UTC|状态|白纸原因|精度 / 锚点|
|---|---|---|---|
|2026-06-18T09:30:01Z|OPEN|fee100 fishing canary started with --live, $8 per trade|EXACT_LOG; `mvp/diagnostics/live/loop.log:5,15`|
|2026-06-18T13:27:13Z|LOSS_AND_ACCOUNTING_DROP|JOTCHUA receipt succeeded, parser recorded tokens=0 and removed the position without a SELL|EXACT_RECEIPT; `mvp/diagnostics/live/loop.log:2933-2963; scripts/live/canary_executor.py:153-156`|
|2026-06-18T17:04:16Z|CANARY_LOG_END|last live loop record; no written stop reason|EXACT_LOG_STATE_UNKNOWN_AFTER; `mvp/diagnostics/live/loop.log:5987-6000`|
|2026-06-22T00:00:00Z|PAUSED|SYSTEM_PANIC set while blacklist, mintability, canary and daily-loss holes were repaired|DAY_BOUND_ONLY; `mvp/audits/disaster_left_tail_audit_20260622/config_fix_changelog_cn.md:1-17,34-35`|
|2026-06-22T17:54:23Z|CONTROLLED_SMOKE_WHILE_PAUSED|manual WETH/USDC receipt roundtrip; panic remained active and this was not natural strategy selection|EXACT_RECEIPT; `mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:610-664`|
|2026-06-23T04:12:11Z|OPEN_UNEXPECTEDLY|human had authorized execution, but SYSTEM_PANIC was unexpectedly absent; first of two natural PARADAENGI buys|OPEN_TIME_BOUNDED_BY_FIRST_RECEIPT; `mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:687,699-728`|
|2026-06-23T04:52:03Z|PAUSED|human explicitly ordered manual_pause_20260623_after_2_honeypots after two abandoned 0.057 ETH positions|AFTER_LAST_ABANDON_BOUND; `mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:732-746`|
|2026-06-23T17:49:13Z|CONTROLLED_REOPEN|panic cleared for signal-path WETH/USDC smoke|EXACT_WATCH_LOG; `infra/compose/smoke_watch.log:1-6`|
|2026-06-23T18:04:13Z|AUTOMATIC_REARM_AFTER_SMOKE|controlled smoke detected and watchdog re-armed|EXACT_WATCH_LOG; `infra/compose/smoke_watch.log:1-6`|
|2026-06-23T18:15:48Z/18:16:56Z|OPEN|human-confirmed autonomous $50 live; panic deleted, .0302 ETH, concurrency 3|BOUNDED_RUNTIME_WINDOW; `mvp/audits/go_live_session_20260624/conversation_transcript_full_cn.md:1833-1841`|
|2026-06-25T17:39:32Z|RESTART_OPEN_AGGRESSIVE|after panic/rebuild/dry-print, historical F lists disabled and trail .25 enabled|EXACT_RUNTIME_LOG; `mvp/audits/golive_aggressive_20260625/report_cn.md:135-149`|
|2026-06-25T15:47:00Z/2026-06-27T06:03:00Z|BRIDGE_OUTAGE_NOT_PROVEN_PAUSE|outage artifact reports seven pools and zero would-have-bought; cause and executor/panic state absent|INTERVAL; `mvp/audits/outage_missed_20260627/outage_missed_counterfactual.json:3-20`|
|2026-06-29T01:10:37Z|THIRD_LOSS_THEN_UNKNOWN|third PARADAENGI receipt; no eligible white-on-black pause/restart artifact afterward|EXACT_RECEIPT; `mvp/audits/blacklist_framework_recompare_20260629/blacklist_framework_recompare.json:607-616`|

6 月 29 日以后没有合格的 runtime banner、panic 记录或部署日志，因此没有把“事故后必然暂停/重启”的人类记忆倒填成事实。6 月 25–27 的 artifact 只证明 bridge outage 和零笔 would-have-bought，不足以证明 executor/panic pause。

## C. 实际配置 pin 与 +56% 估计量

### 实际 runtime 分段

|段|live pin|模型/规模|F / trail / E|主要门|证据|
|---|---|---|---|---|---|
|2026-06-18 fee100 canary|LIVE|corrected_method_detector, not KEYSTONE; size=0.0026667 actual / $8 flag; conc=2|F=not present; trail=arm +5%, pullback 3%; E=not present|fee100 onset/heat/Rule A-B/two-way QV2|`scripts/live/canary_cycle.sh:15-30; scripts/live/canary_executor.py:237-249`|
|2026-06-22 controlled smoke|PANIC_ACTIVE_MANUAL|not natural selection; size=0.003; conc=3|F=not yet deployed; trail=1.0 = disabled; E=not present|manual bluechip|`go_live_session transcript:610-679; disaster audit:84-93`|
|2026-06-23 first natural KEYSTONE|dry_run=false; panic unexpectedly absent|live pickle bd02d79d...; params 749a61da...; effective p<=.27; size=0.057; conc=3|F=not yet deployed; trail=1.0 = disabled; E=not present|MIN_PRE5, friction>=-5%, sell_sim|`go_live_session transcript:699-802,1286-1321`|
|2026-06-23 18:16 through 2026-06-24 ~05:00 initial defensive $50 live|dry_run=false; panic cleared|same live pickle/params hashes; size=0.0302; conc=3|F=session snapshot says 2 deployers + 1 PARA skeleton; later FP audit says PARA skeleton was removed and rebuilt, so exact transition time is bounded/contested; trail=1.0 = disabled; E=1800s/3 blocks/collapse .5|p<=.27, MIN_PRE5, depth>=3 fail-closed, friction>=-5%, sell_sim/external|`mvp/audits/go_live_session_20260624/go_live_session_full_cn.md:182-194,313-326; mvp/audits/cluster_blacklist_fp_validation_20260624/skeleton_false_positive_validation_cn.md:56-71`|
|2026-06-24 historical-F expansion through 2026-06-25 aggressive cutover|deployed before 06-25 runtime comparison; exact deploy timestamp absent|same live pickle/params hashes; size=0.0302; conc=3|F=245 historical deployers + 33 safe skeletons + 2 PARA deployers; PARA skeleton off; trail=1.0 = disabled; E=1800s/3 blocks/collapse .5|p<=.27, MIN_PRE5, depth>=3 fail-closed, friction>=-5%, sell_sim/external|`mvp/audits/funding_cluster_poc_20260624/deployer_funding_cluster_cn.md:79-91; mvp/audits/golive_aggressive_20260625/report_cn.md:8-28`|
|2026-06-25 aggressive restart|17:39:32 live|same live pickle/params hashes; size=0.0302; conc=3|F=historical 245+33 OFF; only 2 PARA deployers + runtime learned; trail=0.25; E=1800s|p<=.27, MIN_PRE5, depth>=3, friction>=-5%, sell_sim/external|`mvp/audits/golive_aggressive_20260625/report_cn.md:8-46,135-149`|
|2026-06-29 after third incident through 07-13|UNKNOWN|current source/post-fix research scenario cannot backfill runtime; size=UNKNOWN_AFTER_INCIDENT; conc=UNKNOWN|F=UNKNOWN; trail=UNKNOWN; E=UNKNOWN|UNKNOWN|`no contemporaneous runtime banner/deploy log; current source defaults rejected by prereg conflict priority`|

### 实盘 vs GL3 / reaudit10 +56%

|字段|实际实盘|+56% 冻结估计量|相同？|
|---|---|---|---:|
|population/model|live pickle trained through 2026-06-01, effective p<=.27|431 same-age candidates / 116 selected; B5 retrain through 2026-02-27; p_cut=.2808669872|**否**|
|notional/portfolio|.057 then .0302 WETH; concurrency 3|.001 WETH per estimand; no wallet concurrency simulation|**否**|
|entry/safety gates|MIN_PRE5, later depth3/F/sell-sim/external/E|prob/friction/regime only; no explicit depth/F/sell-sim/external/E|**否**|
|exit|trail 1.0 off, then .25; 4h|legacy priced-live trail .35 / 4h, trigger +1/+5/+15|**否**|
|execution evidence|11 receipt-backed legs, actual gas, .03-.057 WETH natural size|mostly guarded atomic/QV2 historical quote; $0.075 attempted-leg gas|**否**|
|date/regime|2026-06-18..06-29 fills|frozen historical sample ends by 2026-06-08|**否**|
|aggressive F-off 207 rugs|F was disabled 06-25; third PARA entered 06-29 and became unsellable|not the same cohort; separate aggressive backtest admitted 207 rugs and treated their profitability as core in-sample assumption|**否**|

“策略没变过”据此为假。最多只能说已核的 6/22–6/29 live entry pickle/params 哈希稳定；风控与退出至少两次改变。aggressive 回测不是保守回测的小参数同口径：它关闭 historical F，故意放入保守版会拦掉的 207 个已知 rug；报告也明说这些 rug 仍能盈利是核心、样本内上界假设。06-29 第三笔 PARA 在这个 F-off 段进入并最终精确卖出 revert，是这个假设的直接真钱反例。

## D. 预注册归因分桶

|互斥桶|WETH|证据与量级|
|---|---:|---|
|执行 / 滑点 gap|0.000000000000|两组深池受控烟测自报 swap deviation 0.0%；自然单没有全部可复现的同块原子反事实，未知部分按预注册进 residual，不能填零。没有证据表明它足以翻转 +56%。|
|rug 兑现|-0.160056815171|五仓完整代币仍在钱包、池 WETH 窗口末坍塌≥99%；JOT/BOT 只计精确末窗 QV2 余值，三 PARA 明确 revert=0。占经济损失 99.89%。|
|regime / 衰减|0.000000000000|非加性诊断：真钱在 GL3 样本结束后，aggressive June OOS 过闸 n=0；但没有非-rug 亏损量可独立归入此桶，故识别量为 0，不等于无 regime 风险。|
|选择 / 择时|0.000000000000|缺完整候选全集与精确开停窗反事实，按预注册识别量 0、状态 UNKNOWN；不拿停盘期间故事凑解释。|
|小样本 / 未识别 residual|-0.000179047058|闭环损失、成功/失败/approval gas 及所有无法逐笔识别的执行或 regime 分量。n=8、0 胜只是诊断，不证明“纯噪声”。|
|**严格恒等式**|**-0.160235862229**|`execution + rug + regime + selection + residual == actual`，与实际 `-0.160235862229` 逐浮点值完全相等。|

## E. 对质裁定（待独立逐行终审落最终 grade）

1. **GL1-02：FAIL at observed tiny live scale。** 成功 receipt 不是零：三组闭环净值全负；把链上流动性坍塌的五个未卖仓按末窗精确可执行值标记后，整体仍约 -0.160236 WETH / -$271.51。

2. **没有证据把 GL3 的冻结历史算术本身推翻。** GL3 的 116 子集、.001 WETH、旧模型/退出和历史原子报价，与本月 live 不是同一个估计量；因此 +56% / GL1-12 只能保留为有界历史事实，不能再外推为真钱或未来 edge。

3. **真钱结果推翻了“活-待真钱延迟”的实盘含义，并要求降级 aggressive 叙事。** 真钱已经跑过且亏；两组受控深池没有可见 fill gap，几乎全部损失来自撤池/rug 兑现。F-off 的 207-rug 盈利假设是样本内 artifact，不是可用于 GO 的保守证据。

4. **相容不是背书。** 历史 +56 与真金亏可以因配置、规模、时段和 rug 机制不同而逻辑相容；这只避免把不同 estimand 粗暴相减，不会把负实盘重新说成正 edge。

建议最终分级：GL1-02=`PINNED / FAIL_AT_OBSERVED_TINY_LIVE_SCALE`；GL1-01 与 GL1-12 的历史 grade 保留，但 scope 必须加粗限定“历史/报价型/非实盘/非前瞻”。最终 grade 由独立审查 agent 决定，本文不自我背书。

## 证据等级与限制

- receipt、tx、block、Transfer/WETH logs 和 Chainlink 历史 round 是主证据；旧 narrative 计数若冲突已被覆盖。
- 三个真正卖回的闭环是严格 realized；五个未卖仓不是 realized sell，经济结果使用窗口末 exact-size QV2，只有明确 revert 才记零。
- JOTCHUA/BOT 仍有极小末窗报价，所以没有默认全损；PARADAENGI 三笔的零值有明确 execution revert。
- 6/29 后 runtime 为 UNKNOWN；没有拿当前源码默认值回填。
- USD 是每笔 receipt block 的 Base Chainlink ETH/USD；跨时现金流 USD 合计用于人类量级，不替代 WETH 原生 PnL。

## SOP / 安全核对

- 预注册先于 outcome 读取，SHA sidecar 已冻结；没有训练或改模型。
- `failed_no_7d`、label freeze、feature store、strategy/executor 分支只作历史源读取；本轮不改代码路径、不产 live signal。
- executor 未启动；无 send/sign/broadcast RPC；无 Redis 读写；无 `.env`/私钥输出。
- 没有复用 paper 指标覆盖 real PnL；Sprint075–079/086 与 `mvp/runs` 明确排除为 paper/dry-run/harness。
- `training_allowed=false`、`live_allowed=false`、`self_go_allowed=false`、`GO=NOT_MADE`。

## 可复现锚点

- 机器账本 SHA256=`2dde9b71f8cb4ab18d3787bd1c991ef0777b1e3d35da384f47d1c93275148064`
- corpus manifest SHA256=`51a87dbcd8c36a1271d027718d62692ef42b4a5d46fc3fa0bc2590e8fae69f2c`
- RPC full ledger SHA256=`76fb9ecbc59b784caffb6a7836f6a42906d64ab7d9bb1d5cfce814448b4f267f`
- live model SHA256=`bd02d79d016fb112b2ba8fff95f968b4036bb03a80818ee5b825efa4d714ae6b`；params SHA256=`749a61da5dc3ac0d500b071edfccaf205d1b6141853d4057e343e7736c564cd6`
- 预注册 SHA256=`6cff51454d40b7623cfcd3cf2ed2e10ac461019c67a88e4c921a33547e4ad7b7`

重跑：先由操作者仅向进程注入 `EXECUTOR_READ_RPC_URL`（必须含 `prettiest-blue-aura`，脚本不读真实 `.env`），再运行 `python3 scripts/research/base_gl4/gl4_live_reconciliation.py && python3 scripts/research/base_gl4/gl4_render_report.py`。回归测试另见 `tests/test_base_gl4_live_reconciliation.py`。
