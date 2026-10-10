# KEYSTONE 跨市场阿尔法复现 — Agent 任务书

> **读者:** 另一台 VPS 上的 coding agent（或研究 agent）  
> **目的:** 套用 Base 上已验证的**方法论**，在 BNB / Solana 等市场**独立复现并验证**是否存在同类阿尔法  
> **不是:** 复制 Base 模型权重、参数、或在本 VPS 上跑任何任务  
> **版本:** 2026-06-20 | 来源: EATLQ KEYSTONE 研究线（Base V3 WETH）

---

## 0. 你的任务（一句话）

在新市场上实现 **「进场前可观测 → 深度门 + 左尾 ML → 真实报价成本下的凸性持有」**，用与 Base **相同诚实协议**做 walk-forward 回测；报告是否有独立 edge，**不要**声称「Base 数字会复现」。

---

## 1. 可迁移的阿尔法（必须保留）

### 1.1 核心论题（跨链成立的部分）

Memecoin 新池的期望收益是**高度凸的**（右尾大、左尾致命）。问题不是「预测涨多少」，而是：

1. **在进场前**，用仅可观测数据筛掉 **FATAL** 子集（rug / 蜜罐 / 流动性蒸发 / 进场后无成交）
2. **在进场前**，用 **真实 round-trip 报价** 筛掉摩擦过大、不可执行的池
3. 对存活集合用 **简单凸性出场**（长持有 + 硬止损），不在 tick 上过度优化

这不是 smart-money 跟单；是跟单研究已失败（holdout PF ≈ 0.08，latency 吃 edge）。

### 1.2 Base 上已验证的结构（数字仅作参照，非目标）

| 层 | Base holdout 结果 | 含义 |
|----|-------------------|------|
| 无过滤 | mean ≈ −0.2%, fatal ≈ 42% | 裸买新池 ≈ 左尾彩票 |
| 仅深度门 | ~flat | 深度 alone 不够 |
| **深度门 + ML** | mean ≈ +60%, med ≈ +30%, fatal ≈ 4%, n≈131 | **ML 在可执行子集上剪左尾** |
| Walk-forward pooled | n=217, med ≈ +37%, WR ≈ 71% | 非单月 artifact |
| fee=100 无 ML | med ≈ −23%, fatal ≈ 40% | **fee  tier 不是可部署策略** |

**你要验证的是:** 在新市场上，「深度门 + 左尾 ML」是否仍能把 fatal 从 ~40% 压到 ~10% 以下，且 **Quoter 真实成本下** holdout med > 0。

### 1.3 明确失败、不要复现的路径

| 做法 | Base 结论 | 跨链默认 |
|------|-----------|----------|
| Smart money 跟单 | 信号真但不可执行 | ❌ 不做 |
| Trailing stop 35% | med 比 hold-only 低 13pp | ❌ **默认关闭 trail** |
| 出场前 quote「PnL 从峰值回撤」 | 84/131 误杀 | ❌ 用 **摩擦恶化** 语义，非价格回撤 |
| 直接 import Base `_model.pkl` | 链/池分布不同 | ❌ **必须重训** |
| Tick 代理 P&L 不上 Quoter | 高估/低估 rug | ❌ holdout 必须 **真实 quote** |
| Kelly 全仓 | 未验证 | ❌ 固定 5–10% + cap |

---

## 2. 冻结协议（HONEST PROTOCOL — 所有市场统一）

### 2.1 宇宙（Universe）

```
新池首次 swap 时刻 = t0
Pre-entry 窗口 = [t0, t0 + W]   W = 600s（10 分钟）
Forward 窗口   = (t0 + W, t0 + W + H]   H = 14400s（4 小时）

纳入条件:
  - 原生 gas token 腿（Base: WETH, BNB: WBNB, Sol: SOL/WSOL 对）
  - 集中流动性 AMM V3 类（或链上等价：tick / sqrtPrice 可观测）
  - Pre-entry swap 数 ns_pre >= MIN_PRE (5)
  - 首段数据 ClickHouse/索引可解码（非仅 price API）
```

### 2.2 进场时刻

**Entry = t0 + W**（第 10 分钟）。特征 **仅** 用 `[0, W]`；标签 **仅** 用 forward。

### 2.3 标签 FATAL（训练 y）

```python
LN1P = log(1.0001)
STOP = log(0.5)   # -50% log return from entry tick/price

entry_lp = log_price_at_entry  # orient-adjusted, see §3.2
fwd_min_ret = min_forward_log_price - entry_lp
death = (forward_swap_count <= 2)   # 活动死亡 → 无法出场，记 -100%

FATAL = death OR (fwd_min_ret <= STOP)
```

Convex 评估时可报告 `realized` proxy，但 **gate+model 筛选** 以 FATAL 分类 AUC / 剪尾效果为主。

### 2.4 两道门（执行顺序）

```
1) ML 门:  p(fatal) <= p_cut
2) 深度门: round_trip_fric >= FRIC_THR   # 默认 FRIC_THR = -0.05 (-5%)

round_trip_fric = (quote_sell(quote_buy(NATIVE, size)) / size) - 1
  - 在 entry block/slot 上报价
  - quote 失败 / revert → fric = -1 → 拒绝进场
```

**p_cut 来源（禁止偷看 test）:**  
仅在 **TRAIN** 且 **已通过深度门** 的样本上取 `p.quantile(0.6)`，原样用于 TEST。

### 2.5 时间切分

```
TRAIN = 时间上较早 60% 的池（按 t0）
TEST  = 较新 40%

Walk-forward（推荐）:
  对每个月 M in test_months:
    train = all pools with t0 < M
    fit model + p_cut on train only
    evaluate on pools born in month M
  报告 pooled + 逐月
```

### 2.6 出场（live 对齐、回测默认）

| 规则 | 值 |
|------|-----|
| Hard stop | −50% from entry（tick/path） |
| Trailing | **OFF**（1.0 = disabled） |
| Time stop | ~4h（Base: 7200 blocks @ ~2s） |
| Take profit | OFF |

Exit block 应用 **forward tick path 因果 walk**；在该 block 做 **真实 sell quote** 得 net P&L。

### 2.7 成本（报告 net 必须含）

```
NOTIONAL = $100（默认，便于跨链对比）
net = sell_quote / buy_notional - 1
net_after_gas = net - (gas_usd_rt / NOTIONAL)   # Base ~$0.15/rt
```

**禁止**只用 tick 收益当最终结论。

### 2.8 模型（每链重训，超参可冻结）

```python
HistGradientBoostingClassifier(
    max_iter=300, max_depth=4, learning_rate=0.05,
    l2_regularization=1.0, min_samples_leaf=20, random_state=0,
)
```

---

## 3. 特征工程（15 维 — 语义跨链，实现需适配）

### 3.1 原始列（从 swap 流聚合）

| 列 | 定义 |
|----|------|
| ns_pre | [0,W] swap 数 |
| buyers_pre | unique recipient/buyer |
| senders_pre | unique sender |
| first_tick, last_tick_pre, min_tick_pre, max_tick_pre | pre-entry tick 极值 |
| liq_first, liq_last_pre, liq_min_pre | 池 liquidity 序列 |
| ns_h1, ns_h2 | [0,W/2], (W/2,W] swap 数 |
| last_tick_h1 | W/2 时刻 tick |
| orient | +1 if alt is token1 else -1（相对 native leg） |

### 3.2 派生特征（与 Base 相同公式）

```python
o = orient
lp = lambda tick: tick * LN1P * o

early_ret      = lp(last_tick_pre) - lp(first_tick)
pre_vol        = lp(max_tick) - lp(min_tick)   # hi-lo
pre_pullback   = lp(max_tick) - lp(last_tick_pre)
pre_runup_from_lo = lp(last_tick_pre) - lp(min_tick)
liq_log        = log1p(liq_last_pre)
liq_trend      = liq_last_pre / liq_first
liq_drawdown   = liq_min_pre / liq_first
heat_accel     = ns_h2 / max(ns_h1, 1)
mom_h1         = lp(last_tick_h1) - lp(first_tick)
mom_h2         = lp(last_tick_pre) - lp(last_tick_h1)
swaps_per_buyer    = ns_pre / max(buyers_pre, 1)
buyer_sender_ratio = buyers_pre / max(senders_pre, 1)
```

### 3.3 Solana 适配说明

- 无 Uniswap tick 时：用 **sqrtPriceX64** 或 mid-price 对数收益，替换 `tick * LN1P`
- `recipient/sender` → 用 **signer / user authority**（按你的 decode schema）
- `liquidity` → CLMM `liquidity` 或 pool reserve 代理；缺失则 `liq_*` 填 0 并记录数据质量

---

## 4. 评估清单（每个市场必须输出）

### 4.1 主表（TEST / walk-forward pooled）

| 指标 | 说明 |
|------|------|
| n | 通过 gate+model 的池/笔数 |
| mean, med | Quoter net（含 gas 更好） |
| WR | net > 0 |
| fatal rate | net <= −50% 或 quote_fail |
| best, top1share | 是否单币驱动（top1share < 0.15 更健康） |
| ~trades/mo | 按 TEST 时间跨度年化 |

### 4.2 必做对照

1. **ALL** — 无 ML 无深度  
2. **gate-only** — 仅深度门  
3. **gate+model** — 完整 KEYSTONE  
4. **unfiltered ML** — 仅 ML 无深度（证明深度必要性）

### 4.3 必做稳健性

1. **Walk-forward 逐月**（至少 3 个 test 月）  
2. **Size sensitivity** — $100 / $300 / $1000 重算深度门（预期：大单 pass rate 骤降）  
3. **Exit robustness** — hold_4h vs trail35（预期：hold ≥ trail）

### 4.4 Go / No-Go（建议门槛，可调但需说明）

| 条件 | Go |
|------|-----|
| holdout n | ≥ 60 |
| gate+model med | > 0 |
| gate+model fatal | < 15% |
| vs gate-only | med 或 fatal 至少一项显著改善 |
| walk-forward | ≥ 2/3 月 med > 0 |
| top1share | < 0.20 |

**No-Go:** 任一关键月 fatal > 30% 且 med < 0 → 该市场无独立 edge，不并仓。

---

## 5. 分链实现指南

### 5.1 BNB Chain (BSC) — 优先级 **P1**

**相似度:** 高（EVM + V3 类 + WBNB）

| Base | BNB |
|------|-----|
| chain_id 8453 | **56** |
| WETH `0x4200…0006` | WBNB `0xBB4C…095c` |
| Uniswap V3 | **PancakeSwap V3**（确认 factory / pool init 事件） |
| QuoterV2 | Pancake **V3 Quoter**（查官方部署地址） |
| 区块 ~2s | ~3s |
| 4h blocks | ~4800 blocks |

**数据管道（在新 VPS 上独立建，不要连 Base 生产 CH）:**

1. 索引 Pancake V3 `Swap` 事件 → 解码 tick, liquidity, amount, recipient  
2. 索引 `PoolCreated` → discovered_pools 表  
3. 按 §2 SQL 逻辑聚合（改 chain_id + native token + 表名）

**引用实现（只读照抄逻辑，不照抄地址）:**  
`scripts/research/base_lefttail/keystone_lefttail.py`  
`scripts/research/base_lefttail/keystone_live.py`  
`scripts/lib/v3_quoterv2_struct.py`

### 5.2 Solana — 优先级 **P2**（独立难度）

**相似度:** 低 — 视为 **新方法论文**，只保留 §1 论题与 §2 协议精神。

| 维度 | 做法 |
|------|------|
| AMM | Raydium CLMM / Orca Whirlpool / Meteora（选一个主 DEX 先） |
| 历史 swap | Geyser 归档 / Helius enhanced tx / 自建 indexer |
| Entry quote | **Jupiter Quote API** 或 DEX quoter；历史 slot 报价是难点 |
| t0 | pool 第一笔 swap 的 blockTime |
| MEV | Jito bundle 等 — live 阶段再议；回测先扣固定 slippage 敏感性 |

**最小可行验证（Phase Sol-0）:**

1. 能否构建 ≥500 池 × 600s pre-entry 特征（与 §3 语义对齐）  
2. FATAL base rate 是否也在 ~30–50%（否则标签无意义）  
3. ML AUC on holdout ≥ 0.60（keystone bar 0.65 为 stretch）  
4. 若有历史 quote：gate+model med > 0；若无：仅报告 **tick-proxy + 明确 disclaimer**，不得写「可 live」

---

## 6. 推荐仓库布局（新 VPS）

```
research/
  keystone/
    common/
      features.py      # §3 公式，接受 DataFrame
      labels.py        # FATAL / death
      protocol.py      # train/test split, p_cut, walk-forward
      metrics.py       # stat(), walk-forward driver
    chains/
      base/            # 可选：对照复现
      bnb/
        ingest/        # 独立 indexer 或 batch export
        sql/           # pool aggregation
        quoter.py      # WBNB round-trip @ block
        run_backtest.py
      solana/
        ingest/
        features_sol.py
        quote_jupiter.py
        run_backtest.py
    audits/
      bnb_holdout.json
      sol_holdout.json
      CROSS_MARKET_REPORT.md
```

**磁盘预算建议:** 单链 raw+aggregates < 5GB；Solana 全量 tx 更大，用 **池级 parquet** 而非 raw chain。

---

## 7. 执行流水线（Agent 步骤）

### Step 1 — 数据审计（1–2 天）

- [ ] 统计：日期范围内新 native-leg V3 池数量 / 月  
- [ ] 每池 pre-entry swap 可解码率  
- [ ] FATAL base rate（无 ML）  
- **退出条件:** 月增池 < 20 或 FATAL base < 10% → 市场可能不适合此策略

### Step 2 — 特征 + 标签表（`_pools.tsv` 等价物）

- [ ] 导出 §3.1 列 + t0 + entry_block/slot  
- [ ] 因果检查：无 forward 列进入特征

### Step 3 — ML + 深度门回测

- [ ] TRAIN/TEST + walk-forward  
- [ ] 缓存 entry block 的 round-trip quote（JSON，类似 `_priced_live.json`）  
- [ ] 缓存 forward sell curve（可选，用于出场研究）

### Step 4 — 报告

- [ ] 填 §4 全部表格  
- [ ] 与 Base 数字对比时写清：**机制是否同向**，不是 **幅度是否相同**  
- [ ] 容量：$100/$300/$1000  
- [ ] 结论：Go live research / Need more data / No edge

### Step 5 — （仅 Go 后）Live 设计

- 独立 executor、独立 RPC、独立资金  
- Paper 48h → 小 notional live  
- **不要**与 Base 共用一个钱包 nonce/Redis stream

---

## 8. Base 参考文件清单（从 EATLQ 只读复制）

| 文件 | 用途 |
|------|------|
| `scripts/research/base_lefttail/keystone_lefttail.py` | SQL + 特征 + FATAL 标签 |
| `scripts/research/base_lefttail/keystone_live.py` | 深度门 + exit + Quoter 定价 |
| `scripts/research/base_lefttail/keystone_realcost.py` | 真实成本叙事 |
| `scripts/research/base_lefttail/harden_walkforward.py` | Walk-forward 模板 |
| `scripts/research/base_lefttail/harden_exit.py` | 出场对比 |
| `scripts/research/base_lefttail/size_sensitivity.py` | 容量曲线 |
| `scripts/research/base_lefttail/keystone_full_validation.py` | 全指标 |
| `scripts/lib/v3_quoterv2_struct.py` | EVM quoter 调用 |
| `services/signal_bridge/train_model.py` | 特征列 + 训练流程 |
| `docs/keystone-go-live.md` | Base 固化参数与风险清单 |

**Base 产物数据（可选拷贝做对照，非必须）:**

- `_pools.tsv`, `_priced_live.json`, `_sellcurve.json` — 1275 池研究集

---

## 9. 资源与安全（新 VPS）

- 本任务 **不在 Base 生产 VPS 执行**  
- ClickHouse/RPC 与 Base live **物理隔离**  
- 批量 quote：限速 ≥ 20ms/call，避免 RPC ban  
- 大 SQL：单池或小区块 batch，`max_memory_usage` 上限  
- 回测阶段 **零 live 交易**

---

## 10. 交付物模板（交给人类决策者）

```markdown
# {CHAIN} KEYSTONE 验证报告

## 数据
- 窗口: {date_lo} – {date_hi}
- 池数: {n_all}, TEST gate+model: {n_test}

## 结果 (walk-forward pooled)
| 层 | n | mean | med | WR | fatal |
|----|---|------|-----|-----|-------|
| ALL | | | | | |
| gate-only | | | | | |
| gate+model | | | | | |

## 与 Base 机制对比
- FATAL 剪尾是否有效: {yes/no + 数字}
- 深度门是否必要: {yes/no}
- 容量 $300 vs $100: {pass rate delta}

## 结论
- [ ] 独立阿尔法 Go
- [ ] 继续研究
- [ ] No edge

## 限制
- {quote 历史缺口 / MEV 未建模 / ...}
```

---

## 11. FAQ（Agent 常犯错误）

**Q: Base med +30%，BNB 也要做到 +30%？**  
A: 否。验证 **sign + 剪尾 + 可执行**；幅度可以更低。

**Q: 能否用 DexScreener OHLC 代替 swap 流？**  
A: 不够。缺少 buyers/senders/liq，且无法做 block 级 Quoter gate。

**Q: 多链能否共用 p_cut=0.27？**  
A: 否。每链 TRAIN 重算。

**Q: Solana 先上 live 快钱？**  
A: 否。先 Sol-0 数据审计；无 quote 不得 live。

**Q: 多链能提高 $2K 单笔上限吗？**  
A: **不能**。仅可能提高 **并发机会**；单笔仍受池深限制（见 size_sensitivity）。

---

## 12. 给人类（刘羽）的交接说明

把本文 + `docs/keystone-go-live.md` 发给新 VPS agent。指令示例：

> 「按 `docs/KEYSTONE_CROSS_MARKET_AGENT_BRIEF.md` 在 BNB 上完整走 Step 1–4，Solana 只做 Step 1–2 + Sol-0；不要连 Base 生产；完成后填 §10 模板。」

**Base live 不受此任务影响；新市场 Go 之前不得并仓。**

---

*End of agent brief*
