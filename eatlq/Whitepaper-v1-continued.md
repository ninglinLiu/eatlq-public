# LASZLO 技术白皮书 v1.0 — 续篇（Part 3～5）

**返回**：[v1 正文归档（Original）](docs/whitepaper-v1-original.md)（根目录不设 `Whitepaper-v1.md`；与 `mvp/audits/documentation_path_map.md` 一致）

---

## Part 3: 量化引擎与信号系统

### 3.1 从启发式到概率模型

| 阶段 | 描述 |
|------|------|
| **Phase 1 启发式** | IF-THEN 规则；快但易过拟合。 |
| **Phase 2 随机模型** | XGBoost/LightGBM 表格学习；自适应 Alpha。 |

### 3.2 钱包画像（The Wallet Profiler）

**实体聚类（CIH）**：同交易多输入 → 同一实体；监控 Cluster。

**标签**：Smart Money、Sniper、Diamond Hand、Paper Hand。

### 3.3 特征工厂（Feature Store）

热：Redis；冷：ClickHouse。

| 类型 | 示例 |
|------|------|
| Alpha | `win_rate_30d`, `avg_roi`, `pnl_z_score` |
| 动量 | `price_velocity_5m`, `buy_pressure_ratio` |
| 宏观 | `gas_gwei_trend`, `cex_net_flow` |

### 3.4 融合层

加权集成：  
`Final_Score = w1·Whale + w2·Momentum + w3·Sentiment − w4·Risk`

### 3.5 ML 流水线

ClickHouse 采集特征与目标 → 定期训练 XGBoost → 验证 → 热替换模型 → 实时推理。

---

## Part 4: 交易执行与风控

### 4.1 隐身与原子性

**公池** = MEV 屠宰场；**私有中继（Flashbots）** = 要么上链成功要么不上链（叙事上减少失败扣费与夹层风险）。

### 4.2 Rust Sniper

监听 Redis → 构建交易 → **Fork/Trace 模拟**（卖出、税率、滑点）→ 签名 → **Flashbots Bundle** → Relay。

### 4.3 动态仓位（Kelly 变体）

`Position_Size = Base_Capital × (Confidence × Vol_Adj)`  
分档示例：高分重仓、中分标准、低分观望。

### 4.4 风控卫士

**Pre-trade**：蜜罐模拟卖出、流动性深度、价差。  
**Post-trade**：Trailing（涨 10% 抬线、回撤 5% 卖）、Time（30min 横盘平）、Hard（亏 15% 平）。

### 4.5 系统熔断

日回撤 10% 停买；`SYSTEM_PANIC` 一键清仓逻辑。

---

## Part 5: 实施与资源

### 5.1 总体规划（四阶段 × ~2 周）

| 阶段 | 目标 |
|------|------|
| Phase 1 Foundation | Compose、ClickHouse/Redis/Postgres、迁移 |
| Phase 2 The Eye | Rust Ingestor、Streams、Dashboard 直连 Redis |
| Phase 3 The Brain | 钱包画像、Feature Store、XGBoost |
| Phase 4 The Hand | Flashbots、风控、Paper、Go Live |

### 5.2 Sprint 拆解（摘要）

- Week 1–2：基础设施与 Schema  
- Week 3–4：实时管道与 Archiver  
- Week 5–6：画像离线、模型  
- Week 7–8：Flashbots、纸面、实盘小额

### 5.3 资源

c6i.2xlarge 级、500GB NVMe、Alchemy/Infura 付费 WS、Flashbots RPC。

---

## 结语（v1）

「从个人玩具到印钞机器」的技术宣誓：Rust、ClickHouse、Flashbots 构成门槛与护城河。

---

*v1.0 Draft — 历史蓝图；当前实现见 v2 与 Architecture。*
