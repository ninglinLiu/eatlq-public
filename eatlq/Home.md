# EATLQ Wiki

本目录为项目 **内部知识库**（随仓库版本化）。原作者项目代号 **LASZLO**；EATLQ 在其架构之上做 Linux/Base 适配与清理。文档遵循三层结构：

- **`Home.md`**（你在这里）— 入口索引
- **`Architecture.md`** — 数据流、服务职责、Redis 契约、状态机（**可执行契约以此为准**）
- **`Operations.md`** — 启动、环境变量、常见故障排查

## 目录

| 页面 | 说明 |
| ---- | ---- |
| [Architecture](Architecture.md) | 数据流、服务职责、Redis Stream 字段契约、持仓状态机 |
| [Architecture-Simplified](Architecture-Simplified.md) | 面向日常操作的简化架构与收敛策略 |
| [Operations](Operations.md) | 启动命令、环境变量、故障排查（Linux / bash） |
| [Code-Review](Code-Review.md) | 代码审查结论与后续改进优先级 |
| [白皮书 v2.0（实战）](whitepaper-v2.md) + [续篇](Whitepaper-v2-continued.md) | 战后校准版：Base、执行/风控、路线图；**与实现矛盾时以源码与 Architecture 为准** |
| [白皮书 v1.0（Draft）](docs/whitepaper-v1-original.md) + [续篇](Whitepaper-v1-continued.md) | 战前蓝图：**Ethereum Mainnet / Flashbots / ClickHouse 等历史架构叙事**；**存档对照用**，**非当前实现目标**（正文归档于 `docs/whitepaper-v1-original.md`；根目录无 `Whitepaper-v1.md`） |
| [**原作者 LASZLO 外宣归档**](docs/author-whitepapers/README.md) | **⚠️ EATLQ ≠ LASZLO**：Franklin Nexus 外宣 v2（含 [2026-05 修订全文](docs/author-whitepapers/laszlo-whitepaper-v2-may-2026-author-external.md) + [3月vs5月差异表](docs/author-whitepapers/laszlo-whitepaper-v2-march-vs-may-2026-diff.md)）；**非 EATLQ 真实现状**；3 月初版仍保留于 [`原作者白皮书 2 外宣版.txt`](原作者白皮书%202%20外宣版.txt) |

### 白皮书怎么用（给人 + 给 Agent）

- **人类**：先读 v2 前言与「关键变化」，再按需深入 Part；v1 用于理解「当初为什么那样设计」。
- **Agent**：把 v2 当作**产品/架构叙事**；**可执行契约**（Redis 键名、compose 服务名、Stream 字段）一律以 `Architecture.md` 与代码为准，避免文档漂移误导。
- **Agent（原作者外宣）**：`docs/author-whitepapers/` 下为 **LASZLO 原作者外宣存档**，**不得**当作 EATLQ 实现完成度或 live/train 授权；经济 guard 见 `mvp/audits/failed_no_7d_alpha_guard.md`。

## 快速入口

- Docker 主编排：`infra/compose/compose.yaml`
- 环境变量示例：`infra/compose/.env.example`（首次运行会自动复制为 `.env`）
- 统一入口脚本（推荐）：`scripts/laszlo.sh`（Linux / bash）
- Phase 6 每周训练脚本：`scripts/phase6_weekly_cycle.sh`
- 核心服务（LASZLO 对齐后的简化集合）：
  - 采集（Rust）：`services/ingestor`
  - 归档（Rust）：`services/archiver`
  - 策略引擎（Python）：`services/strategy`
  - 执行器（Python）：`services/executor`
  - 池子发现（Python）：`services/pool_discovery`
  - 仪表盘（Python stdlib HTTP）：`services/dashboard`

## 一分钟上手

```bash
cp infra/compose/.env.example infra/compose/.env   # 首次；编辑里面的 EVM_WS_URL / PRIVATE_KEY
scripts/laszlo.sh up        # 构建并启动全栈
scripts/laszlo.sh status    # 查看容器状态
scripts/laszlo.sh logs executor   # 跟踪执行器日志
scripts/laszlo.sh phase6    # 调用 Phase 6 编排（不表示经济 guard 解除；见 mvp/audits/failed_no_7d_alpha_guard.md）
scripts/laszlo.sh down      # 停止并清理
```
