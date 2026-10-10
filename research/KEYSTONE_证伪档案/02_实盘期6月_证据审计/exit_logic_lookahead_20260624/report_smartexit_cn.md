# 巧刀（智能条件出场）+ 出场延迟埋点 — 决策报告

> 日期：2026-06-24　范围：任务 A = 唯一允许的代码改动（纯增量埋点）；任务 B = 纯离线回测。
> 未改 `.env` / compose `environment:` / Redis live key；未解除 SYSTEM_PANIC；未上链、未下单。
> 镜头标注：rug 侧 = **real-gas 实测**（可信）；clean 侧 = **QuoterV2 单镜头**（乐观上界）。全部 in-sample，且不含"我方进场触发骗子提前撤池"的反应型风险。

---

## 任务 A：出场延迟埋点（已完成，纯增量 + 留对照）

### A.1 现状核对（以真仓库为准）

| 项 | 现状（核对后） | 与上轮报告是否一致 |
|---|---|---|
| `_record_exec_quality` 位置/流 | `main.py:238`（行号因本次改动后移）；写 Redis 流 **`exec:quality`** | 一致（流名 `exec:quality`，上轮写 `exec:quality` ✓） |
| 买入侧延迟 | 传 `sig_block`/`signal_ms` → `delay_sec`/`delay_blocks` **有记录** | 一致 |
| 卖出侧延迟 | `_finalize_close` 调用处 `signal_ms=None, sig_block=None` → **出场延迟未记录**（只记了卖出滑点 `deviation_pct`） | 一致 |

### A.2 改动（纯增量，旧字段一律不动）

- 新增**新字段**（不复用买入侧 `signal_ms`，避免买/卖语义混淆）：`exit_trigger_ms`、`exit_trigger_block`、`exit_delay_sec`、`exit_delay_blocks`。
- 在**出场决策点**（`main.py` 正常卖出路径 `should_exit` 确定后；盲价 panic 路径）记录 `trigger_ms=now_ms`、`trigger_block=_safe_block_number(w3)`（fail-soft，探不到记 None），传入 `_finalize_close → _record_exec_quality`。
- `_safe_block_number` 仅在**真正卖出时**取一次当前块、异常即 None，**不进任何出场判定**。
- **未改**任何 trailing / hard_stop / time_exit / E 窗 的阈值或顺序；买入侧字段、`deviation_pct`、`delay_sec` 全部原样。
- 改动文件：`services/executor/src/main.py`（`_record_exec_quality` 增 2 参 + 4 字段；`_finalize_close` 增 2 参透传；2 个出场点 + 4 处 `_finalize_close` 接线 + `_safe_block_number` helper）。

### A.3 离线验证（不依赖下单）

测试：`tests/test_exit_latency_telemetry.py`，**3/3 PASS**；现有 `tests/test_executor_honeypot_defense.py` **21/21 PASS**（无回归）。
- `test_exit_delay_fields_computed`：trigger→fill +90s/+45块 → `exit_delay_sec=90.0`、`exit_delay_blocks=45`；**旧字段不变**（卖出 `delay_sec=None`、`deviation_pct=-5.0`）。
- `test_backward_compat_no_trigger`：不传新参 → 新字段全 None，**买入侧 `delay_sec`/`deviation_pct` 照常计算**（留对照）。
- `test_finalize_close_wires_trigger`：mock 一次 dry-run 卖出 → `exec:quality` 流中含 `exit_delay_sec/blocks`，仓位正常关闭。

### A.4 判定（一句话）

> **埋点已就绪**：下次自然 live 卖出会自动记录 `exit_delay_sec`/`exit_delay_blocks`；**当前历史 live 出场延迟仍为「未知」**（之前未记）。n 攒到 ≥30 报分位、≥100 才可用于决策。
> A 只是装仪表盘，**不改变任何策略行为**，无需重新做安全验证即可在下次 live 卖出开始积累。

> 说明：E 窗强平走的是 `_abandon_unsellable_position`（−100% 弃仓，无可卖 fill），不产生"触发→成交"延迟，故不在本埋点范围；可测延迟 = trailing / hard_stop / time_exit / 盲价 panic 这四类有 fill 的卖出。

---

## 任务 B：巧刀（智能条件出场）—— train/holdout 验证

### B.1 钝刀 vs 巧刀定义

- **钝刀**：所有交易同一套 trailing/maxhold（已知焊死）。
- **巧刀**：用 **pre-entry 风险分（p_fatal，15 特征已审计全 pre-entry）** 给每笔打分；高风险 → 紧出场（max-hold 上限），低风险 → 松出场（现状 trailing），目标把"降 rug 致命"与"保 clean 右尾"分开。

### B.2 致命前提：路由信号根本不存在（先于回测的硬结论）

巧刀第一步要靠风险分把"脆弱 rug"和"clean 右尾赢家"分开。实测：

| 分离能力 | AUC | 含义 |
|---|---|---|
| p_fatal 分 rug-winner vs clean-winner | **0.425** | **低于 0.5**（甚至反向：clean 的 p_fatal 反而略高，均值 0.099 vs 0.086） |
| p_fatal 分"脆弱快撤池"(真实边际<2min) vs 耐久 | **0.368** | 脆弱 rug 的 p_fatal **更低**（0.061 vs 0.085） |
| 15 个 pre-entry 特征中最佳单特征(swaps_per_buyer) | \|AUC−0.5\|=0.153 | 远低于可用路由所需的 ~0.70 |

> **深层原因（确定性，不是巧合）**：这 167 笔本就是**通过了 pre-entry p_fatal 闸（p≤0.27）且盈利**的池——按定义它们就是"进场前与 clean 赢家无法区分"的幸存者。能被 pre-entry 特征识别的 rug，早在进场闸就被挡了。所以**没有任何 pre-entry 分数能在这批population上路由**，这是选择效应的必然，不是特征工程没做够。

### B.3 train/holdout 回测（仍按要求做完，证据落地）

- 切分：167 rug + 95 clean，各按 t0 中位切 train/holdout。**阈值只在 train 选，指标只报 holdout。**
- train 选出规则（粗粒度，非精细网格）：**p_fatal ≥ 0.08 → max-hold 20min（紧）；否则现状 trailing**（train 上满足 clean 保留 ≥80% 且 rug fatal@+300 最低的组合）。
- 冻结搬 holdout（rug=83、clean=47）：

| 规则 | rug fatal@+0/+60/+300 | rug总PnL | clean总PnL | clean中位ret | 最大赢家 |
|---|---|---|---:|---:|---:|---:|
| **巧刀(train-冻结)** | 0% / 35% / **46%** | $3,428 | $2,107 | 65% | 563% |
| 钝刀-现状(TRAIL0.35) | 0% / 51% / 67% | $3,706 | $3,033 | 104% | 563% |
| **钝刀-TRAIL0.25** | 0% / 36% / **49%** | $3,207 | **$2,694** | 79% | 563% |
| 钝刀-全紧(maxhold20min) | 0% / 12% / **16%** | $2,393 | $1,353 | 45% | 206% |

（rug = real-gas 实测；clean = QuoterV2 乐观上界）

### B.4 判定（二选一，决断）

> **结论 B 成立：巧刀在当前特征下也分不开，结论 A 的"近免费"是画饼。**

证据：
1. **巧刀被钝刀严格压制**：holdout 上巧刀 rug fatal@+300=**46%**、clean 保留 **69%**；而**简单的钝刀 TRAIL0.25** 拿到几乎相同的 fatal（49%）却保住 **89%** clean（$2,694 vs $2,107）。巧刀比"无脑收紧 trailing"还差——因为 p_fatal 把刀砍向了**错的池**（紧了高 p 的 clean 赢家、漏了低 p 的脆弱 rug）。
2. **巧刀两条 A 标准都没达到**：rug fatal@+300=46%（未到 ≤30%）；clean 保留 69%（未到 ≥80%）。
3. **要把 rug fatal 真正打下来只能全紧**（maxhold20min → fatal@+300=16%），代价是 clean **保留仅 45%、中位 104%→45%、最大赢家 563%→206%**——右尾被砍掉一半以上。这正是"焊死"：显著降 fatal ⟺ 砍掉 >50% 右尾。

### B.5 过拟合自检（如实写）

> **train-选出的巧刀规则在 holdout 上退化**：train 上 clean 保留 80%（被约束卡着选出来的）→ holdout **跌到 69%**；train rug fatal@+300=62% → holdout 46%。
> 即：那条"在 train 上勉强达标(≥80%)"的规则，**搬到 holdout 就跌破 A 门槛**。这是路由阈值的轻度过拟合——**该规则不可用**，不藏。

### B.6 必须重申

- **~5% 快撤池（flip≤10min，9/167）任何出场规则都救不了**：连"全紧 maxhold20min"在 holdout 仍留 fatal@+300=16%（地板就是这批 + 中速池）。
- clean 侧是 **QuoterV2 乐观上界**；rug 侧是 real-gas 实测。holdout 只防参数过拟合，**消不掉**"反应型撤池风险未建模"这层结构性乐观——真实 live 只会比这更差。

---

## 给委托人的人话总结（4 句）

1. **出场延迟仪表盘装好了**（任务 A，纯加一个数、没动任何买卖逻辑，离线测试 3/3 + 老测试 21/21 通过）；但**历史延迟仍是未知**，要等下次自然卖出才开始记——这个数仍是"敢不敢碰悬崖钱"的前置闸门。
2. **巧刀失败了，而且是注定失败**：用进场前信息根本分不出"会撤池的 rug"和"干净的大赢家"（AUC 0.425，比抛硬币还差），因为这 167 笔本就是"进场前看起来和好池一模一样"才混进来的。
3. **巧刀在留出集上还不如简单收紧 trailing**：要降撤池致命率，老老实实把 trailing 从 0.35 收到 0.25 更省更稳；想再狠就得砍掉一半以上的右尾大赢家——这就是"焊死"。
4. **所以现实方案 = 钝刀 + size 封顶 + 出场延迟埋点**，不要指望"智能巧刀"两全；是否放宽 F 黑名单，等任务 A 量出真实出场延迟后再定（在那之前任何"追悬崖钱"都是盲赌）。

---

## 证据路径

- 任务 A 代码：`services/executor/src/main.py`（`_record_exec_quality` / `_finalize_close` / `_safe_block_number` / 出场决策点）
- 任务 A 测试：`tests/test_exit_latency_telemetry.py`（3/3）、回归 `tests/test_executor_honeypot_defense.py`（21/21）
- 任务 B 脚本/数据：`scripts/research/base_lefttail/smartexit_backtest.py`、`mvp/audits/exit_logic_lookahead_20260624/smartexit_backtest.json`
- 分离力证据：p_fatal AUC=0.425、最佳单特征 \|AUC−0.5\|=0.153（脚本内联打印）
- rug 基准：`mvp/audits/stress167_20260624/_stress167_cache.json`（real-gas）
- p_fatal 特征 pre-entry 审计：`scripts/research/base_lefttail/keystone_live.py:61-83`（见上一份 report_cn.md §2.1）

> 本报告全程只读 + 离线；任务 A 的代码改动仅为埋点（多记一个延迟数），不构成策略变更。**未上线、未调整任何 live 配置。**
