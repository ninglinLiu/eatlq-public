# 左尾 -100% 配置修复 changelog (2026-06-22)

只改 4 个 executor env，逻辑零改动。改前先 `SET SYSTEM_PANIC` 暂停买入（监控继续），改后 `--force-recreate` 重启 executor。等用户确认后再 `DEL SYSTEM_PANIC` 恢复买入。

## 改了哪几个 env（前后值）
文件：`infra/compose/.env`（compose 用 `${VAR:-default}` 注入 executor 容器）

| env | 前 | 后 | 含义 |
|---|---|---|---|
| `EXECUTOR_EXTERNAL_RISK_BLOCK_BLACKLIST` | 0 | 1 | 可拉黑合约 → 拒买 |
| `EXECUTOR_EXTERNAL_RISK_BLOCK_MINTABLE` | 0 | 1 | 可增发合约 → 拒买 |
| `EXECUTOR_EXTERNAL_RISK_CANARY_ON_MISSING` | 0 | 1 | 外部数据缺失 → $25 canary（不再全仓 fail-open）|
| `EXECUTOR_DAILY_LOSS_PCT` | 0 | 25 | 当日实现亏损 ≥25% → 触发 panic 熔断当日买入 |

未动：`STOP_LOSS=0.50`、`TP=10.0`、`TRAILING=1.0`、`FAIL_CLOSED=0`、`CANARY_USD=25`、`SELL_SIM=1`、所有 sizing/凸性参数。

## 出单量影响（带 n）

样本：`exec:trades` 315 条 → 34 个唯一 pool → registry 可解析 13 个唯一非 WETH token。
方法：用 executor 本体 `external_risk.py` 模块，OLD vs NEW policy 逐 token 回放（GoPlus+Honeypot.is 只读）。
脚本：`scripts/research/base_lefttail/replay_external_risk_policy_diff.py`

转移统计 (n=13)：
- FULL → BLOCK：1（AERO，`goplus_mintable`）
- FULL → CANARY：11（多为 `honeypot_no_simulation_result` 部分缺数据）
- FULL → FULL：1（LSTEAK，clean）

DAILY_LOSS_PCT=25：历史 15 天 day_start/realized 配对，触发次数 = 0（最大单日亏损 ~5.2%）。纯安全网，对正常出单量零影响。

## 关键警告
- **唯一真正实盘成交过的 token 就是 AERO**（两个 live pool 0x82321f / 0x7f670f 都是 AERO/WETH）。AERO 因 `is_mintable=true` 被 `BLOCK_MINTABLE` 拦掉。AERO 是 Aerodrome 治理币，增发是排放机制设计，**不是 rug** → 这是 BLOCK_MINTABLE 的合法币误杀。
- 11/13 → canary 的比例偏高，疑似被「查询陈旧 token、honeypot.is 已无 sim」抬高，应视为 canary 率上界；真实 live canary 率需 forward 测量。

## 当前状态
- SYSTEM_PANIC = active（买入暂停，监控运行）。等用户确认是否 DEL 恢复。
