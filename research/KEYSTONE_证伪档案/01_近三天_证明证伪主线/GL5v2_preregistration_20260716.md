---
task_id: GL5-v2
target_estimand: 2026-06-24_Fon_644_to_120
date: 2026-07-16
frozen_before_any_gl5v2_repricing: true
research_only: true
training_allowed: false
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
---

# GL5v2 预注册：644/120 F-on 可实现出场重定价

本文件在重建 120 行名单、查询其出场成交、前瞻扫描或汇总任何 Arm-1 结果之前冻结。此前只读了任务书、原始 JSON/源码、全表级覆盖范围和文件 hash；没有查看 120 子集的新重定价结果。

## 1. 唯一对象与 Step 0

唯一对象是 `mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest.json` 的 `lenses.qv2.live_full_gates` 及同配置 `fork` lens。禁止引用 431/116 的 +56% 为其 headline。

冻结对象：

- 母体：`_pools.tsv` 1275 行；formal source window 与 observed source coverage 均为 2025-10-01 00:54:35Z 至 2026-06-01 22:23:01Z。
- labeled universe：644 行；observed coverage 为 2025-10-01 02:07:57Z 至 2026-06-01 22:23:01Z，entry block 36,171,321 至 46,781,917。
- 模型训练切点：1275 行 `t0` 的 60% quantile，2026-02-03 21:45:33Z；765 train / 510 test。GL5v2 只复现冻结 learner，不做模型更新或 promotion。
- gate：`ns_pre>=5`、`p_fatal<=0.27`、entry WETH reserve `>=3 ETH`、friction `>=-0.05`、F deployer/skeleton 不命中。
- F：245 `safe_rug_deployers` 加 2 PARADAENGI = 247 deployer；33 safe skeleton。名单、creator map、token skeleton 必须逐文件 pin SHA。
- 仓位：0.0302 ETH；固定 ETH/USD 1658.75，即 $50.09425；round-trip gas $0.15；额外 haircut 2%。
- legacy exit：`keystone_live.exit_block()` 的 hard stop -50%、peak drawdown 35%、最长 7200 blocks；无 TP、无 TP ladder。exit block 来自 `_priced_live.json.exb`。
- 原 QV2 return：`_priced_live.json.net - 0.15/50.09425 - 0.02`。
- 原 fork return：`_realgas_replay.json.real_net - 0.15/50.09425 - 0.02`。

Arm-0 必须不覆盖历史 JSON，而是在内存重建并逐位比较。至少以下字段对两个 lens 都必须 Python `==`：`pass_n=120`、`dr_traded=0`、`dr_blocked=262`、`monthly=0.08574380412768878`、`mdd=-0.003931798477305182`、`fatal_rate=0.016666666666666666`、`fatal_n=2`、`mean_ret=1.4280241443411608`；并逐值比较完整 `live_full_gates`、gate breakdown 和 blocked reasons。数值备用绝对容差 `1e-15`，但先要求 `==`；任何不等即 `STOP_ARM0_REPRODUCTION_DRIFT`。

Step 0 证据构成按“原脚本实际使用的证据”而非事后美化分类：真实 receipt SELL、同块 Atomic 守卫的 quote、无守卫 QV2 quote、fork 模拟、confirmed rug、unresolved。原脚本未读交易 receipt，也未调用 D5；所以不得把 fork 模拟称为真卖，或把裸 QV2 称为 guarded quote。对每个 lens 给 120 行完整计数。

## 2. 数据与安全边界

- 真实成交：本地 `uniswap_v3_swaps_decoded FINAL`，chain 8453、removed=0；其 canonical log 只可能来自成功交易，视为 `receipt.status=1` 的已落链 swap 证据。覆盖母体 blocks 36,171,321–46,789,998。
- reserve：主定义仍为 `WETH.balanceOf(pool)@block`。只有具备 entry absolute balance 与区间完整 WETH transfer-net 时才计算 collapse；swap-only delta 不能冒充完整 reserve。
- 当前 `EXECUTOR_READ_RPC_URL` 未注入。禁止读取 `.env`，禁止用 public RPC 冒充 `prettiest-blue-aura`。因此本轮新 archive RPC 为 0；任何需要新 balanceOf/atomic 的腿若无冻结证据必须 UNRESOLVED。
- `_entry_weth_644.json` 只提供 entry baseline；`base_pool_transfers_b3` 只在其 `base_transfer_fetch_status_b3` 对该 pool/interval 显示 completed 且完整覆盖时才可组成 transfer-net。否则不能确认无 collapse。
- `_realgas_replay.json` 的 `status=ok`/`exit_sell=ok` 是历史 fork 模拟，不是真实 SELL。它可作 atomic-like sell-path guard，但只有在独立前瞻存活证据存在时才能给 quote 分支使用。
- 已有 label/F 声明只作对照，不能单独判定 Arm-1 无 rug。

禁止 send/sign/broadcast、Redis/signals、executor/strategy 服务启停、live config 写入、训练/promotion 与 GO。

## 3. Arm-1 时钟、真卖与前瞻扫描

- target：legacy `exb + offset`；offset 主值 1 block，敏感性 5/15 blocks；nominal 2 秒只用于展示，计算只用 block。
- 真实执行滞后带：target 后 15 blocks；敏感性 5/30。
- 真实 SELL 匹配窗：`[target, target+15]`，端点包含。按 `(block_number, log_index)` 取最早一笔非零 token-in/WETH-out SELL，不要求第三方单笔量覆盖 0.0302 ETH，因为任务书本版只要求“任意地址真实 SELL”；另报其 WETH size 与是否达到反事实仓位。
- entry price proxy：从 `[entry_block, entry_block+900]` 取最早一笔非零 WETH-in/token-out BUY；用实际 WETH/token 成交价。找不到 entry BUY 时，即使找到 SELL 也不能计算真实 return，分类为 `UNRESOLVED_ENTRY_PRICE`。
- 实际 return：用 `exit_price/entry_price-1 - 0.15/50.09425 - 0.02`。若真实 SELL 量小于按 entry price 换算的反事实 token qty，仍可证明池可卖，但 return 点只在 `size_coverage>=1` 时纳入；不足则 `UNRESOLVED_SIZE` 并保留上下界。
- collapse 主阈值：相对 entry WETH balance 下跌 `>=95%`（post/pre `<=0.05`）；敏感性 90%/99%。首次达阈值 block 为 collapse block。
- 扫描区间：entry block 到 target+lag，端点含。只有完整 transfer-net/balanceOf 覆盖才可确认 collapse 或确认未 collapse。
- 失败 receipt：只有已保存的 status=0/revert 加完整无恢复或 reserve collapse 才能确认全损；失败本身不自动推导永久 -100%。

证据优先级：

1. 合格真实 SELL（且 entry price、size coverage 完整）→ 用真实价。
2. 无合格真卖，完整 reserve 扫描确认 target 前 collapse 且窗口无恢复 → `CONFIRMED_COLLAPSE`，用最后确认可回收值；没有正回收证据才 gross=-1。
3. 无真卖，完整扫描确认存活到 target+lag，且 target 对应的冻结 fork/atomic guard 与 exact quote 同块有效 → 用 exact quote；旧证据仅存在于 legacy exb，不能挪到 +1/+5/+15。
4. 数据不足、offset 无同块 quote/guard、entry/size 不完整 → `UNRESOLVED`。

真实 SELL 以后出现但不满足点估计条件，可作为 `SURVIVAL_ONLY`，不能擅自给满额 quote。

## 4. UNRESOLVED 双边界

- 点估计排除全部 unresolved。
- 乐观界：对应 pool 的旧 F-on Arm-0 adjusted return；QVV2/fork 两个界分别报告。若旧 return 缺失，用同窗 resolved 中位数。
- 悲观界：`-1 - 0.15/50.09425 - 0.02`；不另加未发生 gas。
- 120/644 population interval：resolved Arm-1 点值加逐行 optimistic/pessimistic 替代后，普通算术平均。
- 确认 rug rate 区间：lower=`confirmed collapse unique pools / N`；upper=`(confirmed collapse + rug-relevant unresolved) / N`。不得把 upper 当点估计。

## 5. 三臂、分解与裁决

- Arm-0：原 644→120 F-on QV2 与 fork lens。
- Arm-1：同 120、同 legacy trigger、offset +1/+5/+15，新优先级。
- baseline：同 644，用相同 Arm-1 规则；不重新训练或更换 gate。
- 原报价依赖率：`(unguarded_quote + guarded_quote + fork_simulation)/120`，另逐类报告，不能把两个 lens 相加成 240 笔。
- quote-washed rug rate：原 quote/simulation legs 中 Arm-1 confirmed collapse 数 / 可完整前瞻扫描的 quote/simulation legs；同时报以全 120 为分母。
- survived quote rate：完整前瞻确认存活且同块 guard/quote 可用 / 可完整扫描 quote legs。
- positive-profit illusion share：两臂均 resolved 时 `sum(max(0,a0)-max(0,a1))/sum(max(0,a0))`；unresolved 单列。
- F-on rug exposure：主 offset 下 confirmed collapse unique pools；与 old label `dr_traded=0` 和 aggressive 207 只作对照。

右尾逐 offset：resolved pooled mean >0；去 top-1% mean >0；top-1% positive-profit contribution <=25%；所有 n>=20 的 rolling 30-day/step15-day mean >=-10%；seed 20260716、10,000 bootstrap paths terminal p5 >100R；MDD p95 <=20R。primary unresolved >0 时形式状态必为 INCONCLUSIVE。

结论：三个主 offset 全部形式 PASS 且 illusion share<=10% 才是 `EDGE_SURVIVES`；任一 mean<=0 或 illusion share>=50% 才是 `EDGE_COLLAPSES`；否则 `INCONCLUSIVE`。

## 6. Golden 与回归

golden 按 `sha256("GL5v2-golden|"+pool+":"+offset)` 排序选 5，尽量含 2 collapse/unsellable、1 survived；类别不足则全纳并披露。由于授权 archive env 未注入，本轮 golden RPC 状态预注册为 `RPC_NOT_RUN_ENV_UNSET`，但仍对 CH swap、冻结 fork、input hash 做独立对账。不得为满足数量改阈值或换 RPC。

回归覆盖 Arm-0 完整字段、原证据构成、priority 1/2/3/4、collapse 90/95/99 边界、size coverage、UNRESOLVED 双界、三臂计数、无 RPC、GO。所有静态预期用 `==`。

## 7. 冻结输入 SHA256

| 路径 | SHA256 |
|---|---|
| `mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest.json` | `41803d1b17f4a6761585501a0bfa79591db2160039a8aeddc8bcabb49d37d92d` |
| `scripts/research/base_lefttail/live_config_rebacktest.py` | `cb082c01f63f113c925d813447a978bd67f7187f2c18ffd3e375f21da3bd4e08` |
| `scripts/research/base_lefttail/_priced_live.json` | `cfac3bbd1d74bda3f850b80985dfa578041fce409a06f14943d4797c3011e709` |
| `scripts/research/base_lefttail/_realgas_replay.json` | `b77bc2a6bff66fc853dab063ed501e9ab85b9487beb9a97f6e541b9a34016df6` |
| `scripts/research/base_lefttail/_entry_weth_644.json` | `5857d4abf0ecaccc48ec33e63c5690974be4cdcab9cd34b3c47a2f1274b0e2c9` |
| `scripts/research/base_lefttail/_token_skeleton_644.json` | `2f7c7664d9ec6b810f367d67597f1757154006d2caf4ece0a18cde79dcd514a1` |
| `scripts/research/base_lefttail/_rug_seed_validated.json` | `5766c73110468011c507887b2f880e149c6ec56c5ed9a605884b3b67bc43f8b4` |
| `services/executor/fixtures/safe_rug_deployers.json` | `d64b5e679a6ba6d2c2609842c8faf39d1c9e8a7eada0c09cfe08163847b555de` |
| `mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json` | `4d05cc2edb2d8ea6ede0ee70ce18636ddc9461293ea9ee394a17967a966813d4` |
| `scripts/research/base_lefttail/_pools.tsv` | `3e0f7d691396c651b7fdc1abab65601a99747318d249902b4b39665a0056f780` |
| `scripts/research/base_lefttail/_fwdpath.tsv` | `9888a91455e4a3aed1f9b395a2f58304558346f8fb89d73e4dc8dcdf72fceaae` |

发现任何事实错误按 GL5 纪律另列勘误，绝不静默改本文件。`GO=NOT_MADE`。
