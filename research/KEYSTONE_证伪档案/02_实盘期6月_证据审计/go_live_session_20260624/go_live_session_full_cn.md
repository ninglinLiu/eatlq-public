# EATLQ / KEYSTONE 上线会话全记录（2026-06-24）

> 本文档汇总本轮对话全部内容：Stage 1.0 价源验证 → 防御部署 → $5 冒烟 → $50 实盘 → 7 点拒单复盘 → Telegram 通知修复 → MODELZERO 反事实分析。  
> 生成时间：2026-06-24（UTC+8）

---

## 目录

1. [Stage 1.0：ETH/USD 三源交叉验证](#1-stage-10ethusd-三源交叉验证)
2. [Stage 1 后续：配置暂存与防御代码部署](#2-stage-1-后续配置暂存与防御代码部署)
3. [用户决策：Size / E 窗口 / fail-closed](#3-用户决策size--e-窗口--fail-closed)
4. [放行：开闸抓一笔即复位 → 受控 $5 冒烟](#4-放行开闸抓一笔即复位--受控-5-冒烟)
5. [$50 锁仓与正式 GO LIVE](#5-50-锁仓与正式-go-live)
6. [早上 7 点买入信号检查](#6-早上-7-点买入信号检查)
7. [Telegram「买入信号」误读与通知修复](#7-telegram买入信号误读与通知修复)
8. [MODELZERO 反事实：如果没拒绝会怎样](#8-modelzero-反事实如果没拒绝会怎样)
9. [当前系统状态快照](#9-当前系统状态快照)
10. [关键命令备忘](#10-关键命令备忘)

---

## 1. Stage 1.0：ETH/USD 三源交叉验证

**性质**：只读、不重启、不花钱。`SYSTEM_PANIC` 保持 active。

**验收口径（用户钉死）**：
- 真正要验的是 executor 用于 **sizing** 的 `get_eth_usd_price()` 返回值
- 三源：① executor 内部价（主角）② WETH/USDC 池直读 ③ Chainlink ETH/USD（独立锚）
- 容差：两两偏差 ≤1% 直接过；1–2% 人工判断；>2% 停下

**测量结果（block 47,723,027）**：

| 源 | 来源/算法 | 价格 | 说明 |
|---|---|---:|---|
| **S1 · executor `get_eth_usd_price()`** | QuoterV2 `quoteExactInputSingle(WETH→USDC, 1 ETH, fee 0.05%)` | **$1,658.75** | 实际 sizing 用的数；含 1 ETH 价格冲击 |
| S2 · WETH/USDC slot0 | `sqrtPriceX96` 现货 | $1,659.63 | 单块可被瞬时操纵（深池成本高） |
| S3 · Chainlink | feed `0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70`，`description()="ETH / USD"` | $1,657.73 | 聚合喂价；updatedAt 距今 ~719s |

**两两偏差**：
- S1 vs S3：**0.062%**
- S1 vs S2：0.053%
- S2 vs S3：0.114%
- **最坏两两偏差 = 0.114% → ✅ PASS（≤1%）**

**边界说明（不阻断）**：
- 静态兜底 `EXECUTOR_ETH_PRICE_USD=1700` 比实时价高约 2.5%，仅在 QuoterV2 失败时使用
- S1 含 1 ETH 冲击，比纯现货略保守，更接近真实成交

**用户结论**：价源健康，签字放行 Stage 1 后续。

---

## 2. Stage 1 后续：配置暂存与防御代码部署

### 2.1 部署真相（重要）

- executor 是 **Docker 镜像构建型**（`build:`，无源码 volume 挂载）→ **单纯 restart 不会加载新防御代码，必须 rebuild**
- compose 用 `environment:` 逐项透传，**无 `env_file`**
- `EXECUTOR_MIN_POOL_WETH`、`EXECUTOR_POSTBUY_*` 等新开关原先 **未在 compose 透传**，放进 `.env` 也进不了容器

### 2.2 已实现的防御（代码 + 部署）

| 防御 | 机制 | 默认/配置 |
|---|---|---|
| **F** 集群黑名单 | `cluster_blacklist.py` + preflight 首闸 + 启动 `seed_paradaengi()` | PARADAENGI skeleton + 2 deployers |
| **①** 深池地板 | `pool_depth_gate()` in `preflight_trade.py` | `EXECUTOR_MIN_POOL_WETH=3.0` |
| **E** 买后逃生 | `_monitor_once()` 自适应 recheck | blocks=3, window=300→后改 1800s, depth_collapse=0.5 ETH |
| **P0.5** 执行质量埋点 | `_record_exec_quality()` buy + sell | deviation/delay/gas/池龄 → Redis XADD + TG |
| buy minOut | `_SLIPPAGE_TOLERANCE["buy"]=0.03` | 维持 3%，用数据说话，不先验收紧 |

### 2.3 compose / .env 变更

**compose.yaml**（executor `environment:` 新增）：
```yaml
EXECUTOR_MIN_POOL_WETH: ${EXECUTOR_MIN_POOL_WETH:-3.0}
EXECUTOR_MIN_POOL_WETH_FAIL_CLOSED: ${EXECUTOR_MIN_POOL_WETH_FAIL_CLOSED:-0}
EXECUTOR_POSTBUY_RECHECK_BLOCKS: ${EXECUTOR_POSTBUY_RECHECK_BLOCKS:-3}
EXECUTOR_POSTBUY_RECHECK_WINDOW_SEC: ${EXECUTOR_POSTBUY_RECHECK_WINDOW_SEC:-300}
EXECUTOR_POSTBUY_DEPTH_COLLAPSE_ETH: ${EXECUTOR_POSTBUY_DEPTH_COLLAPSE_ETH:-0.5}
```

**启动日志确认**（rebuild 后）：
```
seeded PARADAENGI cluster bytecode=0xa7df… deployers=[0x715c…, 0xc851…]
slippage caps … {'buy': 0.03, …}
SYSTEM_PANIC is active — starting in buy-blocked monitoring mode
```

**容器内实测常量**：
```
MIN_POOL_WETH_ETH = 3.0
DEPTH_GATE_FAIL_CLOSED = False（后改 True）
POSTBUY_RECHECK_WINDOW_SEC = 300（后改 1800）
MAX_POSITION_ETH = 0.057（后改 0.003 → 0.0302）
DYNAMIC_SIZING = 0
```

---

## 3. 用户决策：Size / E 窗口 / fail-closed

| 项 | 用户决定 | 理由 |
|---|---|---|
| **$5 冒烟** | `0.003 ETH` | 测管道，非测 sizing；价错一倍也无所谓 |
| **恢复档** | **$50 = 0.0302 ETH**（锁仓时用已验证价重报，非 $1659 硬锁） | 上轮已敲定 $50 |
| **E 窗口** | **1800s（30 分钟）** | PARADAENGI 撤池在 +12–15 分钟，300s 抓不到 |
| **深度探测失败** | **fail-CLOSED** | 漏挡不可验证池 = −$50；RPC 抖动错过 = $0；与策略哲学一致 |

**`.env` 生效值（冒烟前）**：
```
EXECUTOR_MAX_POSITION_ETH=0.003
EXECUTOR_SIZE_MIN_ETH=0.003
EXECUTOR_MIN_POOL_WETH=3.0
EXECUTOR_MIN_POOL_WETH_FAIL_CLOSED=1
EXECUTOR_POSTBUY_RECHECK_BLOCKS=3
EXECUTOR_POSTBUY_RECHECK_WINDOW_SEC=1800
EXECUTOR_POSTBUY_DEPTH_COLLAPSE_ETH=0.5
EXECUTOR_TIME_DECAY_MIN=2   # 冒烟用 120s time_exit，事后还原 240
```

---

## 4. 放行：开闸抓一笔即复位 → 受控 $5 冒烟

### 4.1 用户指令

- 策略：**开闸 → 抓第一笔 → 成交瞬间复位 panic**
- 不等自然信号；主动跑通 infrastructure

### 4.2 为什么用 WETH/USDC 深池

对 registry 内全部 WETH 池跑完整闸栈（只读）：
- **WETH/USDC 0.05%** `0xd0b53d9277642d899df5c87a3966a349a798f224` → **PASS**（深度 ~4847 ETH）
- AERO/DAI → `external_risk_goplus_mintable`
- cbETH → honeypot
- 其他 → friction / shallow 等

### 4.3 受控注入流程

1. `SET SYSTEM_PANIC` 冻结
2. `EXECUTOR_TIME_DECAY_MIN=2` → hold 120s → time_exit 自动卖
3. `DEL SYSTEM_PANIC` → XADD 一条 BUY 信号（WETH/USDC）
4. 检测到 `positions:pending_buy_set` → 立即 `SET SYSTEM_PANIC`

### 4.4 $5 冒烟结果（完整来回 ✅）

| 阶段 | 结果 |
|---|---|
| 全闸栈 | PASS：friction −0.10%，depth 4847E，sell_sim ok |
| 买入 | tx `062e8c9229e06806313a66810900de90f3454af1da79b69e635eb0adbf6430c6` |
| 买入埋点 | deviation **0.0%**，delay 9.8s，gas **$0.0014**，size $4.97 |
| E 监控 | 120s 内健康，未触发 ABANDON |
| 卖出 | reason=**time_exit**，tx `aefbb39f4fb7dbd400b9a677d2632470bb9e7b7e4927600dd0f800def09be091` |
| 卖出埋点 | deviation **0.0%**，gas $0.0016 |
| 经济账 | 净 swap 损 ~0.006%，双边 gas ~$0.003，**总成本 < 1 美分** |

---

## 5. $50 锁仓与正式 GO LIVE

### 5.1 锁仓时 ETH 价重验

```
fresh get_eth_usd_price = $1653.58
$50 equiv = 0.030237 ETH → 取 0.0302 ETH = $49.94
```

**`.env` 恢复档**：
```
EXECUTOR_MAX_POSITION_ETH=0.0302
EXECUTOR_SIZE_MIN_ETH=0.0302
EXECUTOR_TIME_DECAY_MIN=240   # 还原 4h hold
```

### 5.2 用户 GO LIVE 决定

- **DEL SYSTEM_PANIC** → $50 自主实盘开始
- **并发保持 3**（最大敞口 ~$150）
- 删除一次性冒烟脚本（`smoke_watch.sh`、`controlled_smoke.sh`）防误触

### 5.3 上线就绪快照

| 项 | 值 |
|---|---|
| size | 0.0302 ETH ≈ $49.94 |
| hold | 14400s（4h） |
| ① 深池 | 3.0 ETH，fail-closed |
| E | 1800s / 3 blocks / 0.5 ETH collapse |
| F 黑名单 | 1 skeleton + 2 deployers |
| DYNAMIC_SIZING | 0 |
| MAX_CONCURRENT | 3 |
| 钱包 | ~0.536 ETH（够多笔 $50 + gas） |
| SYSTEM_PANIC | 已清除 → live |

---

## 6. 早上 7 点买入信号检查

**用户询问**：早上七点有买入信号，赶紧检查。

**时间**：2026-06-23 **23:08:33 UTC** = 北京 **07:08**

**结果：被正确拒绝，未成交、未亏钱**

```
pretrade REJECT pool=0x47c9efb239080299a0b784d1291ea4ffb68e18b7
reason=shallow_pool_2.579E_below_3E
```

**信号 payload（bridge 发出）**：
```json
{
  "action": "BUY",
  "pool_address": "0x47c9efb239080299a0b784d1291ea4ffb68e18b7",
  "token_address": "0xf90554c68917cc7510104162c1c85b16b78a8453",
  "chain_id": 8453,
  "signal_source": "keystone_bridge",
  "confidence": 0.9,
  "position_size_usd": 100.0,
  "p_fatal": 0.0764,
  "pool_type": "v3",
  "token0_symbol": "WETH",
  "token1_symbol": "MODELZERO",
  "pre_volume_eth_wei": "810530788574336640"
}
```

**go-live 至今统计（~9h）**：
- exec_quality 真实成交：**0 笔**
- pretrade PASS：**0**
- pretrade REJECT：**1**（全部为 shallow_pool）
- 钱包：0.535991 ETH（与 go-live 前基本一致，差额为 $5 冒烟 gas）

---

## 7. Telegram「买入信号」误读与通知修复

### 7.1 用户问题

> 为什么被拒绝了？Telegram 上还能看到买入信号而不是拒绝信号？被拒绝就没必要发信号了吧。

### 7.2 根因分析

1. **系统里没有**「信号生成即推送」的独立通道
   - `signal_bridge` 写 Redis `signals` 流是**静默**的
   - `telegram_bot` 的 legacy 推送（`exec:trades`/canary）默认 **关闭**（`TG_ENABLE_LEGACY_EXEC_TRADE_PUSH=0`）

2. 7 点那条 TG 来自 **executor 拒绝路径**（`main.py` 原第 902 行）：
   ```
   BUY blocked 0x47c9efb2… reason=shallow_pool_2.579E_below_3E
   ```

3. **误读原因**：消息以 **`BUY`** 开头 → 手机通知预览像「买入信号」，实际是拒绝通知

### 7.3 修复方案（已部署 live）

**路由分流** — 新增 `_notify_gate_rejection()`：

| 拒绝类型 | Telegram |
|---|---|
| `shallow_pool_*` | **静默** |
| `friction_*` | **静默** |
| `depth_read_fail` | **静默** |
| `buy_quote_fail` | **静默** |
| `honeypot` / `cluster_*` / `external_risk_*` / `sim_unavailable_*` 等 | 发送 **`🛡 防御拦截·未买入 …`**，绝不以 BUY 开头 |

**代码位置**：`services/executor/src/main.py`
```python
_SILENT_REJECT_PREFIXES = ("shallow_pool", "friction", "depth_read_fail", "buy_quote_fail")

def _notify_gate_rejection(pool_addr: str, reason: str) -> None:
    if any((reason or "").startswith(p) for p in _SILENT_REJECT_PREFIXES):
        return
    send_telegram(f"🛡 防御拦截·未买入 {pool_addr[:10]}… reason={reason}")
```

**部署**：rebuild + recreate executor；分类实测通过；系统保持 live。

---

## 8. MODELZERO 反事实：如果没拒绝会怎样

**池**：`0x47c9efb239080299a0b784d1291ea4ffb68e18b7`（WETH/MODELZERO，v3 1% fee）  
**代币**：MODELZERO `0xf90554c68917cc7510104162c1c85b16b78a8453`  
**池创建**：约 23:01 UTC（信号时仅 ~7 分钟新）

### 8.1 事后链上 / DexScreener 数据

| 维度 | 数据 |
|---|---|
| 价格变化（h6/h24） | **−60.94%** |
| WETH 深度 | 23:08 拒绝时 **2.579 ETH** → 现在 **1.06 ETH（−59%）** |
| 当前流动性 | ~**$4,099 USD** |
| 可卖性 | 仅 fee=10000 池能卖；1 MODELZERO ≈ 0.0000000038 ETH（极薄） |
| 交易（h6） | 168 买 / 62 卖；h1 萎缩至 11 买 / 1 卖 |

### 8.2 结论

- **不是暴涨**，也**不是秒级硬蜜罐**
- 是 **「边流失流动性边阴跌 −60%」的慢撤池 / 软 rug 左尾**

### 8.3 若 23:08 未拒绝、按 $50（0.0302 ETH）买入

- 标记价约 **−60%** → 浮亏 ~**−$30**
- 实际平仓：$50 砸进仅 ~1 ETH / ~$4k 流动性池，滑点巨大 → **实际损失估计 −$25 ~ −$35**
- 买入时 0.0302 ETH 砸进 2.579 ETH 浅池本身也有可观买入滑点

**→ 深池门（≥3 ETH + fail-closed）挡对了。这是一条「避免的左尾损失」样本。**

---

## 9. 当前系统状态快照

（截至通知修复部署后，2026-06-24 ~05:00 UTC）

| 项 | 状态 |
|---|---|
| executor | live，`dry_run=False`，`panic=False` |
| size | 0.0302 ETH ≈ $49.94 |
| hold | 14400s（4h time_exit） |
| ① 深池 | 3.0 ETH，fail-closed |
| E | 1800s / 3 blocks / 0.5 ETH |
| F 黑名单 | 种子 intact（2 deployers + 1 skeleton） |
| DYNAMIC_SIZING | 0 |
| MAX_CONCURRENT | 3 |
| 持仓 | 0 |
| Telegram 拒单 | shallow/friction 静默；安全拦截发「🛡 防御拦截·未买入」 |

---

## 10. 关键命令备忘

### 紧急暂停（只挡新买，监控/卖出照常）
```bash
docker exec eatlq-redis-1 redis-cli SET SYSTEM_PANIC "manual_halt_$(date -u +%Y%m%d_%H%M%S)"
```

### 恢复买入
```bash
docker exec eatlq-redis-1 redis-cli DEL SYSTEM_PANIC
```

### 查看持仓
```bash
docker exec eatlq-redis-1 redis-cli SMEMBERS positions:open_pools
```

### 查看 panic 状态
```bash
docker exec eatlq-redis-1 redis-cli GET SYSTEM_PANIC
# 空 = live；有值 = 买入全阻
```

### 重建 executor（代码变更后）
```bash
cd /opt/EATLQ/infra/compose
docker compose --profile executor build executor
docker compose --profile executor up -d executor
```

### Chainlink ETH/USD（Base，已验证）
```
0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70
description() = "ETH / USD"
```

### WETH/USDC 0.05% 深池（冒烟用）
```
0xd0b53d9277642d899df5c87a3966a349a798f224
```

### MODELZERO 被拒池（7 点信号）
```
pool:  0x47c9efb239080299a0b784d1291ea4ffb68e18b7
token: 0xf90554c68917cc7510104162c1c85b16b78a8453
```

---

## 附录 A：关键 tx 哈希（$5 冒烟）

| 方向 | tx |
|---|---|
| BUY | `062e8c9229e06806313a66810900de90f3454af1da79b69e635eb0adbf6430c6` |
| SELL (time_exit) | `aefbb39f4fb7dbd400b9a677d2632470bb9e7b7e4927600dd0f800def09be091` |

---

## 附录 B：主要改动文件清单

| 文件 | 变更 |
|---|---|
| `services/executor/src/cluster_blacklist.py` | 新建 F 防御 |
| `services/executor/src/preflight_trade.py` | ① 深池门 + F 接入 |
| `services/executor/src/main.py` | E 买后逃生 + P0.5 埋点 + `_notify_gate_rejection` |
| `services/executor/src/swap.py` | SwapResult 扩展 + minOut 3% |
| `services/executor/src/registry.py` | `get_redis()` |
| `infra/compose/compose.yaml` | 防御 env 透传 |
| `infra/compose/.env` | MIN_POOL / POSTBUY / size / fail-closed |
| `tests/test_executor_honeypot_defense.py` | 离线回归 |

---

## 附录 C：待办 / 可选后续（会话中提及未实施）

- [ ] 把「被避免的损失」（如 MODELZERO）结构化记入埋点，便于复盘统计
- [ ] 用 exec_quality 头十几笔数据评估是否收紧 buy minOut（当前维持 3%）
- [ ] 静态兜底 `EXECUTOR_ETH_PRICE_USD=1700` 是否改到 ~1660（非阻断）

---

*文档结束 — 可直接全文复制粘贴。*
