# KEYSTONE 策略固化 + 上线清单

> 生成日期: 2026-06-18  
> 状态: 策略验证通过 → execution infra 建设中 → 实盘待上线

---

## 一、策略结论（不可丢失）

### 1.1 核心发现

在 Base V3 WETH 新池上，**进场前可观测的两道门（深度门 + ML 左尾预测）**能把致命率从
~42% 压到 3–8%，使得一个简单的"买入→持有 4h→硬止损 −50%"凸性策略**在真实 QuoterV2
成本下翻正**。

### 1.2 数字（全部来自 HONEST PROTOCOL：TRAIN 定阈值 → 原样套 TEST holdout）

| 指标 | 无门（ALL） | **深度门 + ML（holdout）** |
|------|------------|--------------------------|
| n (池) | 322 | 131 |
| ~笔/月 | — | 33 |
| **均值** | −0.2% | **+59.7%** |
| 中位 | −13.6% | **+30.5%** |
| 胜率 | 39% | **68%** |
| 致命率 | 41.6% | **3.8%** |
| 最佳 | — | +566% |
| top1 share | — | 0.06 |

### 1.3 夯实验证（通过）

| 维度 | 结果 |
|------|------|
| Walk-forward 逐月 | 2/3/4 月均正（+79~91%），3 月 rug 月致命 71%→5% |
| 出场规则鲁棒 | 5 种出场全正 +48~65%，不依赖参数 |
| 容量测试 | $100→33笔/月，$300→30笔/月，$1000→9笔/月；边际不衰减 |
| 单点检验 | top1share 0.06~0.08，不是单币运气 |

### 1.4 策略逻辑

```
新池出生 → 等 10 分钟
  → 用前 10 分钟 [liq_log, pre_vol, buyer_sender_ratio, ...] 算 ML 分
  → 如果 p(fatal) ≤ p_cut (TRAIN 定死) → 进入候选
  → QuoterV2 活报价往返（WETH→token→WETH @ entry block）→ 摩擦 ≥ −5%？
    → 通过 → BUY (SEND RPC, MEV-protected)
  → 持有 ~4h，硬止损 −50%，否则到时间卖出
  → SELL (SEND RPC, MEV-protected)
```

### 1.5 模型参数

| 参数 | 值 |
|------|-----|
| W (pre-entry obs) | 600s (10 min) |
| H (forward horizon) | 14400s (4h) |
| MIN_PRE (最少 swap) | 5 |
| STOP | −50% |
| TRAIL | 关闭（回测证明紧 trail 伤右尾）|
| TP | 关闭 |
| friction gate | ≥ −5% round-trip |
| p_cut source | TRAIN gated pools, quantile(0.6) |
| model | HistGradientBoostingClassifier, max_iter=300, max_depth=4, lr=0.05, l2=1.0, min_leaf=20 |
| features (13) | ns_pre, buyers_pre, senders_pre, swaps_per_buyer, buyer_sender_ratio, early_ret, pre_vol, pre_pullback, pre_runup_from_lo, liq_log, liq_trend, liq_drawdown, heat_accel, mom_h1, mom_h2 |

### 1.6 操作参数

| 参数 | 推荐值 |
|------|--------|
| 单笔下单量 | $100–$300（容量甜区）|
| 仓位占比 | 5–10% 本金/笔 |
| 最大并发 | 3 |
| 日亏损熔断 | −20% 本金 |

---

## 二、隐藏风险清单（上线前必须逐项消掉）

| # | 风险 | 严重度 | 对策 | 状态 |
|---|------|--------|------|------|
| 1 | **Sniper 比你快** | 致命 | ingestor 延迟 < 2s；pool discovery 动态注册；Flashblocks pending 确认 | ✅ registry 627池活跃 |
| 2 | **三明治（OFA searcher）** | 高 | SEND RPC 必须 MEV-protected；禁 public mempool routing | ✅ tx_sender.py 双车道 |
| 3 | **Sequencer 可见** | 中（不可消除） | Base 架构限制；Coinbase 隐私原语尚未落地 | ⚪ 不可消除 |
| 4 | **出场砸盘滑点** | 高 | 卖出也用 SEND lane + hard_stop 5% slippage；QuoterV2 实时 re-quote | ✅ 修复 reason→slippage 映射 |
| 5 | **蜜罐** | 致命 | pretrade honeypot gate | ✅ preflight_trade.py |
| 6 | **薄池摩擦** | 致命 | pretrade friction gate ≥ −5% | ✅ preflight_trade.py |
| 7 | **监控延迟** | 高 | 5s 轮询；hard stop 触发时 gas 2× priority | ✅ main.py 已改 |
| 8 | **Signal bridge 缺失** | 阻塞 | keystone 模型 → Redis `signals` 还没接线 | ✅ bridge.py 完成+验证 |
| 9 | **静态 pool registry** | 阻塞 | 只有 10 个硬编码池；新 memecoin 池进不了 | ✅ Redis fallback + 627池活跃 |
| 10 | **MEV 保护未验证** | 高 | QuickNode dashboard 确认 MEV toggle = ON；canary round-trip | 🟡 待验证 |
| 11 | **RPC 断连** | 高 | READ/SEND 双通道 failover；超时重试；panic halt | 🟡 待加 |
| 12 | **循环卡死 / 死锁** | 致命 | xreadgroup timeout 2s；monitor try/except；signal handler | ✅ 审查通过 |
| 13 | **nonce 冲突** | 高 | 本地 nonce tracker + pending fallback | ✅ swap.py 已有 |
| 14 | **异常吞噬** | 高 | 所有 except 必须 log；不能静默跳过 | ✅ 审查通过 |
| 15 | **私钥泄漏** | 致命 | env only on VPS；never commit；.gitignore | ✅ |
| 16 | **gas 暴涨** | 中 | EXECUTOR_MAX_GAS_USD 上限 | ✅ 已有 |
| 17 | **token approve 无限** | 中 | MAX_UINT256 approve 一次性；approve 前检查 allowance | ✅ swap.py |
| 18 | **余额不足** | 中 | 进场前检查 ETH 余额 > stake + gas buffer | ✅ main.py 已加 |
| 19 | **止损滑点太紧** | 致命 | hard_stop/trailing reason 映射到正确 slippage key | ✅ 已修复 |

---

## 三、上线检查清单（按优先级）

### P0 — 阻塞项（不做不能上真钱）

- [x] 动态 pool registry：pool_discovery 发现的新池自动注册到 executor (Redis fallback + 627池活跃)
- [x] Signal bridge：keystone 模型打分 → Redis `signals` stream (bridge.py 完成+验证)
- [x] 全链路代码审查：修复 4 个关键 bug (slippage映射/余额检查/approval等待/urgency分发)
- [ ] MEV 验证：用 SEND RPC 发一笔 $5 canary round-trip，确认 tx 不出现在 public mempool
- [ ] Paper-forward 48h：零资金跑完整链路（检测→打分→摩擦门→模拟成交→出场跟踪）

### P1 — 强烈建议

- [ ] Blink 备用 SEND RPC（QuickNode 挂了时的 failover）
- [ ] Telegram 告警（BUY/SELL/止损/蜜罐拒绝/RPC 错误/panic）
- [ ] 日亏损熔断接线（EXECUTOR_DAILY_LOSS_PCT 已有，需在 _handle_signal 里实时检查）
- [ ] 余额检查门（BUY 前检查 ETH balance > stake + gas_buffer）

### P2 — 优化

- [ ] Flashblocks `pending` tag 做 sub-second 确认反馈
- [ ] 多 wallet nonce 隔离（并发多笔时）
- [ ] 动态 slippage（按池深度调整 amountOutMinimum）

---

## 四、突发情况应急预案

| 情况 | 检测方式 | 自动响应 | 人工响应 |
|------|---------|---------|---------|
| **RPC 全挂** | 3 次连续超时 | panic halt, 停止新 BUY, Telegram 告警 | 切换备用 RPC |
| **链停/sequencer 挂** | newHead 超时 30s | 停止新 BUY; 不卖（等链恢复） | 监控 Base status page |
| **gas 暴涨** | gas > MAX_GAS_USD | 跳过 BUY; 卖出不限（止损优先） | 调 MAX_GAS_USD |
| **被夹（sandwich detected）** | BUY receipt amount_out << expected (>20% deviation) | 标记池、Telegram 告警 | 审查 SEND RPC 是否真 MEV-protected |
| **蜜罐漏网（买了卖不出）** | SELL revert 连续 3 次 | 标记为 stuck, Telegram 告警, 停止该池重试 | 手动检查合约 |
| **余额耗尽** | balance < min_reserve_eth | 停止新 BUY | 充值或缩小仓位 |
| **日亏损超限** | sum(realized_loss) > DAILY_LOSS_PCT | panic halt 当天不再 BUY | 隔天自动恢复 |
| **进程崩溃** | Docker unhealthy / exit | autoheal 自动重启 | 检查 crash log |
| **nonce 卡死** | pending tx > 60s 未确认 | 自动 cancel (self-transfer +50% fee) | 手动检查 nonce gap |
| **数据断流** | market_ticks stream 停 > 30s | pool_health 标记 DOWN | 检查 ingestor WS 连接 |

---

## 五、执行架构图

```
         新池诞生                    策略层                        执行层
  ┌───────────────┐         ┌──────────────────┐         ┌──────────────────┐
  │ Rust Ingestor │──WS──►  │ Pool Discovery   │         │                  │
  │  (≤200ms)     │         │ + ClickHouse     │         │    Executor      │
  └───────┬───────┘         └────────┬─────────┘         │                  │
          │                          │                    │  ┌────────────┐  │
          │                   feature calc                │  │ Pretrade   │  │
          │                   + ML score                  │  │ Gates      │  │
          │                          │                    │  │ ·honeypot  │  │
          │                     ┌────▼────┐               │  │ ·friction  │  │
          │                     │ Signal  │──Redis BUY──► │  │ ·model p   │  │
          │                     │ Bridge  │               │  └─────┬──────┘  │
          │                     └─────────┘               │        │pass     │
          │                                               │  ┌─────▼──────┐  │
          │                                               │  │ BUY        │  │
          │                                               │  │ SEND RPC   │──── MEV-protected
          │                                               │  └─────┬──────┘  │
          │                                               │        │         │
          │                                               │  ┌─────▼──────┐  │
          └──────────market_ticks──────────────────────►   │  │ Monitor    │  │
                                                          │  │ 5s poll    │  │
                                                          │  │ QuoterV2   │  │
                                                          │  │ mark       │  │
                                                          │  └─────┬──────┘  │
                                                          │  ┌─────▼──────┐  │
                                                          │  │ EXIT       │  │
                                                          │  │ −50% stop  │  │
                                                          │  │ 4h time    │  │
                                                          │  │ SEND RPC   │──── MEV-protected
                                                          │  └────────────┘  │
                                                          └──────────────────┘
```

---

## 六、关键文件路径

| 组件 | 路径 |
|------|------|
| 策略回测（地基） | `/opt/EATLQ/scripts/research/base_lefttail/keystone_lefttail.py` |
| 真实成本审计 | `/opt/EATLQ/scripts/research/base_lefttail/keystone_realcost.py` |
| 实盘候选回测 | `/opt/EATLQ/scripts/research/base_lefttail/keystone_live.py` |
| Walk-forward | `/opt/EATLQ/scripts/research/base_lefttail/harden_walkforward.py` |
| 出场鲁棒 | `/opt/EATLQ/scripts/research/base_lefttail/harden_exit.py` |
| 容量测试 | `/opt/EATLQ/scripts/research/base_lefttail/size_sensitivity.py` |
| Executor 主循环 | `/opt/EATLQ/services/executor/src/main.py` |
| Swap 发送 | `/opt/EATLQ/services/executor/src/swap.py` |
| TX 双车道 | `/opt/EATLQ/services/executor/src/tx_sender.py` |
| 进场门禁 | `/opt/EATLQ/services/executor/src/preflight_trade.py` |
| Pool 注册表 | `/opt/EATLQ/services/executor/src/registry.py` |
| Position 管理 | `/opt/EATLQ/services/executor/src/position.py` |
| Rust Ingestor | `/opt/EATLQ/services/ingestor/` |
| Pool Discovery | `/opt/EATLQ/services/pool_discovery/` |
| QuoterV2 库 | `/opt/EATLQ/scripts/lib/v3_quoterv2_struct.py` |
| **Signal Bridge** | `/opt/EATLQ/services/signal_bridge/bridge.py` |
| ML 模型训练 | `/opt/EATLQ/services/signal_bridge/train_model.py` |
| ML 模型文件 | `/opt/EATLQ/services/signal_bridge/_model.pkl` |
| ML 参数 | `/opt/EATLQ/services/signal_bridge/_params.json` |
| Compose 配置 | `/opt/EATLQ/infra/compose/compose.yaml` |
| Env 模板 | `/opt/EATLQ/infra/compose/.env.example` |
| **本文档** | `/opt/EATLQ/docs/keystone-go-live.md` |
