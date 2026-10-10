# LASZLO 技术白皮书 v2.0 — 实战修订版

> **读者说明**：本文档供人类与 Agent 共读。与**当前仓库实现**不一致之处，以**仓库根目录** [`Architecture.md`](Architecture.md)、[`Operations.md`](Operations.md) 及源码为准（本仓 **不**使用 `wiki/` 目录；见 `mvp/audits/documentation_path_map.md`）。例如 Feature Store 实际前缀为 `feat:`、部分表格中的容器内存与 compose 细节可能随环境变化。

**作者**：Franklin Nexus  
**约读时长**：31 min  
**日期**：2026 年 3 月 17 日（正文中原「2926」系笔误，已校正）

---

## 前言：从 v1 到 v2 — 战场反馈修正

v1.0 白皮书是一份**战前蓝图**：Ethereum Mainnet、Flashbots、ClickHouse、完整钱包画像等理想架构。

v2.0 是**战后报告**：经过约 6 周实战与 Base L2 验证，标明哪些假设成立、哪些已修正。

### 关键变化一览

| 维度 | v1.0 蓝图 | v2.0 实战 |
|------|-----------|-----------|
| 目标链 | Ethereum Mainnet | **Base L2（OP Stack）** |
| MEV 防护 | Flashbots Bundle | **精确 EIP-1559 Gas**（单 Sequencer，maxPriority ≈ 1 wei） |
| 蜜罐检测 | 概念描述 | **Quoter V2 `eth_call` 模拟卖出** |
| 止盈止损 | 概念描述 | **Uniswap V3 `slot0()` 实时报价**（Position Manager 周期检查） |
| ML 模型 | 合成数据 XGBoost | **v1 合成 + v2 真实标注**自动切换 |
| 日回撤保护 | 手动 Kill Switch | **自动 10% 日回撤熔断**（UTC 日界重置） |
| 数据管道 | `newPendingTransactions` | **`eth_subscribe("logs")` + Swap ABI 解码** |

---

## Part 1: 愿景、核心哲学与系统边界

### 1.1 项目定义 (Executive Summary)

LASZLO 是机构级、闭环式、低延迟的加密资产 Alpha 终端与自动交易系统。

- **不是什么**：被动 Dashboard、浏览器仿品、纯看盘玩具。  
- **是什么**：从链上噪音中提取信号，并覆盖分析到安全执行的**武器系统**。  
- **核心目标**：具备可扩展至 $100M AUM **逻辑**的单兵系统（不等于当前资金规模）。

**v2.0 状态（宣言级）**：Rust Ingestor 解析 Swap、Python 引擎推理、Rust Executor 执行、Position Manager 以链上价格监控止盈止损——闭环在工程上成立；**模型与 RPC 质量**仍决定实盘边缘。

### 1.2 核心痛点与解决方案

| 维度 | 散户现状 | LASZLO |
|------|----------|--------|
| 数据质量 | 噪音大 | 黑名单降噪 + 巨鲸 USD 阈值 |
| 时效性 | 轮询/刷新慢 | Rust WS + Redis Streams |
| 决策闭环 | 工具割裂 | Ingestor → Strategy → Executor → PM |
| 交易执行 | 裸奔公池 | Base 上 Gas/滑点约束 + 蜜罐检测 |
| 止损 | 手动 | Trailing / Time / Hard 自动化 |

### 1.3 设计哲学

- **信噪比 > 数据量**：无决策价值的数据不进管线与屏幕。  
- **慢即是快**：地基（Rust/Redis/契约）先稳，再上实盘。  
- **现实主义架构**：单机 Docker Compose；Phase 中 **ClickHouse/Archiver 可关**，Dashcam/Redis 扛实时与离线。

### 1.4 系统边界

- **资产**：Base L2 为主，架构预留 EVM 扩展。  
- **交易类型**：Uniswap V3 现货 Swap，`exactInputSingle`，多 fee tier 池发现。  
- **用户规模**：单租户 / 小团队极致性能。

---

## Part 2: 技术架构与基础设施

### 2.1 异构混合架构

**Rust 外壳 + Python 大脑 + Redis 神经**。单机垂直扩展；Docker 网络 `laszlo-net`（如 `172.30.0.0/16`）。

### 2.2 基础设施层级（摘要）

**Layer 1 — Ingestor（Rust）**  
- `eth_subscribe("logs")`，Swap 事件解码，`PoolCache`，`TokenRegistry`，USD 与方向估计。  
- **MsgPack**：`rmp_serde::to_vec_named` → **Redis Stream 字段名 `data`**（见根目录 [`Architecture.md`](Architecture.md)）。

**MarketTick（概念字段，与代码对齐时以 `crates/ingestor` / `archiver` model 为准）**  
`timestamp`, `block_number`, `tx_hash`, `log_index`, `pool_address`, `token_address`, `symbol`, `wallet_address`, `side`, `amount_usd`, `amount_raw`, `price`, `event_type`, `is_whale`（布尔或 u8 以实现为准）。

**Layer 2 — Redis**  
- Streams：`market_ticks`、`signals`；Consumer groups：strategy / execution /（archiver 若启用）。  
- **Feature Store（v2 文档称 `fs:`）**：当前代码前缀多为 **`feat:`**（见 `feature_store.py`）— 读文档时请交叉对照源码。

**Layer 3 — Vault**  
- **ClickHouse**：Phase 4.5 可暂停。  
- **PostgreSQL**：钱包画像等 OLTP。

**Layer 4 — Brain & Hand**  
- Strategy：`services/strategy/src/main.py`  
- Execution：`crates/execution/`  
- Dashboard：Streamlit

### 2.3 数据流全景（v2 叙述中的延迟数字）

链上 Swap → Alchemy WS → Ingestor 解码 → MsgPack → `XADD market_ticks` → Strategy 特征与推理 → 条件满足则 `XADD signals` → Executor 风控与上链 → Position Manager 持仓与止损逻辑。

（文档中毫秒级分解为设计叙事；**实测**依赖负载与 RPC。）

### 2.4 基础设施清单（与 compose 对照）

v2 表格列出的服务/内存为**目标配置**；`deploy.resources` 在普通 Docker Compose 下**可能不生效**—以主机实际为准。ClickHouse/Archiver 在 compose 中可处于注释/停用状态。

---

## 续篇

**Part 3～6、复盘与结语** 见：[Whitepaper-v2-continued.md](Whitepaper-v2-continued.md)
