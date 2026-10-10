# 上线「适当激进版」+ 影子日志 + 上线前体检 —— 执行回报（2026-06-25）

> 状态：**所有可逆改动已完成并通过验证；live 行为尚未改变（容器仍跑旧配置）。**
> **在执行不可逆的 compose 重建（=动真钱上线）前停下，因发现一个与委托人假设不符的关键事实，需委托人确认上线编排。** 见 §0。

---

## §0 必须先报告的关键事实（与提示假设不符）

| 项 | 提示里的假设 | 实测真相（证据） | 影响 |
|---|---|---|---|
| **SYSTEM_PANIC** | "锁定" | **未设置**（`redis EXISTS SYSTEM_PANIC = 0`，无任何 `*PANIC*` key）。executor 已 **live 运行 9 小时**，启动横幅 `panic=False`，**当前正用旧配置真钱交易**。 | §六"最后一步解除 PANIC"的前提不存在。重建会**立即**带新配置上线，没有验证窗口——除非我先主动设 PANIC 造一个窗口。 |
| **TRAIL 现状** | "0.35" | **实为 1.0 = trailing 关闭**（`.env EXECUTOR_TRAILING_STOP_PCT=1.0`；运行中横幅 `trail=100%`；代码 `main.py:1448` 在 `<0.99` 才触发）。0.35 只是回测值，从未在 live 生效。 | 改 0.25 是**首次开启** trailing，不是"0.35→0.25 微调"。方向与你的实验（早走一点）一致，但行为变化更大，请知悉。 |
| **F deployer 数** | "247 deployer" | fixture 实为 **245** 个 + 2 个 PARADAENGI 蜜罐 = 247 个 EOA。33 skeleton 一致。 | 我的 unseed 精确移除 245 个 fixture，保留 2 个 PARADAENGI。等价于你说的"关 247 撤池名单、留蜜罐种子"。 |

> 这三条都不改变你的决策本身，只是落地编排必须据实调整。**核心问题：重建前要不要先设 PANIC？** 见 §六。

---

## §1 目标配置落地（改前→改后 + 生效方式）

所有 executor 配置走 compose `${VAR:-default}` 从 `.env` 插值（**无 `env_file`、无硬编码覆盖**，已逐行核对 `compose.yaml:436-541`）。
→ **`.env` 是权威源；但必须 `compose up -d --build executor`（重建+重建容器）才生效**，仅改 `.env` 不重启=不生效（历史坑已规避）。

| 旋钮 | 改前（live 实际） | 改后 | 落地位置 | 生效方式 |
|---|---|---|---|---|
| F 撤池黑名单（245 deployer + 33 skeleton） | 启用 | **关**（保留 2 PARADAENGI 蜜罐 + 运行期学习） | `.env: EXECUTOR_SEED_RUG_BLACKLIST=0` + 代码 `main.py:1661-1675` + `cluster_blacklist.py:332-388` | compose 重建 |
| TRAIL | **1.0（关闭）** | **0.25** | `.env: EXECUTOR_TRAILING_STOP_PCT=0.25` | compose 重建 |
| 30min 硬顶 | 无 | **维持无** | 未改（`EXECUTOR_TIME_DECAY_MIN=240` 即 4h time-exit 不动） | — |
| 单笔 size | $50（`MAX_POSITION_ETH=SIZE_MIN_ETH=0.0302`） | **不变** | 未改 | — |
| 最大并发 | 3（`MAX_CONCURRENT_POSITIONS=3`） | **不变** | 未改 | — |
| 影子日志 | 无 | **开** | `.env: EXECUTOR_SHADOW_30MIN=1 / _SEC=1800` + 代码 | compose 重建 |

---

## §2 安全边界确认（§二每条防线"保留"逐项打勾）

未触碰任何买入/卖出决策，以下全部**保留**（`.env`/compose 值未动）：

- ✅ 深池门 ≥3 ETH，fail-closed：`EXECUTOR_MIN_POOL_WETH=3.0` / `_FAIL_CLOSED=1`
- ✅ p_fatal 进场模型 + MIN_PRE 闸：未动（signal-bridge `BRIDGE_P_CUT_MAX=0.27`）
- ✅ sell-sim / state-override：`EXECUTOR_SELL_SIM=1` / `_FAIL_CLOSED=1`
- ✅ external risk（honeypot.is + GoPlus）：`EXECUTOR_EXTERNAL_RISK_GATE=1`，蜜罐类 critical 仍拦
- ✅ E 窗 post-buy 1800s：`EXECUTOR_POSTBUY_RECHECK_WINDOW_SEC=1800`（含 depth-collapse 0.5E + 拉黑）
- ✅ $50 size cap：未动
- ✅ **PARADAENGI 蜜罐种子可单独保留**：F 由 3 个独立函数 seed（`seed_paradaengi`=2 蜜罐 / `seed_safe_rug_skeletons`=33 / `seed_safe_rug_deployers`=245）。`SEED_RUG_BLACKLIST=0` 时只 unseed 后两者，且 `unseed_safe_rug_deployers` 显式排除 PARADAENGI 地址，并在 unseed 后**重新 seed 一次 PARADAENGI** 兜底。运行期 abandon 拉黑（E 窗的"拉黑"）走 `record()`，不受影响、继续生效。

> 结论：**能只关"撤池惯犯名单"、保留蜜罐种子。**（结构上天然可拆，无需一刀切。）

---

## §3 代码改动 diff（本轮唯一允许的改动：F 开关 + TRAIL 值 + 影子日志 + 确认出场延迟埋点）

> 注：工作区有大量**此前未提交**的改动，整体 `git diff` 不可用于审核。以下按精确位置列出**本轮**改动，均为纯增量、不改任何买卖决策。

1. `cluster_blacklist.py:332-388`：新增 `unseed_safe_rug_skeletons` / `unseed_safe_rug_deployers`（可逆 SREM 已知列表，保留 PARADAENGI）。
2. `main.py:1655-1676`：F seed 包一层 `EXECUTOR_SEED_RUG_BLACKLIST` 开关；=0 时 unseed 历史撤池名单 + 重新 seed PARADAENGI。
3. `main.py:88-89`：新增 `SHADOW_30MIN_SEC` / `SHADOW_30MIN_ENABLED` 常量。
4. `main.py:341-385`：新增 `_record_shadow_30min()`（只写 `exec:shadow` 流）。
5. `main.py:1494-1510`：监控循环内，算出 `mark` 后、出场判定**前**，满 30min 时 `mark_shadow_once` + 记录。**不设 should_exit、不改任何 trailing/stop/time 行为。**
6. `position.py:272-282`：新增 `mark_shadow_once()`（HSETNX 原子幂等，跨监控周期不重复记录）。
7. `.env` / `compose.yaml`：上述 env 变量值与透传。
8. **出场延迟埋点**：已在代码（`main.py:283-311` `exit_trigger_ms/block` + `exit_delay_sec/blocks`；`_finalize_close` 已传参）。**当前 live 镜像（9h 前构建）尚未包含**——本次重建会一并带入。

---

## §4 全量上线前体检（§五 1–6）

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 单元/执行器测试 | ✅ **PASS** | 影子 4 + 出场延迟 3 = 7/7；honeypot defense **21/21** 无回归。`.venv/bin/python -m pytest` |
| 2 | 配置 dry-print 核对 | ⏳ **待重建后验证** | 需 `compose up -d --build executor` 后看启动横幅 + 新增 dry-print（见 §6 命令） |
| 3 | 代码 diff 核对 | ✅ **PASS** | §3：仅 F 开关/TRAIL/影子/确认埋点，未触碰买卖逻辑，旧字段保留 |
| 4 | ETH 价格源 | 🟡 **部分** | live 关键路径=链上 WETH→USDC QuoterV2 报价（无预言机、缓存5min，`swap.py:1241`），静态兜底 `EXECUTOR_ETH_PRICE_USD=1700`。"3源交叉校验"是**人工运维核对**（`.env` 注释记录与 Chainlink 偏差0.06%），**非运行时自动门**——如实标注。 |
| 5 | Telegram 告警 | ✅ **配置在位** | `TELEGRAM_BOT_TOKEN`/`CHAT_ID` 已设；防御拦截带 `🛡`（`main.py:554`）、例行过滤静默（`_SILENT_REJECT_PREFIXES`）；启动横幅 + 买/卖通知存在。live 模式无 TG 会硬停启动（`validate_telegram_or_raise`）。 |
| 6 | 回滚预案 | ✅ 见 §5 | |

> 第 2 项是唯一"待重建后"才能确认的——而重建就是不可逆上线点。故停在此处等确认。

---

## §5 回滚预案

**一键停买（立即，任何时刻）：**
```bash
docker exec eatlq-redis-1 redis-cli SET SYSTEM_PANIC 1
# 监控/卖出继续，只暂停新买入
```

**完全恢复旧配置（重新启用 F、TRAIL 回关闭）：**
```bash
cd /opt/EATLQ/infra/compose
# 编辑 .env：
#   EXECUTOR_SEED_RUG_BLACKLIST=1     (重新启用 245+33 撤池名单)
#   EXECUTOR_TRAILING_STOP_PCT=1.0    (trailing 回到关闭=旧 live 行为)
#   EXECUTOR_SHADOW_30MIN=0           (可选，关影子日志)
docker compose -f compose.yaml up -d --build executor
# F 名单会在启动时重新 seed（seed 函数幂等）；trailing 回 100%（不触发）
```

---

## §6 拟执行的安全上线编排（需委托人确认后再跑）

因 PANIC 当前是**关**的（系统已 live 在旧配置），为获得提示要求的"验证窗口"，拟用如下顺序——**每一步都更安全，最后才真正放新配置上线**：

```bash
cd /opt/EATLQ/infra/compose
# 1) 先停买（造验证窗口；比"裸重建直接上线"安全）
docker exec eatlq-redis-1 redis-cli SET SYSTEM_PANIC 1
# 2) 重建 executor（带新配置 + 出场延迟埋点 + 影子日志；启动即读 panic=ON，不会买）
docker compose -f compose.yaml up -d --build executor
# 3) 核对启动横幅 dry-print（§五.2）：
#    trail=25%  hold=14400s  size=$50  max_concurrent=3
#    "F LP-pull blacklist DISABLED ... PARADAENGI honeypot seed KEPT"
#    panic=True
docker logs eatlq-executor-1 --tail 40
# 4) 全 PASS → 开闸
docker exec eatlq-redis-1 redis-cli DEL SYSTEM_PANIC
# 5) 死盯第一笔成交闭环（买→持仓→告警→卖），异常立即 SET SYSTEM_PANIC 1
```

⚠️ 跳过 $5 smoke test（委托人决定）= 第一笔真实信号即 $50 真钱 + 全新配置。**强烈建议死盯第一笔完整闭环。**

---

## §7 熔断线与判决标准（已记录，待开闸后生效）

- 熔断（任一触发→立即 `SET SYSTEM_PANIC 1` + 回报）：累计亏损 −$500 / 连续 3 笔 ≤−50% / execution 管道异常。
- 一个月不动策略参数（execution 正常的前提下）。
- 判决：**继续** = 净 PnL>0 且出场延迟中位 <2min；**判死** = 净亏 或 出场延迟中位>2min 且毁灭率高。

---

## §8 开闸执行结果（2026-06-25 已上线）

按委托人选择的安全编排执行，已上线：

1. ✅ `SET SYSTEM_PANIC 1`（停买造验证窗口）→ 验证为 `1`。
2. ✅ `compose up -d --build executor`（重建镜像+重建容器）→ Image Built / Container Recreated。
3. ✅ **dry-print 全项核对一致**（容器内 env = 运行进程真相）：
   `TRAIL=0.25 · SEED_RUG=0(F关) · SHADOW=1 · EWIN=1800 · MINPOOL=3.0 · SIZE=0.0302($50) · CONC=3 · SELLSIM=1 · EXTRISK=1`；
   启动横幅 `exit_policy stop=50% hold=14400s trail=25% monitor=5s`；
   `F LP-pull blacklist DISABLED ... PARADAENGI honeypot seed KEPT`。
4. ✅ **Redis 黑名单真相**：deployer 集 = **2**（仅 2 个 PARADAENGI 蜜罐），skeleton 集 = **0**（33 已移除）。
5. ✅ `DEL SYSTEM_PANIC` → `EXISTS=0`；executor 在 17:39:32 停止打 "blocking BUY"、进入正常 live 监听（`_panic_active` 每轮重读 Redis 已生效）。
6. ✅ 容器 Up、新镜像、无 error/traceback；当前 0 持仓，等待下一笔信号。

**状态：已正式上线（live，无 panic，等待第一笔信号）。**

⚠️ **死盯第一笔成交完整闭环**（买→持仓→告警→卖）。一键停手：`docker exec eatlq-redis-1 redis-cli SET SYSTEM_PANIC 1`。
