# LASZLO 白皮书 v2.0：3 月外宣初版 vs 5 月修订版 — 全文差异对照

> ## 🛑 AGENT 显著提示 — EATLQ ≠ LASZLO
>
> 本对照表比较的是 **原作者 Franklin Nexus 的两版 LASZLO 外宣白皮书**，**不是** EATLQ 实现变更日志。
>
> - **基线 A**：[`原作者白皮书 2 外宣版.txt`](../../原作者白皮书%202%20外宣版.txt)（2026-03-17，仓库内已保留）
> - **基线 B**：[`laszlo-whitepaper-v2-may-2026-author-external.md`](laszlo-whitepaper-v2-may-2026-author-external.md)（2026-05 作者外宣 + 补充，Agent 入库备份）
>
> **禁止**用本表推断 EATLQ 当前代码状态。EATLQ 契约见 `Architecture.md`；经济 guard 见 `mvp/audits/failed_no_7d_alpha_guard.md`。
>
> 生成日期：2026-05-27  
> 规则：每条仅列 **可逐字核对的原文差异** 或 **100% 置信度的合理推测**（推测项显式标注）。

---

## 差异总览

| 类别 | 3 月初版 | 5 月修订版 |
| ---- | -------- | ---------- |
| 文档体量 | 无 2026-05 段落；约 31 min read（外宣 txt） | 新增 2026-05 补充 + 2026-05 注记；外宣标注 37 min read |
| 哲学重心 | 工程闭环 + Phase 6 猎 Alpha | 闭环 ≠ Alpha；研究纪律（标注、复盘、克制） |
| Feature Store 叙述 | Redis Sorted Sets（`fs:*`） | `feat2:{symbol}` Hash 时间分桶 |
| 日回撤语义 | 触发 `SYSTEM_PANIC` | 独立 `EXECUTION_DRAWDOWN_HALT` |
| Phase 6 | Steps 1–4 | 新增 Step 5 复盘门槛 + 开篇目标句 |
| ML 流水线 | 5 步（无 prepare_dataset） | 6 步（含 Dataset Preparation） |

---

## 一、仅存在于 5 月版的全新段落（3 月版无对应原文）

### D-01｜文首「2026-05 补充：v2.0 之后的校准」整节

**5 月原文（摘要）**：

> v2.0 证明了 LASZLO 的工程闭环可以跑通……但「系统能开枪」不等于「系统已经有 Alpha」。2026-05 之后，LASZLO 的重点不再是继续堆新模块，而是回到三件更硬的事：数据可信 / 模型克制 / 风险前置……

> 所以 v2.0 之后的 LASZLO 不需要再发布更多「宏大路线图」。下一阶段的核心是把现有系统变成一个严肃的链上研究循环：采集 → 标注 → 重训 → 回测 → 小额验证 → 复盘。

**3 月版**：无此节。

**100% 置信推测**：作者在 Phase 6 推进后，主动把叙事从「加功能」收缩为「研究循环 + 预期管理」。

---

### D-02｜文尾「2026-05 注记：真正的分水岭」整节

**5 月原文（摘要）**：

> 好信号必须能被复现……好模型必须能解释失败……好系统必须能在亏损时自动缩手……

> 如果 v1.0 的关键词是「架构野心」，v2.0 的关键词是「实战闭环」，那么 v2.1 的关键词应该是：标注、复盘、克制。

**3 月版**：结语止于「这就是 Phase 6 — Alpha Hunt 的使命。」+ 日期行；无 v2.1 关键词。

**100% 置信推测**：作者自命名 v2.1 精神，但未改主标题版本号（仍为 v2.0 实战修订版）。

---

### D-03｜Phase 6 Step 5「复盘门槛」

**5 月原文（摘要）**：

> 在重新扩大实盘前，必须同时满足以下条件：
>
> - 每一笔候选信号都能追溯到原始 MarketTick、特征快照和模型版本。
> - 训练集、验证集与实盘窗口必须按时间切分，避免未来函数。
> - 胜率、平均盈亏比、最大连续亏损、最大日内回撤必须一起看，不能只看单一准确率。
> - 若模型在小额实盘中连续触发日回撤熔断，自动降级回 Paper Mode，重新进入标注与训练循环。

**3 月版**：Phase 6 仅 Steps 1–4，Step 4 结束后直接接「5.3 长期路线图」。

**100% 置信推测**：小额实盘或 Phase 6 过程中出现过「指标准、经济差」或「回撤后仍扩仓」类问题，故制度化复盘门槛。

---

### D-04｜Phase 6 开篇目标句

**5 月原文**：

> Phase 6 的目标不是「更快实盘」，而是确认 LASZLO 是否真的能从 Base L2 的真实 Swap 流中提取可重复的 Alpha。

**3 月版**：5.2 直接进入 Step 1，无此句。

---

### D-05｜ML 流水线「Dataset Preparation」步骤

**5 月原文**：

> **Dataset Preparation (数据准备)**：`scripts/prepare_dataset.py` 清洗 Dashcam，生成可训练的 `dashcam_cleaned.csv`，避免把坏行、重复行和不可标注样本直接喂给模型。

**3 月原文（3.5）**：Data Collection 之后直接 Labeling，无 prepare 步骤。

---

### D-06｜Labeling 可限制标注窗口

**5 月原文**：

> 当前实现可限制标注窗口，例如只处理最近一段时间的数据。

**3 月原文（3.5 Labeling）**：无「限制标注窗口」表述。

---

## 二、同一章节内的原文替换（3 月 vs 5 月逐条对照）

### D-07｜前言「关键变化一览」表格 — 新增「特征存储」行

| | 3 月版 | 5 月版 |
| --- | --- | --- |
| 行数 | 7 行（至「数据管道」为止） | **8 行**，新增一行 |

**5 月新增行原文**：

> 特征存储 | ClickHouse / Redis ZSET 设想 | feat2:{symbol} Redis Hash 时间分桶（当前实现）

**3 月版**：表格无此行（特征存储仅在正文 Layer 2 描述）。

---

### D-08｜1.3 现实主义架构 — Feature Store 热存储表述

| | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| v2.0 修正句 | 用 **Redis Sorted Sets** 承担热特征存储 | 用 Redis 中的 **feat2:{symbol} 时间分桶** 承担热特征存储 |

其余同句（ClickHouse 暂时禁用、Dashcam CSV）两版一致。

---

### D-09｜2.2 Layer 2 — Feature Store 热数据（整段重写）

**3 月原文**：

> fs:price:{symbol} — Sorted Set，按 timestamp 存储价格  
> fs:buys:{symbol} / fs:sells:{symbol} — Sorted Set……  
> fs:flow:{symbol} — Sorted Set……  
> fs:ticks:{symbol} — Sorted Set……  
> 自动 5 分钟滑动窗口（ZREMRANGEBYSCORE 清理过期数据）

**5 月原文**：

> 当前实现使用 feat2:{symbol} Redis Hash，以时间分桶方式聚合价格、买卖方向、资金流和活跃度。  
> 旧版叙述中的 fs:price:{symbol} / fs:buys:{symbol} 等 Sorted Set 前缀，属于早期设计，不再代表当前写入路径。  
> 这种 Hash 分桶方案牺牲了一部分逐笔可见性，换来更低的更新成本、更简单的 TTL 管理……

**100% 置信推测**：实现已从 ZSET 滑动窗口迁移到 Hash 分桶；5 月文明确将 3 月叙述降级为「早期设计」。

---

### D-10｜2.2 Layer 2 — 系统状态 Redis 键

| 项 | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| SYSTEM_PANIC | 全局紧急停止开关**（手动或自动触发）** | **人工**全局急停开关 |
| EXECUTION_DRAWDOWN_HALT | **（不存在）** | 自动日回撤停买状态，**只阻止新开 BUY，不等同于一键清仓** |
| daily_start_balance | 日内回撤追踪 | 日内回撤追踪**（需结合 ETH + WETH 与未平仓状态判断）** |

**100% 置信推测**：3 月把日回撤与 panic 绑在一起；5 月拆成两个语义，且余额口径从「仅 ETH」扩展为「ETH + WETH + 持仓状态」。

---

### D-11｜2.3 数据流 T+9ms — FeatureStore.update 目标

| | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| T+9ms | FeatureStore.update() 更新 **Redis Sorted Sets** | FeatureStore.update() 更新 **feat2:{symbol} Redis Hash 时间分桶** |

T+0–T+8ms、T+10ms 及之后步骤两版相同（含「v2.0 实测」标题）。

**100% 置信推测**：5 月补充（D-12）声明毫秒分解为「设计叙事」；但 2.3 标题仍为「实测」，两版均未改标题——存在文内张力。

---

### D-12｜2026-05 补充 — 毫秒延迟定性（对 1.2 与 2.3 的 retroactive 校准）

**5 月原文**：

> 文中的毫秒级链路分解是设计叙事，真实延迟取决于 RPC、负载、容器资源与网络状态。

**3 月版**：无此句；且 1.2 仍写「**实测** Tick-to-Signal 延迟 < 20ms」。

**对照**：

- **3 月 1.2 原文**：「毫秒级推流……**实测** Tick-to-Signal 延迟 < 20ms」
- **5 月 1.2**：该句**未删除**（正文仍保留「实测」）
- **5 月补充**：另起一段否定毫秒分解的「实测」含义

**100% 置信推测**：5 月版在保留 1.2 营销句的同时，用补充段做免责声明；读者需以补充段为准理解 2.3 时间线。

---

### D-13｜3.3 Feature Store 实现描述

| | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| 首句 | 基于 **Redis Sorted Sets** 的实时滑动窗口特征计算 | 当前采用 **feat2:{symbol} Redis Hash 时间分桶**；**早期 Sorted Sets 方案已退为历史叙述** |

特征四维定义表（price_velocity_5m 等）两版**一致**。

---

### D-14｜3.5 ML — Dashcam 列描述

| | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| Data Collection | 包含……**八列** | 包含 timestamp、token_address、final_score、whale_val、momentum_val、activity_val、risk_val、triggered_sniper **等列**（未写「八列」） |

列名列表实质相同；仅「八列」计数表述变化。

---

### D-15｜3.5 ML — Training 时间切分表述

| | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| Training | 时间序列切分（**最旧 80% 训练，最新 20% 验证**），只有 holdout 准确率 > 55% 才部署 | 时间序列切分（**训练回看窗口 + 最新 holdout**），只有**验证表现达到门槛**才部署 |

**100% 置信推测**：5 月弱化固定 80/20 比例，改为更灵活的「回看窗口 + holdout」表述（可能与实现参数已非严格 20% 有关）。**非 100% 部分不展开。**

---

### D-16｜4.4 日回撤熔断 — 触发键与余额口径（核心风控差异）

**3 月原文**：

> 每天 UTC 0:00 自动记录起始 **ETH 余额**到 Redis。  
> 每次交易前比对当前余额：如果 (start - current) / start > 10%，**自动设置 SYSTEM_PANIC = true**。  
> 系统立即停止所有买入，仅允许卖出。次日 UTC 0:00 自动重置。

**5 月原文**：

> 每天 UTC 0:00 自动记录起始余额。  
> 每次交易前综合比较当前 **ETH + WETH** 与日初余额；**若无未平 positions:\*** 且回撤超过阈值，触发 **EXECUTION_DRAWDOWN_HALT**。  
> 该状态**只阻止新开 BUY**，不等同于 SYSTEM_PANIC。人工急停仍由 SYSTEM_PANIC 负责。

---

### D-17｜4.5 系统级熔断 — Kill Switch 语义

**3 月原文**：

> Max Daily Drawdown：**ETH 余额**下降 > 10% 时自动触发 **SYSTEM_PANIC**。  
> Kill Switch（**一键清仓**）：Redis Key SYSTEM_PANIC = true。可通过前端按钮、脚本或**日回撤机制自动触发**。

**5 月原文**：

> Max Daily Drawdown：ETH + WETH……触发 **EXECUTION_DRAWDOWN_HALT**，阻止新开 BUY。  
> Kill Switch（**一键急停**）：Redis Key SYSTEM_PANIC = true。这是**人工急停机制，语义上和日回撤停买分开**。

| 子项 | 3 月 | 5 月 |
| ---- | ---- | ---- |
| 日回撤触发键 | SYSTEM_PANIC | EXECUTION_DRAWDOWN_HALT |
| Kill Switch 措辞 | 一键**清仓** | 一键**急停** |
| 日回撤能否触发 Kill Switch | **能**（「或日回撤机制自动触发」） | **不能**（明确分开） |

---

### D-18｜5.1 Phase 3 交付描述

| | 3 月原文 | 5 月原文 |
| --- | --- | --- |
| Phase 3 核心交付 | Feature Store (**Redis Sorted Sets**) | Feature Store (**feat2:{symbol} Redis Hash**) |

---

### D-19｜5.2 Phase 6 Step 4 之后 — 新增过渡段

**5 月原文（Step 5 前导语）**：

> 这一步是 v2.0 之后最重要的变化：LASZLO 不再把「上线」视作终点，而把上线视作获取真实反馈的一种受控实验。

**3 月版**：无此段。

---

## 三、5 月补充段对 3 月正文的 retroactive 校准（正文未改但语义被覆盖）

以下条目在 3 月正文中有旧表述，5 月**未逐处改正文**，但在「2026-05 补充」中统一校准：

| ID | 3 月正文仍写 | 5 月补充校准 |
| ---- | ------------ | ------------ |
| D-20 | 2.3「v2.0 **实测**」+ 毫秒时间线 | 毫秒分解 = **设计叙事** |
| D-21 | Layer 2 / 3.3 Sorted Sets | feat2 Hash；fs:/feat: = **历史路径** |
| D-22 | ClickHouse「暂时禁用」 | 可在实时阶段**暂停**；Dashcam+Redis **足够早期标注** |
| D-23 | 5.3 长期路线图 Month 2–6 | **不需要再发布更多宏大路线图**（与 5.3 表格并存） |

**100% 置信推测**：5.3 路线图表格在 5 月 PDF/外宣中可能仍保留为「历史计划」，但作者意图是冻结扩张叙事；读者应以 2026-05 补充为准。

---

## 四、两版相同、5 月未改动的要点（便于 Agent 勿误报为差异）

以下在 3 月 txt 与 5 月归档中**原文一致**（或仅排版差异）：

- Part 1.1「系统已在 Base L2 主网完成**实盘验证**」
- 1.2「实测 Tick-to-Signal 延迟 < 20ms」（5 月正文未删，见 D-12）
- 融合公式 Mode A/B 权重与公式
- 4 维特征定义与 net_flow 动量代理论述
- Quoter V2 蜜罐、liquidity×20、三重止损参数（5%/30min/15%）
- Base_Capital = $25、WHALE_USD_THRESH = $100k
- Part 6 四条 Lessons Learned 全文
- 5.3 长期路线图 Month 2–6 表格原文
- 结语「系统已经就位。弹药已经上膛……」全文

---

## 五、与 EATLQ 仓库的关系（非 LASZLO 两版差异，仅供 Agent 防误判）

> 本节 **不是** 3 月 vs 5 月 LASZLO 差异，而是入库时附带的 **EATLQ 事实锚点**（100% 可核对）。

| LASZLO 5 月叙述 | EATLQ 仓内现状（2026-05-27） | 含义 |
| ---------------- | ---------------------------- | ---- |
| `feat2:{symbol}` | `services/strategy/src/feature_store.py` 使用 **`feat:*`** 前缀；全仓 **无 `feat2:` 字符串** | 5 月外宣 ≠ EATLQ 已实现 |
| `EXECUTION_DRAWDOWN_HALT` | 全仓 **无此 Redis key**；日回撤叙事仍多指向 **`SYSTEM_PANIC`** | 5 月风控拆分 ≠ EATLQ 已落地 |
| 「闭环已验证 / Phase 6 进行中」 | `FAILED_NO_7D_ALPHA` guard 仍 active | EATLQ 经济读数未过线 |
| Rust Executor `crates/execution/` | EATLQ 执行器主要为 **`services/executor`（Python）** | 路径/语言可能与 LASZLO 外宣不一致 |

**100% 置信推测**：EATLQ 团队应把本对照表当作「原作者意图漂移记录」，而非 sprint 实施清单。

---

## 六、差异条目索引（共 23 条）

| ID | 类型 | 摘要 |
| -- | ---- | ---- |
| D-01 | 新增 | 2026-05 补充整节 |
| D-02 | 新增 | 2026-05 注记 / v2.1 关键词 |
| D-03 | 新增 | Phase 6 Step 5 复盘门槛 |
| D-04 | 新增 | Phase 6 开篇目标句 |
| D-05 | 新增 | prepare_dataset.py 步骤 |
| D-06 | 新增 | 标注窗口可限制 |
| D-07 | 修改 | 关键变化表 +特征存储行 |
| D-08 | 修改 | 1.3 Sorted Sets → feat2 分桶 |
| D-09 | 修改 | 2.2 Feature Store 整段 |
| D-10 | 修改 | 2.2 系统状态键 |
| D-11 | 修改 | 2.3 T+9ms 更新目标 |
| D-12 | 校准 | 毫秒 = 设计叙事 |
| D-13 | 修改 | 3.3 Feature Store 描述 |
| D-14 | 修改 | Dashcam「八列」表述 |
| D-15 | 修改 | Training 切分表述 |
| D-16 | 修改 | 4.4 日回撤触发逻辑 |
| D-17 | 修改 | 4.5 Kill Switch vs 日回撤 |
| D-18 | 修改 | 5.1 Phase 3 交付 |
| D-19 | 新增 | Phase 6 受控实验导语 |
| D-20 | 校准 | 2.3 实测标题 vs 补充 |
| D-21 | 校准 | fs/feat 历史路径 |
| D-22 | 校准 | ClickHouse 可长期暂停 |
| D-23 | 校准 | 冻结宏大路线图 vs 5.3 表格 |

---

**Changed files（本轮）**：

- `docs/author-whitepapers/README.md`（新建）
- `docs/author-whitepapers/laszlo-whitepaper-v2-may-2026-author-external.md`（新建）
- `docs/author-whitepapers/laszlo-whitepaper-v2-march-vs-may-2026-diff.md`（本文件，新建）

**未修改 / 未覆盖**：

- `原作者白皮书 2 外宣版.txt`
- `whitepaper-v2.md` / `Whitepaper-v2-continued.md`
- `docs/whitepaper-v1-original.md`
