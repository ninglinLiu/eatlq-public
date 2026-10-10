# 运维与操作（EATLQ / Linux）

> 原作者项目（LASZLO）的 PowerShell 操作手册已移植为 bash。Windows 片段不再维护。

> **Guard (Phase 3):** Operational runbooks only—**`not an authorization`**. Compose `up` / maintenance / start steps do **not** authorize labeling, training, live, Phase 6, or alignment. **`contract docs override narrative drift`** for what is allowed—see `mvp/audits/whitepaper_v2_contract_index.md` and `docs/*` contracts. **`FAILED_NO_7D_ALPHA`** remains until superseded (`mvp/audits/failed_no_7d_alpha_guard.md`). **Never paste** `.env`, `PRIVATE_KEY`, or API keys into docs or audit artifacts.

## 环境要求

- Linux（生产 VPS 常见 Ubuntu 22.04 / Debian 12）
- Docker Engine 24+ 与 `docker compose` 插件
- 仓库根目录可访问 `/opt/EATLQ`；`.env` 位于 `infra/compose/.env`（不入库；参考 `infra/compose/.env.example`）

## 核心环境变量（摘要）

| 变量 | 用途 |
| ---- | ---- |
| `EVM_WS_URL` / `ALCHEMY_WS_URL` | ingestor WebSocket（必选） |
| `EVM_WS_URL_FALLBACK` | ingestor 轮换备用 WS（推荐） |
| `LIVE_BASE_RPC_URL` | Anvil fork、部分 HTTP、标注脚本 |
| `LIVE_BASE_RPC_URL_FALLBACK` | 可选 HTTP 备用 |
| `LIVE_TRADING_MODE` | `true` 时执行器走主网与 `PRIVATE_KEY`（**真钱风险**） |
| `EXECUTOR_ETH_PRICE_USD` | 执行器与 Dashboard 的 USD↔ETH 换算基准（默认 3500；执行器会将活跃价写入 `exec:eth_price_usd`） |
| `SIGNAL_MIN_CONFIDENCE` | 策略发单与执行器接单共用门槛（0.0–1.0，默认 **0.52**） |
| `REDIS_URL` | 容器内通常为 `redis://redis:6379/0` |

**安全**：勿将 `.env`（含私钥、API Key）提交到 Git；泄漏后应立即轮换密钥。

## 日常启动（推荐）

```bash
cd /opt/EATLQ
scripts/laszlo.sh up          # 构建并拉起全栈
scripts/laszlo.sh status      # docker compose ps
scripts/laszlo.sh logs executor   # 跟踪某个服务
scripts/laszlo.sh down        # 停止 + 清理 orphans
```

兼容原始 compose 命令：

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml up -d
```

`scripts/laszlo.sh` 首次运行若检测不到 `infra/compose/.env`，会自动从 `.env.example` 复制。

## Phase 6 每周训练（推荐）

```bash
scripts/laszlo.sh phase6
```

**以脚本为准：** 实际步骤、容器内工作目录、`LABELER` 默认值与退出码语义以 **`scripts/phase6_weekly_cycle.sh`**（由本命令调用）及其头部注释为准；以下为当前默认（`LABELER=executable`）的摘要。

流程顺序（失败即停；在 **strategy** 容器内执行）：

1. `scripts/label_dashcam_executable.py`（默认 `LABEL_MAX_AGE_DAYS=30`；若显式设置 `LABELER=approximate` 则改用 `scripts/label_dashcam.py`，仅调试）
2. `scripts/retrain_model.py`（训练窗口 `RETRAIN_WINDOW_DAYS=28`，holdout 固定最新 `RETRAIN_HOLDOUT_DAYS=7` 天；绝对门槛 `RETRAIN_MIN_ACCURACY=0.55`，并要求 PR-AUC 较当前模型提升）

退出码约定（用于 cron 告警；**与 `scripts/phase6_weekly_cycle.sh` 一致**；**不** 表示经济 guard 解除）：

- `0`：本轮脚本成功且 **model promoted**（仍受 `mvp/audits/failed_no_7d_alpha_guard.md` 等约束）
- `1`：编排脚本自身异常
- `2`：训练完成但未通过部署门槛（准确率 / PR-AUC 未提升 / 过拟合）
- `3`：数据门槛不满足（样本量、验证集、holdout 过小）

cron 示例（每周日 03:30 运行）：

```cron
30 3 * * 0 cd /opt/EATLQ && scripts/laszlo.sh phase6 >> logs/phase6_cron.log 2>&1
```

## 日志与磁盘

- Compose 已为各服务配置 **json-file 轮转**（默认 `10m x 2`；高噪声服务更小）。
- **Executor**：`restart: on-failure:10`，避免 panic/异常时无限重启刷盘。
- **Executor 依赖链**：等待 `redis` 与 `anvil`（若启用）均 healthy 后再启动。
- 构建上下文：`.dockerignore` 已排除 `logs/`、大 CSV、`.git/` 等，减少无效镜像层。

### 一键维护

```bash
scripts/laszlo.sh maintain
```

等价于：`docker image prune -f` + `redis-cli MEMORY PURGE` + `redis-cli INFO keyspace`。更激进的清理（`-a`、stopped containers、unused volumes）请手动执行 `docker system prune`。

## 常见问题

### ingestor 日志持续 429

- **现象**：无新 `market_ticks`，`logs/dashcam.csv` 不再增长。
- **处理**：更换/新增 Alchemy App、主备 key 分层使用；短暂降载；避免多机共用同一免费配额。

### SELL 卡住 / 重复挂单排查

- **当前机制**：`position_manager` 使用 `pending_sell=1` + `pending_sell_at` 锁，默认 TTL 为 60 秒。
- **自动恢复**：超过 TTL 后会打印 `Sell lock expired ...` 并自动重发 SELL 信号。
- **人工检查**：
  - 查看 `positions:{token}` 是否长期 `pending_sell=1`；
  - 查看 executor 是否持续消费 `signals`；
  - 若确认链上未成交，可手工 `HSET positions:{token} pending_sell 0` 并 `HDEL positions:{token} pending_sell_at`。
- **零余额兜底**：若 Executor 收到 SELL 但 `balanceOf(wallet)=0`，会尝试直接 `close_position` 清理 ghost 仓位；若 Redis 清理失败会记录告警。

### BUY 与 SELL 风控差异（重要）

- **BUY**：执行流动性检查 + 蜜罐检查（Live 下失败即拒绝）。
- **BUY（余额查询）**：`get_balance` 在 Live 下若 RPC 失败，执行器会记录 `warn!` 并立即拒绝 BUY（fail-safe）。
- **SELL**：不走 BUY 风控，优先保证退出路径；若 `balanceOf` 查询失败，执行器按 fail-open 继续尝试卖出。

### market_ticks 字段与 MsgPack

- **约定文档**：`docs/market-ticks-stream-contract.md`（**非**对 live / 训练 / Phase6 的授权）。
- **要点**：ingestor 默认写 **`data`**；strategy 按固定顺序尝试 **`data` → `payload_msgpack` → `payload` → `tick`**；缺失 MsgPack 块或解码失败时跳过该条但仍 **XACK**，避免消费组 pending 堆积。

### 死信流（dead-letter）排障

- 执行器消费 `signals` 时，若条目缺少 `payload` / `signal_json`、JSON 解析失败，或 BUY 缺少合法池地址，会写入 `signals_dead_letter`（字段含 `entry_id`、`error`、`payload` 预览）后再 `XACK`。
- **Sprint005 `error` 分类（节选）：** `parse_failed`、`buy_missing_pool_or_token`、`buy_invalid_pool_or_token_address`（`0x`+40 位十六进制）、以及 `_handle_signal` 异常时的 `handle_failed: ...`。完整表见 `docs/executor-language-contract.md`。
- **非 BUY**（如 `SELL`、或小写 `buy`）：不进死信队列，仅跳过并 XACK（设计上允许同流多类型消息）。
- Archiver 消费 `market_ticks` 遇到坏 MsgPack 同样会逐条 `XACK` 并告警，避免消费组 pending 累积。
- **静态字段摘要（无 Redis）：** Python `signals_dead_letter_ops_summary()` 在 `services/executor/src/main.py`（仅供排障/单元测试对齐字段名与环境变量）。
- 建议定期巡检：

  ```bash
  scripts/laszlo.sh logs executor | grep dead_letter
  docker compose exec redis redis-cli XLEN signals_dead_letter
  ```

### 流滞后（stream lag）与坏消息量（无专用 metrics 后端）

- **PEL / 消费组：** 对 `market_ticks`、`signals` 使用 `XINFO GROUPS <stream>`（或 `XPENDING`）查看 pending 条数；持续增大通常表示消费者卡住、解析变慢或上游洪峰。
- **近似队列深度：** `XLEN <stream>` 是流总长度（含已交付历史，需结合 `last-delivered-id` 解读）；死信深度用 `XLEN signals_dead_letter`（流名可被 `SIGNALS_DEAD_LETTER_STREAM` 覆盖）。
- **malformed `market_ticks`：** strategy / archiver 对缺字段或坏 MsgPack **跳过并 XACK**，代码路径**不维护** Redis 计数器；排查依赖服务日志与抽样 `XRANGE`。约定见 `docs/market-ticks-stream-contract.md`。
- **汇总说明：** `docs/observability-stream-contract.md`。

### 回撤与紧急停机（drawdown / panic）

- **Executor** 侧 `_check_daily_loss` 比较当日已实现 PnL 与日起始余额比例，超限时调用 `_set_panic` 写入 **`SYSTEM_PANIC`**（键名可由 `SYSTEM_PANIC_KEY` 覆盖，默认 `SYSTEM_PANIC`）。
- **Strategy** `should_block_signal` 读取同一 panic 键，停发新信号（与 executor 侧止损检查互补，避免重复数学）。
- 巡检：`redis-cli GET SYSTEM_PANIC`（或自定义键）看是否为停机原因明文；Throttle 细节见 `Architecture.md` 中 executor 主循环说明。

### 信号置信度阈值

- 策略侧与执行器侧共用 `SIGNAL_MIN_CONFIDENCE`，默认 `0.52`。两侧都会过滤，防止配置漂移。
- 调参时只改 `infra/compose/.env`，重启 strategy + executor 即可生效。

### 持仓键迁移

- 旧版 `exec:pos:{pool}`（JSON 字符串）已迁移为 `positions:{pool}`（Redis Hash）。
- Executor 启动时自动 `migrate_legacy_exec_keys()` 把残留的 `exec:pos:*` / `exec:realized_pnl:*` / `exec:day_start_balance:*` 搬到 `positions:*`，一次性迁移；迁移后无需人工介入。

### Redis `NOGROUP`

- 策略侧已实现组缺失时的恢复逻辑；若 Redis 被 flush，确认 consumer group 与流一致后重启对应服务即可。

## 本地策略（无 Docker）

见 `services/strategy/LOCAL_STARTUP_GUIDE.md`（需本机 Redis/Python 路径与文档路径一致时自行调整）。

## Paper / dry-run readiness（静态预检，非 live 授权）

本节描述如何把仓库推进到 **可观察的 paper / dry-run 配置态**。它 **不是** live、训练、打标或 Phase 6 的授权；**不解除** `mvp/audits/failed_no_7d_alpha_guard.md` 等经济门禁。

### 静态预检（无 Docker / 无 RPC / 无 Redis）

- `make check-readiness` 或 `python3 scripts/paper_dryrun_readiness.py`：校验 `infra/compose/.env.example` 键集合与 `scripts/paper_dryrun_readiness.py` 中声明一致、核心路径文件存在、`.gitignore` 覆盖常见 `logs/*.csv` / `mvp/diagnostics/*.csv` 运行时产出、`compose` 中 dashboard/strategy/executor 为 **profile 可选**、`scripts/laszlo.sh` 默认携带 `--profile strategy|executor|dashboard`。
- `make check-local-safety`：顺序执行 `check-static` 与 `check-readiness`（仍不拉起任何外部服务）。
- `scripts/laszlo.sh doctor`：同上；可加 `--env-file infra/compose/.env`（**仅**解析两行 guard 变量；**Sprint010：** 若 `LIVE_TRADING_MODE` 为真则直接 FAIL，防止误走 live 栈预检）。加 `--compose-config` 可在静态通过后调用 `docker compose config -q`（**不** `up`；需本机 Docker CLI）。

### 安全组合（executor 启动门禁）

- `EXECUTOR_DRY_RUN=1` 为执行器主要模拟开关（见 `docs/executor-language-contract.md`）。
- **`LIVE_TRADING_MODE=true` 与 `EXECUTOR_DRY_RUN=1` 同时成立时，执行器会拒绝启动**（配置歧义）。预检脚本在有本地 `.env` 时可仅此两项做静态报警。
- `infra/compose/.env.example` 中的 `LIVE_TRADING_MODE` 供文档与substitution 使用；若需在 **Docker** 容器内生效，须在 `compose.yaml` 的 `executor.environment` 中显式传入（当前文件未默认注入该项时，容器内该变量为空，视同为非 live）。

### Dashboard profile / 可选观测

- `dashboard` 服务使用 `profiles: ["dashboard"]`：**不带 profile 的** `docker compose up` 不会启动 dashboard；`scripts/laszlo.sh up` 默认启用 **strategy + executor + dashboard** 三 profile，便于一体观测。若只需核心链路与执行器、不要 HTTP 看板，可自行改 `laszlo.sh` 的 `PROFILES` 或改用裸 `docker compose` 命令。

### Compose 静态展开（Sprint010，不启动容器）

- `make check-compose-config` 或 `python3 scripts/paper_dryrun_readiness.py --compose-config`：在静态检查通过后，使用默认 `infra/compose/.env.example` 跑 `docker compose ... config -q`，仅校验 **YAML + 变量插值**；**不** 拉 Redis、**不** 连 RPC、**不** 创建容器。
- 使用本地 `infra/compose/.env` 时：`python3 scripts/paper_dryrun_readiness.py --compose-config --compose-env-file infra/compose/.env --env-file infra/compose/.env`（`--env-file` 仍只做 guard 扫描；**不要**在终端打印 `.env`）。

- **Sprint011 受控 bring-up（摘录）：** 门禁通过后可用 `docker compose ... up -d redis postgres clickhouse`，再用 `--profile strategy --profile executor up -d strategy executor` 起策略与执行器；Executor 启动日志应出现 **`dry_run: true`**。若项目内 **已有** ingestor / swap-poller / pool-discovery 在跑，仍会占用 WS/RPC——**净新增 ingestor** 须在确认配额后再执行；Dashboard 仅作只读观测（常见 HTTP 401 表示需 Basic Auth）。

---

## 仓库静态契约检查（无 Docker / 无 RPC）

用于 MR 前或重构后快速确认：**不**拉起链、**不**改 `mvp/audits/*.json` 授权布尔、**不**表示 guard 解除。

```bash
cd /opt/EATLQ
make audit-validate-json    # mvp/audits/*.json 中 allow_* / *_ALLOWED 须全为 false
make static-contracts       # python3 -m unittest tests.test_whitepaper_v2_static_contracts
make check-static           # 以上两项顺序执行
make check-readiness        # Sprint009：paper/dry-run 静态预检
make check-local-safety     # check-static + check-readiness
make check-compose-config   # Sprint010：docker compose config -q（需 Docker CLI；不 up）
```
