# EATLQ vs LASZLO：四个月全景对照（2026-07-19）

> 交互版：[eatlq-vs-laszlo-whitepaper.canvas.tsx](/root/.cursor/projects/root/canvases/eatlq-vs-laszlo-whitepaper.canvas.tsx)  
> LASZLO 文本源：`agent_readable/laszlo-whitepaper-v{1,2}.md` + `FIGURES.md`  
> 作者边界：`/root/.cursor/skills/eatlq-authorship/SKILL.md`  
> 本报告为内部研究对照，不构成投资建议。

## 0. 一句话判决

你们从「完全 copy LASZLO」出发，四个月后变成**证据门禁更重、研究面更广、生产更隔离**的路径；作者则保持**更窄的单一工厂命题 + 0 BUY 合法化**。两边都还没有可部署 alpha。当下最该借的是作者的**窄问题、经济门禁、停止条件**，不是抄 Pe72 数字或再堆功能。

## 1. 对象定义（避免双真源）

| 对象 | 是什么 | 不是什么 |
|------|--------|----------|
| **LASZLO** | Franklin Nexus 原作者系统 + WisdomEchoes 白皮书 v1/v2 外宣 | EATLQ 仓库完成度证明 |
| **/opt/EATLQ** | 你们以 LASZLO 架构为底的生产适配栈 | 作者本人当前代码快照 |
| **EATLQ 族（当下）** | EATLQ + Atlas + LPI + Quant Research OS | 仍等于「白纸 Mode B 工厂」 |

原作者文档（仓库内）仅：`Architecture.md`、`Architecture-Simplified.md`、`whitepaper-v2.md`、`Whitepaper-v*-continued.md`、`Operations.md`、`Code-Review.md`、`Home.md`、`docs/author-whitepapers/*` 等。其余 `docs/`、`mvp/`、`reports/GL*`、meme 层、KEYSTONE、Atlas 均为团队叠加。

## 2. 时间线

| 时段 | LASZLO | 你们 |
|------|--------|------|
| ~战前→2026-03 | v1 Mainnet/Flashbots 蓝图；迁 Base；工程闭环 | 2026-03-14 git bootstrap，意图 copy/适配 |
| 2026-04 | 外宣持续强调闭环与克制 | meme_sniper 叠加 → **04-22 整层拆除**，回调作者钱包标签 |
| 2026-05 | （作者侧继续校准） | SOP/paper-shadow 高峰；**FAILED_NO_7D_ALPHA**；总战报：无 canary |
| 2026-06 | 外宣诚实态：holdout≪72%，**预期 0 BUY** | KEYSTONE go-live 叙事 → 亏损事件 / **SYSTEM_PANIC** |
| 2026-07 | （公开页仍以 6 月快照为主） | GL/B 诚实重审；**Atlas / LPI 隔离轨**；Research OS 家底 |

龄期粗算：作者公开叙事 ~6 个月；你们仓库 ~4 个月（Mar→Jul）。

## 3. 维度对照

### 3.1 命题与边界

- **LASZLO**：Base L2 DEX 现货、Uniswap V3、单租户 Compose；不做 CEX/跨链/SaaS；没过门禁不给真实 BUY。
- **你们**：同链主战场，但研究扩展到 KEYSTONE 新池左尾、Atlas 30 正交 V3 机制、Clanker/V4 发射台取证；生产与研究硬隔离。

### 3.2 架构

作者主路径（外宣）：

```text
Alchemy → Rust Ingestor → Redis market_ticks → Python Strategy
  → Redis signals → Rust Executor → Anvil/Mainnet → trade_ledger.jsonl
```

你们现实：

```text
Alchemy → Rust Ingestor/(Archiver→CH) → Redis → Python Strategy / signal_bridge
  → Redis signals → Python Executor → Anvil/live → Redis positions (+ 研究侧 GL ledger)
```

要点：

- 分层思想同源（采集 / 总线 / 策略 / 唯一资金出口）。
- **Executor 语言**：外宣偏 Rust；你们契约已承认 Python（`docs/executor-language-contract.md`）。
- **特征键**：外宣 `feat2:`；你们运行时多为 `feat:`，另加第 5 维 `unique_buyer_entropy_5m`（作者无）。

### 3.3 模型与门禁

| 项 | LASZLO 外宣/作者续篇 | 你们 |
|----|----------------------|------|
| 特征 | 4 维资金流代理 | 4 维 + entropy；线上主线迁 KEYSTONE |
| 离线窗 | 28d + 7d holdout（作者续篇） | 同结构曾在 `retrain_model.py`；经济上被 FAILED_NO_7D 压制 |
| 部署门槛 | 外宣 **Pe≈72%** + `fire_thr=max(model,0.68)`；续篇也有 **>55% accuracy** 叙事 | 代码侧多见 accuracy≥0.55 / PR-AUC；**无** `precision_economics.py` / fire_thr |
| 合法无交易 | **0 BUY 是正确结果** | 门禁极多，但曾 live canary，后 panic |
| 现状 | holdout 远低于 72%；继续校准 | `live_proven=false`；无 promotion；Atlas/LPI 无 PnL |

### 3.4 执行与风控

双方共享原则：**BUY fail-closed / SELL fail-open**、单一链上出口、蜜罐/流动性预检思想。

参数已分叉：作者外宣三重止损约 5% / 30m / 15%；KEYSTONE 文档侧为更长持仓、更宽硬止损等——**原则继承、策略参数换血**。

### 3.5 诚实状态（收敛点）

两边最重要的相同句：

> 工程闭环可以成立，**不等于**有可部署 alpha。

作者（2026-06 外宣）：dashcam~29d；QC 过不等于模型过；不降阈值造交易。  
你们（2026-05→07）：总战报无 canary；FAILED_NO_7D；KEYSTONE 历史漂亮数字被 B8/GL 压回 NOT_MADE。

## 4. 优缺点

### LASZLO 优

1. 边界窄，抗自我膨胀  
2. 经济门禁叙事清楚（从摩擦倒推）  
3. 0 BUY 合法，减少表演型交易  
4. 真实故障课完整（序列化/单位/占位/失败默认值）  
5. 明确停止与 pivot 条件  

### LASZLO 劣

1. 外宣完成度 ≠ 你们仓库真实现  
2. 单一 4 维命题可能问错标签/regime  
3. 样本窗短、regime 不足  
4. Executor/ledger 叙事与常见实现易漂移  
5. 公开仍停在「能拒绝」，未交出可复现盈利  

### 你们优

1. 证伪与反过拟合纪律强（SOP、echo-alpha 拒杀、GL 重审）  
2. 工程路径真实可跑  
3. 研究/生产隔离（Atlas/LPI 不脏主仓）  
4. 敢拆错误叠加（meme_sniper）  
5. 对自身 live 叙事二次诚实  

### 你们劣

1. 命题漂移快：copy 工厂 → KEYSTONE → 30 卡 Atlas  
2. 门禁文书重，单一闭环反馈变慢  
3. 曾打破 0 BUY 式克制（live/panic）  
4. 白皮书与实现双真源，易误导 Agent  
5. 同样没有 round-trip 盈利证明  

## 5. 分叉机制（为何走到不同路径）

1. **Copy 阶段**：先要「像 LASZLO 一样能转」。  
2. **叠加再回撤**：meme 层证明「作者没设计的东西」会制造假进度，于是拆除。  
3. **经济否决**：FAILED_NO_7D 把「模型 promoted」和「能上钱」切开——这是对作者精神的强化，不是背叛。  
4. **主策略换血**：白纸 XGB/dashcam 不够用时，KEYSTONE 成为 live 候选。  
5. **联邦化**：为防再污染生产，研究拆到 Atlas/LPI/OS——获得安全，失去作者式「一个闭环打穿」的纯度。

## 6. 对当下的借鉴（可执行）

### 高优先级

1. **重新钉死单一可证伪命题**  
   Atlas Wave1 ≤6；其余只设计。作者现在只问一件事：摩擦后是否仍有 edge。

2. **经济门禁显式化（不必抄 72%）**  
   用你们 Quoter/摩擦重算部署线；禁止 accuracy/promoted 覆盖经济 readout。

3. **0 BUY / Paper-only 写回操作文化**  
   生产默认 buy-block；离线研究不得制造 live 压力。

4. **标签与特征语义审计优先于新策略名**  
   作者强调 train/online 同名异义；对 KEYSTONE 与 Atlas F01/F03 先做 label/regime 证伪。

### 中优先级

5. **账本真源单一化**（Redis 非真相；事件链可重放）  
6. **给 Atlas/LPI 写日历停止条件**（作者第 10 章的 pivot 条款）  
7. **不要为对齐而复活 Pe72 全流水线**——除非重新选择「4 维 dashcam 工厂」为主命题  

### 明确不借

- 降阈值换 BUY  
- 用 dashboard/策略改名当进展  
- 30 张卡片同时烧全量 backtest  

## 7. 收敛表

| 问题 | 答案 |
|------|------|
| 还要不要继续 copy LASZLO？ | **架构原则继续借；实现细节与 Pe72 流水线不要无脑同步** |
| 谁更接近「可交易」？ | **都还没到**；你们证据机器更重，作者命题更干净 |
| 下一步最贵的错误是什么？ | 在没有新经济 readout 的情况下再次 live；或把 Atlas 做成第二个无限脚手架 |

## 8. 关键路径索引

- LASZLO 镜像正文：`/root/laszlo_whitepaper_v2_mirror/agent_readable/`  
- EATLQ 入口：`/opt/EATLQ/Home.md`、`Architecture.md`  
- 经济卫兵：`/opt/EATLQ/mvp/audits/failed_no_7d_alpha_guard.md`  
- 2026-05 总战报：`/root/EATLQ_LASZLO_Project_Total_Report_CORRECTED.md`  
- 2026-07 诚实重审：`/opt/EATLQ/reports/B8_*`、`GL1_*`…  
- Atlas：`/root/base_alpha_atlas_30_v0/`  
- LPI：`/root/lpi_base_production_safe_node/`  
- Research OS：`/root/quant_research_os/`  
