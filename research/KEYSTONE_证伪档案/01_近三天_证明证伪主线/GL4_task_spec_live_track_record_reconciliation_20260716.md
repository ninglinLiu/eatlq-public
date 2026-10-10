---
sop_applied: pending
alpha_id: AR-GL4-LIVE-RECON
family: F1/F4/F6/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL4 · 实盘真相账本：还原真实战绩，并拿它对质 IRON +56%

> 审查/指挥 agent 下发。大任务,一次通读全月实盘语料再交,最后逐行审。
> **起因**:GL1/GL2/GL3 账本几乎只用回测/回放证据,系统性无视了过去一个月"真实上过实盘"这件事。人类指出:近一个月新增文件基本 100% 与实盘有关,而我(审查 agent)只瞄了几篇就以偏概全。本轮把真实战绩完整还原,并回答核心矛盾:**回放说 +56%,真金为什么是亏的。**

## 0. 人类给的记忆锚(当假设去核实,不当定论,证据冲突以证据为准)
真金投过、**规模很小(几十~几百美元级)**、**整体是亏的**、因此**反复开盘→亏→暂停→重启**。请用文件证据确认或修正这个画面,不得无证据地推翻它,也不得无证据地照抄。

## 1. 语料范围(通读,别再抽样)
过去一个月(约 2026-06-10 → 2026-07-13,排除 07-14 起的 GL/RM 工作)与实盘相关的全部:
- 执行栈:`services/executor/src/*`(tx_sender/swap/position/preflight_trade/external_risk/cluster_blacklist/main…)、`services/strategy/`、`services/signal_bridge/`(bridge/train_model/_model.pkl/_params.json)。
- canary/live 循环:`scripts/live/*`(armed_live_loop/canary_executor/canary_ledger/mvl_canary_roundtrip…)。
- 真实台账:`mvp/diagnostics/live/live_ledger.csv`、`canary_state.json`、各 `*realized_pnl*`、`keystone_eth_usd_pnl.json`。
- 运行日志:`logs/sprint075~079/086/*`、`mvp/runs/*`。
- 审计trail:`mvp/audits/*`(go_live_session、live_config_rebacktest、aggressive_live_rebacktest、execution_readiness、sprint*_live_review/pilot/canary…)、`docs/keystone-go-live.md`、`configs/*live*/canary*`。

## 2. 必须还原/交付

### A. 真金战绩(区分真实 vs paper/canary-dry)
- 逐笔列出所有 `dry_run=false` 的**真实成交**:时间、池、size、tx_hash、**链上 receipt 核对的真实 in/out**、真实 gas、已实现 PnL(WETH + 用链上预言机换 USD)。
- **真实成交必须以链上 receipt/log 为准**,不能只信执行器自报;报价永不充当成交。
- 汇总:真金**累计投入**、**累计已实现 PnL**、真实笔数、胜率、最坏单笔。

### B. 开盘/暂停/重启时间线 + 每次原因
- 按时间列:每次 go-live、每次暂停、每次重启,各自的**白纸黑字原因**(亏损/rug/基础设施/安全/人工决策)+ 文件锚点。
- 标出每段"真实开着能下单"的窗口有多长、期间真成交几笔。

### C. 实际跑的配置 vs 产生 +56% 的估计量(核实"策略没变过"这个信念)
- 逐段 pin 实盘真实配置(F 撤池名单 开/关、TRAIL、单笔 size、并发、各门槛、E 窗、模型版本)。
- 与产生 IRON +56% 的 reaudit10/GL3 估计量逐项对比,**列出每一处不一致**。人类记忆是"完全没变过",用证据确认或纠正(例如 aggressive 版曾 F 关、放 207 笔 rug 进场——这与保守回测口径是否同一个?)。

### D. 亏损归因分解(本任务的命门,先预注册分桶再算量级)
把真金净亏分解到:
1. **执行/滑点 gap**:真实 fill vs 同块 QV2/atomic 报价的系统性差(这直接检验"+56% 是不是报价虚高")。
2. **rug 兑现**:实盘真吃到的 rug(aggressive 版本就是赌 207 笔 rug 能盈利、且自承"live 真实致命率应高于回测");真实延迟/反应型撤池造成的额外亏。
3. **regime/衰减**:实盘所处时段(如 6 月候选=0)与 +56% 样本时段的差异。
4. **选择/择时**:是否在赢的时段暂停、在亏的时段开着。
5. **小样本噪声**:真实笔数少、方差大。
每桶给证据 + 量级。

### E. 对质裁定(允许推翻 IRON)
- 明确回答:真实战绩是**推翻** IRON +56%(即历史 alpha 是 artifact/口径漏洞,如报价 gap 或 in-sample rug 假设),还是**与之相容**(历史 alpha 作为冻结事实成立,但真金亏由执行 gap/rug 兑现/配置不一致/regime 解释)。
- 若证据要求**下调任何 IRON/PINNED**,直说,别护着既有结论(宪章:FAIL 门槛不是更低,但也不许为保住结论而无视真实亏损)。
- 更新 GL1 账本:新增一等条目"实盘真实战绩",并据此把 GL1-02(真钱净 edge)从 OPEN 落到真实数据支持的裁定。

## 3. 交付物
1. 实盘真相账本(机器 JSON + 人读 markdown)+ 人话版一段(真金投了多少、亏了多少、为什么亏、+56% 到底还算不算数)。
2. 开盘/暂停时间线表、真金逐笔表、配置对比表、亏损归因分桶表。
3. 预注册(归因分桶定义、真实成交判定、真vs paper 判定)+ SHA256,做前冻结。
4. 更新后的 GL1 账本 + 受影响分级说明。
5. 可复现 hash;链上 receipt 核对可用 RPC(prettiest-blue-aura),只读;回归测试(断言 ==)。

## 4. 铁律
纯只读/历史还原;**不下单/签名/广播/改 live/signals/Redis**;executor 保持关闭;不自判 GO。真实成交以链上 receipt 为准,区分真实/paper;缺证据不默认全损也不默认盈利;预注册在前;可复现产出 hash。

## 5. 审查 agent 终审保留
逐行审:真金逐笔是否都经链上 receipt 核对(不是执行器自报)、真实 vs paper 是否分清、暂停原因是否有文件锚点、配置对比是否诚实(有没有把"变过的配置"说成没变)、亏损归因是否有量级证据、对质裁定是否敢于在证据要求时下调 IRON。**裁定最终由审查 agent 定,codex 不自我背书。**
