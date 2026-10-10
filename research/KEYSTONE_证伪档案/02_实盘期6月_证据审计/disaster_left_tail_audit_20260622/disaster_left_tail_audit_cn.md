# 灾难左尾审计（−100% / 数学崩塌路径）

日期：2026-06-22  范围：EATLQ/KEYSTONE Base V3 实盘（只读扫描）
审计性质：只读 + 报告。未发交易 / 未写 Redis / 未重度打 RPC。实盘 `eatlq-executor-1` 全程运行未触碰。

## 0. 一句话裁定

| 子项 | 裁定 |
|---|---|
| Q1 −100% 场景穷举 + 防御核对 | **WEAK_PASS** — 主要链上路径有防御；但外部风险闸在 live 配置里被大幅关闭，多条结构性 rug 路径仅剩链上闸兜底 |
| Q2 真实 gas 上限 + state-override 卖出模拟 存在性/可绕过/误杀 | **PASS（存在且 fail-closed）** + **WEAK_PASS（绕过/误杀）** — 机制存在、live 强制开启；正常币误判方向是"拒买"（安全侧） |
| Q3 风控重心在 −100% 左尾而非 −50% | **PASS** — 凸性配置确认：hard_stop=−50% 是已定价的凸性地板，TP/trailing 实质关闭，让右尾跑 |
| 整体 | **WEAK_PASS** — 凸性左尾防御框架成立，但 **live 风险闸配置把多个 rug 检测关掉了**，是头号必补项 |

> 头号发现（可执行）：live 的 `external_risk` 闸把 **blacklist / proxy / mintable / unverified / owner-concentration** 检测**全部关闭**，且在 API 无数据/超时时 **fail-open 到全仓**（非 canary）。同时 **每日亏损熔断 `EXECUTOR_DAILY_LOSS_PCT=0`（关闭）**。这把"−100% 左尾"的防线从"外部情报 + 链上模拟"双层，削成了几乎只剩"链上 QuoterV2 蜜罐 + real-gas 卖出模拟 + 转账税>10%"。

---

## Q1. −100% / 数学崩塌路径穷举 + 防御核对

下表逐条。防御列：✅有 / ⚠️部分 / ❌无。类别：(a)回测已建模 (b)同源 eth_call 存疑 (c)只能 live。

| # | −100% 场景 | 机制 | 防御 | 代码路径 | 残余风险 | 类别 |
|---|---|---|---|---|---|---|
| 1 | **gas 炸弹蜜罐**（QuoterV2 报价健康，真实 gas 下 revert，永远卖不掉） | quote 用近无限 gas，真实 tx 卡 gas 上限 revert | ✅ | 进场 `preflight_trade.executable_sell_gate`→`swap.simulate_real_sell`（3M gas state-override 重放）；持仓每 60s 复检 `main.py:1005-1026`；live `EXECUTOR_SELL_SIM=1, FAIL_CLOSED=1` | 仅 v3/aero_cl 模拟（`swap.py:1124`）；v2/aero_vamm 直接 `sim_skip` 放行 → v2 gas 炸弹无此防御 | (b)→(a) |
| 2 | **转账税蜜罐**（买卖抽税，等效慢性 −100%） | 高 buy/sell tax | ⚠️ | `external_risk.assess_external_token_risk` tax>10%→critical 拒（live `MAX_*_TAX_PCT=10` 仍开）；+ friction gate 往返 | API 无数据时 **fail-open 全仓**（live `FAIL_CLOSED=0,CANARY_ON_MISSING=0`）；<10% 税不拦只告警 | (b) |
| 3 | **出生即 rug / 蜜罐**（创建即不可卖） | 卖路径直接 revert / quote=0 | ✅ | 进场 `honeypot_gate`(QuoterV2)+`friction_gate`(往返,fail-closed)+`executable_sell_gate`(real-gas)；三闸串联 `preflight_trade.run_pretrade_gates` | v2 池 real-gas 不模拟；QuoterV2 同源盲区(见审计①) | (b) |
| 4 | **买后秒 rug / toggle**（进场时能卖，下一块抽走流动性/翻 honeypot） | 进场后状态变化 | ⚠️ | 持仓 `_monitor_once`：每 tick `check_honeypot` + 每 60s `simulate_real_sell` 复检 → 不可卖即 `_abandon_unsellable_position`(`main.py:997-1026`) | 复检间隔（监控 5s / real-gas 60s）内的瞬时 rug 仍吃满；abandon 只止损不可逆损失 | (c) |
| 5 | **出场流动性蒸发**（卖得掉但深度塌，−90%+） | LP 撤走，卖出巨滑点 | ⚠️ | 紧急退出 `_is_emergency_sell` 允许 `min_out=0` 抢跑出逃（`swap.py:142-149`）；hard_stop/time_exit 不被 gas cap 阻挡（`swap.py:201-207`） | min_out=0 = 接受任意差价成交（可能 −95%）；这是"尽量逃"而非"防损"，残余损失大 | (c) |
| 6 | **持仓中 RPC/Base 宕机**（看不到价、发不出卖单） | mark 连续失败 / 节点不可达 | ✅ | `_mark_eth_wei` 连续 10 次失败→返回 −1→`blind_mark_panic` 强制卖出(`main.py:957-1070`)；READ 有 fallback lane(`tx_sender._ReadFallbackHTTPProvider`)；SEND 有 fallback URL（live 未配，见残余） | 若整个 Base 链停摆，卖单也无法上链——不可防（系统性）；SEND fallback live 为空 | (c) |
| 7 | **买成功但 0 token**（cancel/replace 竞态或抽税到 0） | 买回执成功、钱包无币 | ✅ | `main.py:743-809` 多次 balanceOf 重试 + receipt log 回收；仍 0 则 abandon + 告警，不建仓 | 真到账后又被秒抽则落入#4 | (a) |
| 8 | **孤儿仓**（买成功但崩溃在建仓前，token 无人监控→无人卖） | executor 崩溃窗口 | ✅ | 买前写 `record_pending_buy` 意图；启动 `_reconcile_orphan_buys` 对账钱包余额接管(`main.py:511-584`) | 接管路由需 registry/metadata，丢失则需人工(已告警) | (a) |
| 9 | **不可路由仓**（registry + 持仓 metadata 都丢，卖不出去） | 元数据缺失 | ⚠️ | `_pool_info_from_position` 用持仓内存的 token0/1 重建路由；连续失败→`unroutable` panic 告警(`main.py:977-992`) | panic 只告警不自动卖；需人工 | (c) |
| 10 | **多笔同日连环 −100%（账户级崩塌）** | 多个独立 rug 叠加 | ❌ | 单笔 cap ~0.057 ETH + `max_balance_fraction=0.10`；**但 `EXECUTOR_DAILY_LOSS_PCT=0` 每日熔断关闭** | 无日级 kill-switch；`max_concurrent=10` → 理论同时 10 仓暴露 | (a) |
| 11 | **nonce/卡单导致卖单永不落地** | RPC pending 滞后 | ✅ | 本地 nonce 跟踪 `_get_next_nonce`；卖单 +20%/+50% 两次替换、**永不取消**(`swap.py:279-312`)；hard_stop gas 估失败用 500k 兜底 | 极端 gas 飙升下替换仍可能慢；非 −100% 主路径 | (a) |
| 12 | **数学崩塌：除零 / 单位错配 / decimals** | 代码 bug 致错价 | ⚠️ | slot0 价计算有 decimals 处理(`swap.py:1199-1216`)；PnL 用 `max(entry,1)` 防除零 | 未见单元测试覆盖 fee_bps/decimals 边界；`p1_fee_bps_audit.py` 存在但未在本审计复跑 | (b) |

### Q1 小结
- **强防御（✅）**：gas 炸弹（v3）、链宕机盲价 panic、买后 0 token、孤儿仓、卡单。
- **半防御（⚠️）**：转账税（依赖外部 API 且 live fail-open）、买后秒 rug（间隔风险）、出场流动性蒸发（只能尽量逃）、不可路由（仅告警）。
- **无防御（❌）**：账户级同日连环亏（日熔断关闭）；v2 池 gas 炸弹（real-gas 模拟跳过 v2）。
- KEYSTONE 实盘 universe 由 bridge 限定为 **v3 WETH 新池**（`bridge.py:123` `pool_type=='v3'`），所以"v2 gas 炸弹无防御"在当前信号源下**不暴露**——但 registry/执行层支持 v2 卖出，属潜在口子。

---

## Q2. 真实 gas 上限 + state-override 卖出模拟：存在性 / 可绕过性 / 误杀

### 存在性：PASS

- 实现：`services/executor/src/swap.py:1105-1172` `simulate_real_sell()`。
  - 用 `eth_call` + `stateDiff` state-override 给探针地址（默认 `0xdead` 或真实钱包）授予 token 余额 + router allowance，**在买入前**就能重放真实 `exactInputSingle` 卖出。
  - gas 上限 `EXECUTOR_SELL_SIM_GAS=3000000`（真实档，非 node 默认无限），revert 或 out≤0 → 判不可卖。
  - 自动探测 balance/allowance 存储槽（`_detect_balance_slot`/`_detect_allowance_slot`，sol/vyper 两种布局，扫 30 槽）。
- 接入点：
  - 进场闸 `preflight_trade.executable_sell_gate`→`run_pretrade_gates` 第 4 道（`preflight_trade.py:123-149,177`）。
  - 卖失败后判定 `_should_abandon_after_sell_fail`（`main.py:893-912`）。
  - 持仓期每 60s 复检（`main.py:1010-1024`）。
- live 配置确认（docker env）：`EXECUTOR_SELL_SIM=1`、`EXECUTOR_SELL_SIM_FAIL_CLOSED=1`、`EXECUTOR_SELL_SIM_GAS=3000000`。**强制开启且 fail-closed**。

### 可绕过路径分析：WEAK_PASS（有边界，但已知口子）

| 绕过路径 | 是否可绕过 | 说明 |
|---|---|---|
| dry-run 跳过 | 不适用 live | 仅 `cfg.dry_run/非 live` 跳过 `run_pretrade_gates`（`main.py:671`）；live 必过 |
| 非 v3/aero_cl 池 | **是** | `simulate_real_sell` 对 v2/aero_vamm 返回 `sim_skip_pooltype`=放行（`swap.py:1124`）。当前信号源只发 v3，未暴露 |
| 存储槽探测失败 | 否（fail-closed） | 槽测不到→`sim_unavailable_*`；live `FAIL_CLOSED=1` → `executable_sell_gate` 判 **拒买**（`preflight_trade.py:143-145`） |
| RPC 错误 | 否（fail-closed） | 同上，`sim_rpc_error` 在 fail-closed 下拒买 |
| 孤儿仓接管 | **是（设计内）** | `_reconcile_orphan_buys` 接管已买入的 token 不跑进场闸——但它不是"买"，是接管已持有仓去监控/卖出，合理 |
| 买后秒 rug | 部分 | 进场模拟通过后，token 在下一块翻 honeypot → 靠 60s 复检捕获，间隔内不可防 |

→ 在 **当前 v3-only 信号源 + fail-closed** 下，进场无 live 可绕过路径让"模拟判不可卖"的币通过。最大现实口子是 **v2 池放行** 与 **60s 复检间隔**。

### 正常币（USDC/WETH）误杀分析：PASS（误判方向安全）

- KEYSTONE universe 不含 USDC/WETH 这类老池（bridge 只发 t0 在 10–15min 的新 v3 WETH 池），所以正常稳定币**根本不会被信号选中**，不存在"误杀正常交易"。
- 即便被模拟到，代码有**针对正常币的专门防误判**：`simulate_real_sell` 注释 + `swap.py:1142-1147` 明确**不**用 `MAX_UINT256` 授余额——因为 USDC(FiatToken v2.2) 把黑名单标志打包在余额槽最高位，全 1 会被误判成"已拉黑"导致 `transferFrom` revert（假阳性）。改为只授所需额度（高位清零）。这正是为防止把正常币误判成不可卖。
- 误杀的方向：若正常币存储布局异常导致槽测不到 → fail-closed → **拒绝买入**（安全侧，错过=无损），**不会**造成 −100%。
- 结论：误杀风险存在但方向安全（拒买而非套牢），且对 USDC 类有专门处理。

---

## Q3. 风控重心在 −100% 左尾，而非 −50% 止损：PASS

### 凸性配置确认（live docker env）

| 参数 | live 值 | 含义 |
|---|---|---|
| `EXECUTOR_STOP_LOSS_PCT` | 0.50 | hard_stop = −50%，**凸性地板**（已定价的最大单笔可控损） |
| `EXECUTOR_TAKE_PROFIT_PCT` | 10.0 | =1000%，**实质关闭** TP（不提前止盈，让右尾跑） |
| `EXECUTOR_TRAILING_STOP_PCT` | 1.0 | =100%，**实质关闭** trailing（不被回撤打出） |
| `EXECUTOR_TIME_DECAY_MIN` | 240 | 4h 持有后时间退出 |

代码侧 `main.py:54-58,1082-1093` 与 `swap.py:109-119` 一致：唯一会真正触发的退出是 **hard_stop(−50%) 与 time_exit(4h)**；TP/trailing 因阈值设到极端而不触发。这正是 §`1.md` §0 与 `keystone_lefttail.py` 的凸性论：

- `keystone_lefttail.py:1-9` 明确："prune the predicted-fatal must REMOVE the left tail WITHOUT killing the right-tail winners"——风控目标是**砍掉 fatal 左尾**，不是优化均值。
- `keystone_realcost.py` 的裁决指标是 `fatal=ret<=-50%` 占比、winners(>30%) 计数，而非夹紧止损。
- `keystone_exit_upgrade/verdict_cn.md`（n=131）：`live_hold4h`（不止盈、4h 持有）med=+44%、fatal=7%，**击败**所有更紧的 quote_dd15/25 规则（后者把右尾也砍了，med 掉到 +7.7%/+15%）。即"−50% 地板 + 让赢家跑"在 holdout 上优于"紧止损"。

### 关键确认：−50% 是成本不是故障

- −50% hard_stop 是**主动选择的凸性成本**：做多新池本就预期大量小亏换少数大赢。它已进入回测的 fatal 统计与破产模拟（`1.md` §0/§8 Track B）。
- 真正的"故障"是 **−100% 路径（Q1 那 12 条）**——它们不是策略定价内的成本，是执行/合约风险。审计①②的重心放在这里是正确的。
- 残余张力：hard_stop=−50% 在"出场流动性蒸发"(#5)时可能**无法在 −50% 成交**，紧急退出 `min_out=0` 会让实际成交远低于 −50%（滑成 −80%~−95%）。所以 −50% 不是硬地板，是"触发点"；真实左尾仍可超过 −50%。这与凸性框架不冲突（fatal 定义就是 ≤−50%），但需在破产模拟里用**实测出场滑点**而非假设恰好 −50%（见审计①Q4 的 exec-gap 回填）。

---

## 必补项排序（按对 −100% 左尾的边际防护，全部为配置/观测改动，不改交易逻辑）

> 注意：以下是**审计建议**，涉及实盘配置变更，需人工确认后再改；本审计只读，未改任何配置。

| 优先级 | 必补项 | 当前 live 状态 | 建议 | 残余风险若不补 |
|---|---|---|---|---|
| **P0** | 重开外部风险结构性闸 | `BLOCK_BLACKLIST/PROXY/MINTABLE/UNVERIFIED=0`、`MAX_OWNER/CREATOR_PCT=100` 全关 | 至少重开 `BLOCK_BLACKLIST=1`、`BLOCK_MINTABLE=1`（这两类是直接 −100% enabler：可拉黑你的卖出、可增发砸盘） | 可拉黑/可增发的币按"正常"放行，链上 QuoterV2 当下查不出 → 买后被拉黑=不可卖=−100% |
| **P0** | 外部 API 无数据时不要 fail-open 全仓 | `FAIL_CLOSED=0` + `CANARY_ON_MISSING=0` → 无数据=全仓买入 | 设 `CANARY_ON_MISSING=1`（无数据→canary $25），新池仍可交易但小仓 | 最新池（最常无数据）以全仓暴露在最高风险段 |
| **P1** | 重开每日亏损熔断 | `EXECUTOR_DAILY_LOSS_PCT=0`（关闭） | 设一个非零值（如 20%）做账户级 kill-switch | 同日多笔 −100% 无自动刹车，仅靠单笔 cap |
| **P1** | 缩短买后 rug 复检间隔 / v2 纳入 real-gas | real-gas 复检 60s；v2 池 `sim_skip` | 高风险新池前 N 分钟把 `_REALGAS_RECHECK_MS` 调小；或限定只交易能模拟的 v3 | #4 间隔内 rug、v2 gas 炸弹 |
| **P1** | 配置 SEND fallback lane | `EXECUTOR_SEND_RPC_URL_FALLBACK=`（空） | 配置备用 MEV-protected 发送端点 | 主 SEND RPC 故障时卖单发不出（#6 残余） |
| **P2** | 出场滑点实测回填破产模拟 | 假设 −50% 触发即 −50% 成交 | 用审计①的 `exec:fills` 实测出场 `min_out=0` 成交价，喂回 Track B | 破产概率被低估（真实左尾 < −50%） |
| **P2** | fee_bps/decimals 边界单测 | `p1_fee_bps_audit.py` 存在未复跑 | 复跑并补单测覆盖 #12 | 错价导致错误 min_out / 错误 mark |

---

## 仍未知项（UNKNOWN + 测量方案）

1. **买后秒 rug 的真实发生率与复检间隔内损失** — UNKNOWN（需 live 计数）。测量：在 `exec:fills`（审计①）加 `realgas_recheck_history`，统计"进场通过但 60s 内翻不可卖"的笔占比，n≥30 才报率。类别(c)。
2. **出场流动性蒸发时的真实成交滑点分布** — UNKNOWN。测量：对 hard_stop/time_exit 且 `min_out=0` 的真实成交，记 `realized/mark - 1` 分布。当前 live N=8（审计①），不足。类别(c)。
3. **外部 API（GoPlus/honeypot.is）对 Base 新池的覆盖率与延迟** — UNKNOWN。测量：离线对历史信号池批量查两 API，统计"有数据/无数据/超时"占比，决定 canary 的实际触发频率。类别(b)。
4. **v2/aero_vamm 池在当前信号源下是否真的零暴露** — 当前 `bridge.py` 只发 v3，但 registry 支持 v2 卖出；UNKNOWN 是否有其他信号源/手动路径注入 v2。测量：审计 `signals` 流历史 `pool_type` 分布。类别(c)。
5. **整链 Base 宕机时的卖单积压行为** — 不可防（系统性），但 UNKNOWN 当前是否有"链恢复后批量退出"演练。测量：故障注入演练（非只读，需授权）。

---

## changed files / commands

- **changed files：无（只读审计）。** 仅新增本报告：
  - `mvp/audits/disaster_left_tail_audit_20260622/disaster_left_tail_audit_cn.md`
- **未运行**任何写 Redis / 发交易 / 改配置 / 重 RPC 的命令。
- 配置事实读取自 `docker inspect eatlq-executor-1` 的环境变量（敏感 URL/KEY 已脱敏，未记录）。
- 所有"必补项"均为**建议**，涉及实盘配置变更需人工确认；本审计未改任何 env / 文件 / 容器。

### 关键证据路径速查
- gas 炸弹防御：`services/executor/src/swap.py:1105-1172`(simulate_real_sell)、`preflight_trade.py:123-149`、`main.py:1005-1026`
- 外部风险闸（live 多项关闭）：`services/executor/src/external_risk.py:35-62`(policy)、`:264-294`(决策树)；live env `EXECUTOR_EXTERNAL_RISK_*`
- 转账税/honeypot critical 列表：`external_risk.py:145-207`
- 凸性退出（−50% 地板/TP·trail 关闭）：`services/executor/src/main.py:54-58,1082-1093`、`swap.py:109-119`
- 紧急出逃 min_out=0：`swap.py:122-149`
- 盲价 panic：`main.py:943-964`(_mark_eth_wei)、`:1031-1070`(blind_mark_panic)
- abandon 不可卖：`main.py:864-912`
- 孤儿仓对账：`main.py:511-584`
- MEV-protected send + fallback：`services/executor/src/tx_sender.py:68-131`；live `EXECUTOR_MEV_PROTECTED=required`
- 凸性裁决证据(n=131)：`mvp/audits/keystone_exit_upgrade/verdict_cn.md`
- 凸性论基底：`scripts/research/base_lefttail/keystone_lefttail.py:1-9`、根 `1.md` §0
- live 配置事实：`docker inspect eatlq-executor-1`（`DAILY_LOSS_PCT=0`、`SELL_SIM=1/FAIL_CLOSED=1`、`MAX_CONCURRENT=10`、外部风险多项=0）
