# D — 仓位前视 + 极端行情压测（只读，2026-06-22）

只读。目标：不前视、不过拟合前提下压测仓位与极端行情。

## 1. Look-ahead（前视）逐项核实 — 判定 PASS（带 caveat）

### 模型特征：全部 pre-entry（PASS）
`keystone_live.py:61-83` build()：15 个特征全部来自 `*_pre` / `h1`(0-300s) / `h2`(300-600s) 列；
SQL 窗口 `keystone_realcost.py:55-71` 明确 `sec<=W(600)` 为特征、`W<sec<=W+H(15000)` 仅用于 label/出场回放。
前向列（`min_tick_fwd`/`max_tick_fwd`/`ns_fwd`）**只**出现在 `fatal` 标签（`keystone_live.py:80-82`），不进特征矩阵 `f`。
live bridge 同源同窗 `bridge.py:145-162`（`sec<=600`）。→ 特征无前视 **PASS**。

| 特征 | 来源 | pre-entry? |
|---|---|---|
| ns_pre/buyers_pre/senders_pre/swaps_per_buyer/buyer_sender_ratio | `*_pre` countIf(sec<=600) | ✅ |
| early_ret/pre_vol/pre_pullback/pre_runup_from_lo | tick_pre 区间 | ✅ |
| liq_log/liq_trend/liq_drawdown | liq_*_pre | ✅ |
| heat_accel/mom_h1/mom_h2 | h1/h2(≤600s) | ✅ |

### 训练/划分：时间序划分（PASS）
研究脚本 `keystone_live.py:161-164`：`cut=df.t0.quantile(0.6); tr=t0<=cut; te=~tr`，`clf.fit(X[tr],y[tr])`；
`p_cut` 只在 TRAIN 上学（`185-186`）。全代码路径**无** `train_test_split(shuffle=True)`。
出场定价 `exit_block`(86-98) 沿 tick 路径**因果**走 trailing/stop；卖价在因果出场块取 QuoterV2（`136-139`）。→ PASS。

### Caveat（非 FAIL，但要知道）
- (c) **live 训练 `services/signal_bridge/train_model.py:137-139` 在全历史上 fit、无时间 holdout**，`p_cut` 是样本内 60 分位。
  → 不是"每笔决策前视"（推理时是固定产物），但训练卫生比研究脚本弱，可能过拟合。建议 live 训练也加时间 holdout。
- (b) **live 进场时点 ≠ 回测进场块**：回测 entry_block=argMaxIf(sec<=600)；live bridge 窗口 `[600,900]s`。
  特征冻结在 600s，但真实成交价/摩擦约晚 ~5min → 是**仿真 gap**（非特征泄漏）。
- (c) `keystone_realcost.py:239` 凸性体检用 TEST 分位 → 读研究报告时的**评估泄漏**，不影响 live 推理。

## 2. 满仓并发的相关性尾部风险 — ELEVATED

deployed `.env`：`MAX_CONCURRENT=10`、`MAX_POSITION_ETH=SIZE_MIN_ETH=0.057`（固定仓）、
`MAX_BALANCE_FRACTION=0.10`、`MAX_POOL_SHARE=0.05`、`MAX_VOLUME_SHARE=0.10`、`DYNAMIC_SIZING=1`。

- 风控全是**逐仓**（笔数 + 每笔余额比 + 每池深度/量份额），**没有跨仓/组合层相关性闸门**（`main.py:629-631` 只数 count）。
- 峰值在险：满 10 仓 × 0.057 = **~0.57 ETH** 进场名义（受每笔 balance×0.10 约束，需钱包≥~0.57 ETH 才填满）。
- 相关 −50%（memecoin 同步崩 / Base 全域砸盘）：单轮约 **−0.285 ETH** ≈ 1 ETH 钱包的 ~28.5% 回撤（+gas）。
- 唯一组合层兜底 = 每日亏损熔断（我已设 `DAILY_LOSS_PCT=25`，`main.py:106-133`）——但它**事后**（实现亏损后）才触发，非事前限敞口。

## 3. 极端场景行为表

| 场景 | 系统行为（证据）| 残余风险 |
|---|---|---|
| Base 超级爆发（信号洪峰）| bridge 30s 轮询、一轮全发、`maxlen=10000`(`bridge.py:249`)；executor `xreadgroup count=10`(`main.py:1320`)；买入前过 max_concurrent+重复+每日亏损+前置闸门 | bridge **无节流**；并发上限+闸门是唯一限速；洪峰下尾部信号排队/延迟成交 |
| 进场后急跌 | 硬止损 `pnl<=-0.50`（`main.py:1080`，5s 一查 `MONITOR_INTERVAL_SEC`）；trailing=1.0/TP=10.0 **实质关闭**（凸性右尾设置）；蜜罐/真实gas 每 60s 复检 | 5s 间隔 + tx 延迟内的滑点；−50% 是已定价成本非故障 |
| 持仓中 Base/RPC 宕机 | 读路有 fallback(`tx_sender.py:78-81`，.env 配 base.org 备用)；连续 10 次取价失败(~50s)→`blind_mark_panic` 强平(`main.py:943-964,1032-1069`)；卖不掉→`_abandon_unsellable_position`(停重试，token 留钱包,864-890)；panic 期间 monitor 仍跑 | 主备双挂时无告警（见 B-#2）；abandon 后 token 滞留需人工；50s 盲窗内 PnL 用 entry 顶替 |

## 缓解建议（按优先级）
1. **加组合层敞口闸门**（P1）：开仓前校验 Σ未平仓 stake ≤ 钱包 X%（如 30%），把"事后熔断"补成"事前限敞口"，直接压相关 −50% 尾部。
2. **爆发期 max_concurrent / 相关性感知 sizing**（P2）：洪峰时同类池同时进多仓=隐性高相关，可对同窗口新池数设软上限。
3. **质量卫生**（P2）：live `train_model.py` 加时间 holdout，避免样本内 p_cut 过拟合。
4. 见 B 报告：修 panic 读失败 fail-closed、RPC 双挂告警（与本节尾部风险同根）。

## 判定
- 仓位逻辑无前视：**PASS**（特征严格 pre-entry、时间序划分、live sizing 用 pre-trade 链上态）；caveat = live 训练无 holdout（训练卫生，非前视）。
- 并发尾部：**WEAK_PASS / ELEVATED**——逐仓闸门完善，但缺组合层相关性限制；每日亏损熔断（25%）是唯一事后兜底。
- 极端场景：行为明确、有 blind_mark_panic / abandon / fallback 兜底；主要残余是"主备 RPC 双挂无告警"与"abandon token 滞留"。

## 仍未知 / n
- 满仓 10 笔相关 −50% 的**实际**联合分布无 live 样本（live 至今仅 17 笔 AERO，n 太小）→ 尾部相关性属 (c) 仅能 live/MC 估计；MC 见 `risk_rebacktest.py` fatal_multiplier 压力表。
- 不得据 n=17 改并发/仓位参数；组合闸门属"加保险"而非调参，建议照加。
