# 执行层真实性 + MEV/延迟 测量审计

日期：2026-06-22  范围：EATLQ/KEYSTONE Base V3 实盘（只读扫描）
审计性质：只读 + 报告。未运行任何会写 Redis / 发交易 / 重度打 RPC 的脚本。实盘容器在审计期间保持运行（`eatlq-executor-1` Up，`LIVE_TRADING_MODE=true`, `EXECUTOR_DRY_RUN=0`, 单笔 `EXECUTOR_MAX_POSITION_ETH=0.057`）。

## 0. 一句话裁定

| 子项 | 裁定 |
|---|---|
| Q1 回测卖出端定价来源 / 与 live 蜜罐同源？ | **FAIL（确认同源）** — 回测与 live 蜜罐/卖价都用 QuoterV2 `quoteExactInputSingle`(eth_call)，共享盲区已定位 |
| Q2 gross-of-MEV/latency 假设位置 | **已列全**（见 §2）；最大未建模项＝信号→上链延迟 + 三明治/MEV + 自身冲击后的真实成交块 |
| Q3 预测 vs 实现 / 信号时间 vs 上链时间 日志 | **FAIL（live 执行端无此账本）** — 字段散落在 shadow/strategy 旁路，未接入实盘 executor 成交 |
| Q4 从 N 笔 live 算执行 gap 分布并回填 | **INCONCLUSIVE → 给落地方案** — 当前真实 live-forward N=8，远低于可出分布的 n≥30 |
| 当前"测真实执行成本"的整体能力 | **WEAK_PASS（离线） / FAIL（在线闭环）** |

**WEAK_PASS 的含义**：离线侧已经做了相当扎实的真实成本建模（QuoterV2 实价往返、真实 notional 滑点、卖路径失败=-100%、real-gas 重放抓 gas-bomb），这比"固定百分比成本"强很多。但**在线闭环缺失**：实盘没有把"QuoterV2 预测价↔真实成交价、信号时刻↔上链时刻"逐笔落账，所以**无法用实盘数据证伪回测**——而回测和 live 又共用同一个 QuoterV2/eth_call 估值器，盲区是同源的、彼此无法相互检验。

---

## Q1. 回测卖出端定价用什么？是否和 live 蜜罐检查同源？

### 结论：FAIL —— 同源已确认（风险类别 b）

回测和 live 的卖出端估值**是同一个机制**：Uniswap V3 `QuoterV2.quoteExactInputSingle` 经 `eth_call`。

**回测侧**（`scripts/research/base_lefttail/keystone_realcost.py:136-149` `roundtrip()`）：
- 进场：`quote_exact_input_single_struct(rpc, WETH, alt, amt, fee, block=entry_block)`
- 出场：同一函数，`block=entry_block+7200`（≈4h）
- 卖出 quote revert → 直接记 `-100%`（`return -1.0, ..., "sell_fail"`，行 148）
- 该 helper 在 `scripts/lib/v3_quoterv2_struct.py`，底层就是 QuoterV2 `quoteExactInputSingle` 的 eth_call。

**live 侧**（同一个 QuoterV2 selector、同一个合约 `0x3d4e44Eb...`）：
- 蜜罐检查 `services/executor/src/swap.py:988-1006` `_honeypot_check_v3()` → QuoterV2 `quoteExactInputSingle`
- 进场滑点保护 `swap.py:518-533` `_estimate_buy_output_v3()` → QuoterV2
- 持仓 mark / 卖前报价 `swap.py:848-904` `quote_sell_eth_wei()`（v3/aero_cl 分支）→ QuoterV2；`main.py:943-964` `_mark_eth_wei()` 以它为主、`get_slot0_price_eth()`（slot0）为兜底
- 进场前 friction gate `services/executor/src/preflight_trade.py:49-85` → QuoterV2 买价 + 卖价往返

> 即：**回测用来给"4h 后卖出"定价的东西，和 live 用来判"这 token 能不能卖（蜜罐）/能卖多少（mark）"的东西，是同一个 eth_call。** 用户担心的"和被骗同源"成立。

### 共享盲区（QuoterV2/eth_call 在 node 默认近无限 gas 下看不见的东西）

| 盲区 | 性质 | 回测是否建模 | live 是否有缓解 |
|---|---|---|---|
| gas-bomb 蜜罐（quote 健康、真实 gas 下 revert） | 同源盲区(b) | 默认**否**；有 `replay_realgas_sellsim.py` 离线补测 | **是**：`simulate_real_sell()`（3M gas state-override 重放）+ 进场 `executable_sell_gate` + 持仓每 60s 复检（`main.py:1005-1026`）。实盘 `EXECUTOR_SELL_SIM=1, FAIL_CLOSED=1` |
| 三明治 / MEV 抢跑（quote 是现价，不是被抢跑后的成交） | 完全未建模(c) | 否 | **否**（无 Flashbots/私有 mempool，见 `execution_readiness_readout.md` "MEV_PROTECTION_ABSENT"） |
| 自身冲击后的"下一区块"真实成交价（quote 是当前块现价） | 完全未建模(c) | 否（同块 quote） | live 真实成交 `amount_out` 反映之，但未与 quote 对账 |
| quote-block 与真实落地块之间的 toggle/rug | 同源盲区(b) | 否 | 持仓 60s real-gas 复检部分覆盖 |

### 同源盲区的已测量级（关键反直觉点，n 标注）

`replay_realgas_sellsim.py` 已运行，产物 `scripts/research/base_lefttail/_realgas_replay.json`：
- **n=743** 笔 friction-gated 回测交易，用 real-gas（3M gas、state-override 授余额+授权）重放每笔出场。
- 结果：181 笔 exit 在真实 gas 下 revert；但 **hidden honeypots = 0**（即"QuoterV2 说能卖却 real-gas 死"的笔数为 0）。

含义（必须精确）：在这 743 笔历史样本上，**QuoterV2 历史报价 与 real-gas state-override 模拟 是一致的** —— 同源盲区在这个样本里**没有表现为可测的差**。但请注意这仍是 `eth_call vs eth_call`（两者都不是真实广播交易），它**没有**覆盖真正未建模的部分（三明治/抢跑/自身冲击后的真实成交）。所以：
- 能证明的：gas-bomb 这一类（b 盲区）在 n=743 历史样本上没造成回测虚高。
- **不能**证明的：真实广播成交 vs QuoterV2 预测 的差（需要 live 对账，见 Q3/Q4），n=743 对这条**完全无话语权**。

### 旁证

`keystone_lefttail.py`（纯 tick 代理，行 122-137）是更原始的"不用 QuoterV2"的死亡代理：靠前向 tick 判 death/-50%/-100%。`keystone_realcost.py` 的存在就是为了**纠正它过严的 -100%**——用 QuoterV2 实价往返替代 tick 代理。所以真实成本回测确实做了，只是它的估值器与 live 同源。

---

## Q2. 代码里所有 gross-of-MEV/latency 的假设位置

按"越上面越致命"排序。每条标风险类别 (a) 回测已建模可信 / (b) 同源 eth_call 存疑 / (c) 完全没建模只能 live。

1. **进场时点 = 零延迟（最大假设）** — `keystone_realcost.py:140` 在 `entry_block`（pre-entry 观察窗最后一笔 swap 所在块）直接 quote 进场。**实盘根本做不到**：bridge 在 t0 后 10–15min 才进场（`services/signal_bridge/bridge.py:51` `WINDOW_MARGIN=300`，窗口 10–15min），再叠加 ClickHouse 解码延迟、`BRIDGE_POLL_SEC=30` 轮询、executor 处理、tx 落地。**信号→上链的整段延迟在回测里＝0。** 类别 (c)。
   - 注：Solana 聪明钱线已用 `scripts/research/sol/latency_decay.py` 零成本证伪过"提前到同块反而更亏"（`mvp/audits/sprint101_speed_thesis/latency_decay_verdict_cn.md`，n=200）。但那是 **SOL pump.fun 债券曲线**的结论，**不能直接外推到 Base V3 KEYSTONE**——Base 线的延迟敏感性尚无同等实测。

2. **无三明治 / 无 MEV** — `keystone_realcost.py:roundtrip()` 的 quote 是池现价输出，不含被抢跑后的恶化。`execution_readiness_readout.md` 明确列 `MEV_PROTECTION_ABSENT`。类别 (c)。

3. **gas 成本未从回测收益中扣除** — `keystone_realcost.py:145-149` `ret = amount_out/amt - 1.0`，只含 LP 费 + 价格冲击（两次 quote 内含），**不扣 tx gas（ETH）**。行 246 自标 "gross-of-funding"。Base L2 gas 小但非零；live 端 `swap.py:564` 真实记 `gas_cost_wei`。类别 (a)（可补建模）。
   - 对照：sprint086 overlay 的 `real_cost_model.py` **已**把 gas 以 USD 计入（$0.05 买 + $0.05 卖，见 `overlay_readout.md` 方法节），比 keystone_realcost 更细。即同一仓库里"扣 gas"的能力已存在，只是 keystone_realcost 没用它。

4. **静态价 / 固定 notional** — `keystone_realcost.py:29` `ETH_USD=3000.0`、`NOTIONAL_USD=100.0`。live 端用动态链上 WETH→USDC 报价（`swap.py:1230` `get_eth_usd_price`）+ 动态分仓（`main.py:_dynamic_stake_wei`）。回测的固定 notional 不反映 live 的 depth-aware 真实下单量。类别 (a)。

5. **slippage tolerance 是常数表，不是分布** — `swap.py:109-119` `_SLIPPAGE_TOLERANCE`（buy 3% / TP 1% / SL 5% / trailing 2% / time 3%）。这是 live 的 `amountOutMinimum` 设定，不是对真实滑点分布的建模；回测里则用 QuoterV2 精确输出（无随机滑点项）。类别 (b)/(c)。

6. **overlay 的进场价 = 信号成形价（潜在前视）** — sprint086 `overlay_readout.md` 在 paper-shadow CSV 的进场时点上叠加成本，而该进场时点本身是"信号成形那一刻"的价；latency_decay 的核心警告正是"信号成形所需时间里价格已走完"。所以 overlay 的 5m/10m 档 `real_mean` 已转负（如 sprint055 5m: gross +0.43% → real −0.71%，n=1032），是已建模可信的成本侵蚀证据。类别 (a)。

7. **live PnL 是净的，但只日聚合、无逐笔归因** — `main.py:_finalize_close` 用真实 `result.amount_out` 记 PnL（已含 MEV/gas/滑点的真实结果），但只 `add_realized_pnl_wei(day_key, delta)`（`position.py:318`）累进**每日**计数。逐笔的真实成交价没有被单独留存用于回填回测。类别 (a，数据在但没存)。

---

## Q3. 是否存在"预测 vs 实现"日志？（QuoterV2 预测进/出价↔真实成交价；信号时间↔上链时间）

### 结论：FAIL（实盘 KEYSTONE 执行端没有这个账本）。零件散落两处但没接成闭环。

**实盘 KEYSTONE 路径（`services/executor/`）—— 缺**：
- **进场预测被丢弃**：`buy_v3` 里算出的 QuoterV2 预测产出 `expected`（`swap.py:626`）只用于设 `min_out`，之后丢弃。`main.py:819` 把 `entry_quote_eth_wei` 错设成 `result.amount_in`（花掉的 ETH），**不是**预测产出。真实拿到的 token 存 `token_amount`。→ 进场"预测↔实现"无法对账。
- **出场预测未与实现并排留存**：`_mark_eth_wei()`（QuoterV2 预测 mark）每个监控 tick 算一次；卖出后 `_finalize_close` 只用真实 `result.amount_out` 记 delta，且只进**每日聚合** Redis 计数 `positions:realized_pnl:{YYYY-MM-DD}`。决策时刻的 `mark`（预测）没有和这笔的真实成交并排落库。
- **时间戳缺**：`opened_at_ms` 只是"仓位建立时刻"（买单确认后）。信号**发出**时刻没留（bridge payload 无 timestamp 字段，见 `bridge.py:235-248`；只有 Redis stream msg-id 隐含毫秒时间，但 executor 没把它绑到仓位）。上链确认块号/时间也没逐笔记。→ **信号时刻↔上链时刻 gap 完全没记。**
- `exec:trades` 账本**不是** executor 写的（已核：executor 目录内 `grep exec:trades` 无命中）。

**shadow / strategy 旁路（`services/strategy/`、dashboard）—— 有半套零件，但没接 live 成交**：
- `strategy/src/main.py:818-852` `_write_shadow_signal()` 确实记 `signal_ts_ms`、`notional_in_wei`、`expected_token_out`（QuoterV2 预测）、`confidence`、`closed_at_ms`、`net_return_pct` → `exec:shadow` 列表。
- dashboard `services/dashboard/server.py:416-436` 读它并算 `hold_minutes = (closed_at_ms - signal_ts_ms)/60000`。
- **但**：这是 paper/strategy 血统，存的是 QuoterV2 **预测**值，**不是某笔真实广播 tx 的成交回执**；它没有"QuoterV2 预测 ↔ 真实 fill"的对照，也没接到实盘 executor 的真实成交。

> 一句话：**字段 schema 在 shadow 里已有雏形（预测 + 信号时刻），但实盘 executor 不发"对账记录"**。要回答用户的问题，必须把对账记录建在 live 成交路径上。下面给设计。

### 设计：实盘逐笔执行对账账本（`exec:fills` 流，新增、只追加、不改现有逻辑）

在 executor 的买/卖成交点各 `xadd` 一条记录（建议用 Redis Stream `exec:fills`，maxlen≈50k；并镜像到 `logs/exec_fills.jsonl` 便于离线分析）。字段：

```
{
  "leg": "buy" | "sell",
  "pool": "0x...", "token": "0x...", "pool_type": "v3",
  // --- 时间链（回答 信号时刻↔上链时刻） ---
  "signal_id": "<redis stream id>",          // 来自 signals 流，隐含发出毫秒
  "signal_emit_ms": <int>,                    // 解析自 signal_id（或 bridge 写入 payload.emit_ms）
  "decision_ms": <int>,                       // executor 决定下单时刻
  "broadcast_ms": <int>,                      // 签名广播时刻
  "fill_block": <int>, "fill_ts_ms": <int>,   // 回执块号 + 块时间
  "quote_block": <int>,                       // 取预测 quote 时的块
  // --- 价格链（回答 QuoterV2 预测↔真实成交） ---
  "predicted_out_wei": <int>,                 // 买:QuoterV2 预测 token_out; 卖:quote_sell_eth_wei 预测 ETH_out
  "realized_out_wei": <int>,                  // 买:真实到账 token; 卖:真实到账 ETH(从 Withdrawal log)
  "amount_in_wei": <int>,
  "min_out_wei": <int>,                        // 设的滑点底
  "gas_cost_wei": <int>, "priority_fee_wei": <int>, "effective_gas_price": <int>,
  // --- 派生 gap（也可离线算） ---
  "exec_gap": realized_out/predicted_out - 1, // 执行 gap（核心）
  "latency_ms": fill_ts_ms - signal_emit_ms,  // 信号→上链总延迟
  "block_lag": fill_block - quote_block,
  "reason": "buy" | "time_exit" | "hard_stop" | ...,
  "tx_hash": "0x..."
}
```

落点（最小侵入，不动交易逻辑、不动 dry-run 判定）：
- 进场：`main.py` 在 `pos = OpenPosition(...)` 之后、`store.open_position(pos)` 旁边追加一次 `xadd`。`predicted_out_wei` 来自把 `friction_gate` 的 `expected_out_wei` 透传进来（现在它在 `run_pretrade_gates` 里算了但没回传到建仓点——顺带修 `entry_quote_eth_wei` 的误赋值）。
- 出场：`_finalize_close` 增参 `predicted_out_wei`（即决策时的 `mark`）与 `tx_hash/gas`，已经在调用点可得（`main.py:1111` `result` + `mark`）。
- bridge：`emit_signal` 的 payload 增 `"emit_ms": int(time.time()*1000)`（`bridge.py:235`），让信号发出时刻显式可得，不必反解 stream id。

这套只新增"观测"，不改买卖/风控判定，符合"不影响实盘"。

---

## Q4. 从 N 笔 live 算"执行 gap 分布"并回填回测的具体方案

### 当前样本量：n=8（真实 live-forward），远不足以出分布

证据：`mvp/audits/sprint087_executor_lag_repair/lag_repair_readout.md` 的 `n_accounting`：`true_live_forward_executor_n=8, n_goal=100, pct_to_goal=8.0%`。（注：`mvp/audits/execution_readiness/n_accounting.json` 现读为 0，因为该探针连 Redis 失败 `Connection refused`——是探针口径/连接问题，不代表真实为 0；以 lag_repair 的 8 为准。）

按本审计纪律：**n=8 → INCONCLUSIVE**，不能用来定 gap 分布、更不能据此改回测参数（n=1 不得改参数；n=8 仅够做"是否存在量级异常"的眼看，不够出分位）。先把 §Q3 的 `exec:fills` 上线攒样本。

### 落地方案（四步，先攒样本→出分布→回填→重判）

**Step 1 — 上线对账账本，攒 N≥30（最好≥100）。**
- 部署 §Q3 的 `exec:fills`。在现有 ~0.057 ETH 小仓 live 下自然积累。
- 同时可用"影子对账"加速：对每个进场信号，**不下单也记一条** `predicted_out`（QuoterV2）+ 之后某固定 horizon 的 QuoterV2 复价（已有 shadow 基础设施 `_write_shadow_signal`），但这条只能测"QuoterV2 自洽性"，**不能**测真实成交 gap——真实 gap 必须来自真实 tx 回执。

**Step 2 — 算执行 gap 分布（每条腿分别算）。**
- 进场 gap：`g_in = realized_token_out / predicted_token_out_at_decision_block - 1`
- 出场 gap：`g_out = realized_eth_out / predicted_eth_out_at_decision - 1`
- 延迟：`latency_ms = fill_ts - signal_emit_ms`；`block_lag`
- 报告：每个分布给 `n、median、p25、p75、p05（左尾）`，并标注它包含哪些成分（gas 已扣？是；MEV？只要是真实 tx 就含；自身冲击？含）。
- 出分布的 n 门槛：**n≥30 才报分位，n≥100 才用于回填决策**（与回测 §8 的 per-bin n≥30 一致）。

**Step 3 — 把 gap 分布作为"执行折让"叠加回 QuoterV2 回测（不改回测估值器，只加 haircut 层）。**
- 在 `keystone_realcost.py` 的每笔 `ret` 上，按腿施加抽样折让：
  `ret_exec = (1 + qi_out_ret) * (1 + g_in_sample) * (1 + g_out_sample) - 1 - gas_haircut`
  其中 `g_in_sample / g_out_sample` 从 Step 2 的经验分布 **bootstrap 抽样**（保留左尾），`gas_haircut` 用 live 实测 `gas_cost_wei / notional` 中位。
- 保守档：用 `p25`（而非 median）施加，看回测结论是否仍稳健。
- 关键守则：折让分布来自真实成交，**不要**用 QuoterV2 自洽性（那是同源，等于没测）。
- 产物：新脚本（建议 `scripts/research/base_lefttail/exec_gap_overlay.py`），读 `exec:fills`/`logs/exec_fills.jsonl` + 现有 `_priced_live.json`，输出 `gross / real-gas / exec-haircut` 三档对照的收益分布与凸性/破产门复判。

**Step 4 — 重判 gate。**
- 把 exec-haircut 档喂回 KEYSTONE 的凸性检查（`keystone_realcost.py` 的 `keep safest q%` + 月收益估算）和 Track B 破产模拟。
- 若加折让后高分组仍满足"removes deaths、keeps winners"且月收益>0，则执行成本被吸收；否则按红线诚实降级。

### 这能/不能证明什么

- **能**：量化 QuoterV2 预测与真实成交之间的系统性偏差（含 MEV/gas/自身冲击的净效果），并把它做成可回填回测的折让分布——这是目前唯一能"用实盘证伪回测"的桥。
- **不能（在 N 足够前）**：分离 MEV 与延迟各自的贡献（需要更细的 mempool/排序数据）；也不能外推到比 live 更大的下单量（gap 随 size 变大，必须按 live 实际 size 解释）。

---

## 修复清单（按优先级，全部"只加观测/离线"，不改实盘交易逻辑）

| # | 动作 | 文件 | 风险 |
|---|---|---|---|
| P0 | 新增 `exec:fills` 逐笔对账（买/卖各一条，含预测↔实现、信号↔上链时间） | `services/executor/src/main.py`（建仓后 / `_finalize_close`）、新增 `services/executor/src/fills_ledger.py` | 仅 `xadd`，纯追加，不入交易临界路径 |
| P0 | 修 `entry_quote_eth_wei` 误赋值：应存 QuoterV2 预测 token_out（透传 `friction_gate.expected_out_wei`），而非 `amount_in` | `services/executor/src/main.py:819` | 仅改写入字段语义，不改下单 |
| P1 | bridge 在 payload 加 `emit_ms` | `services/signal_bridge/bridge.py:235` | 加字段，executor 已忽略未知字段 |
| P1 | 新脚本：从 `exec:fills` 算 gap 分布（n/median/p25/p75/p05/latency） | 新增 `scripts/research/base_lefttail/exec_gap_distribution.py` | 离线只读 |
| P2 | 新脚本：gap 折让叠加回 keystone 回测并复判 gate | 新增 `scripts/research/base_lefttail/exec_gap_overlay.py` | 离线只读 |
| P2 | keystone_realcost 增可选 `--gas-haircut` / `--exec-overlay`，并显式打印 "gross-of-MEV/latency" 警示 | `scripts/research/base_lefttail/keystone_realcost.py` | 仅离线回测，默认行为不变 |
| P3 | 把 sprint086 的 `real_cost_model.py`（已含 gas-in-USD）接入 keystone_realcost，统一"扣 gas" | 二者 | 离线 |

---

## 仍未知项（UNKNOWN + 测量方案）

1. **Base V3 KEYSTONE 的真实信号→上链延迟分布** — UNKNOWN。SOL 线已证伪"更快更好"，但 Base 线无实测。测量：`exec:fills.latency_ms`，n≥30。
2. **真实成交 vs QuoterV2 预测的 gap 分布（含 MEV）** — UNKNOWN（n=8 不足）。测量：Step 1–2。
3. **MEV/三明治在 Base 新池上的实际发生率与幅度** — UNKNOWN，且 QuoterV2 与 real-gas 都测不到，只能 live。测量：对 `exec:fills` 里 `exec_gap` 显著为负的笔，事后查该 tx 同块是否有夹击（链上 trace），单独成表。
4. **gap 随下单 size 的弹性** — UNKNOWN。当前 live size 单一（~0.057 ETH）。测量：需在风控允许内做分档 canary（属改动，需人工授权，不在本只读审计内）。
5. **回测 vs live 的"同一池"重叠度** — UNKNOWN。回测 TEST 池与 live 实际成交池可能几乎不重叠（live N=8）。在 N 增长前，回填的代表性存疑。

---

## changed files / commands

- **changed files：无（只读审计）。** 仅新增本报告：
  - `mvp/audits/execution_mev_latency_audit_20260622/execution_mev_latency_audit_cn.md`
- **未运行**任何写 Redis / 发交易 / 重度 RPC 的脚本。`replay_realgas_sellsim.py` 的结论引用自其**已存在**的产物 `_realgas_replay.json`（未重跑）。
- 审计期间用到的只读命令（git/docker/读文件）均无副作用；实盘容器全程未被触碰。

### 关键证据路径速查
- 回测卖价同源：`scripts/research/base_lefttail/keystone_realcost.py:136-149`、`scripts/lib/v3_quoterv2_struct.py`
- live 同源估值：`services/executor/src/swap.py:848-904`(quote_sell)、`:988-1006`(honeypot v3)、`services/executor/src/preflight_trade.py:49-85`
- gas-bomb 缓解（live 有/回测有离线工具）：`swap.py:1105-1172` `simulate_real_sell`、`scripts/research/base_lefttail/replay_realgas_sellsim.py`(+ `_realgas_replay.json`, n=743, hidden=0)
- 无逐笔对账：`services/executor/src/main.py:811-848`(建仓)、`:915-941`(_finalize_close)、`services/executor/src/position.py:318`(仅日聚合)
- shadow 半套字段：`services/strategy/src/main.py:818-852`、`services/dashboard/server.py:416-436`
- live N=8：`mvp/audits/sprint087_executor_lag_repair/lag_repair_readout.md`
- 已建模成本侵蚀（n=1032）：`mvp/audits/sprint086_real_cost_overlay/overlay_readout.md`
- MEV 缺失自陈：`mvp/audits/execution_readiness/execution_readiness_readout.md`（MEV_PROTECTION_ABSENT）
- 延迟证伪（SOL，n=200，勿外推 Base）：`mvp/audits/sprint101_speed_thesis/latency_decay_verdict_cn.md`
