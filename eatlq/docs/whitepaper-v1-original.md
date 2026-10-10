<!-- markdownlint-disable -->

# EATLQ Whitepaper v1 Original

> This file preserves the original whitepaper content provided by the user.
> It is kept as an archival reference and should not be treated as the sole source of truth for current implementation details.
> Current engineering reality is documented separately in `docs/whitepaper-v2-current.md`.

# EATLQ

## Part 1: 愿景、核心哲学与系统边界

### 1.1 项目定义 (Executive Summary)

EATLQ是一个**机构级、闭环式、低延迟**的加密资产阿尔法（Alpha）终端与自动化交易系统。

- **它不是什么**：它不是一个被动的数据看板（Dashboard），不是另一个 Etherscan 浏览器，也不是一个仅仅用来“看盘”的玩具。
- **它是什么**：它是一套**武器系统**。它负责在毫秒级时间内完成从“链上噪音”中提取“智能信号”，并提供从“情报分析”到“安全执行”的一站式解决方案。
- **最终核心目标**：构建一套能够承载 **$100M AUM**（资产管理规模）逻辑的单兵作战系统。

### 1.2 核心痛点与解决方案 (Problem & Solution)

| **维度** | **当前市场痛点 (Retail Status Quo)** | EATLQ**解决方案 (Institutional Standard)** |
| --- | --- | --- |
| **数据质量** | 噪音极大。满屏的 USDT/USDC 转账，无法区分巨鲸抄底还是左手倒右手。 | 极度降噪。通过“黑名单机制”清洗 99% 的无效数据，结合“实体聚类”识别真实资金流向。 |
| **时效性** | 高延迟。等待 Etherscan 刷新或 15秒一次的轮询。 | 毫秒级推流。基于 Rust 的 WebSocket 监听与 Redis Streams 分发，实现 Tick-to-Trade 的极低延迟。 |
| **决策闭环** | 割裂。在 Nansen 看数据，在 TradingView 看 K 线，去 Uniswap 下单。切换窗口时机会已流失。 | 一体化闭环。左侧战术地图（信号），右侧狙击流（执行），侧边栏风控（账本）。上下文自动联动。 |
| **交易执行** | 裸奔。直接广播到公共内存池（Mempool），易被 MEV 机器人夹击（三明治攻击）。 | 隐身执行。集成 Flashbots 私有交易通道，确保交易不被抢跑，失败不上链。 |

### 1.3 设计哲学 (Core Philosophy)

为了在有限的资源下达成机构级目标，我们遵循以下三条哲学：

1. **信噪比 > 数据量 (Signal over Noise)**
   1. 我们不追求“全网所有数据”。我们只关心“能赚钱的数据”。
   2. *原则*：如果一条数据不能辅助决策，它就不应该出现在屏幕上，甚至不应该进入数据库。
2. **慢即是快 (Slow is Smooth, Smooth is Fast)**
   1. **慢**在架构设计：不急着写策略，先用 Rust + ClickHouse 把地基打牢。
   2. **快**在交易执行：因为地基牢固，信号处理和下单速度将是极致的。
3. **现实主义架构 (Pragmatic Architecture)**
   1. **折中方案**：我们不盲目追求 Google 级别的 Kubernetes 集群。
   2. *决策*：在单机（High-Spec Server）上使用 **Docker Compose** 编排 **Rust (IO密集)** + **Python (逻辑密集)** + **ClickHouse (数据密集)**。这是单兵作战效能最高的组合。

### 1.4 系统边界 (System Boundaries)

为了防止项目无限膨胀，我们明确EATLQ 的**能力边界**：

- **支持资产**：初期专注于 **EVM 兼容链**（以 Ethereum Mainnet 为主，后续扩展 Base/Arbitrum）。暂不涉及 Solana/Bitcoin（架构预留接口，但暂不实现）。
- **交易类型**：专注于 **DEX 链上现货交易**（Uniswap V2/V3）。暂不涉及 CEX 合约、借贷协议或复杂的跨链套利。
- **用户规模**：系统设计为**单租户（Single Tenant）或小团队**使用，而非面向大众的 SaaS 服务。这允许我们为了性能牺牲并发隔离性。

## Part 2: 技术架构与基础设施 (Architecture & Infrastructure)

### 2.1 架构总览：异构混合架构 (Hybrid Heterogeneous Architecture)

为了兼顾**极低延迟**（Rust）和**快速迭代**（Python），EATLQ采用**“Rust 外壳，Python 大脑，Redis 神经”**的设计模式。

- **核心原则**：IO 密集型任务交给 Rust，计算与逻辑密集型任务交给 Python，数据流转交给 Redis。
- **部署模式**：**单机垂直扩展 (Single-Node Vertical Scaling)**。
    - 放弃 K8s，在高性能服务器上通过 **Docker Compose** 编排。利用 Docker 内部网络 (`laszlo-net`) 实现容器间高速通信。

### 2.2 基础设施层级 (The Infrastructure Stack)

我们将系统划分为四个物理层级：

### Layer 1: 感知层 (The Interceptor) - `Rust`

- **角色**：系统的“耳目”。负责监听链上一切动静。
- **核心组件**：`Ingestor Service` (Rust Crate)。
- **技术实施标准**：
    - **语言**：Rust (2021 Edition)。
    - **连接**：使用 `tokio-tungstenite` 建立 WebSocket 长连接。
    - **数据源**：严格依赖 `.env` 配置 `ALCHEMY_WS_URL`（Alchemy 优于 Infura，因其对 Pending Transactions 支持更好）。
    - **序列化 (关键决策)**：放弃复杂的 Protobuf，采用 **MsgPack** (通过 `rmp-serde` crate)。
        - *理由*：MsgPack 是二进制的 JSON，既保持了 Schema-less 的灵活性（无需维护 `.proto` 文件），又提供了接近 Protobuf 的压缩率和解析速度。
- **职责**：
    - 订阅 `eth_subscribe` ("logs" 或 "newPendingTransactions")。
    - 解析 JSON-RPC Payload，转换为 Rust Struct。
    - 使用 MsgPack 序列化为二进制 `Vec<u8>`。
    - 推送到 Layer 2。

### Layer 2: 传输层 (The Synapse) - `Redis Stack`

- **角色**：系统的“神经中枢”。负责解耦 IO 与计算。
- **核心组件**：Redis Stack (In-Memory)。
- **数据流定义 (Protocol Spec)**：
    - **Stream 1: `market_ticks`** (原始行情 - MsgPack Binary)
        - Consumer Group A: `archiver_group` (归档)
        - Consumer Group B: `strategy_group` (量化)
    - **Stream 2: `signals`** (交易信号 - JSON)
        - Consumer Group: `execution_group` (执行)
- **职责**：充当高吞吐消息总线，Redis 负责内存缓冲，防止 Rust 发送过快冲垮 Python 消费者。

### Layer 3: 记忆层 (The Vault) - `ClickHouse` + `Postgres`

- **角色**：系统的“大脑皮层”。
- **核心组件 A：ClickHouse (OLAP - 历史数据)**
    - **用途**：存储 Trades, Logs。
    - **ETL 策略 (双重触发)**：`Archiver Service` (Rust) 采用 **Batch Size + Time Timeout** 策略。
        - **规则**：缓冲区每积攒 **1000 条** 数据，或者距离上次写入超过 **1 秒**，立即执行一次 `INSERT`。
        - *目的*：高频时段减少 IO 次数（合并写），低频时段保证实时性（低延迟）。
    - **加速**：使用 Materialized Views 预计算每小时聚合数据。
- **核心组件 B：PostgreSQL (OLTP - 业务状态)**
    - **用途**：存储 User Profile, API Keys, Order History。
    - **优势**：利用 ACID 事务特性，确保策略配置和资金记录零误差。

### Layer 4: 应用层 (The Brain & Face) - `Python` + `Streamlit`

- **角色**：系统的“意识与面孔”。
- **核心组件**：
    - **Quant Engine (Python)**：
        - 反序列化 MsgPack 数据。
        - 特征提取 -> XGBoost 推理 -> 生成信号。
    - **Frontend (Streamlit)**：
        - 通过 WebSocket 直连 Redis 读取实时数据，直连 Postgres 读取用户配置。

### 2.3 数据流全景 (The Life of a Tick)

模拟**“一笔 Uniswap 交易”**的生命周期：

1. **T+0ms**: 链上发生交易。
2. **T+5ms (Rust)**: Ingestor 捕获 Log。
3. **T+6ms (Rust)**: 数据被 `rmp-serde` 压缩为二进制 MsgPack。
4. **T+8ms (Redis)**: 二进制块写入 `market_ticks` Stream。
5. **T+10ms (Parallel Processing)**:
   1. *Path A (Archiver)*: 进入 Rust 内存 Buffer。如果 Buffer < 1000 且 Time < 1s，暂存内存；否则触发 ClickHouse 写入。
   2. *Path B (Strategy)*: Python Engine 读取并解压 MsgPack。
6. **T+15ms (Inference)**: 模型打分。
7. **T+20ms (Execution)**: 信号触发。

### 2.4 基础设施清单 (Bill of Materials)

Docker Compose 服务列表（已更新配置细节）：

1. `redis-stack`: 开启 AOF 持久化。
2. `clickhouse-server`: 调优 `max_insert_block_size` 适配批量写入。
3. `postgres`: 生产级配置。
4. `ingestor` (Rust): 挂载 `.env`，连接 Alchemy。
5. `archiver` (Rust): 配置 `BATCH_SIZE=1000`, `BATCH_TIMEOUT=1s`。
6. `brain` (Python): 量化引擎。
7. `dashboard` (Streamlit): 前端。

## Part 3: 量化引擎与信号系统 (Quant Engine & Signal System)

### 3.1 核心理念：从确定性规则到概率模型 (From Heuristics to Probabilities)

EATLQ的智能进化分为两个阶段，这决定了我们的开发路径：

- **Phase 1: 启发式阶段 (Heuristic Era)** —— *现状*
    - 逻辑：基于专家经验的硬规则。
    - *例子*：`IF (Buy_Volume > $50k) AND (Wallet_Age > 1yr) THEN BUY`。
    - 优点：开发快，逻辑透明，易于调试。
    - 缺点：容易被针对，无法捕捉非线性关系，适应性差。
- **Phase 2: 随机模型阶段 (Stochastic Era)** —— *目标*
    - 逻辑：基于历史数据的概率预测。
    - *例子*：`Model_Score(Features) -> 0.87 (Strong Buy)`。
    - 优点：能发现人类忽视的规律（Alpha），自适应市场变化。
    - 技术栈：**XGBoost** (处理表格数据胜率极高) + **LightGBM**。

### 3.2 智能模块一：钱包画像引擎 (The Wallet Profiler)

这是 EATLQ 最核心的护城河。我们不仅仅看“谁在买”，我们要看“这个人的水平如何”。

- **实体聚类 (Entity Clustering)**：
    - *问题*：巨鲸通常使用几十个新钱包分批建仓。
    - *算法*：**Common Input Heuristic (CIH)**。如果地址 A 和地址 B 在同一笔交易中作为输入，它们属于同一个人。
    - *产出*：`Cluster_ID`。我们将监控的是“Cluster”而不是单一 Address。
- **标签体系 (Labeling Taxonomy)**：
    - **Smart Money (聪明钱)**：历史上在 Token 涨幅前 10% 阶段买入，跌幅前 10% 阶段卖出的地址。
    - **Sniper (狙击手)**：在开盘 0-3 区块内买入且未被 Rug 的地址。
    - **Diamond Hand (钻石手)**：平均持仓时间 > 30 天，且无视 50% 回撤的地址。
    - **Paper Hand (纸手)**：一涨 10% 就跑，或一跌就割肉的地址（反向指标）。

### 3.3 智能模块二：特征工程工厂 (Feature Store)

为了训练模型，我们需要将原始数据转化为“特征”。这些特征将存储在 Redis (热) 和 ClickHouse (冷) 中。

| **特征维度** | **具体指标 (Metrics)** | **逻辑解释 (Why)** |
| --- | --- | --- |
| **Alpha 特征** | win_rate_30d (30天胜率) | 只有赢家才值得跟随。 |
|  | avg_roi (平均回报率) | 排除那些只赚 1% 就跑的高频机器人。 |
|  | pnl_z_score (盈亏标准分) | 这个人现在的表现是否异常好？ |
| **动量特征** | price_velocity_5m (5分钟价格加速度) | 价格涨得有多急？ |
|  | buy_pressure_ratio (主动买入/总成交) | 多头是否占据绝对主导？ |
| **宏观特征** | gas_gwei_trend (Gas 趋势) | Gas 飙升通常意味着热点爆发或崩盘前夕。 |
|  | cex_net_flow (交易所净流向) | 提币意味着吸筹，充币意味着砸盘。 |

### 3.4 信号融合层 (The Fusion Layer)

单一信号往往是片面的。EATLQ 采用**加权投票机制 (Weighted Ensemble)** 来生成最终决策。

$$Final\_Score = (w_1 \cdot Whale\_Score) + (w_2 \cdot Momentum) + (w_3 \cdot Sentiment) - (w_4 \cdot Risk)$$

- **Whale_Score (40%)**: 来自钱包画像模型。巨鲸都在买，权重最高。
- **Momentum (30%)**: 来自价格动量。趋势是朋友。
- **Sentiment (20%)**: 来自社交/交易热度。是否有人气？
- **Risk (10%)**: 负分项。池子是否过小？是否未开源？是否有貔貅风险？

### 3.5 机器学习流水线 (The ML Loop)

这是一个自动化的闭环，确保模型越用越聪明：

1. **Data Collection (采集)**：Rust 监听器将每一笔交易的 `Feature Vector` (特征向量) 和随后的 `Target` (例如未来 1 小时的涨跌幅) 记录到 ClickHouse。
2. **Training (训练)**：每周日凌晨，Python 脚本从 ClickHouse 提取过去 3 个月的数据，重新训练 XGBoost 模型。
3. **Validation (验证)**：使用最近 1 周的数据进行回测。如果新模型胜率 > 旧模型，自动替换。
4. **Inference (推理)**：实时交易时，加载最新的 `.model` 文件，对每一笔实时 Tick 进行打分。

## Part 4: 交易执行与风控体系 (Execution & Risk Management)

### 4.1 核心理念：隐身与原子性 (Stealth & Atomicity)

机构级执行与散户执行的最大区别在于：**散户在明，机构在暗**。

- **Public Mempool (公共内存池)**：这是屠宰场。任何广播到这里的普通交易，都会被 MEV 机器人（Searchers）监控。如果你的滑点设置不当，会被毫秒级“三明治攻击”（Sandwich Attack）。
- **Private Relay (私有中继)**：这是 LASZLO 的通道。我们不走常规广播，而是通过 Flashbots 将交易打包，直接发送给矿工（Block Builders）。
    - *特性*：**要么成功上链，要么完全失败（不上链，不耗 Gas）。** 绝不会出现“交易失败但扣了 Gas”或者“买入价格比预期高 10%”的情况。

### 4.2 执行架构：Rust 狙击手 (The Rust Sniper)

虽然 Python 负责思考（生成信号），但**扣动扳机必须由 Rust 完成**。

- **组件**：`Execution Engine` (Rust Crate)。
- **工作流**：
    - **监听**：订阅 Redis `signal_stream` 中的高置信度信号。
    - **构建 (Build)**：根据信号生成 Unsigned Transaction (未签名交易)。
    - **模拟 (Simulate)**：在本地 Fork 的节点（如 Anvil/Hardhat）或 Trace API 上模拟执行。
      1. *检查点*：买入后能否卖出？（貔貅检测）、税率是否 > 5%？、滑点是否 > 3%？
    - **签名与捆绑 (Sign & Bundle)**：如果模拟通过，使用私钥签名，并封装为 Flashbots Bundle。
    - **发射 (Fire)**：发送给头部 Relay (Flashbots, BeaverBuild, Titan)。

### 4.3 动态仓位管理 (Dynamic Position Sizing)

我们摒弃“每单固定买 1000U”的散户思维，采用 **Kelly Criterion (凯利公式)** 的变体进行动态下注。

$$Position\_Size = Base\_Capital \times (Confidence\_Score \times Volatility\_Adjustment)$$

- **Confidence_Score (置信度)**：来自 Part 3 的 XGBoost 模型输出（0.0 - 1.0）。
    - *Score > 0.9*：重仓出击（例如 $20,000）。
    - *Score > 0.7*：标准仓位（例如 $5,000）。
    - *Score < 0.6*：观望（$0）。
- **Volatility_Adjustment (波动率调节)**：如果该 Token 波动率极大（风险高），自动降低仓位系数。

### 4.4 自动风控卫士 (The Risk Guardian)

风控不是事后诸葛亮，而是**事前阻断**和**事中熔断**。

### Pre-Trade Checks (事前阻断)

在 Rust 引擎发出交易前的毫秒级时间内，必须通过以下检查：

1. **Honeypot Check (貔貅检测)**：模拟交易必须能成功卖出。
2. **Liquidity Depth (流动性深度)**：池子深度必须 > 交易额的 20倍（确保滑点 < 5%）。
3. **Spread Check (价差检查)**：当前买一价与卖一价的差值不能过大。

### Post-Trade Management (事中管理)

一旦买入成功，系统立即生成一个**影子挂单 (Shadow Order)** 监控离场条件：

1. **Trailing Stop (移动止盈)**：价格每上涨 10%，止损线自动上移。回撤 5% 立即触发卖出。
2. **Time Stop (时间止损)**：如果买入后 30 分钟价格横盘（动量消失），强制平仓换车，不恋战。
3. **Hard Stop (硬止损)**：任何时候亏损达到 15%，无条件市价全平。

### 4.5 系统级熔断 (System Circuit Breakers)

为了防止代码 Bug 或市场黑天鹅导致账户归零，我们设置物理层面的熔断机制：

- **Max Daily Drawdown (日内最大回撤)**：如果当日累计亏损超过总资金的 10%，系统自动**停止所有买入权限**，仅允许卖出。
- **Kill Switch (一键清仓)**：Redis 中设有一个特殊的 Key `SYSTEM_PANIC`。一旦置为 `TRUE`（可以通过前端按钮或脚本触发），Rust 引擎会立即优先处理所有持仓的卖出请求，并拒绝一切新信号。

这是 **《EATLQ 技术白皮书》** 的最后一部分。

一张蓝图绘到底。这一章我们将把前四章的宏大架构拆解为**可落地的 8 周作战计划**。我们将遵循“先基建、后数据、再策略、最后交易”的顺序，确保每一步都踩在坚实的台阶上。

---

# EATLQ Project Whitepaper v1.0

## Part 5: 实施路线图 (Implementation Roadmap)

### 5.1 总体规划 (The Master Schedule)

我们将整个转型过程划分为四个阶段，每个阶段约 2 周。

| **阶段** | **代号** | **核心目标** | **交付物** |
| --- | --- | --- | --- |
| **Phase 1** | Foundation (基石) | 部署 Docker 异构环境，迁移历史数据。 | 运行中的 ClickHouse/Redis 容器，清洗后的历史数据表。 |
| **Phase 2** | The Eye (感知) | 替换 Python 监听器为 Rust，打通实时数据流。 | Rust Ingestor, Redis Streams 可视化, Dashboard v2 (读 Redis)。 |
| **Phase 3** | The Brain (大脑) | 实装钱包画像与 ML 模型，生成 Alpha 信号。 | Wallet Profiler 服务, XGBoost 训练管道, 信号流。 |
| **Phase 4** | The Hand (执行) | 接入 Flashbots，实装自动交易与风控。 | Rust Execution Engine, 自动交易开关, 模拟/实盘切换。 |

### 5.2 详细拆解 (Detailed Sprint Plan)

### Phase 1: 基础设施重构 (Weeks 1-2)

- **目标**：彻底告别 SQLite 和本地脚本运行模式。
- **任务清单**：
    - **Docker 环境搭建**：编写 `docker-compose.yml`，编排 Redis Stack, ClickHouse, Postgres。
    - **Schema 设计**：在 ClickHouse 中建立 `trades` (MergeTree) 和 `orderbook` 表；在 Postgres 中建立 `users` 和 `strategies` 表。
    - **数据迁移**：编写 Python 脚本 (`migrator.py`)，将旧 SQLite 中的数据清洗后 bulk insert 到 ClickHouse。
    - **接口封装**：编写 `database_api` (Python)，统一所有上层应用对 ClickHouse/Redis 的读写调用。

### Phase 2: 实时数据管道 (Weeks 3-4)

- **目标**：实现毫秒级数据吞吐，前端不卡顿。
- **任务清单**：
    - **Rust Ingestor 开发**：初始化 Rust 项目，使用 `tungstenite` 连接 Alchemy WebSocket，解析 Log 并序列化。
    - **Redis 管道打通**：Rust 将数据推送到 Redis Stream `market_ticks`；编写 Python 测试脚本验证消费。
    - **Archiver 服务**：编写简单的 Go/Rust 服务，从 Redis 搬运数据到 ClickHouse（解耦写入压力）。
    - **Dashboard 重构**：修改 Streamlit 前端，不再轮询 DB，而是通过 WebSocket 订阅后端转发的 Redis 消息，实现“真·实时”刷新。

### Phase 3: 量化与智能层 (Weeks 5-6)

- **目标**：让系统能够识别“谁是巨鲸”并计算“胜率”。
- **任务清单**：
    - **钱包画像计算**：编写离线脚本，跑全量 ClickHouse 数据，计算每个地址的 Win Rate 和 PnL，生成 `wallet_tags` 表。
    - **特征工程管道**：建立 Feature Store，实时计算 `price_momentum`, `whale_flow_ratio` 等因子。
    - **模型训练**：导出训练集，训练第一版 XGBoost 模型，保存为 `.json`。
    - **推理服务**：在 Python `Strategy Engine` 中加载模型，实时消费 Redis 数据并产出 `Signal` (Buy/Sell/Hold)。

### Phase 4: 执行与生产就绪 (Weeks 7-8)

- **目标**：闭环交易，把钱赚回来。
- **任务清单**：
    - **Flashbots 集成**：编写 Rust 执行模块，实现 Bundle 的构建与签名。
    - **风控中间件**：在下单逻辑中插入“Pre-trade Checks”（滑点、貔貅、黑名单）。
    - **纸面交易测试 (Paper Trading)**：在主网 fork 环境中模拟运行 1 周，验证 PnL 和逻辑漏洞。
    - **实盘上线 (Go Live)**：配置 `PROD` 环境变量，存入小额资金（如 1 ETH），开启自动驾驶。

### 5.3 资源需求 (Resource Requirements)

- **硬件**：
    - 推荐：AWS EC2 `c6i.2xlarge` (8 vCPU, 16GB RAM) 或同等配置的裸金属服务器。
    - 存储：至少 500GB NVMe SSD (ClickHouse 对 IOPS 要求高)。
- **服务**：
    - 节点：Alchemy / Infura (付费版，需支持 WebSocket)。
    - 执行：Flashbots RPC (免费)。

## 结语 (Conclusion)

这份白皮书不仅是技术文档，更是 EATLQ 从“个人玩具”向“印钞机器”进化的宣誓书。

我们选择了一条艰难的路：**Rust** 很难写，**ClickHouse** 很难调，**Flashbots** 很难接。但正是这些技术门槛，构成了你的**Alpha 护城河**。当别人的 Python 脚本还在轮询 Etherscan 时，你的 Rust 引擎已经在下一个区块的 Mempool 里完成了狙击。

**至此，《EATLQ 技术白皮书 v1.0》连载结束。**
