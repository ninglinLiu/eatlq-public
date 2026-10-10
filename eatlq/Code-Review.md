# 代码审查摘要

> **Guard (Phase 3):** Alignment rhetoric needs three lanes—contract audit (`mvp/audits/`)、static tests、**economic readout**. “E2E path exists” ≠ whitepaper parity. **`contract docs override narrative drift`**. **`P0 Blockers: none`** means no exploit proven in **static audit**, not absent operational risk. Model metrics **cannot override** **`FAILED_NO_7D_ALPHA`** (`mvp/audits/failed_no_7d_alpha_guard.md`). This guide is **`not an authorization`** to claim alignment, Phase 6, live, labeling, or training.

- **Whitepaper-related changes**: before implementing or rewording any whitepaper-derived item, check `mvp/audits/whitepaper_v2_engineering_gap_backlog_summary.json`, `mvp/audits/whitepaper_v2_selective_implementation_plan_summary.json`, and `mvp/audits/whitepaper_v2_contract_index.md`; `BLOCKED_BY_FAILED_NO_7D_ALPHA` / `DO_NOT_IMPLEMENT_NOW` items must not be implemented. This checklist is not an authorization.
- **Doc paths**: canonical narrative entrypoints are repo-root `Home.md` / `Architecture.md` / `Operations.md` (no parallel `wiki/` copies per `mvp/audits/documentation_path_map.md`).

**审查范围**：ingestor / execution / archiver / strategy / 基础设施与脚本（截至 wiki 编写时的仓库状态）。  
**结论**：工程管线完整，具备 MVP 与迭代基础；**盈利与安全**仍强依赖外部 RPC 质量、标注数据密度与风控参数，需在 `mvp/` 中用真金白银或小资金验收。

---

## 强项

- **端到端链路清晰**：采集 → Redis → 策略 → 信号 → 执行；离线 dashcam → 清洗 → 标注 → 重训闭环存在。  
- **防御性处理**：策略侧 v2 特征 clip、概率裁剪；执行侧 Quoter 估算 `amountOutMinimum`；Anvil 重启与日志上限降低踩坑成本。  
- **可运维性**：compose 显式 env 校验、日志轮转、dev overlay 减少无效构建。

---

## 风险与债务（按优先级）

### P0 — 真钱或线上正确性

1. **ingestor 上游 429**  
   - 免费/低配 RPC 限流会直接**断流**，与策略好坏无关。  
   - **缓解**：独立 app key、主备端点、监控「最近一条 `market_ticks` 时间」。

2. **密钥与 `.env`**  
   - 私钥与 API key 只存本地/密钥管理；**禁止**进截图与聊天记录长期留存。

3. **执行路径与模式**  
   - `LIVE_TRADING_MODE=true` 前必须再核对单笔仓位、滑点、钱包余额与熔断逻辑（见 `mvp/checklist.md`）。

### P1 — 性能与可扩展性

1. **RiskGuardian**  
   - 流动性等检查为启发式，可能误判；宜配合实盘小额统计假阳性/假阴性。

2. **策略侧 Redis I/O**  
   - FeatureStore 已做 pipeline/短时缓存；高 tick 率下仍可继续 profiling。

3. **compose `deploy.resources`**  
   - 非 Swarm 环境下可能**不生效**；真生产需用引擎原生限制或主机层约束。

### P2 — 可维护性

1. **`ingestor` 与 `archiver` 模型**  
   - 历史上存在字段/模型分叉；archiver 侧已兼容 `data`/`payload`，长期宜单一 schema。

2. **测试覆盖**  
   - 跨服务契约、执行 calldata、重训门禁等仍可补集成测试与 CI。

---

## 建议的后续动作（不写进代码的约定）

- 在 **`mvp/issues.md`** 中跟踪 P0/P1。  
- 周维度在 **`mvp/metrics.md` / `mvp/runs/`** 记录：429 时长、成交数、净 PnL、回撤。  
- Wiki 随架构变更增量更新 **Architecture.md** 与 **Operations.md**。

---

## 深度审查补充（执行 / 风控 / 持仓）

**日期**：2026-04-08  

**路径说明（2026-05-03 对齐仓库）**：本节下列条目保留原audit措辞，其中 Rust 符号名（如 `risk.rs`、`PositionManager`、`warn!`）对应**上游/早期 `crates/execution` 布局**的审查记录。**当前仓库**执行器为 Python：`services/executor/`；链上采集为 Rust：`services/ingestor/`。可执行契约以 `Architecture.md` 与 `docs/executor-language-contract.md` 为准。

**重点文件（当前仓库）**：`services/executor/src/main.py`、`services/executor/src/swap.py`、`services/executor/src/position.py`；`services/strategy/src/feature_store.py`、`main.py`；`services/ingestor/src/main.rs`

### 须优先修正（P0 / 强正确性）

1. **`risk.rs` 流动性检查量纲错误**  
   `liquidity()` 返回的是 Uniswap V3 的 **L**（集中流动性参数），**不是**「ETH 计价深度」。当前实现将 `U256` 按 `1e18` 缩放后与 `trade_size_eth * 20` 比较（`risk.rs` 约 67–76 行），与白皮书的「池子深度」叙事不符，可能 **系统性误判**（误放行或误拒）。应改为基于储备量、`liquidity`+`sqrtPrice` 估算可成交额、或 Quoter 对目标 `amountIn` 的反向估算。

2. **流动性 RPC 失败时默认放行**  
   `check_liquidity_depth` 在 `eth_call` 失败时返回 **true**（注释写 paper mode，但函数无模式参数）。Live 下等同于 **静默绕过** 该检查，建议失败时 **保守拒绝** 或区分 `LIVE_TRADING_MODE`。

3. **`PositionManager::emit_sell_signal` 与 Redis 状态一致性问题**  
   在向 `signals` 写入 SELL 后立即 `close_position`（删除 `positions:*`）。若 Executor 未成交或长时间积压，**失去持仓跟踪**，且可能重复发 SELL 或无法再做风控。建议仅在成交确认后删仓位，或使用「待平仓」状态机。

### 中优先级（P1）

- ~~**执行侧固定 `DEFAULT_FEE = 3000`**~~（**已在代码中解决**：`pick_fee_for_exact_input` 对 3000/10000/500 选 Quoter；`fee_tier` 写入 Redis，SELL 与 BUY 一致。）

- ~~**`RiskGuardian::find_v3_pool` 未尝试 fee 500**~~（**已解决**：`find_v3_pool` 与蜜罐检测均为 3000 → 10000 → 500。）

- **硬编码 `ETH_PRICE_USD = 2300.0`**  
   已支持环境变量；**运行期仍不自动刷新**，名义仓位与熔断会随汇率漂移。建议 Oracle 或运维周期更新。

- ~~**Executor XACK 与毒消息**~~（**已缓解**：坏条目进 `signals_dead_letter` 后再 `XACK`；仍可补 metrics/告警。）

### 策略与采集（P1 / 设计层面）

- **`feature_store` 的 `zcount` 时间窗**  
   使用 `zcount(..., start, now)` 而 member 内含 `ts:...`；score 为 `ts` 时语义正确。需保证 ingestor 的 `timestamp` 与策略机时钟一致（否则窗偏移）。

- ~~**ingestor `PoolCache` 永久跳过**~~（**已改进**：连续失败达到上限后进入 **10 分钟冷却**，到期自动重试；成功发现后清除重试状态。）

### 仍成立的整体判断

- 端到端契约（MsgPack、`payload`、熔断键名）在工程上可对齐；**盈利与安全性**仍强依赖 RPC、模型与上述风控实现的数学/状态正确性。

---

## 复审更新（2026-04-08，Live 前）

### 已完成修复（原 P0）

1. **流动性检查语义修正**：由 `liquidity()` 切换为 `WETH.balanceOf(pool)`，并支持 `live_mode` 分支。  
2. **Live Fail-Safe**：流动性 RPC 失败时，Live 拒绝，Paper 放行。  
3. **持仓删除时机修正**：`emit_sell_signal` 不再删仓；由 Executor 在 SELL 成功后删 `positions:*`。  
4. **SELL 风控路径修正**：BUY 才做流动性/蜜罐检查；SELL 不再被 BUY 风控阻断。  
5. **`pending_sell` 防死锁**：新增 `pending_sell_at` + 60s TTL，超时自动重发 SELL 信号。  
6. **BUY 余额查询 fail-safe（Live）**：`get_balance` RPC 失败时保留 `warn!` 并立即拒绝 BUY；Paper 仍可继续用于调试。
7. **零余额 ghost 清理兜底**：SELL 收到时若 `balanceOf(wallet)=0`，执行器会直接清理 Redis 仓位记录，避免重发环路。
8. **死信流落地**：`signals` 条目缺少 `payload` 或 JSON 解析失败时写入 `signals_dead_letter`，再执行 `XACK`。
9. **ETH 价格参数化**：`ETH_PRICE_USD` 从环境变量读取（无效时回退默认并告警），替代纯硬编码。

### 复审结论（当前）

- **无新增 P0 阻断项**（基于当前 diff 与构建通过结果）。  
- 仍建议在小资金实盘前验证：
  - SELL fail-open 路径（`balanceOf` 失败时仍尝试退出）；
  - `pending_sell` TTL 触发后的重发节奏与幂等性；
  - 多费率池下 BUY/SELL 路由一致性与滑点表现。

### 仍需跟踪的 P1/P2（未关闭）

- `ETH_PRICE_USD` 仍为静态配置（运行期不自动刷新），需由运维按周期更新。  
- dead-letter 已落地，但建议补消费/告警面板，避免仅写不看。

### 增量改进（2026-04-12）

- **蜜罐 fail-safe**：`QUOTER_V2` / `WETH` 常量地址解析失败时 **拒买**（不再误放行）。  
- **PoolCache**：失败冷却 **600s**，避免 RPC 抖动导致「永久漏池」。  
- **置信度单一配置**：新增 `SIGNAL_MIN_CONFIDENCE`（0.0–1.0），**策略**与**执行器**共用默认 0.52；策略在模块加载前 `load_dotenv()` 以读取 `.env`。

### 复审更新（2026-04-15，Phase 6 训练链路）

#### 已完成修复

1. **周周期自动化落地**：新增 `scripts/phase6_weekly_cycle.ps1`，顺序固定为 `prepare_dataset -> label_dashcam -> retrain_model`。  
2. **标注窗口参数化**：`label_dashcam.py` 支持 `LABEL_MAX_AGE_DAYS`（默认 30），避免硬编码 7 天导致训练样本过窄。  
3. **评估口径对齐白皮书**：`retrain_model.py` 采用「训练回看窗口 + 最新 7 天 holdout」切分。  
4. **失败语义收紧**：未达部署门槛或数据门槛时返回非 0 退出码，避免任务计划器“假成功”。

#### 当前仍需跟踪（P1）

1. **标注 RPC 退化路径可观测性不足**  
   `label_dashcam.py` 在主 RPC 健康检查失败后会自动回退到公共端点（`base.llamarpc.com`）。该路径提高了可用性，但在高负载期可能拉长标注时间或导致周任务抖动。建议把“是否进入 fallback”写入独立 run 记录，并在任务层做耗时阈值告警。

## 审查签字区（可选）

| 日期 | 范围 | 备注 |
| ---- | ---- | ---- |
| 2026-04-08 | 全栈摘要 | 首次 wiki 落盘 |
| 2026-04-08 | execution/risk/PM/strategy/ingestor | 深度审查补充见上节 |
| 2026-04-12 | 蜜罐/PoolCache/置信度 env | 见「增量改进（2026-04-12）」 |

---

## 复审更新（2026-04-17，稳定性与限额友好）

### 新发现并已修复

1. **Drawdown 检查 RPC 过频（P1）**  
   `run_sniper` 主循环 `BLOCK=100ms`，原实现每轮都调用 `check_daily_drawdown`，导致高频 `get_balance` RPC 请求，容易放大外部 RPC 限额压力。  
   **修复**：执行器将 drawdown 检查节流为 **每 30 秒一次**（不改变熔断语义）。

2. **executor 启动依赖不完整（P1）**  
   compose **`executor`** 服务（历史文档亦写 `rust_executor`）原先仅依赖 `redis` healthy，若 `anvil` 尚未可用，Paper 启动期会出现 DNS/连接抖动噪声。  
   **修复**：编排文件增加 `executor -> anvil: service_healthy` 依赖（具体文件名以 `infra/compose/compose.yaml` 为准）。

3. **ingestor 误依赖 anvil（P1）**  
   `ingestor` 是链上数据采集服务，仅依赖 Redis 与外部 WS/RPC；若错误依赖 `anvil` healthy，会在本地 fork 故障时阻断采集启动，放大故障面。  
   **修复**：移除 `ingestor -> anvil` 依赖，恢复解耦。

4. **Phase6 编排脚本退出码丢失（P1）**  
   `phase6_weekly_cycle.ps1` 原先在任意失败场景统一 `exit 1`，会覆盖 `retrain_model.py` 的 `exit 2/3` 语义，导致任务计划器无法区分“质量未达标”与“编排异常”。  
   **修复**：编排脚本按错误消息中的 `exit=<code>` 透传子步骤退出码。

5. **archiver 坏消息未 ACK（P1）**  
   `market_ticks` 条目若缺少 `payload/data` 字段，archiver 仅告警不 ACK，会把垃圾条目留在 PEL（pending entries list）中，长期可能造成消费组积压与观测噪声。  
   **修复**：对缺失字段的坏条目执行 `XACK`（与反序列化失败路径保持一致）。

6. **Dashboard Redis 流字段兼容不完整（P2）**  
   `src/database_api.py` 的实时读取此前仅取 `data/tick`，对历史 `payload` 条目会静默丢失。  
   **修复**：读取路径统一兼容 `data/payload/tick`。

### 当前结论

- 在 **Alchemy key 超额** 场景下，系统仍会因外部配额受限无法进入正常交易态；但本轮已清理掉内部可控的启动链路与无谓 RPC 压力问题。  
- 建议后续补充 dead-letter 监控面板和 drawdown 触发指标，以便长期回归验证。
