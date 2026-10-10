# KEYSTONE 上线 — 你必须手动做的事

> 本文档列出 Agent 无法代你完成、必须在 VPS / QuickNode dashboard 上亲自操作的步骤。
> 代码改动见 `keystone-go-live.md`；本文只讲「你亲手做什么」。

---

## 一、上一轮 Agent 做了什么（总结）

### 已完成（代码层）

| 模块 | 做了什么 |
|------|---------|
| **策略固化** | `docs/keystone-go-live.md` — 策略数字、风险清单、架构图 |
| **Signal Bridge** | 新服务 `services/signal_bridge/` — 监听新池 → 10min 特征 → ML 打分 → Redis `signals` |
| **Executor 执行层** | 双 RPC 车道 (`tx_sender.py`)、进场门禁 (`preflight_trade.py`)、5s 监控、QuoterV2 实时 mark |
| **代码审查修复** | 止损滑点映射、余额检查、零滑点 abort、RPC 盲 mark 强制平仓、ghost 仓位防护 |
| **P1 补全** | 日亏损熔断接线、Telegram 告警、SEND RPC failover、panic 时仍监控持仓 |

### 未完成 / 不能自动做

| 项 | 为什么必须你来做 |
|----|----------------|
| QuickNode MEV 开关确认 | 需要你的 dashboard 登录 |
| `.env` 填 RPC URL + 私钥 | 密钥不能由 Agent 写入 git |
| MEV canary 真钱测试 | 需要你的钱包私钥发 tx |
| Paper-forward 48h 观察 | 需要服务在 VPS 上持续跑 |
| 小额真钱上线 | 需要你在确认 paper 无问题后手动改 env |

---

## 二、Agent 说还需要优化什么（诚实版）

### 已在本轮修复

- 日亏损熔断（`EXECUTOR_DAILY_LOSS_PCT`）— 之前只有 env 变量，没接线
- Telegram 告警 — BUY/SELL/门禁拒绝/SELL 卡死
- RPC failover — `EXECUTOR_SEND_RPC_URL_FALLBACK`
- Signal bridge Dockerfile — 之前 build context 错误，容器起不来
- Panic 时仍跑 monitor — 之前 panic 会停掉所有监控

### 仍建议后续做（不阻塞 paper）

| 优先级 | 项 | 说明 |
|--------|-----|------|
| P1 | Dry-run PnL 更真实 | 目前 paper 用 entry 价 mark，不能反映真实滑点 |
| P2 | Flashblocks pending tag | sub-second 确认，当前 200ms poll 已够用 |
| P2 | 多 wallet nonce 隔离 | 并发 >3 笔时需要 |
| P2 | 动态 slippage | 按池深度调整 amountOutMinimum |
| P2 | SELL 失败 dead-letter | 3 次失败后标记 stuck pool |

---

## 三、手动操作清单（按顺序）

### Step 0 — 前置条件（5 分钟）

```bash
ssh 你的VPS
cd /opt/EATLQ/infra/compose
cp .env.example .env   # 如果还没有
nano .env              # 或用你喜欢的编辑器
```

**必须在 `.env` 里填写的变量：**

```bash
# RPC — 同一个 QuickNode endpoint 填 READ 和 SEND 即可
EXECUTOR_READ_RPC_URL=https://你的endpoint.quiknode.pro/xxx/
EXECUTOR_SEND_RPC_URL=https://你的endpoint.quiknode.pro/xxx/
EXECUTOR_MEV_PROTECTED=1

# 可选 failover（Blink 或其他 MEV RPC）
EXECUTOR_SEND_RPC_URL_FALLBACK=

# Paper 模式（先不要改 live）
EXECUTOR_DRY_RUN=1
LIVE_TRADING_MODE=

# 策略参数
EXECUTOR_FRICTION_MIN=-0.05
EXECUTOR_MAX_POSITION_USD=100
EXECUTOR_MAX_CONCURRENT_POSITIONS=3
EXECUTOR_STOP_LOSS_PCT=0.50
EXECUTOR_TIME_DECAY_MIN=240
SIGNAL_MIN_CONFIDENCE=0.52

# Telegram（强烈建议）
TELEGRAM_BOT_TOKEN=你的bot_token
TELEGRAM_CHAT_ID=你的chat_id

# ClickHouse 密码（与 compose 里 clickhouse 服务一致）
CLICKHOUSE_PASSWORD=你的密码

# 私钥 — 仅 paper 阶段可留空；MEV canary 和 live 需要
EXECUTOR_PRIVATE_KEY=
```

**不要 commit `.env` 到 git。**

---

### Step 1 — QuickNode MEV 确认（2 分钟，必须手动）

1. 登录 [QuickNode Dashboard](https://dashboard.quicknode.com/)
2. 找到你的 **Base Mainnet** endpoint
3. 确认 **MEV Protection = ON**
4. 截图保存（上线审计用）

---

### Step 2 — 启动基础设施 + Paper 链路（10 分钟）

```bash
cd /opt/EATLQ/infra/compose

# 启动核心
docker compose up -d redis clickhouse postgres archiver

# 等 healthy
docker compose ps

# 训练模型（首次必须，约 1-2 分钟）
docker compose --profile executor build signal-bridge
python3 /opt/EATLQ/services/signal_bridge/train_model.py
# 或在容器内: docker compose run --rm signal-bridge python train_model.py

# 启动完整 paper 链路
bash /opt/EATLQ/scripts/paper_forward_keystone.sh
```

**或者手动分步启动：**

```bash
docker compose --profile ingestion up -d ingestor
docker compose --profile strategy up -d pool-discovery swap-poller
docker compose --profile executor up -d executor signal-bridge
```

---

### Step 3 — 验证链路在跑（15 分钟）

```bash
# 1. Pool discovery 在发现新池
docker compose logs --tail=20 pool-discovery
docker exec eatlq-redis-1 redis-cli KEYS "pool:discovered:*" | wc -l
# 应该 > 0

# 2. Signal bridge 在打分
docker compose logs -f signal-bridge
# 应该看到: Model loaded / REJECT / BUY signal emitted

# 3. Executor 在消费 signals
docker compose logs -f executor
# 应该看到: DRY_RUN BUY / DRY_RUN SELL

# 4. Redis 信号流
docker exec eatlq-redis-1 redis-cli XLEN signals
docker exec eatlq-redis-1 redis-cli XLEN signals_dead_letter
# dead_letter 应该为 0 或极少

# 5. 持仓
docker exec eatlq-redis-1 redis-cli SMEMBERS positions:open_pools
```

---

### Step 4 — MEV Canary（需要私钥，~$0.01 gas）

**仅在确认 MEV Protection = ON 后做：**

```bash
cd /opt/EATLQ
export EXECUTOR_READ_RPC_URL="你的quicknode"
export EXECUTOR_SEND_RPC_URL="你的quicknode"
export EXECUTOR_PRIVATE_KEY="0x你的私钥"

python3 scripts/mev_canary_check.py
```

脚本会输出 tx hash。在 tx **pending 阶段**（前 2 秒）去 https://mempool.base.org 搜索：

- **Pending 不可见** → MEV 保护生效 ✅
- **Pending 可见** → 危险，不要上 live ❌

---

### Step 5 — Paper-forward 48h 观察

| 时间 | 检查什么 | 命令 |
|------|---------|------|
| 每 4h | signal-bridge 有 PASS/REJECT | `docker compose logs --tail=50 signal-bridge` |
| 每 4h | executor 有 DRY BUY/SELL | `docker compose logs --tail=50 executor` |
| 每 4h | 无 stuck SELL | `docker compose logs executor \| grep "SELL stuck"` |
| 每 4h | dead_letter 无异常 | `docker exec eatlq-redis-1 redis-cli XLEN signals_dead_letter` |
| 每 12h | Telegram 收到通知 | 手机看 bot 消息 |
| 48h 后 | 汇总 | 见下方验收标准 |

**48h 验收标准（全部通过才能上真钱）：**

- [ ] signal-bridge 至少发出 1 次 BUY signal（或大量 REJECT 说明模型在跑）
- [ ] executor 至少完成 1 轮 DRY BUY → DRY SELL（time_exit 或 hard_stop）
- [ ] signals_dead_letter = 0 或只有 parse_failed（无 pretrade_honeypot 大量堆积说明数据正常）
- [ ] 无 executor crash / autoheal 重启
- [ ] Telegram 收到 BUY/SELL 通知
- [ ] MEV canary 通过

---

### Step 6 — 小额真钱上线（Paper 48h 通过后）

```bash
cd /opt/EATLQ/infra/compose
nano .env
```

修改：

```bash
EXECUTOR_DRY_RUN=0
LIVE_TRADING_MODE=true
LIVE_AUTHORIZED_BY=HUMAN          # preflight 需要
EXECUTOR_PRIVATE_KEY=0x你的私钥
EXECUTOR_MAX_POSITION_USD=50      # 第一笔极小
EXECUTOR_MAX_CONCURRENT_POSITIONS=1
```

重启：

```bash
docker compose --profile executor up -d --force-recreate executor signal-bridge
docker compose logs -f executor
```

**第一笔 live 检查清单：**

1. pretrade PASS 日志（friction + honeypot）
2. BUY tx 在 basescan 确认
3. 5s monitor 在跑（mark 价格变化）
4. 4h 后或 -50% 时 SELL 自动触发
5. Telegram 收到 SELL 通知

---

## 四、紧急操作

| 情况 | 命令 |
|------|------|
| **立即停止所有 BUY** | `docker exec eatlq-redis-1 redis-cli SET SYSTEM_PANIC "manual_halt"` |
| **恢复交易** | `docker exec eatlq-redis-1 redis-cli DEL SYSTEM_PANIC` |
| **查看 open 持仓** | `docker exec eatlq-redis-1 redis-cli SMEMBERS positions:open_pools` |
| **查看某持仓详情** | `docker exec eatlq-redis-1 redis-cli HGETALL positions:0x池地址` |
| **重启 executor** | `docker compose --profile executor restart executor` |
| **看 crash log** | `docker compose logs --tail=100 executor` |

---

## 五、文件索引

| 你要看的 | 路径 |
|---------|------|
| 策略结论 | `/opt/EATLQ/docs/keystone-go-live.md` |
| **本文档（手动清单）** | `/opt/EATLQ/docs/keystone-manual-runbook.md` |
| Paper 一键启动 | `/opt/EATLQ/scripts/paper_forward_keystone.sh` |
| MEV 验证 | `/opt/EATLQ/scripts/mev_canary_check.py` |
| Env 模板 | `/opt/EATLQ/infra/compose/.env.example` |
| 你的实际配置 | `/opt/EATLQ/infra/compose/.env`（不 commit）|
