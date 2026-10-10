---
task_id: GL5-v2-independent-review
review_target: 2026-06-24_Fon_644_to_120
review_verdict: PASS
decision_verdict_supported: INCONCLUSIVE
substantive_artifact_blockers: 0
go_decision: NOT_MADE
research_only: true
review_date: 2026-07-16
---

# GL5v2 独立逐行终审

## 终审结论

**PASS（接受工件，决策仍为 INCONCLUSIVE）。** 本审查没有发现换对象、把 +56% 冒充 F-on headline、把模拟冒充 receipt、把未知默认为安全/全损、分支算错、静默改预注册或触碰 live 的证据。GL5v2 主报告的强度边界正确：它只证明了少数有 canonical SELL 的条件子集，没有证明完整 120 笔 edge，也没有独立证明“F-on ⇒ rug≈0”。

没有阻止接收这批工件的 substantive blocker。阻止作出 `EDGE_SURVIVES`/`EDGE_COLLAPSES` 的实质证据缺口仍有两个：archive RPC 未注入且本地 transfer-net 不完整，故不能做完整 reserve collapse 扫描；任意地址 canonical SELL 只证明 pool-level 某笔可卖，不能证明策略钱包不存在地址特定限制。主报告没有越过这两个边界，故这些是**决策 blocker**，不是本次 `INCONCLUSIVE` 工件的完整性 blocker。

## 逐项审查

| # | 审查项 | 裁定 | 独立核验结果 |
|---:|---|---|---|
| 1 | 预注册先于结果冻结、无静默改 | PASS | 当前 prereg SHA256 为 `c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e`，与 sidecar、脚本常量、结果 JSON、逐腿 JSON、测试证据一致。文件时间顺序为 prereg 08:10:09、sidecar 08:10:17、实现 08:14:51、结果运行 08:16:57 UTC；现有工件无冻结后修改迹象。预注册原文直接披露 RPC env unset 与因此产生的 UNRESOLVED，不是看到结果后补写。 |
| 2 | 644/120 F-on 对象、配置、coverage、自己的 headline | PASS | 独立 import 2026-06-24 原源码重建 644 行；F union 为 247 deployer（245+2）、33 skeleton；QV2/fork 选择集合相同且均 120。644 observed 为 `2025-10-01T02:07:57`→`2026-06-01T22:23:01`，120 observed 为 `2025-10-01T02:07:57`→`2026-05-15T16:56:53`。两个 lens 的 `monthly == 0.08574380412768878`、`mean_ret == 1.4280241443411608`、`mdd == -0.003931798477305182`、fatal `2/120`，没有称为 +56%。 |
| 3 | 原 JSON 完整逐位复现 | PASS | 对两个 lens 独立重算 `live_full_gates`、gate breakdown、blocked reasons、fork crosstab，共 70 个叶字段；70/70 Python `==`，0 mismatch、0 tolerance fallback。原 source JSON SHA 为 `41803d1b...d37d92d`，源码 SHA 为 `cb082c01...3bd4e08`。 |
| 4 | 原证据构成 | PASS | QV2 120/120 为 `UNGUARDED_QV2_QUOTE`；fork 120/120 为 `FORK_SIMULATION`；原脚本 receipt SELL=0、Atomic guarded quote=0、confirmed rug=0、保留 unresolved=0。两个 lens 是同 120 池，报告没有加成 240。独立复算 fork crosstab `n=65`：64 `(qv2_ok,fork_ok)`、1 `(qv2_bad,fork_ok)`、其余 0。 |
| 5 | canonical SELL、方向、size、price、分支 | PASS | 独立重新查询 `uniswap_v3_swaps_decoded FINAL` 得 498,137 行、644 pools；规范 TSV SHA `80dc2bdf96d509e3f3c17fd82ef5ad20a0950e8baa9fba67e2043b804674d794` 与结果一致。按 V3 pool-perspective amount 符号及 `orient` 独立分类，644×3 共 1,932 腿与逐腿 JSON **0 分支差异**。F-on full-size REAL_SELL 为 31/34/38，survival evidence 为 62/64/64；所有 REAL_SELL 均满足目标窗、entry BUY 窗、整数仓位 coverage≥1，逐笔 `exit_price/entry_price-1-cost` 与 JSON 相等。 |
| 6 | branch totals、双边界、baseline、分解 | PASS | F-on 三档分支总数均 120；baseline 均 644。+1 分支严格为 REAL_SELL 31、UNRESOLVED_SIZE 20、SURVIVAL_ONLY 11、UNRESOLVED 58；+5 为 34/19/11/56；+15 为 38/22/4/56。独立逐行重算 population QV2/Fork bounds、resolved mean、baseline 与 illusion 分解均与机器结果相等；+1 QV2 界 `[-0.3606990866834547,1.4370311344990674]`。 |
| 7 | 0 collapse 与 rug 结论的对称诚实 | PASS | 每腿 reserve status 均明示 `INCOMPLETE_NO_FULL_TRANSFER_NET_OR_ARCHIVE_RPC`；confirmed collapse=0 没有被解释为 rug=0。无 survival 的 58/56/56 被放入 rug upper bound，得到 `[0,48.33%]`、`[0,46.67%]`、`[0,46.67%]`；收益 unresolved 进入双界而不进入点估计。`F-on ⇒ rug≈0` 被裁为未识别，合规。 |
| 8 | Golden 与 RPC shortfall | PASS | 5 条由冻结 hash 排序选出，类别为 3 REAL_SELL、2 UNRESOLVED；0 confirmed collapse 导致“至少 2 rug”无法满足，主报告和机器 JSON 均披露 category shortfall。所有 archive 项标 `RPC_NOT_RUN_ENV_UNSET`，新 RPC=0，未用 public RPC 替代。 |
| 9 | 回归、阈值边界、确定性 | PASS | `python3 -B -m unittest tests.test_base_gl5v2` 独立运行 10/10 PASS。90/95/99% 边界含端点，`1e-15` 修复与冻结的 `<=` 定义一致；测试证据披露初次失败及 0 result rows affected。将输出改写到 `/tmp` 后完整重跑，两份机器 JSON SHA 与提交工件字节级相同。 |
| 10 | no-live / GO | PASS | 生成脚本只读本地源和 ClickHouse，并写研究工件；没有 RPC client、sign/send/broadcast、Redis/signals、executor 启停、训练或 live config 写入。结果与测试均为 `GO=NOT_MADE`，decision flags 均 false。 |

## 独立数值锚

| offset | F-on branches（REAL / size / later / none） | survival | resolved mean | QV2 population bounds | rug interval |
|---:|---:|---:|---:|---:|---:|
| +1 | 31 / 20 / 11 / 58 | 62/120 | 1.5407292661261793 | [-0.3606990866834547, 1.4370311344990674] | [0, 0.48333333333333334] |
| +5 | 34 / 19 / 11 / 56 | 64/120 | 1.4617990311983722 | [-0.31896956270218835, 1.4387270371061625] | [0, 0.4666666666666667] |
| +15 | 38 / 22 / 4 / 56 | 64/120 | 1.1135535571619766 | [-0.3464208499191141, 1.4391985419805635] | [0, 0.4666666666666667] |

Baseline resolved/644 为 64/65/64，branches 分别闭合到 644；其 unresolved 为 580/579/580。`resolved alpha` 只比较被真实 SELL 选择过的稀疏子集，主报告已明确不把它当完整因果 alpha。

## 方法边界与非阻断问题

1. `REAL_SELL` 是任意地址的 canonical swap。它按任务与预注册可证明某笔 pool-level 流动性和成交价，但不能证明策略钱包必然可通过 token 的地址级限制；不得把 31/34/38 叫作策略账户 receipt。
2. return 的入场价是 `[entry, entry+900]` 内最早第三方 BUY proxy，不是策略自身 receipt，也未要求该 BUY 有 0.0302 ETH size coverage。它适合冻结口径下的 proxy-return 对账，但 resolved-only +111%～+154% 不能升级为可部署回报估计。
3. 原 120 笔 100% 依赖报价/模拟；当前完整 population 的 quote illusion share 因大量 unresolved 仍是 `NOT_IDENTIFIED`。负的 matched-pair illusion 只适用于 31/34/38 个被选择的 pair，主报告对此没有外推。
4. filesystem mtime、sidecar 和嵌入 SHA 能支持“prereg 在结果前冻结”，但任何单机文件系统审查都不能证明作者在落 prereg 前从未看过别处的同类结果；本审查只裁定现有链条无漂移迹象。

## 证据与复现

- prereg：`c3d411bc07509df4e791b2094cb9145cdb43d19626633fdcb3bedb645bef970e`
- source JSON：`41803d1b17f4a6761585501a0bfa79591db2160039a8aeddc8bcabb49d37d92d`
- source script：`cb082c01f63f113c925d813447a978bd67f7187f2c18ffd3e375f21da3bd4e08`
- result JSON：`2a13c95d1ad6fd645d4e7037ae0e436a3f90f22112bc036b1b145dfcc269dcb8`
- per-leg JSON：`1976a18ba40418b2c4c68806460c28d66aa1663c8f47ee6f5ca5190d64dad8f4`
- test evidence：`36b622a1f72b3f3c5c35c811fbc61fbb8d982aee4cc108f6514244759a42b9c5`
- implementation：`ed57476f2b28643e8d865717cfa1c1d5b9116ecba3a3f6b93d0be6b172736995`
- canonical swap content：`80dc2bdf96d509e3f3c17fd82ef5ad20a0950e8baa9fba67e2043b804674d794`

最终终审意见：**工件 PASS；edge/rug 决策 INCONCLUSIVE；GO=NOT_MADE。**
