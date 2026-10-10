# LASZLO 技术白皮书 v2.0 — 续篇（Part 3～6）

**返回**：[whitepaper-v2.md](whitepaper-v2.md)（Linux 大小写敏感：链到仓库内实际文件名，非 `Whitepaper-v2.md`）

---

## Part 3: 量化引擎与信号系统

### 3.1 从确定性规则到概率模型

| 阶段 | 内容 |
| ---- | ---- |
| **Phase 1 — Synthetic Bootstrap** | 合成数据训练 XGBoost v1；钱包特征在过渡期常默认；产出 `whale_sniper_v1.json`。 |
| **Phase 2 — Real-Data Calibration** | Dashcam（如 `logs/dashcam*.csv`）积累真实样本；`label_dashcam.py` + `retrain_model.py`；准确率门槛（文档：验证集 > 55%）；`whale_sniper_v2.json`。 |
| **Phase 3 — Adaptive Evolution** | 定时标注/训练/A/B（规划中）。 |

### 3.2 钱包画像（The Wallet Profiler）

标签：Smart Money、Sniper、Diamond Hand、Paper Hand 等。**v2 过渡**：巨鲸可用 **WHALE_USD_THRESH**（如 $100k）近似。与 DB 表 `wallet_profiles` 配合。

### 3.3 特征工程（Feature Store）

**Redis Sorted Sets** 上 5 分钟滑动窗口（定期裁剪）。

| 特征 | 含义 |
| ---- | ---- |
| `price_velocity_5m` | 净资金流 / 总绝对流 ∈ [-1, 1] |
| `buy_pressure_ratio` | 买笔数占比 |
| `token_volatility_5m` | 规模变异系数上限 2.0 |
| `activity_heat` | 窗口内 tick 数 |

**设计点**：DEX 小币常缺稳定 USD 价 → 用**资金流方向**代理动量。

### 3.4 融合层

**模式 A（v1 + 手动权重）**  
`Final_Score = 0.40×Whale + 0.30×Momentum + 0.20×Activity − 0.10×Risk`  
（Risk：波动超阈惩罚。）

**模式 B（v2 模型）**  
若存在 `whale_sniper_v2.json`，直接输出盈利概率类分数（以代码分支为准）。

### 3.5 ML 闭环

1. **采集**：Dashcam 记 `final_score > 阈值` 的候选（列：时间、token、分数、各特征、`triggered_sniper` 等）。  
2. **数据准备**：`prepare_dataset.py` 生成 `dashcam_cleaned.csv`。  
3. **标注**：`label_dashcam.py`（如 5 分钟后池价变化；当前默认 `LABEL_MAX_AGE_DAYS=30`）。  
4. **训练**：`retrain_model.py`，时间序列切分（当前实现为“训练回看窗口 + 最新 7 天 holdout”）。  
5. **推理**：热加载 v2。  
6. **监控**：`analyze_dashcam.py` 等离线分析。

---

## Part 4: 交易执行与风控

### 4.1 Base L2「隐身执行」叙事

- **无 Flashbots Bundle**：代之以 base fee 查询、`maxPriorityFeePerGas` 极低、滑点 `amountOutMinimum` 硬上限。  
- **Gas 异常熔断**：如 baseFee 超阈拒绝交易（具体常数以 `risk.rs` / env 为准）。

### 4.2 Rust Sniper（摘要）

- **Paper / Live**：RPC 与私钥来源不同；**链 ID 必须为 Base 8453**（Live）。  
- 工作流：`XREADGROUP signals` → 日回撤 / 流动性 / 蜜罐 → `exactInputSingle` → 持仓写入 Redis。

### 4.3 动态仓位（Kelly 变体）

`Position_Size = Base_Capital × Final_Score × Vol_Adj`  
文档示例将 `Base_Capital` 从理论 $10k 校准为**与实际余额一致**的小资金—避免仓位荒谬。

### 4.4 风控卫士

**Pre-trade**  

1. **蜜罐**：Quoter V2 `quoteExactInputSingle` 多 fee tier `eth_call`。  
2. **流动性**：`liquidity()` 与规模比较。  
3. **日回撤**：UTC 日起始余额 vs 当前，超 10% 触发 `SYSTEM_PANIC`。

**Post-trade — Position Manager**  
周期（如每 2s）`slot0()`、三重止损：Trailing（如峰值回撤 5% 且仍盈利）、Time（如 30min 横盘 ±2%）、Hard（如亏 15%）。

### 4.5 Kill Switch

`SYSTEM_PANIC`、Gas 熔断、日回撤等协同。

---

## Part 5: 路线图

| 阶段 | 状态（叙事） | 交付 |
| ---- | ------------ | ---- |
| Phase 1–4.5 | 已完成/实弹 | Compose、Ingestor、Strategy、Executor、Dashcam、实盘修复 |
| Phase 5 | 叙事完成 | PM、蜜罐、日回撤、Gas、重训管线 |
| Phase 6 Alpha Hunt | 进行中 | 数据积累 → 标注 → v2 → 再验证胜率 |

**Phase 6 步骤（文档）**：Paper 长跑积累 → `label_dashcam` → `retrain_model` → 小额 Live。

**长期**：画像增强、ClickHouse、多链、资金规模与自动化训练循环。

**资源**：WSL/Docker 开发 vs 推荐云主机；Alchemy 等级。

---

## Part 6: 实战复盘（Lessons Learned）

1. **MsgPack 列表 vs 映射**：`to_vec_named` + Python 兼容 list/dict。  
2. **特征爆炸**：小币伪价格 → 改资金流代理。  
3. **占位符致命**：PM 用 `entry_price` 占位 → 止损失效；须真实链上价。  
4. **Base vs Mainnet**：低成本快确认 vs Flashbots/流动性差异。

---

## 结语

v2.0 是**经过实弹验证的作战手册**：从 Swap 事件到执行与 PM 的链路已跑通；**模型与数据**仍是 Phase 6 核心跃迁点。

---

*v2.0 实战修订版 — 与仓库同步请以代码为准。*
