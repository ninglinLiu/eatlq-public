# 架构简化文档（2026-04-17）

> **Guard (Phase 3):** Simplified sketch only—not a full alignment proof. **`contract docs override narrative drift`** on training/live/technical claims. **`FAILED_NO_7D_ALPHA`**: see `mvp/audits/failed_no_7d_alpha_guard.md`. This document is **`not an authorization`** for labeling, training, live, Phase 6, or whitepaper alignment claims.

> **Executor note:** see `docs/executor-language-contract.md` for the executor language contract — **current executor is Python**; **Rust Sniper is narrative/historical wording**, not a service-language requirement nor authorization.

**Static contracts (no whitepaper parity claim):** MR 前可跑 `Operations.md` §「仓库静态契约检查」与 `make check-static`。

## 1) 当前系统，一句话说明

LASZLO 是一个“链上事件采集 -> 信号计算 -> 执行交易 -> 回测训练”的多服务系统，功能完整，但入口分散、脚本过多、认知成本偏高。

## 2) 先给结论（简化目标）

- **目标A：一个入口**  
  所有高频操作统一从 `scripts/laszlo.sh`（Linux / bash）进入。
- **目标B：两条主链路**  
  - `交易链路`：ingestor -> strategy -> executor  
  - `训练链路`：prepare -> label -> retrain
- **目标C：三层文档**  
  - `Home`：导航  
  - `Architecture`：系统如何工作  
  - `Operations`：怎么跑、怎么修

## 3) 当前复杂度来自哪里

### 3.1 服务面复杂

- 核心服务已经不少：`redis-stack`、`postgres`、`ingestor`、`strategy_engine`、**executor**（旧文档亦称 `rust_executor`）、`anvil`、`dashboard`
- 还有可选链路：`archiver`、各类训练脚本和验证脚本

### 3.2 入口分散

- 用户需要记住多个入口脚本：历史上曾有 `parallel_pipeline.ps1`、`phase6_weekly_cycle.ps1` 等；**Linux 当前以 `scripts/laszlo.sh` 收敛**（同类维护见 `laszlo.sh maintain`）
- 调试脚本、迁移脚本、历史脚本都在 `scripts/` 下，难以第一眼分辨“常用/非常用”

### 3.3 文档层级不够收敛

- 文档内容本身较完整，但缺少“面向日常操作”的单页总览，导致新人仍然要跨多个页面拼图

## 4) 新的简化架构（操作视角）

```text
单入口: scripts/laszlo.sh
  ├── 交易链路（up/status/logs/down）
  │     └── docker compose（infra/compose/compose.yaml）
  ├── 训练链路（phase6）
  │     └── scripts/phase6_weekly_cycle.sh（由 laszlo.sh phase6 调用）
  └── 维护链路（maintain）
        └── laszlo.sh maintain（prune/redis）
```

## 5) 文件收敛策略（不做高风险大迁移）

本次采用“**控制面收敛**”而非“立即大量删文件”：

- 不强行删除历史脚本，避免破坏既有流程
- 用统一入口把常用路径固定下来
- 通过根目录 `Home.md` / `Operations.md` 明确“推荐入口”与“遗留脚本”（仓库不另建 `wiki/` 重复副本，见 `mvp/audits/documentation_path_map.md`）

这样可以先把认知复杂度降下来，再按迭代逐步清理脚本库存。

## 6) 已落地调整（本轮）

1. 新增 **`scripts/laszlo.sh`** 统一入口（EATLQ / Linux）。
2. 更新根目录 **`Home.md`** 与 **`Operations.md`**，把常用入口切换为 **`scripts/laszlo.sh`**。
3. 保留 Windows / 历史脚本引用于旧文档或兼容层，降低切换风险。

## 7) 下一阶段建议（可选）

- 把 `scripts/` 分为 `scripts/core` 与 `scripts/legacy`（目录级整理）  
- 给 legacy 脚本统一加抬头注释 `LEGACY`，并在 README 列迁移映射  
- **`scripts/laszlo.sh doctor` + `make check-readiness`：** 已实现静态 paper/dry-run preflight（`scripts/paper_dryrun_readiness.py`），无 Redis/RPC；**Sprint010** 起可加 `doctor --compose-config` 或 `make check-compose-config` 做 **docker compose config -q**（不 `up`）。见 `Operations.md` §「Paper / dry-run readiness」

