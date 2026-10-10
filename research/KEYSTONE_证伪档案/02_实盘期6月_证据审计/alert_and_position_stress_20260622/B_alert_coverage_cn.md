# B — 告警覆盖矩阵 + 静默失败审计（只读，2026-06-22）

只读。目标：保证不在场时不盲飞——每条致命/异常路径是否发 Telegram。
告警入口：`services/executor/src/notify.py::send_telegram`（fire-and-forget，失败只 `log.warning`，
缺 `TELEGRAM_BOT_TOKEN/CHAT_ID` 时静默 no-op）。

## 先纠正用户前提（Bug2 残留）
用户说「收到 0 token 的 ERROR 路径目前无告警」——**已不成立**。
`main.py:794-809`：买成功但钱包 0 token、重试 receipt+balanceOf 后仍为 0 →
`log.error` + **`if cfg.live_trading_mode: send_telegram("⚠️ CRITICAL … MANUAL CHECK REQUIRED")`**
+ `clear_pending_buy` + dead_letter + return。**live 模式下已告警**（仅 dry-run 不发，合理）。
→ 该 bug 的告警侧已修复；判定 **PASS**。

## 告警覆盖矩阵（路径 × 是否告警，关键项）

| 路径 | file:line | 日志级别 | 告警 |
|---|---|---|---|
| SYSTEM_PANIC 触发（含每日亏损熔断）| `main.py:100-103` / `131-133` | error | ✅ |
| BUY 被前置闸门拒（外部风险/蜜罐/摩擦/卖出模拟）| `main.py:692-697`,`preflight_trade.py:160-183` | warning | ✅ |
| BUY 失败（quote 失败 / tx revert / status0）live | `main.py:735-740` | error | ✅(live) |
| **BUY 成功但 0 token（Bug2）** live | `main.py:794-806` | error | ✅(live) |
| BUY 建仓成功 | `main.py:841-847` | info | ✅ |
| 不可卖→放弃持仓 ABANDON（蜜罐/真实gas/连续revert）| `main.py:883-890`,`997-1024`,`1120-1124` | error | ✅ |
| blind_mark_panic 强平成功 | `main.py:1050-1055`→`934-940` | error | ✅ |
| 卖出连续卡住 ≥3 次 | `main.py:1064-1067`,`1126-1132` | error | ✅ |
| 持仓不可路由（路由元数据缺失，第1/5/每20次+第60次→panic）| `main.py:978-991` | error | ✅ |
| 孤儿仓重建路由失败 / 链上余额找回 | `main.py:539-544`,`576-580` | error | ✅ |
| 每日心跳（open/pnl/panic 状态）| `main.py:1299-1303` | — | ✅(默认86400s) |
| **BUY skip：注册表缺失/低置信/重复/满并发** | `main.py:615-631` | info/warn | ❌(部分进 dead_letter) |
| **动态 sizing 无尺寸过闸 / 余额不足** | `main.py:661-688` | warn | ❌(dead_letter) |
| **第 1–2 次卖出失败（未到 abandon/stuck 阈值）** | `main.py:1118-1119` | error | ❌ |
| **mark 取价失败 1–9 次（PnL 盲算用 entry 顶替）** | `main.py:957-964` | warning | ❌ |
| **RPC 读主用失败→fallback / 发送主用失败→fallback** | `tx_sender.py:47-51`,`127-131` | warning | ❌ |
| **Redis xreadgroup / xautoclaim 失败** | `main.py:1327-1329`,`1166-1167` | warning | ❌ |
| **monitor 整轮异常（loop 继续）** | `main.py:1304-1305` | exception | ❌ |
| 启动 banner | `main.py:1266-1272` | — | ✅ |

## SYSTEM_PANIC 一键暂停 — PASS
- 阻断新买：`main.py:1307-1310` `_panic_active` → `continue`，跳过 `xreadgroup`(1319) 与 PEL 重放(1314)。
- 监控仍跑：`_monitor_once` 在 `main.py:1289-1290` **先于** panic 检查执行 → 暂停期间持仓仍被标价/止损/放弃。
- 不产孤儿：买入前先 `record_pending_buy`(广播前, 713-730)，0-token/失败清 pending，启动 `_reconcile_orphan_buys`(1230-1236) 兜底。

## 静默失败清单（出错但无人知道）——按危险度排序
1. **【高危】panic key 读取异常 → 视为无 panic**：`main.py:208-209` `_panic_active` except → return False。
   若 Redis 抖动正好命中，暂停会"静默失效"、买入悄悄恢复。建议 fail-closed（读失败按 panic-active 处理或告警）。
2. **【高】RPC 主+备都退化**：`tx_sender.py:47-51/127-131` 仅 warning。两路都挂时无法标价/卖出 = 左尾，却无告警。
3. **【高】monitor 整轮异常**：`main.py:1304-1305` 仅 `log.exception`，循环继续。若持续抛错，持仓无人管理且静默。
4. **【中】Telegram 自身发送失败**：`notify.py:18-26` 只 warning，无备用通道；缺 env 时(`15-16`)静默 no-op、无启动校验。
5. **【中】mark 取价失败 1–9 次**：`main.py:962-964` 用 entry 顶替（PnL 盲算），止损可能延迟，仅 warning。
6. **【中】第 1–2 次卖出失败**：`main.py:1118-1119` 到第 3 次才告警；急跌时这两次可放大滑点。
7. 其余 log-only：`main.py:308/315/338/656/689/1003/1025/909`，`swap.py:461/513/900/1217/1249`，`external_risk.py:240/252`。

## 必补告警清单（优先级）
| # | 补什么 | 位置 | 优先级 |
|---|---|---|---|
| 1 | panic key 读失败 → fail-closed + 告警（修静默失效）| `main.py:201-209` | P0 |
| 2 | RPC 主用连续失败 / 主备双挂 → 告警 | `tx_sender.py:47-51,127-131` | P0 |
| 3 | monitor 连续整轮异常 → 告警（带计数）| `main.py:1304-1305` | P1 |
| 4 | 启动时 live 模式校验 TG 已配置，否则硬告警/拒启 | `notify.py` / preflight | P1 |
| 5 | mark 盲算达 N 次（如5）预警、卖出第1次失败即提示 | `main.py:962-964,1118-1119` | P2 |

## 判定
- Bug2「0-token 无告警」：**PASS（已修，live 会告警）**。
- 致命路径告警覆盖：**WEAK_PASS**——真正会亏钱/丢仓的终态（panic/熔断/拒买/买失败/0token/放弃/卡单/孤儿）都告警；但**基础设施退化与"静默失效"类**（尤其 #1 panic 读失败、#2 RPC 双挂、#3 monitor 异常）无告警，是不在场时的盲区。
- SYSTEM_PANIC：**PASS**。

## 仍未知 / 需 forward 验证
- 真实 TG 触达率、panic 读失败的实际发生频率：无历史计数，UNKNOWN，需加监控计数后观察。
- n：本审计是代码路径枚举（非样本统计），覆盖性结论可信；触发频率类结论需 live 数据。
