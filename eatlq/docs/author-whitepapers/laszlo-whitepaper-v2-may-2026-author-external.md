<!-- markdownlint-disable -->

# LASZLO Project Whitepaper v2.0 — 实战修订版（2026-05 作者外宣归档）

> ## 🛑 AGENT 显著提示 — 请勿误判为 EATLQ 真实现状
>
> | 维度 | 本文档 | EATLQ 项目 |
> | ---- | ------ | ---------- |
> | 项目名 | **LASZLO**（原作者 Franklin Nexus） | **EATLQ**（在 LASZLO 基础上的 Linux/Base 适配与工程团队） |
> | 文档性质 | 原作者**对外宣称**的产品/架构叙事 + 2026-05 校准补充 | 可执行契约见 `Architecture.md` + 源码 |
> | 能否 live/train | 文中「实盘验证」「闭环成立」= **原作者自述** | 以 `mvp/audits/failed_no_7d_alpha_guard.md` 等 guard 为准 |
> | Feature Store | 文中写 `feat2:{symbol}` | 仓内当前为 `feat:*`（见 `feature_store.py`），**未验证与本文一致** |
> | 风控 Redis | 文中写 `EXECUTION_DRAWDOWN_HALT` | 仓内当前主要为 `SYSTEM_PANIC`，**未验证与本文一致** |
>
> **本文件为历史证据存档，供对照原作者意图；不得用于声称 EATLQ 白皮书对齐或实现完成度。**
>
> 归档入库日期：2026-05-27  
> 来源：原作者 Crypto 平台外宣（Franklin Nexus），含 2026-05 补充段落  
> 对照基线：[`原作者白皮书 2 外宣版.txt`](../../原作者白皮书%202%20外宣版.txt)（2026-03-17 初版，**未删除、未覆盖**）

---

**Crypto • Mar 17, 2026**  
**LASZLO Project Whitepaper v2.0 — 实战修订版**  
**FN Franklin Nexus • 37 min read**

---

## 前言：从 v1 到 v2 — 战场反馈修正

v1.0 白皮书是一份战前蓝图。它描述了一个理想化的系统：Ethereum Mainnet、Flashbots 私有中继、ClickHouse 数据仓库、完整的钱包画像引擎。

v2.0 白皮书是一份战后报告。经过 6 周的实战开发与 Base L2 实盘测试，我们用真金白银验证了哪些假设成立、哪些假设需要修正，并据此重新校准了整套系统。

### 关键变化一览

| 维度 | v1.0 蓝图 | v2.0 实战 |
| ---- | --------- | --------- |
| 目标链 | Ethereum Mainnet | Base L2（OP Stack） |
| MEV 防护 | Flashbots Bundle | 精确 EIP-1559 Gas 定价（单 Sequencer，无 tip 拍卖） |
| 蜜罐检测 | 概念描述 | Quoter V2 模拟卖出（eth_call 零 Gas 验证） |
| 止盈止损 | 概念描述 | Uniswap V3 slot0() 实时报价（每 2 秒链上查询） |
| ML 模型 | 合成数据 XGBoost | 双阶段模型（v1 合成 + v2 真实标注数据自动切换） |
| 日回撤保护 | 手动 Kill Switch | 自动 10% 余额熔断（UTC 每日重置） |
| 数据管道 | newPendingTransactions | eth_subscribe("logs") + Uniswap V3 Swap ABI 解码 |
| 特征存储 | ClickHouse / Redis ZSET 设想 | feat2:{symbol} Redis Hash 时间分桶（当前实现） |

---

## 2026-05 补充：v2.0 之后的校准

v2.0 证明了 LASZLO 的工程闭环可以跑通：监听、特征计算、信号生成、执行、持仓监控、自动退出与熔断都已形成系统。

但「系统能开枪」不等于「系统已经有 Alpha」。2026-05 之后，LASZLO 的重点不再是继续堆新模块，而是回到三件更硬的事：

1. **数据可信**：每一次信号都必须能被回放、标注、复盘，而不是只看交易结果。
2. **模型克制**：真实数据不足时，宁可 Paper Mode 运行，也不要用合成数据训练出来的自信扩大仓位。
3. **风险前置**：任何策略迭代必须先通过蜜罐检测、滑点上限、日回撤熔断和退出逻辑，再谈收益。

同时，当前实现相较本文初版又做了几处技术校准：

- Feature Store 从早期叙述中的 Redis Sorted Sets，收敛为 `feat2:{symbol}` Redis Hash 时间分桶；历史 `fs:` / `feat:` 前缀不再代表当前写入路径。
- ClickHouse / Archiver 可以在实时交易阶段暂停，Dashcam CSV 与 Redis 足以支撑早期标注和回放。
- 文中的毫秒级链路分解是设计叙事，真实延迟取决于 RPC、负载、容器资源与网络状态。
- `SYSTEM_PANIC` 更适合作为人工急停；自动日回撤使用独立 halt 状态，避免把「停止新开仓」和「一键清仓」混成同一件事。

所以 v2.0 之后的 LASZLO 不需要再发布更多「宏大路线图」。下一阶段的核心是把现有系统变成一个严肃的链上研究循环：**采集 → 标注 → 重训 → 回测 → 小额验证 → 复盘**。

---

## Part 1: 愿景、核心哲学与系统边界

### 1.1 项目定义 (Executive Summary)

LASZLO 是一个机构级、闭环式、低延迟的加密资产阿尔法（Alpha）终端与自动化交易系统。

- **它不是什么**：它不是一个被动的数据看板（Dashboard），不是另一个 Etherscan 浏览器，也不是一个仅仅用来「看盘」的玩具。
- **它是什么**：它是一套武器系统。它负责在毫秒级时间内完成从「链上噪音」中提取「智能信号」，并提供从「情报分析」到「安全执行」的一站式解决方案。
- **核心目标**：构建一套能够承载 $100M AUM（资产管理规模）逻辑的单兵作战系统。

**v2.0 状态**：系统已在 Base L2 主网完成实盘验证。Rust Ingestor 实时解析 Uniswap V3 Swap 事件，Python 量化引擎生成信号，Rust Executor 执行链上交易，Position Manager 通过 slot0() 实时监控价格并自动止盈止损。

### 1.2 核心痛点与解决方案 (Problem & Solution)

| 维度 | 当前市场痛点 (Retail Status Quo) | LASZLO 解决方案 (Institutional Standard) |
| ---- | -------------------------------- | ---------------------------------------- |
| 数据质量 | 噪音极大。满屏的 USDT/USDC 转账，无法区分巨鲸抄底还是左手倒右手。 | 极度降噪。通过「黑名单机制」清洗基础路由代币（WETH/USDC），结合巨鲸 USD 阈值过滤无效数据。 |
| 时效性 | 高延迟。等待 Etherscan 刷新或 15 秒一次的轮询。 | 毫秒级推流。基于 Rust 的 WebSocket 监听与 Redis Streams 分发，实测 Tick-to-Signal 延迟 < 20ms。 |
| 决策闭环 | 割裂。在 Nansen 看数据，在 TradingView 看 K 线，去 Uniswap 下单。 | 一体化闭环。Ingestor → Strategy → Executor → Position Manager 全自动流水线。 |
| 交易执行 | 裸奔。直接广播到公共内存池，易被 MEV 机器人夹击。 | 隐身执行。Base L2 精确 Gas 定价（maxPriorityFeePerGas = 1 wei），3% 滑点硬上限。 |
| 止损管理 | 手动。买完币就忘了，涨了不卖，跌了割肉。 | 自动三重止损。Trailing Stop (5%)、Time Stop (30min)、Hard Stop (15%)，全部由链上实时价格驱动。 |

### 1.3 设计哲学 (Core Philosophy)

**信噪比 > 数据量 (Signal over Noise)**

我们不追求「全网所有数据」。我们只关心「能赚钱的数据」。

原则：如果一条数据不能辅助决策，它就不应该出现在屏幕上，甚至不应该进入数据库。

v2.0 实践：黑名单机制在 Python 策略层和 Rust Ingestor 层双重过滤。WETH、USDC 等基础路由代币在进入特征计算之前就被丢弃，节省了 80%+ 的计算资源。

**慢即是快 (Slow is Smooth, Smooth is Fast)**

- 慢在架构设计：不急着写策略，先用 Rust + Redis 把地基打牢。
- 快在交易执行：因为地基牢固，信号处理和下单速度将是极致的。

v2.0 实践：我们在 Anvil 模拟环境中花了数周校准阈值、修复 MsgPack 序列化格式、调试 Redis Consumer Group 机制，才在实盘开出第一枪。这种「慢」让我们避免了在 Mainnet 上用真金白银调试代码。

**现实主义架构 (Pragmatic Architecture)**

- 折中方案：我们不盲目追求 Google 级别的 Kubernetes 集群。
- 决策：在单机上使用 Docker Compose 编排 Rust (IO 密集) + Python (逻辑密集) + Redis (数据密集)。

v2.0 修正：ClickHouse 在实时交易阶段可以暂时禁用（OOM 风险），用 Redis 中的 `feat2:{symbol}` 时间分桶承担热特征存储，用 Dashcam CSV 承担离线分析。这是「现实主义」的典型体现——不需要的组件就关掉。

### 1.4 系统边界 (System Boundaries)

- **支持资产**：Base L2（OP Stack）为主战场，架构预留 EVM 兼容链扩展接口。
- **交易类型**：专注于 Uniswap V3 链上现货 Swap。通过 exactInputSingle 路由执行，支持 3000/10000/500 三档费率池自动发现。
- **用户规模**：单租户（Single Tenant）系统，优化为个人或小团队极致性能。

---

## Part 2: 技术架构与基础设施 (Architecture & Infrastructure)

### 2.1 架构总览：异构混合架构 (Hybrid Heterogeneous Architecture)

LASZLO 采用「Rust 外壳，Python 大脑，Redis 神经」的设计模式。

- **核心原则**：IO 密集型任务交给 Rust，计算与逻辑密集型任务交给 Python，数据流转交给 Redis。
- **部署模式**：单机垂直扩展 (Single-Node Vertical Scaling)。通过 Docker Compose 编排，利用 Docker 内部网络 (laszlo-net, subnet 172.30.0.0/16) 实现容器间高速通信。
- **内存管理**：通过 .wslconfig 限制 WSL2 为 8GB + 4GB swap，各容器设定 deploy.resources.limits.memory 硬上限，避免 OOM 全面卡死。

### 2.2 基础设施层级 (The Infrastructure Stack)

#### Layer 1: 感知层 (The Interceptor) — Rust Ingestor

角色：系统的「耳目」。实时监听 Base L2 链上所有 Uniswap V3 Swap 事件。

核心组件：`crates/ingestor/` — Rust Crate (2021 Edition)

v2.0 实现细节：

- **订阅方式**：`eth_subscribe("logs")`，过滤 Uniswap V3 Swap 事件签名 `Swap(address,address,int256,int256,uint160,uint128,int24)`。
- **ABI 解码**：自定义 `decode_int256()` 处理有符号 256 位整数（amount0, amount1）。
- **动态池发现**：PoolCache 结构体通过 RPC 调用 token0()/token1()/decimals()/symbol() 自动发现新池子信息并缓存。
- **USD 估值**：`estimate_usd_and_side()` 函数根据已知代币价格表（TokenRegistry）估算每笔 Swap 的 USD 价值、方向（BUY/SELL）和价格。
- **序列化**：`rmp_serde::to_vec_named(&tick)` 序列化为 MsgPack Map（而非 Array），确保 Python 端能以字典形式反序列化。

**数据模型 — MarketTick (14 字段)**：

```rust
struct MarketTick {
    timestamp: u64,
    block_number: u64,
    tx_hash: String,
    log_index: u64,
    pool_address: String,
    token_address: String,
    symbol: String,
    wallet_address: String,
    side: String,          // "BUY" | "SELL"
    amount_usd: f64,
    amount_raw: String,
    price: f64,
    event_type: String,    // "SWAP"
    is_whale: bool,
}
```

Base L2 代币注册表：内置 11 种 Base 链核心代币（WETH, USDC, USDbC, DAI, cbETH, AERO, DEGEN, WBTC, BRETT, TOSHI, OX）的地址、精度和近似 USD 价格。

#### Layer 2: 传输层 (The Synapse) — Redis Stack

角色：系统的「神经中枢」。承担消息总线 + 实时特征存储双重职能。

核心组件：Redis Stack (In-Memory)，限制 512MB。

**数据流定义 (Protocol Spec)**：

- **Stream 1: market_ticks** (原始行情 — MsgPack Binary)
  - Consumer Group A: archiver_group（归档，当前停用）
  - Consumer Group B: strategy_group（量化引擎）
- **Stream 2: signals** (交易信号 — JSON)
  - Consumer Group: execution_group（执行引擎）

**v2.0 新增 — Feature Store 热数据**：

- 当前实现使用 `feat2:{symbol}` Redis Hash，以时间分桶方式聚合价格、买卖方向、资金流和活跃度。
- 旧版叙述中的 `fs:price:{symbol}` / `fs:buys:{symbol}` 等 Sorted Set 前缀，属于早期设计，不再代表当前写入路径。
- 这种 Hash 分桶方案牺牲了一部分逐笔可见性，换来更低的更新成本、更简单的 TTL 管理，以及更适合单机实时策略的读写模式。

**v2.0 新增 — 系统状态**：

- `SYSTEM_PANIC` — 人工全局急停开关。
- `EXECUTION_DRAWDOWN_HALT` — 自动日回撤停买状态，只阻止新开 BUY，不等同于一键清仓。
- `daily_start_balance` / `daily_date` — 日内回撤追踪（需结合 ETH + WETH 与未平仓状态判断）。
- `positions:{token_address}` — Hash，存储持仓信息（入场 ETH 值、峰值、时间戳）

#### Layer 3: 记忆层 (The Vault) — ClickHouse + PostgreSQL

角色：系统的「大脑皮层」。

v2.0 状态：

- **ClickHouse (OLAP)**：当前暂停启用。在 Phase 4.5 实时交易阶段，为了节省 WSL2 内存（8GB 硬限制），ClickHouse 和 Archiver 服务被注释。用 `logs/dashcam.csv` 替代离线分析需求。
- **PostgreSQL (OLTP)**：运行中。存储钱包画像（wallet_profiles 表）和系统配置。当前以默认值（win_rate_30d = 0.5, avg_roi = 0.0）提供基线，待真实数据积累后更新。

优雅降级设计：Python database_api.py 对 ClickHouse 和 PostgreSQL 均做了 try...except 处理。任一组件不可用时，系统降级运行而非崩溃。

#### Layer 4: 应用层 (The Brain & Hand) — Python + Rust

- **Strategy Engine (Python)** — `services/strategy/src/main.py`：反序列化 MsgPack Tick → Feature Store 实时特征计算 → XGBoost 推理 → 融合层打分 → Kelly 仓位计算 → 发布 JSON 信号；内置 Dashcam CSV 日志器，记录所有 final_score > 0.40 的候选信号。
- **Execution Engine (Rust)** — `crates/execution/`：消费 JSON 信号 → Pre-trade 风控检查 → Uniswap V3 exactInputSingle 执行 → Position Manager 持仓追踪；支持 Paper (Anvil) / Live (Base Mainnet) 双模式切换。
- **Position Manager (Rust)** — 自动止盈止损子系统
- **Dashboard (Streamlit)** — 实时监控前端

### 2.3 数据流全景 (The Life of a Tick) — v2.0 实测

| 时刻 | 组件 | 动作 |
| ---- | ---- | ---- |
| T+0ms | 链上 | Swap 交易发生 |
| T+2ms | Alchemy | WebSocket 推送 eth_subscription 日志 |
| T+3ms | Rust Ingestor | 捕获 Log，ABI 解码 Swap 事件参数 |
| T+4ms | Rust Ingestor | PoolCache 查找/发现池子；estimate_usd_and_side() |
| T+5ms | Rust Ingestor | rmp_serde::to_vec_named 序列化为 MsgPack Map |
| T+6ms | Redis | XADD market_ticks * data {binary} |
| T+8ms | Python Strategy | XREADGROUP 读取；msgpack.unpackb 反序列化 |
| T+9ms | Python Strategy | FeatureStore.update() 更新 feat2:{symbol} Redis Hash 时间分桶 |
| T+10ms | Python Strategy | FeatureStore.compute() 计算 4 维特征向量 |
| T+12ms | Python Strategy | XGBoost predict_proba() |
| T+13ms | Python Strategy | 融合层 final_score |
| T+14ms | Python Strategy | Kelly 动态仓位 |
| T+15ms | Python Strategy | final_score > 0.52 → XADD signals |
| T+16ms | Rust Executor | XREADGROUP 读取信号 |
| T+17ms | Rust Executor | Pre-trade：余额 → 流动性 → 蜜罐 |
| T+20ms | Rust Executor | exactInputSingle 上链 |
| T+2000ms | Base L2 | Sequencer 打包确认 |
| T+2001ms | Position Manager | open_position() 写入 Redis |
| T+4000ms+ | Position Manager | 每 2 秒 slot0() 监控三重止损 |

特征向量示例：

```python
{
    "price_velocity_5m": 0.23,    # net_flow / abs_flow (range [-1, +1])
    "buy_pressure_ratio": 0.67,   # buys / (buys + sells)
    "token_volatility_5m": 0.45,  # std(abs_flows) / mean(abs_flows) (capped at 2.0)
    "activity_heat": 42.0,        # tick count in 5min window
}
```

### 2.4 基础设施清单 (Bill of Materials) — v2.0 实际部署

| 服务 | 容器名 | 内存限制 | IP | 状态 |
| ---- | ------ | -------- | -- | ---- |
| redis-stack | laszlo-redis | 512M | 172.30.0.10 | 运行中 |
| postgres | laszlo-postgres | 512M | 172.30.0.30 | 运行中 |
| ingestor | laszlo-ingestor | 256M | 172.30.0.70 | 运行中 |
| strategy_engine | laszlo-strategy | 1G | 172.30.0.40 | 运行中 |
| rust_executor | laszlo-executor | 256M | 172.30.0.50 | 运行中 |
| dashboard | laszlo-dashboard | 512M | 172.30.0.90 | 运行中 |
| anvil | laszlo-anvil | 1G | 172.30.0.60 | 运行中（Paper 模式） |
| clickhouse | laszlo-clickhouse | 4G | 172.30.0.20 | 暂停 |
| archiver | laszlo-archiver | 256M | 172.30.0.80 | 暂停 |

总内存占用：~3.3GB（峰值），在 WSL2 8GB 限制下稳定运行。

---

## Part 3: 量化引擎与信号系统 (Quant Engine & Signal System)

### 3.1 核心理念：从确定性规则到概率模型

| 阶段 | 状态 | 内容 |
| ---- | ---- | ---- |
| Phase 1: Synthetic Bootstrap | 已完成 | 合成 20,000 条训练 XGBoost v1；产出 whale_sniper_v1.json |
| Phase 2: Real-Data Calibration | 进行中 | Dashcam 真实数据 → label → retrain v2；holdout > 55% 才部署 |
| Phase 3: Adaptive Evolution | 规划中 | 每周自动标注/重训/A/B；ClickHouse + 滚动验证 |

### 3.2 钱包画像引擎 (The Wallet Profiler)

标签：Smart Money、Sniper、Diamond Hand、Paper Hand。

v2.0 临时方案：WHALE_USD_THRESH = $100,000 作为巨鲸判定简化替代。

### 3.3 特征工程工厂 (Feature Store)

v2.0 实现：`services/strategy/src/feature_store.py` — 当前采用 `feat2:{symbol}` Redis Hash 时间分桶；早期 Sorted Sets 方案已退为历史叙述。

（特征表与 3 月版相同：price_velocity_5m、buy_pressure_ratio、token_volatility_5m、activity_heat）

### 3.4 信号融合层 (The Fusion Layer)

**模式 A**：`Final_Score = 0.40×Whale + 0.30×Momentum + 0.20×Activity − 0.10×Risk`

**模式 B**：v2 模型直接输出 `P(profitable | whale, momentum, activity, risk)`

### 3.5 机器学习流水线 (The ML Loop)

1. **Data Collection**：final_score > 0.40 → logs/dashcam.csv
2. **Dataset Preparation**：`scripts/prepare_dataset.py` → dashcam_cleaned.csv
3. **Labeling**：`scripts/label_dashcam.py`（5 分钟 slot0 标注；可限制标注窗口）
4. **Training**：`scripts/retrain_model.py`（时间序列切分；验证达门槛才部署）
5. **Inference**：自动检测 v2 模型切换
6. **Monitoring**：`scripts/analyze_dashcam.py`

---

## Part 4: 交易执行与风控体系 (Execution & Risk Management)

（4.1–4.3 与 3 月版实质相同：Base Gas、Rust Sniper、Kelly Base_Capital=$25）

### 4.4 自动风控卫士 — 日回撤（5 月修订要点）

**日回撤熔断**：

- 每天 UTC 0:00 自动记录起始余额。
- 每次交易前综合比较当前 **ETH + WETH** 与日初余额；若无未平 `positions:*` 且回撤超过阈值，触发 **`EXECUTION_DRAWDOWN_HALT`**。
- 该状态**只阻止新开 BUY**，不等同于 `SYSTEM_PANIC`。人工急停仍由 `SYSTEM_PANIC` 负责。

### 4.5 系统级熔断

- **Max Daily Drawdown**：触发 `EXECUTION_DRAWDOWN_HALT`，阻止新开 BUY。
- **Kill Switch**：`SYSTEM_PANIC = true`，人工急停；与 day halt 语义分开。
- **Gas 异常熔断**：baseFee > 5 gwei 拒绝交易。

---

## Part 5: 实施路线图 (Implementation Roadmap)

### 5.1 Phase 进度

Phase 3 交付描述（5 月版）：Feature Store (**feat2:{symbol} Redis Hash**)，非 Sorted Sets。

### 5.2 Phase 6 — Alpha Hunt（5 月增补）

**开篇（新增）**：Phase 6 的目标不是「更快实盘」，而是确认 LASZLO 是否真的能从 Base L2 的真实 Swap 流中提取可重复的 Alpha。

Steps 1–4 同 3 月版。

**Step 5: 复盘门槛（新增）**

在重新扩大实盘前，必须同时满足：

- 每一笔候选信号都能追溯到原始 MarketTick、特征快照和模型版本。
- 训练集、验证集与实盘窗口必须按时间切分，避免未来函数。
- 胜率、平均盈亏比、最大连续亏损、最大日内回撤必须一起看，不能只看单一准确率。
- 若模型在小额实盘中连续触发日回撤熔断，自动降级回 Paper Mode，重新进入标注与训练循环。

> 这一步是 v2.0 之后最重要的变化：LASZLO 不再把「上线」视作终点，而把上线视作获取真实反馈的一种受控实验。

### 5.3 长期路线图

（Month 2–6 目标表与 3 月版相同；但 2026-05 补充声明「不需要再发布更多宏大路线图」——见文首补充段。）

---

## Part 6: 实战复盘与教训

（6.1–6.4 与 3 月版相同：MsgPack、特征爆炸、slot0 占位符、Base vs Mainnet）

---

## 结语 (Conclusion)

v2.0 不再是一份蓝图，而是一份经过实弹验证的作战手册。

我们在 Base L2 上完成了从「链上噪音」到「执行交易」的完整闭环。系统能够在毫秒内捕获 Uniswap V3 Swap 事件、计算实时特征、生成信号、执行交易、并自动监控止盈止损。

但我们也清醒地认识到：当前的模型还不够聪明。它基于合成数据训练，用硬编码的融合权重打分。这是 Phase 6 要解决的核心问题——用真实链上数据重训模型，让 XGBoost 自己学习什么样的信号值得开枪。

系统已经就位。弹药已经上膛。我们需要的，只是一个足够聪明的瞄准镜。

这就是 Phase 6 — Alpha Hunt 的使命。

---

## 2026-05 注记：真正的分水岭

LASZLO 的分水岭不在于能否接更多链、支持更多池子或增加更多指标，而在于能否建立一套不会自欺的研究纪律：

- 好信号必须能被复现，不只是一次交易盈利。
- 好模型必须能解释失败，不只是回测曲线好看。
- 好系统必须能在亏损时自动缩手，不靠人的情绪刹车。

如果 v1.0 的关键词是「架构野心」，v2.0 的关键词是「实战闭环」，那么 **v2.1 的关键词应该是：标注、复盘、克制**。

---

《LASZLO 技术白皮书 v2.0》— 实战修订版，2026 年 3 月 17 日（含 2026-05 作者外宣补充）。
