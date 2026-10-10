# EATLQ Agent Handoff

## 1. 当前任务目标

Phase 1、Phase 2 和 Phase 3 bridge-to-action runtime 最小闭环已全部完成并在 VPS 验证。

当前现网已稳定的主链路是：

`newHeads websocket -> eth_getLogs HTTP pull -> ingestor -> Redis Stream market_ticks -> archiver -> ClickHouse raw_evm_logs`

Phase 3 runtime bridge 已完成并 VPS 验证：
- 3A `scripts/extract_training_data.py`：训练数据提取，578 行，chain_id=1 (Mainnet)
- 3B `scripts/train_model.py`：XGBoost PASS，accuracy=0.621，AUC-ROC=0.797，`models/signal_model_v1.json` 已落盘
- 3C `services/strategy/src/feature_store.py`：Redis 5分钟滑动窗口特征层，deterministic unit test 通过
- 3D `services/strategy/src/main.py`：策略引擎，消费 `market_ticks`，发布信号到 `signals` stream，SYSTEM_PANIC gate 已接入
- 3E `logs/dashcam.csv`：dashcam 反馈闭环集成在 3D 内，final_score > 0.40 的候选全部记录

下一位 agent 的默认目标应当是：

- 保持 Phase 1/2 现网链路不变，不动 `ingestor`/`archiver`
- Phase 3 runtime 已闭环，**不要继续扩 SQL-only 语义层**
- **Phase 3.5 已实施**：FeatureStore 现已解码原始 EVM 日志中的 swap 金额（V2/V3/Aerodrome）；4 维特征全部有效；详见 `docs/model-feature-gap.md`
- 部署步骤：(1) VPS apply `025_realtime_training_views.sql`，(2) 运行 `run-realtime-training.sh` 训练实时模型，(3) 重启 strategy 加载新模型
- Ingestor healthcheck 已从 file-existence 升级为 file-freshness（3 分钟超时自动重启）
- 当前唯一保留 warning（`historical_unprojectable_raw:erc20_transfers`）已解释清楚，不要继续 rebuild 修复
- 如果实时模型训练 PASS，可启动 Phase 4 执行层设计

**最新完成（2026-03-28）：Phase 3.5 FeatureStore 修复 + 实时训练管线 + Ingestor 稳定性**

- **FeatureStore 事件解码**：`feature_store.py` 根据 `topic0` 解码 `data` 字段，V2/Aerodrome vAMM 提取 4 个 uint256 金额，V3 提取 2 个 int256 金额；4 个运行时特征全部产生有意义的值
- **ClickHouse 历史特征视图**：`025_realtime_training_views.sql` 新增 `pool_features_5m`（复制运行时特征逻辑）和 `pool_realtime_training_v1`（self-join 构造未来买压标签）
- **训练管线**：新增 `extract_realtime_training_data.py`；`train_model.py` 新增 `--features realtime --label label` 模式；`run-realtime-training.sh` 一键训练
- **模型加载优先级**：`ModelScorer` 最优先加载 `signal_model_realtime_v*.json`
- **黑名单调整**：默认黑名单清空（FeatureStore 跟踪 pool 地址非 token 地址）；新增 `STRATEGY_POOL_BLACKLIST` 环境变量
- **tick_scored 日志**：`token_address` 重命名为 `pool_address`
- **Ingestor healthcheck**：从 `test -f` 升级为 `find -mmin -3`，3 分钟无更新自动标记 unhealthy 并重启

**最新完成（2026-03-25）：Base L2 Aerodrome 池扩展已落地并验证**

- `006_registry.sql` 已新增 cbBTC token + 2个 vAMM 池 + 3个 CL 池（共5个 Aerodrome 池）
- `LOG_FILTER_JSON` 已扩展到 10 个地址，新增 Aerodrome vAMM Swap topic `0xb3e2773...`
- `EVM_HTTP_URL` 已从 QuickNode 切换至 `https://mainnet.base.org`（根因：QuickNode 免费计划限制 eth_getLogs 地址数量上限约为 5）
- 所有 9 个 Base 池均已达到 30+ 天覆盖，ClickHouse 总数据量约 6.91M 条原始日志
- **Base L2 数据现已可用于 Phase 3 retrain**

**最新完成（2026-03-25 #2）：多链训练 + Aerodrome vAMM normalized 接入 + 策略引擎链感知模型**

- **策略引擎链感知模型加载**：`ModelScorer` 现在接收 `chain_id`，当 `chain_id=8453` 时优先加载 `signal_model_base_v*.json`，找不到时回退到通用 `signal_model_v*.json`；启动日志增加 `model_path` 字段
- **多链合并训练**：`extract_training_data.py` 新增 `--chain-ids` 参数（逗号分隔），可一次提取多链数据合并训练；CSV 新增 `chain_id` 列
- **训练脚本链标记**：`train_model.py` 新增 `--tag` 参数，自动重命名输出文件（如 `--tag base` → `signal_model_base_v1.json`）
- **run-phase3-training.sh 支持链参数**：`bash scripts/run-phase3-training.sh 8453` 自动提取 Base 数据并输出 `signal_model_base_v1.json`
- **run-multichain-training.sh**：新增多链合并训练脚本，`bash scripts/run-multichain-training.sh 1,8453` 提取两链数据合并训练
- **Aerodrome vAMM Swap 接入 normalized 层**：`003_normalized_events.sql` 新增 `aerodrome_vamm_swaps_mv` 物化视图，将 topic `0xb3e2773...` 路由到 `uniswap_v2_swaps` 表（数据布局与 Uniswap V2 Swap 完全相同）
- **rebuild/verify/healthcheck 同步更新**：`rebuild-normalized-surface.sh`、`verify-normalized-events.sh`、`phase2-healthcheck-strict.sh`、`010_operational_status_views.sql`、`inspect-raw-duplicate-origins.sh` 的 raw V2 swap 计数已扩展为 `topic0 IN (V2, Aerodrome vAMM)` 以保持 parity 校验正确
- **label_balance OOM 修复**：`extract_training_data.py` 的 label_balance 诊断查询包裹在 try/except 中，OOM 时打印 WARNING 并继续，不再中断数据提取
## 2. 已完成事项

### 2.1 Phase 1 基础设施与主链路

- `infra/compose/compose.yaml` 已包含 `redis`、`clickhouse`、`postgres`、`ingestor`、`archiver`
- `infra/compose/.env.example` 已对齐当前运行模式和当前关键变量
- `scripts/dev-up.sh` 和 `scripts/init-streams.sh` 可用于本地或 VPS 启动
- `services/ingestor` 已从原始 `eth_subscribe logs` 路径切换到 `newHeads + eth_getLogs`
- `services/archiver` 已稳定消费 Redis Streams 并写入 `eatlq.raw_evm_logs`
- `services/archiver` 保持“先写 ClickHouse，再 XACK”
- Redis pending recovery 已实现并验证

### 2.2 Ingestor 稳定性

- `services/ingestor/src/ws.rs` 具备 reconnect 和 subscribe 失败回退
- `services/ingestor/src/ws.rs` 用 `try_send` 做背压降级，避免 websocket reader 被内部阻塞
- `services/ingestor/src/codec.rs` 会过滤非 `eth_subscription` 的无效消息
- `services/ingestor/src/rpc.rs` 会按新区块执行 `eth_getLogs`
- `services/ingestor/src/rpc.rs` 已把预期确认包降噪
- `services/ingestor/src/config.rs` 支持 `EVM_HTTP_URL`、`LOG_FILTER_JSON`、`RPC_REQUEST_TIMEOUT_SECS`

### 2.3 Raw 数据契约与归档

- `crates/common/src/types.rs` 中 `IngestRecordV1.received_at_ms` 已成为 guaranteed archival timestamp
- `infra/clickhouse/init/001_raw_events.sql` 已明确 `received_at_ms` 是归档时钟
- `block_time` 已允许为空
- `services/archiver/src/clickhouse_sink.rs` 会 opportunistic 从 `payload_json` 提取 `blockTimestamp`
- `eatlq.raw_evm_logs` 已在 VPS 上持续增长

### 2.4 Backfill / Replay / State 层

- dedicated one-shot `backfill` worker 已实现
- Postgres-backed checkpoint / gap state 已实现
- `backfill_enqueue_gap` 已实现
- `backfill_discover_gaps` 已实现
- realtime `ingestor` head watermark state 已实现
- `backfill_scheduler` 已实现
- `backfill_scheduler` 在 VPS 上已完成真实 enqueue -> claim -> complete 验证

### 2.5 Backfill / State 相关表

- `app.backfill_jobs`
- `app.backfill_gap_ranges`
- `app.backfill_chunk_progress`
- `app.ingestor_head_state`

这些表已经在 VPS 上实际落库并验证过：

- gap 可入队
- scheduler 可认领
- chunk progress 可记录
- completed / failed 状态可更新
- realtime head state 可写入

### 2.6 第一期 normalized event tables

以下表已经实现并在 VPS 上跑通：

- `eatlq.erc20_transfers`
- `eatlq.uniswap_v2_swaps`
- `eatlq.uniswap_v3_swaps`

这些表的特点：

- 由 `raw_evm_logs` 投影而来
- 保留 `payload_json` 便于追溯
- 地址统一 lowercase
- 数值字段第一版保留 raw hex，避免过早做脆弱 SQL 数值解码

### 2.6.1 第二期 liquidity lifecycle normalized tables

以下表已经实现并在 VPS 上 apply / rebuild / verify：

- `eatlq.uniswap_v2_mints`
- `eatlq.uniswap_v2_burns`
- `eatlq.uniswap_v3_mints`
- `eatlq.uniswap_v3_burns`

这些表的特点：

- 仍然由 `raw_evm_logs` 下游投影而来
- 不改 live ingest path，只扩展 downstream projection
- V3 的 `tickLower` / `tickUpper` 暂时保留为 raw hex
- `LOG_FILTER_JSON` 已同步纳入 V2/V3 `Mint` / `Burn` topics

### 2.7 第一期 deduped query views

以下 deduped views 已实现并在 VPS 上验证：

- `eatlq.erc20_transfers_deduped`
- `eatlq.uniswap_v2_swaps_deduped`
- `eatlq.uniswap_v3_swaps_deduped`

这些视图会：

- 以 normalized key 聚合
- 暴露 `duplicate_rows`
- 暴露 `latest_ingested_at`
- 作为更安全的默认查询面

### 2.7.1 第二期 liquidity lifecycle deduped views

以下视图已经实现并在 VPS 上 apply / verify：

- `eatlq.uniswap_v2_mints_deduped`
- `eatlq.uniswap_v2_burns_deduped`
- `eatlq.uniswap_v3_mints_deduped`
- `eatlq.uniswap_v3_burns_deduped`

### 2.8 第一期 activity summary views

以下 derived hourly activity views 已实现：

- `eatlq.erc20_transfers_hourly`
- `eatlq.uniswap_v2_swaps_hourly`
- `eatlq.uniswap_v3_swaps_hourly`

这些视图基于 deduped data 做小时级活动聚合，可直接支撑：

- top token inspection
- top V2 pair inspection
- top V3 pool inspection

### 2.8.1 第二期 liquidity lifecycle activity views

以下视图已经实现并在 VPS 上 apply / verify：

- `eatlq.uniswap_v2_mints_hourly`
- `eatlq.uniswap_v2_burns_hourly`
- `eatlq.uniswap_v3_mints_hourly`
- `eatlq.uniswap_v3_burns_hourly`

这些视图可直接支撑：

- 最近 N 小时最活跃 liquidity add/remove pair/pool inspection
- mint / burn 的活跃度与去重压力巡检

### 2.8.2 liquidity lifecycle VPS 验证结果

刚完成的 VPS 验证已确认：

- `scripts/apply-clickhouse-init.sh` 可成功应用 `mint` / `burn` DDL
- `scripts/backfill-normalized-events.sh` 可成功重建新表
- 最近 30 分钟窗口中：
  - `raw_v2_swap == normalized_v2_swap`
  - `raw_v3_swap == normalized_v3_swap`
  - `raw_v2_mint == normalized_v2_mint == 0`
  - `raw_v2_burn == normalized_v2_burn == 0`
  - `raw_v3_mint == normalized_v3_mint == 0`
  - `raw_v3_burn == normalized_v3_burn == 0`
- dedupe 汇总显示：
  - `v2_mint_rows = 4` 且 surplus 为 `0`
  - `v2_burn_rows = 1` 且 surplus 为 `0`
  - `v3_mint_rows = 21` 且 surplus 为 `0`
  - `v3_burn_rows = 32` 且 surplus 为 `0`
- `inspect-normalized-duplicates.sh v3-mint 20` 与 `v3-burn 20` 均为空

### 2.8.3 Phase 3 hex decode layer

以下只读 decode 层、decimal-aware semantic amount views、trade-oriented semantic views、第一批最小 wallet profiling views、第一版 wallet feature views、第一版 wallet behavior / preference views、第一版 sender-day 训练数据视图、第一版未来窗口 outcome label 视图、第一版成熟样本 / 训练分桶视图、第一版训练质量 summary views，以及第一版训练就绪 / label balance views 已在仓库中实现并完成 VPS 验证；同时第一版在线推理输入视图已在仓库中实现，尚待下一轮 VPS apply / verify：

- `infra/clickhouse/init/013_decoded_views.sql`
- `eatlq.erc20_transfers_decoded`
- `eatlq.uniswap_v2_swaps_decoded`
- `eatlq.uniswap_v3_swaps_decoded`
- `eatlq.uniswap_v2_mints_decoded`
- `eatlq.uniswap_v2_burns_decoded`
- `eatlq.uniswap_v3_mints_decoded`
- `eatlq.uniswap_v3_burns_decoded`
- `scripts/verify-decoded-values.sh`
- `infra/clickhouse/init/014_semantic_amount_views.sql`
- `eatlq.erc20_transfers_semantic`
- `eatlq.uniswap_v2_swaps_semantic`
- `eatlq.uniswap_v2_mints_semantic`
- `eatlq.uniswap_v2_burns_semantic`
- `eatlq.uniswap_v3_swaps_semantic`
- `eatlq.uniswap_v3_mints_semantic`
- `eatlq.uniswap_v3_burns_semantic`
- `scripts/verify-semantic-amounts.sh`
- `infra/clickhouse/init/015_semantic_trade_views.sql`
- `eatlq.uniswap_v2_swaps_trade_semantic`
- `eatlq.uniswap_v3_swaps_trade_semantic`
- `scripts/verify-semantic-trades.sh`
- `infra/clickhouse/init/016_wallet_profile_views.sql`
- `eatlq.swap_trade_events_all`
- `eatlq.sender_swap_profile`
- `eatlq.sender_token_trade_profile`
- `scripts/verify-wallet-profiles.sh`
- `infra/clickhouse/init/017_wallet_feature_views.sql`
- `eatlq.sender_venue_trade_profile`
- `eatlq.sender_feature_store_v1`
- `scripts/verify-wallet-features.sh`
- `infra/clickhouse/init/018_wallet_behavior_views.sql`
- `eatlq.sender_preference_profile_v1`
- `eatlq.sender_behavior_labels_v1`
- `scripts/verify-wallet-behavior.sh`
- `infra/clickhouse/init/019_training_dataset_views.sql`
- `eatlq.sender_trade_day_rollup_v1`
- `eatlq.sender_training_dataset_v1`
- `scripts/verify-training-dataset.sh`
- `infra/clickhouse/init/020_outcome_label_views.sql`
- `eatlq.sender_forward_outcome_labels_v1`
- `eatlq.sender_training_examples_v1`
- `scripts/verify-outcome-labels.sh`
- `infra/clickhouse/init/021_training_split_views.sql`
- `eatlq.sender_training_examples_matured_v1`
- `eatlq.sender_training_examples_split_v1`
- `scripts/verify-training-splits.sh`
- `infra/clickhouse/init/022_training_quality_views.sql`
- `eatlq.sender_training_split_summary_v1`
- `eatlq.sender_training_daily_dataset_summary_v1`
- `scripts/verify-training-quality.sh`
- `infra/clickhouse/init/023_training_readiness_views.sql`
- `eatlq.sender_training_examples_ready_v1`
- `eatlq.sender_training_label_balance_v1`
- `scripts/verify-training-readiness.sh`
- `infra/clickhouse/init/024_inference_input_views.sql`
- `eatlq.sender_inference_candidates_v1`
- `eatlq.sender_inference_cohort_summary_v1`
- `scripts/verify-inference-inputs.sh`

这一层的边界：

- 只基于 deduped views 做查询层解码
- 不改任何 Phase 2 已有表、视图、materialized view、或运维脚本
- 不改 `ingestor` / `archiver`
- 当前只验证 decode correctness，不引入新的物化表或 live write path
- 当前 repo 已支持对 V3 `tickLower` / `tickUpper` 做 `Int32` 查询层解码
- semantic amount views 当前使用 registry decimals 做只读归一化金额，不回写底层表
- trade semantic views 当前只覆盖 swap surfaces，用于暴露 direction / sold / bought 语义，不改底层 amount contract
- wallet profile 当前只基于已验证的 swap trade semantic views，按 `sender_address` 做最小活动画像，不直接声称 entity / PnL
- wallet feature views 当前只提供基于 `sender` 的 venue concentration、token concentration、activity cadence、cross-protocol 等第一版只读特征，不直接声称 alpha / PnL / entity attribution
- wallet behavior views 当前只把已验证的 sender features 压成 top venue / top token / protocol preference / concentration band / activity band 等可解释标签，不直接产生交易信号
- training dataset views 当前只提供基于 `sender x day` 的只读样本面，目标是给后续训练集抽样与规则回测提供稳定输入，不直接产生监督标签
- outcome label views 当前只提供基于 `t+1 day` 的未来窗口活跃度与 protocol outcome 标签，用于后续监督目标定义，不直接表示收益或真实 alpha
- training split views 当前只提供成熟样本过滤与稳定 train/validation/test 分桶，不直接做模型训练或结果持久化
- training quality views 当前只提供对 supervised surface 的样本量、split 分布和 label 覆盖率 summary，不直接做训练或评估
- training readiness views 当前只提供监督目标类别、label balance 与 cohort-ready 入口，不直接触发训练任务
- inference input views 当前只提供最新交易日的候选打分输入与 cohort summary，不直接产生模型分数或信号

### 2.9 VPS 上已经明确验证过的结果

已明确验证：

- `ingestor` 持续出现 `fetched block logs via eth_getLogs`
- Redis `market_ticks` 增长
- `archiver` 持续写入 ClickHouse
- `backfill_scheduler` 能认领 pending gap 并完成
- normalized tables 建表成功
- normalized tables 历史回灌成功
- 30 分钟窗口内 raw 与 normalized 实时增量计数对齐
- deduped views 可用
- V3 存在 5 条重复富余行时，deduped views 能把它们压到唯一 key
- 重复来源已确认主要来自 `quicknode` 与 `quicknode-backfill` 的重叠
- `scripts/backfill-normalized-events.sh` 经改造后可按 normalized key 重建并清掉那 5 条 V3 重叠
- 重建后 `v3_duplicate_surplus` 变为 `0`
- activity view 可正常查询最近 24 小时活跃 V3 pool

### 2.11 Base L2 Aerodrome 池扩展（2026-03-25 落地）

**背景**：Base 链 (chain_id=8453) 原本只有 5 个 Uniswap V3/V2 池。本次扩展加入 Aerodrome Finance（Base 上最大 DEX，Velodrome/Solidly fork）的核心池，覆盖 AERO 生态主要交易对。

**新增内容**：

`infra/clickhouse/init/006_registry.sql` 新增：
- token_registry：`cbBTC`（Coinbase Wrapped BTC，decimals=8，`0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf`）
- uniswap_v2_pair_registry（映射 Aerodrome vAMM 池）：
  - `AERO/USDC [Aerodrome vAMM]`：`0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d`
  - `AERO/WETH [Aerodrome vAMM]`：`0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6`
- uniswap_v3_pool_registry（映射 Aerodrome CL 池）：
  - `AERO/WETH [Aerodrome CL]`：`0x82321f3beb69f503380d6b233857d5c43562e2d0`
  - `AERO/cbBTC [Aerodrome CL]`：`0xdfe5f275020def30993f042174fc2d335678b626`
  - `AERO/USDC [Aerodrome CL]`：`0xbe00ff35af70e8415d0eb605a286d8a45466a4c1`

`infra/compose/.env.example` 中 `LOG_FILTER_JSON` 新增：
- 5 个 Aerodrome 池地址（地址总数从 5 扩展到 10）
- 新 topic：`0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b`（Aerodrome vAMM Swap，3 topics + 128 bytes data）

**Aerodrome 事件 topic 对照表**（实测于 Base 公共 RPC）：

| topic0 | 含义 | n_topics | data |
|---|---|---|---|
| `0xb3e2773...` | Aerodrome vAMM Swap | 3 | 128B |
| `0x7a53080b...` | Aerodrome CL Swap（与 Uniswap V3 Swap 相同）| 4 | 128B |
| `0x0c396cd...` | Aerodrome CL PositionModified（Mint/Burn） | 4 | 96B |
| `0x70935338...` | Aerodrome CL PositionModified（对称） | 4 | 96B |
| `0xcf2aa508...` | Aerodrome vAMM Sync | 1 | 64B |
| `0x112c2569...` | Aerodrome vAMM Fees | 2 | 64B |

**VPS 现网已配置**：

- VPS `.env` 中 `LOG_FILTER_JSON` 已手动同步 10 个地址 + 8 个 topic（包括 `0xb3e2773...`）
- `EVM_HTTP_URL` 已切换至 `https://mainnet.base.org`（QuickNode 保留 WebSocket `EVM_WS_URL` 用于新块订阅，不受限）
- 30 天历史回补已完成（`BACKFILL_FROM_BLOCK=42525709`，公共 RPC，chunk_size=500）

**最终 Base 池覆盖状态（验收时快照）**：

```
WETH/USDC 0.30%            2,976,563 events  34.2 days
WETH/USDC 0.05%            2,399,300 events  34.2 days
AERO/WETH [Aerodrome CL]     515,412 events  30.1 days
AERO/cbBTC [Aerodrome CL]    289,663 events  30.1 days
WETH/USDbC 0.05%             281,809 events  34.2 days
AERO/USDC [Aerodrome vAMM]   145,168 events  30.1 days
AERO/USDC [Aerodrome CL]      93,453 events  30.1 days
AERO/WETH [Aerodrome vAMM]    25,500 events  30.1 days
cbETH/WETH 0.05%               1,452 events  30.0 days
```

### 2.10 关键结论

当前结束时，Phase 2 已不再只是“有 replay / 有 raw archive”：

- 已经有可靠 replay state
- 已经有 pending gap scheduler
- 已经有 normalized event tables
- 已经有 deduped query views
- 已经有 duplicate inspection tooling
- 已经有 hourly activity summary views
- 已经有 registry / enriched / candidate ranking 查询面
- 已经有 status / alert / remediation / cron monitor 运维出口
- 已经有 temp-table + row-count validation + atomic rename swap 的安全 rebuild / backfill 路径

决定性结论：

- `Phase 2` 数据面已经在 VPS 上封板
- `scripts/phase2-healthcheck-strict.sh 30 300 1` 持续通过
- `scripts/phase2-monitor-cron.sh` 已在 VPS 上验证可用，summary 与退出码正确
- 仅剩 `historical_unprojectable_raw:erc20_transfers` 一个 warning
- 该 warning 已通过 `scripts/inspect-erc20-unprojectable-raw.sh` 定性为 `data = 0x` 的 `Transfer` topic 碰撞，来源全为 `quicknode`，更像 NFT / 非 ERC20 日志，不是 ERC20 value payload 丢失
- 下一位 agent 默认应切到 `Phase 3`，而不是继续在 `Phase 2` 打转

## 3. 未完成事项

### 3.1 Phase 2 仍未完成的核心项

- 没有动态 pool registry
- 虽然已有 static registry，但还没有更丰富的 token / pool 元信息 registry
- `value_usd` 仍然是 `NULL`
- 没有更强的物理去重存储策略
- dedupe 仍主要靠 query views 和 rebuild script
- 没有 dead-letter queue
- 没有更强的 replay overlap prevention 机制

### 3.2 数据语义层未完成项

- normalized 表里的数值字段底层仍是 raw hex
- deduped 下游已开始提供只读 decoded views，semantic views 也已开始提供 decimal-aware amount decoding
- normalized 表底层仍保留 raw hex，但 decoded views 已开始提供 V3 `tickLower` / `tickUpper` 的查询层 decode helper
- enriched views 已可暴露静态 symbol / decimals / pair/pool label，但底层 normalized tables 仍未内建 enrichment
- wallet/entity 层已开始有最小 sender-based profiling，但还没有 entity clustering / attribution / PnL
- semantic trade sizing 已开始在 swap trade semantic views 中提供第一版 sold/bought amount
- feature store 已开始提供第一版 sender-level 行为特征，但还没有 label、训练集或在线推理链路
- sender-level behavior labeling 已开始提供第一版可解释分群标签，但还没有监督标签、收益标签或样本切片策略
- training dataset definition 已开始提供第一版 sender-day 样本切片，但还没有收益标签、future window、或 train/validation split 约定
- outcome label definition 已开始提供第一版 `t+1 day` 活跃度 / protocol 目标，但还没有收益标签、PnL 标签或 horizon 选择策略
- dataset split definition 已开始提供第一版 mature/train/validation/test 切分，但还没有时间穿越防护策略或更严格的 cohort 版本化
- training quality monitoring 已开始提供第一版 split/day summary，但还没有 feature drift、label imbalance 报警或版本化快照
- training readiness monitoring 已开始提供第一版 supervised target class 与 label balance 视图，但还没有自动基线阈值或报警
- inference input definition 已开始提供第一版最新日候选输入与 gate summary，但还没有真实模型打分或 signal contract

### 3.3 策略层未完成项

- 只有第一版只读 feature / behavior / training-dataset / outcome-label / split / quality / readiness / inference-input views，还没有可持久化的 feature serving / snapshot 机制
- 没有收益标签 / PnL 标签 / 多 horizon future outcome 定义
- 没有 inference service
- 没有 signal stream
- 没有 risk engine
- 没有 execution engine

### 3.4 运维面未完成项

- 没有长期 dashboard
- 已有基于 `010/011` 的 hardcoded alert 规则，但还没有外部可配置阈值
- 已有 `phase2-monitor-cron.sh` 最小监控出口，但还没有接入长期 dashboard / 外部 alerts
- 已有 duplicate anomaly inspection tooling，但没有长期自动 duplicate anomaly 监控
- 没有 scheduler / replay 统计面板
- `phase2-healthcheck.sh` 与 `phase2-healthcheck-strict.sh` 已可稳定验收现网

## 4. 关键约束 / 不能破坏的假设

### 4.1 不要破坏运行模式

- 不要把 `services/ingestor` 改回 `eth_subscribe logs`
- 当前稳定模式是 websocket 只订阅 `["newHeads"]`
- 日志过滤必须通过 HTTP JSON-RPC `eth_getLogs`
- `WS_SUBSCRIBE_METHOD=eth_subscribe` 与 `WS_SUBSCRIBE_PARAMS_JSON=["newHeads"]` 这组语义不能乱动

### 4.2 不要破坏数据边界

- `received_at_ms` 是归档时钟，不能改成别的
- `block_time` 允许为空，不能重新假设它恒定存在
- `market_ticks` 是 `ingestor -> archiver` 的 durable handoff
- `archiver` 必须保持“ClickHouse insert 成功后才 XACK”

### 4.2.1 QuickNode 免费计划的 eth_getLogs 限制

**已确认的限制（2026-03-25 实测）**：
- eth_getLogs 块范围限制：≤5 块（`-32615 error: eth_getLogs is limited to a 5 range`）
- eth_getLogs 地址数量限制：实测约 ≤5 个地址（超出后静默截断，不报错，导致多余地址的事件丢失）

**后果**：如果 `LOG_FILTER_JSON` 中 address 数组超过 5 个而继续使用 QuickNode HTTP URL，后面的地址事件将被静默丢弃，不会报错，极难排查。

**当前决策**：
- `EVM_HTTP_URL` 已切换至 `https://mainnet.base.org`（公共 RPC，无地址数量限制）
- `EVM_WS_URL` 继续保留 QuickNode WebSocket（只用于 `newHeads` 订阅，不调用 eth_getLogs，不受限）
- **如果未来更换 RPC，必须验证 eth_getLogs 对多地址过滤的支持**

### 4.3 不要破坏 replay 隔离

- `backfill` 和 realtime `ingestor` 要保持职责分离
- 回补失败不能中断 live ingest
- `backfill_scheduler` 不应替代 realtime path

### 4.4 不要误解 ClickHouse init 行为

- `infra/clickhouse/init/*.sql` 在已有卷上不会自动重放
- 现网已有卷必须用 `bash scripts/apply-clickhouse-init.sh`
- 如果 agent 新增 ClickHouse DDL，必须记得更新运维验证路径

### 4.5 不要误用 normalized rebuild 脚本

- `bash scripts/backfill-normalized-events.sh` 会先 `TRUNCATE` normalized tables
- 它是重建工具，不是轻量增量同步工具
- 目前它适合在已有历史 raw 数据基础上做安全重建

### 4.6 不要误判重复来源

- 当前已经确认的一批重复主要来自 `quicknode` 与 `quicknode-backfill` 的重叠
- 不要直接把 dedupe 结果误判成 realtime path 自己在疯狂重复写
- 如需再判断来源，优先用 `bash scripts/inspect-raw-duplicate-origins.sh`

### 4.7 提交与推送约束

- 用户明确要求：以后改完就推送
- 不要提交 `.claude/`
- 不要顺手带上无关文件
- 每次结束前先看 `git status`

## 5. 修改过的文件及原因

### 5.1 当前长期关键文件

- `crates/common/src/types.rs`
  - 保留 `received_at_ms`
  - 维持 raw event contract

- `services/ingestor/src/config.rs`
  - 支持 HTTP RPC、filter、backfill、discovery、scheduler 相关配置

- `services/ingestor/src/ws.rs`
  - live websocket 稳定性修复

- `services/ingestor/src/rpc.rs`
  - `newHeads -> eth_getLogs` 逻辑
  - head watermark 持久化

- `services/ingestor/src/backfill.rs`
  - one-shot replay 主逻辑

- `services/ingestor/src/backfill_state.rs`
  - Postgres-backed replay ledger / checkpoint / gap state

- `services/ingestor/src/backfill_discovery.rs`
  - candidate gap discovery

- `services/ingestor/src/backfill_scheduler.rs`
  - pending gap scheduler

- `services/archiver/src/clickhouse_sink.rs`
  - raw row 映射
  - `blockTimestamp` opportunistic extraction

- `infra/compose/compose.yaml`
  - 现网 compose shape
  - `backfill` / `backfill-scheduler` / related services

- `infra/compose/.env.example`
  - 当前所有关键 env 示例
  - **2026-03-25 更新**：`LOG_FILTER_JSON` 扩展至 10 个地址（含 5 个 Aerodrome 池），topics 扩展至 8 个（含 Aerodrome vAMM Swap `0xb3e2773...`）
  - **注意**：VPS 现网 `.env` 中 `EVM_HTTP_URL` 已改为 `https://mainnet.base.org`，但 `.env.example` 中该值仍为占位符，不直接同步 VPS 运行时值

### 5.2 ClickHouse DDL 相关文件

- `infra/clickhouse/init/001_raw_events.sql`
  - raw archive table

- `infra/clickhouse/init/002_hourly_mv.sql`
  - raw hourly aggregate

- `infra/clickhouse/init/003_normalized_events.sql`
  - `erc20_transfers`
  - `uniswap_v2_swaps`
  - `uniswap_v3_swaps`
  - `uniswap_v2_mints`
  - `uniswap_v2_burns`
  - `uniswap_v3_mints`
  - `uniswap_v3_burns`

- `infra/clickhouse/init/004_normalized_dedup_views.sql`
  - deduped query views
  - 含 liquidity lifecycle dedupe views

- `infra/clickhouse/init/005_normalized_activity_views.sql`
  - hourly activity summary views
  - 含 liquidity lifecycle activity views

- `infra/clickhouse/init/006_registry.sql`
  - static token registry
  - static Uniswap V2 pair registry（含 Aerodrome vAMM 池，fee_bps=30）
  - static Uniswap V3 pool registry（含 Aerodrome CL 池，fee_bps=100）
  - unified pool registry
  - **2026-03-25 更新**：新增 cbBTC token；新增 AERO/USDC vAMM、AERO/WETH vAMM、AERO/WETH CL、AERO/cbBTC CL、AERO/USDC CL 五个池；Aerodrome vAMM 映射到 `uniswap_v2_pair_registry`，Aerodrome CL 映射到 `uniswap_v3_pool_registry`（结构兼容）

- `infra/clickhouse/init/007_registry_enriched_views.sql`
  - registry-backed enriched query views

- `infra/clickhouse/init/008_registry_gap_views.sql`
  - registry gap summary
  - registry candidate ranking summary

- `infra/clickhouse/init/009_dedupe_audit_views.sql`
  - duplicate anomaly summary / drilldown views

- `infra/clickhouse/init/010_operational_status_views.sql`
  - raw/status views
  - 包含 raw/projectable/unique/duplicate/unprojectable 口径

- `infra/clickhouse/init/011_phase2_alert_views.sql`
  - alert summary / alert events
  - remediation target / command

- `infra/clickhouse/init/012_hourly_mv_cleanup.sql`
  - 给已有 VPS 卷移除 `unique_address_state`
  - 重建 `raw_evm_logs_hourly_mv`

- `infra/clickhouse/init/013_decoded_views.sql`
  - 新增 `hex -> UInt256 / Int256` SQL helper function
  - 新增基于 deduped views 的 decoded query views
  - 当前只覆盖 `erc20_transfers`、`uniswap_v2_swaps`、`uniswap_v3_swaps`
- `infra/clickhouse/init/014_semantic_amount_views.sql`
  - 新增 decimal-aware semantic amount views
  - 把 decoded integer amount 与 registry decimals 组合成只读 normalized amount
- `infra/clickhouse/init/015_semantic_trade_views.sql`
  - 新增 swap trade semantic views
  - 暴露 `swap_direction`、`sold_*`、`bought_*` 查询层语义
- `infra/clickhouse/init/016_wallet_profile_views.sql`
  - 新增最小 sender-based wallet profile views
  - 暴露 sender 级别的 swap profile 与 token trade profile
- `infra/clickhouse/init/017_wallet_feature_views.sql`
  - 新增第一版 wallet feature store views
  - 暴露 venue concentration、token concentration、activity cadence 等 sender-level 只读特征
- `infra/clickhouse/init/018_wallet_behavior_views.sql`
  - 新增第一版 wallet behavior / preference views
  - 暴露 top venue、top token、protocol preference、concentration band、activity band 等 sender-level 可解释标签
- `infra/clickhouse/init/019_training_dataset_views.sql`
  - 新增第一版 sender-day training dataset views
  - 暴露日级 trade count、diversity、concentration、protocol preference 等训练样本特征
- `infra/clickhouse/init/020_outcome_label_views.sql`
  - 新增第一版未来窗口 outcome label views
  - 暴露 `t+1 day` 活跃度、protocol outcome 与 training example 拼接面
- `infra/clickhouse/init/021_training_split_views.sql`
  - 新增第一版成熟样本 / 训练分桶 views
  - 暴露成熟样本过滤与稳定 train/validation/test 分桶结果
- `infra/clickhouse/init/022_training_quality_views.sql`
  - 新增第一版训练质量 summary views
  - 暴露 split 级样本量、label 覆盖率与日级样本覆盖 summary
- `infra/clickhouse/init/023_training_readiness_views.sql`
  - 新增第一版训练就绪 / label balance views
  - 暴露监督目标类别、activity bucket、growth bucket 与 split 级 label balance
- `infra/clickhouse/init/024_inference_input_views.sql`
  - 新增第一版在线推理输入 views
  - 暴露最新交易日候选打分输入与 cohort gate summary

### 5.3 新增 / 重要脚本

- `scripts/dev-up.sh`
  - 本地或 VPS 快速启动

- `scripts/init-streams.sh`
  - 初始化 Redis consumer groups

- `scripts/apply-clickhouse-init.sh`
  - 对已有 ClickHouse 卷补应用新 DDL

- `scripts/backfill-normalized-events.sh`
  - 重建 normalized tables
  - 当前版本会按 normalized key 去重重建
  - 已扩展到 mint / burn tables

- `scripts/verify-normalized-events.sh`
  - 验证 raw 与 normalized 最近窗口计数对齐
  - 已扩展到 mint / burn topics

- `scripts/verify-normalized-dedupe.sh`
  - 查看 dedupe 前后 row count 与 duplicate surplus
  - 已扩展到 mint / burn tables

- `scripts/inspect-normalized-duplicates.sh`
  - 打印 normalized 层重复样本
  - 已支持 `v2-mint` / `v2-burn` / `v3-mint` / `v3-burn`

- `scripts/inspect-dedupe-anomalies.sh`
  - 基于统一 audit views 汇总所有 normalized surface 的 duplicate anomaly
  - 可先看 summary，再按 target 深挖 recent duplicate events

- `scripts/rebuild-normalized-surface.sh`
  - 按单个 normalized surface 定向重建并物理重塌重复
  - 避免为了一个 `v3-burn` 异常去全量重建所有 normalized tables

- `scripts/inspect-raw-duplicate-origins.sh`
  - 打印 raw 层重复样本及 `source`

- `scripts/inspect-normalized-activity.sh`
  - 查看最近 N 小时 top token / pair / pool activity
  - 已支持 mint / burn activity inspection

- `scripts/inspect-registry.sh`
  - 查看 static registry 内容

- `scripts/verify-registry-coverage.sh`
  - 检查当前 normalized 数据对 static registry 的覆盖率
  - 当前输出已按 event family 展开

- `scripts/inspect-registry-gaps.sh`
  - 打印未被当前 curated registry 覆盖的 token / pair / pool

- `scripts/inspect-enriched-events.sh`
  - 查看带 registry metadata 的 enriched query surface

- `scripts/inspect-registry-candidates.sh`
  - 按 event family 聚合后输出最值得纳入下一批 curated registry 的候选地址
- `scripts/phase2-healthcheck.sh`
  - 一次串跑 raw heartbeat、normalized parity、dedupe、registry coverage、activity inspection
- `scripts/phase2-healthcheck-strict.sh`
  - 对 raw freshness、recent parity、duplicate surplus、lifecycle registry coverage 做 PASS/FAIL 断言
- `scripts/inspect-phase2-status.sh`
  - 直接读取 `eatlq.phase2_raw_status` 与 `eatlq.phase2_surface_status`
  - 用统一 schema 查看各 normalized surface 的 raw-total / raw-projectable / raw-projectable-unique / raw-projectable-duplicate / raw-unprojectable / normalized、近 30 分钟 parity、duplicate surplus、registry gap
- `scripts/inspect-phase2-alerts.sh`
  - 直接读取 `eatlq.phase2_alert_summary` 与 `eatlq.phase2_alert_events`
  - 把 `critical` 与 `warning` 分开打印，区分 recent parity / duplicate / lifecycle coverage 告警、historical unique projectable gap、以及历史 raw filter 不可投影告警
  - 现在会直接打印每条 alert 的 `remediation_target` 与 `remediation_command`
- `scripts/remediate-phase2-alerts.sh`
  - 读取 `phase2_alert_events` 中可由定向 rebuild 修复的 alerts
  - 默认只做 dry-run；显式传 `--apply` 才会串跑 `rebuild-normalized-surface.sh` 并在结束后重跑 alerts 与 strict healthcheck
  - 对仅因历史 raw rows 不满足 projection filter 而触发的 warning，不会自动执行 rebuild
- `scripts/phase2-monitor-cron.sh`
  - 给 `cron` / 外部监控一个极简出口：打印单行 summary，并复用 `phase2-healthcheck-strict.sh` 的退出码
  - 可选把 summary line 写到文件，方便 node exporter textfile 或自定义守护进程读取
- `scripts/inspect-erc20-unprojectable-raw.sh`
  - 专门剖析 `erc20` 历史 `unprojectable raw`
  - 会输出 reason/source/token breakdown，以及最近样本，帮助解释当前那 312 条历史 raw 为何未进 normalized
  - 当前已验证：这 312 条全部是 `data_len` 失败，`data = 0x`，来源全是 `quicknode`，更像 NFT / 非 ERC20 的 `Transfer` 签名碰撞，而不是 ERC20 value payload 丢失
- `scripts/verify-decoded-values.sh`
  - 从 decoded views 抽样最近交易
  - 用独立 big-int 逻辑复核 `UInt256` / `Int256` 解码结果
  - 当前用于 VPS apply `013` 后的首轮 correctness 验证
- `scripts/verify-semantic-amounts.sh`
  - 复核 semantic amount views 中的 decimals-aware normalized amount
- `scripts/verify-semantic-trades.sh`
  - 复核 trade semantic views 中的方向与 sold / bought amount 推导
- `scripts/verify-wallet-profiles.sh`
  - 复核 sender-based wallet profile 聚合
- `scripts/verify-wallet-features.sh`
  - 复核第一版 wallet feature views 中的 venue / token concentration 与 activity features
- `scripts/verify-wallet-behavior.sh`
  - 复核第一版 wallet behavior views 中的 top venue / top token / behavior label 推导
- `scripts/verify-training-dataset.sh`
  - 复核第一版 sender-day training dataset views 中的 rollup、concentration 与日级标签推导
- `scripts/verify-outcome-labels.sh`
  - 复核第一版未来窗口 outcome label views 中的 `t+1 day` 活跃度与 protocol outcome 推导
- `scripts/verify-training-splits.sh`
  - 复核第一版成熟样本过滤与 train/validation/test 分桶结果
- `scripts/verify-training-quality.sh`
  - 复核第一版训练质量 summary views 中的 split/day 样本量与 label 覆盖统计
- `scripts/verify-training-readiness.sh`
  - 复核第一版训练就绪 / label balance views 中的目标类别与 split 级 label balance
- `scripts/verify-inference-inputs.sh`
  - 复核第一版在线推理输入 views 中的最新日过滤、priority band 与 cohort gate 统计

### 5.4 文档文件

- `docs/phase1-runbook.md`
  - 当前部署、验证、replay、normalized、dedupe、activity 的实际命令入口

- `docs/phase1-session-summary.md`
  - Phase 1 的稳定性演进总结

- `docs/phase2-backfill-design.md`
  - replay / scheduler / state 层设计与边界

- `docs/phase2-normalized-events-design.md`
  - normalized / dedupe / activity 层设计与当前限制

- `docs/phase2-registry-design.md`
  - static registry 设计与当前限制
  - enriched query views 与 gap inspection 路径
  - candidate ranking 路径

- `docs/whitepaper-v2-current.md`
  - 当前项目总纲与 phase map

- `docs/logs-filter-profile.md`
  - 当前过滤器相关背景与 topic 列表
  - 已补 V2/V3 `Mint` / `Burn` topics

- `docs/agent_handoff.md`
  - 给下一位 agent 的简洁但完整上下文

- `.claude/phase2-architecture-review.md`
  - 记录本轮架构级 review 结论
  - 已明确“修 H2，顺手修 H1，其余记 tech debt”

## 6. 已知问题 / 风险

### 6.1 业务与数据完整性风险

- 当前过滤器仍是 curated allowlist，不是全链全事件
- 缺块发现仍然是 heuristic，不是证明式 gap detection
- `raw_evm_logs` 缺少某个 block 只表示“这个 block 没有匹配到当前 filter 的事件”或“确有遗漏”，两者不能自动区分
- `value_usd` 仍未实现
- normalized 数值字段仍未 decode 成业务友好数值

### 6.2 重复与回补风险

- live materialized view 仍可能吃到 future overlap
- 目前更安全的物理去重方式是重跑 `bash scripts/backfill-normalized-events.sh`
- `backfill-normalized-events.sh` 与 `rebuild-normalized-surface.sh` 现已使用 temp-table + row-count validation + atomic rename swap，不再先 `TRUNCATE` 再冒险 `INSERT`
- 这意味着 physical dedupe 还不是自动在线完成的
- 如下一位 agent 想做物理去重表，必须非常谨慎评估写入路径复杂度
- `uniswap_v3_swaps` 历史上那 4 条 residual gap 已证明只是 raw duplicate backlog，不是缺失 normalized 投影
- 当前唯一剩余 warning `historical_unprojectable_raw:erc20_transfers` 已解释清楚，不建议继续用 rebuild 尝试“修复”

### 6.3 配置风险

- VPS `.env` 历史上出现过重复定义
- 如果 agent 改 `.env.example`，要保证与现网逻辑一致
- `LOG_FILTER_JSON` 一旦改宽，吞吐与延迟都要重新评估
- **QuickNode 地址限制**：VPS `.env` 中 `EVM_HTTP_URL=https://mainnet.base.org`，**不要改回 QuickNode HTTP URL**，否则超过 5 个地址的池会静默丢失事件（无报错，极难发现）
- `https://mainnet.base.org` 是公共 RPC，无 SLA 保障；如果遇到限速或抖动，可换用 Alchemy/Infura 免费层（验证其 eth_getLogs 地址上限 ≥10 后再换）

### 6.4 运维风险

- QuickNode endpoint 曾出现在历史对话和日志中，建议轮换
- 用户偶尔会把终端提示符和输出一起粘回 shell，导致“命令执行失败”假象
- 给用户的命令必须尽量短、可直接复制、不要带占位符尖括号

### 6.5 代码与架构风险

- `services/ingestor/src/rpc.rs` 仍按 block 串行拉 logs
- `services/archiver` 的 pending recovery 仍主要是启动时 reclaim，不是持续后台治理
- normalized 的 activity layer 目前只是地址级计数，不是 semantic alpha layer
- 尚未进入 wallet/entity intelligence

## 7. 推荐下一步

### 7.1 默认优先级建议

下一位 agent 默认优先做：

1. **Base L2 数据已就绪（30天，~6.91M 条）**——可立即针对 chain_id=8453 跑 Phase 3 训练，验证 Aerodrome 数据是否提升模型信号质量；入口：`bash scripts/run-phase3-training.sh`
2. 如果 dashcam 有足够候选记录，启动 Phase 4 执行层设计
3. 先把 inference input views（`024_inference_input_views.sql`）在 VPS 上 apply / verify（此步骤之前遗留，尚未在 VPS 执行）
4. 复用当前 normalized / enriched / status / alert surfaces，不要重写 Phase 2 数据面

原因：

- `Phase 2` 主目标已完成并在 VPS 封板
- Base L2 Aerodrome 扩展已完成，现在是第一次有多链 30 天数据可供 Phase 3 多链 retrain
- 当前监控、告警、重建、根因剖析路径已闭环，不需要再扩 Phase 2 工具链

### 7.2 如果做 registry

应同步补：

- ClickHouse registry views
- enriched query views
- inspection 脚本
- coverage 校验脚本
- gap inspection 脚本
- candidate ranking 脚本
- runbook 验证命令
- handoff / whitepaper 当前状态

### 7.3 registry 范围建议

推荐目标：

- 一个最小 token registry
- 一个最小 pool registry
- 不要一开始就追求全自动 onchain discovery
- 先做静态或半静态 registry，支撑后续 normalized enrichment

### 7.4 不推荐的下一步

- 不要回到 `eth_subscribe logs`
- 不要先做 UI
- 不要先做 execution
- 不要为了“修 warning”去继续 rebuild `erc20_transfers`
- 不要重写 `status/alerts/remediation/cron` 这套已经验证过的 Phase 2 运维面
- 不要在没有必要的情况下重写 live ingest path

## 8. 验收命令与预期结果

### 8.1 基础服务状态

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml ps
```

预期：

- `redis`、`clickhouse`、`postgres`、`ingestor`、`archiver` 为 `Up`
- `redis`、`clickhouse`、`postgres` 为 `healthy`

### 8.2 Ingestor runtime

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=5m ingestor
```

预期：

- 持续出现 `fetched block logs via eth_getLogs`
- 不再频繁出现 `upstream went away`

### 8.3 Archiver runtime

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --since=5m archiver
```

预期：

- 出现 `archiver flushed records to clickhouse`

### 8.4 Redis Stream

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T redis redis-cli XINFO STREAM market_ticks
```

预期：

- `entries-added` 增长

### 8.5 Raw ClickHouse 增长

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SELECT count(), max(received_at_ms) FROM eatlq.raw_evm_logs"
```

预期：

- `count()` 增长
- `max(received_at_ms)` 接近当前时间

### 8.6 Backfill scheduler 验证

先入队一个最近 `fetched_logs=0` 的 block：

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml --profile backfill run --rm -e BACKFILL_FROM_BLOCK=24662931 -e BACKFILL_TO_BLOCK=24662931 backfill-enqueue-gap
```

再查：

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml logs --tail=50 backfill-scheduler
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id, job_key, from_block, to_block, status, claimed_at, run_started_at, run_finished_at, last_completed_to_block, last_error FROM app.backfill_gap_ranges ORDER BY id DESC LIMIT 5;"'
```

预期：

- scheduler claim 成功
- 最新 gap 变成 `completed`
- `last_error` 为空

### 8.7 应用 ClickHouse 新 DDL

```bash
bash scripts/apply-clickhouse-init.sh
```

预期：

- 所有 init SQL 正常应用
- 对已有卷不需要删库重建
- `012_hourly_mv_cleanup.sql` 生效后，`eatlq.raw_evm_logs_hourly` 不再含 `unique_address_state`

### 8.8 重建 normalized tables

```bash
bash scripts/backfill-normalized-events.sh
```

预期：

- 按 temp-table -> row-count validation -> atomic rename swap 执行
- 如果 rebuild insert 失败，原表保持不变
- 能清理已知的 overlap duplicates
- 新增的 mint / burn tables 也会被一并重建

### 8.16 Phase 2 收官验收

```bash
bash scripts/phase2-monitor-cron.sh 30 300 1
bash scripts/phase2-monitor-cron.sh 30 300 1 /opt/EATLQ/monitor/phase2-summary.txt
bash scripts/inspect-erc20-unprojectable-raw.sh 20
```

预期：

- `phase2-monitor-cron.sh` 正常打印 summary line
- `strict_exit_code=0`
- `critical_targets=none`
- `warning_targets=historical_unprojectable_raw:erc20_transfers`
- 第二条命令成功写出 `/opt/EATLQ/monitor/phase2-summary.txt`
- `inspect-erc20-unprojectable-raw.sh` 显示当前 backlog 全部是 `data_len` 失败，`data = 0x`，来源全是 `quicknode`

### 8.9 检查 raw vs normalized 最近窗口对齐

```bash
bash scripts/verify-normalized-events.sh 30
```

预期：

- `raw_transfer == normalized_transfer` 或非常接近
- `raw_v2_swap == normalized_v2_swap` 或非常接近
- `raw_v3_swap == normalized_v3_swap` 或非常接近
- `raw_v2_mint == normalized_v2_mint` 或非常接近
- `raw_v2_burn == normalized_v2_burn` 或非常接近
- `raw_v3_mint == normalized_v3_mint` 或非常接近
- `raw_v3_burn == normalized_v3_burn` 或非常接近

### 8.10 检查 dedupe 汇总

```bash
bash scripts/verify-normalized-dedupe.sh
```

预期：

- `*_deduped_rows <= *_rows`
- `*_duplicate_surplus >= 0`
- 在最近一次已验证状态下：
  - `transfer_duplicate_surplus = 0`
  - `v2_duplicate_surplus = 0`
  - `v3_duplicate_surplus = 0`
  - 新增 mint / burn 表第一次验证时不强设历史基线，但至少不应出现负 surplus 或明显异常

### 8.11 检查重复样本

```bash
bash scripts/inspect-normalized-duplicates.sh v3 20
bash scripts/inspect-normalized-duplicates.sh v3-mint 20
bash scripts/inspect-normalized-duplicates.sh v3-burn 20
```

预期：

- 如果 normalized rebuild 刚完成，V3 重复样本应为空
- 如果新加 mint / burn 刚完成重建，对应重复样本也应为空或极少

### 8.12 检查 raw duplicate 来源

```bash
bash scripts/inspect-raw-duplicate-origins.sh v3 20
```

预期：

- 可直接看到 `sources`
- 当前已验证过的重复样本来源是 `['quicknode','quicknode-backfill']`

### 8.13 检查 hourly activity views

```bash
bash scripts/inspect-normalized-activity.sh v3 24 20
bash scripts/inspect-normalized-activity.sh v3-mint 24 20
bash scripts/inspect-normalized-activity.sh v3-burn 24 20
```

预期：

- 返回最近 24 小时最活跃的 V3 pools
- 能看到 `pool_address`、`swaps`、`unique_txs`、`latest_ingested_at`
- 对 mint / burn 目标会分别返回 `mints` 或 `burns`

### 8.14 检查 static registry

```bash
bash scripts/inspect-registry.sh
bash scripts/verify-registry-coverage.sh
bash scripts/inspect-registry-gaps.sh
bash scripts/inspect-registry-candidates.sh
bash scripts/inspect-enriched-events.sh
bash scripts/phase2-healthcheck.sh 30 24 10
bash scripts/phase2-healthcheck-strict.sh 30 300 1
bash scripts/inspect-phase2-status.sh
bash scripts/inspect-phase2-alerts.sh
bash scripts/remediate-phase2-alerts.sh
```

预期：

- token / V2 pair / V3 pool registry 均可直接查询
- `verify-registry-coverage.sh` 会按 event family 打印 `seen / covered / uncovered`
- 对当前历史数据，transfer / swap 看到大量 uncovered 是正常的，因为 registry 仍是 curated subset
- `inspect-registry-gaps.sh` 可直接打印最值得纳入下一批 curated registry 的地址样本
- `inspect-registry-candidates.sh` 会把多条 event family 的未覆盖地址合并成更稳的候选清单
- `inspect-enriched-events.sh` 可直接看到 registry metadata 已 join 到查询面
- `phase2-healthcheck.sh` 会把 raw heartbeat、normalized parity、dedupe、registry coverage、v2/v3 activity 一次性串起来
- `phase2-healthcheck-strict.sh` 会在 freshness / parity / dedupe / lifecycle coverage 退化时直接返回非零退出码
- `inspect-phase2-status.sh` 会把 raw heartbeat 与各 surface 的 parity / duplicate / registry 状态统一打印出来
- `inspect-phase2-alerts.sh` 会把当前 active alerts 拆成 `critical` 与 `warning`；其中 recent parity / duplicate / lifecycle coverage 退化会作为 critical 暴露，历史 backlog 会拆成 `historical_projectable_gap` 与 `historical_unprojectable_raw` 两类 warning，并直接给出 remediation command
- `remediate-phase2-alerts.sh` 默认只做 dry-run；仅对可由 targeted rebuild 修复的 warning 才应考虑 `--apply`，不要对 `historical_unprojectable_raw:erc20_transfers` 做无意义 rebuild
- `inspect-dedupe-anomalies.sh` 会先给 cross-surface duplicate summary，再给 recent duplicate event 样本
- `rebuild-normalized-surface.sh` 可在异常只落在某个 surface 时做更小粒度的 physical re-collapse
- 在最新一轮候选结果里，`0xe0554a476a092703abdb3ef35c80e0d76d32939f` 与 `0xc7bbec68d12a0d1830360f8ec58fa599ba1b0e9b` 已可确认分别是 `USDC/WETH 1.00%` 与 `USDT/WETH 1.00%` 的 Uniswap V3 pool
- `0xa40907f1a6949c29996a09cc1ac1ac0872070c32` 与 `0x09718a7260b1a8d694f381dcf908890ecb318f2d` 已可确认分别是 `DOGGO/WETH` 与 `SHISHI/WETH` 的 Uniswap V2 pair
- 再下一批里，`0x53ead11073fc0651dce70572666f0ed0752abfea` 已可确认是 `BMBG/USDT 0.30%`，`0x41a561b972039a9ccb975a4c17b84c9f95099e15` 已可确认是 `ADI/USDC 0.05%`
- `0x878723b7e12183853c8d41ab9f728c277226bb06` 已可确认是 `01001000/WETH`，`0x0b07188b12e3bba6a680e553e23c4079e98a034b` 已可确认是 `MOCA/USDT`
- lifecycle gap 里，`0x5cac7cb341b089e15abcfc17bba5dff50f37ae12` 已可确认是 `BLIN/WETH`，`0xca7c2771d248dcbe09eabe0ce57a62e18da178c0` 已可确认是 `FLOKI/WETH`
- `0x8acc975dc4e9f3fc989cd2f10ef15e8caaa7e97e` 已可确认是 `pALVA/pfUSDC-22`，`0xe6d7ebb9f1a9519dc06d557e03c522d53520e76a` 与 `0x6f161ad0e297ecb9d1b33c048272ccc964cb4b6a` 已可确认分别是 `USDe/USDC 1.00%` 与 `USDM1/USDC 1.00%`
- `0x8eea6cc08d824b20efb3bf7c248de694cb1f75f4` 虽然出现在 `v2_mints` gap，但 `token0()` 在公开 RPC 上回退，当前先不纳入 curated registry
- 再下一批里，`0xb76726b4befe761a1859c1c02e7d157142e077c0` 已可确认是 `WETH/SMEL 0.30%`，`0x8d7625fcf354c0961ac51fc121b9dd1708d6b226` 已可确认是 `ERBB/USDT 1.00%`
- `0xd640333b71b015092d9b3afcff3e427036304370` 已可确认是 `USDC/EIGEN 1.00%`，`0x4585fe77225b41b697c938b018e2ac67ac5a20c0` 已可确认是 `WBTC/WETH 0.05%`
- 剩余 overlap 里，`0x40ab23e8f571bf19a85605b9638e50cc25a256ec` 已可确认是 `WBTC/USDT 0.05%`，`0x9d52d8a9f2fbf430fd634a21267b4414049c0064` 已可确认是 `WETH/99BTC 0.30%`
- `0x32d9259e6792b2150fd50395d971864647fa27b2` 已可确认是 `RSR/WETH 0.30%`，`0x41e9f7cf65fe0c3c71a077e49023ba3c4f4f65b3` 已可确认是 `BDX/WETH 0.30%`
- 再下一批单面尾部里，`0x95dbb3c7546f22bce375900abfdd64a4e5bd73d6` 已可确认是 `EURC/USDC 0.05%`，`0x433a00819c771b33fa7223a5b3499b24fbcd1bbc` 已可确认是 `wTAO/WETH 1.00%`
- `0x25392d7129040710f152174af5019004a6f9b18d` 已可确认是 `BITCOIN/WETH 0.30%`，`0x9a99748cf3a6b21207bcc9aeebafbd734bfdaba8` 已可确认是 `BIFI/WETH 1.00%`
- `0x1a5a974874ba51a7b38e30f63449495c58b243e7` 已可确认是 `Q/WETH 1.00%`，`0x46b63703fa267bfcdb9ce66a1c5216e94d840fbf` 已可确认是 `REACT/WETH 1.00%`
- `0x6546055f46e866a4b9a4a13e81273e3152bae5da` 已可确认是 `XAUt/USDT 0.05%`
- `0x7f63306a62c345365881e0fff85cb2c8baaa13d5`、`0x15483ab0d7351509ff5236c617004903e67a386d`、`0x9ebb79b1759eaa95ad9c132c3a394e2dd792120d` 与 `0x94391eecd76d9cb35013d7f263b64921e31842c8` 均是 `XOR/WETH` 的不同 V3-compatible venue，因此 label 里加入了短地址后缀以避免混淆

### 8.14.1 检查 decoded views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-decoded-values.sh 3
```

预期：

- `013_decoded_views.sql` 被自动 apply
- `eatlq.erc20_transfers_decoded`、`eatlq.uniswap_v2_swaps_decoded`、`eatlq.uniswap_v3_swaps_decoded`、`eatlq.uniswap_v2_mints_decoded`、`eatlq.uniswap_v2_burns_decoded`、`eatlq.uniswap_v3_mints_decoded`、`eatlq.uniswap_v3_burns_decoded` 可直接查询
- `verify-decoded-values.sh` 会打印抽样 `tx_hash` / `log_index`
- `UInt256` 字段会与独立 Python big-int 解码结果一致
- `uniswap_v3_swaps_decoded.amount0` / `amount1` 会与 two's complement `Int256` 解码一致
- `uniswap_v3_swaps_decoded.tick` 会与 `tick_raw` 的有符号解码一致
- `uniswap_v3_mints_decoded` / `uniswap_v3_burns_decoded` 中 `tick_lower` / `tick_upper` 会与 raw topic 的有符号解码一致

### 8.14.2 检查 wallet feature views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-wallet-features.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%feature%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%venue_trade_profile%'"
```

预期：

- `017_wallet_feature_views.sql` 被自动 apply
- `eatlq.sender_venue_trade_profile` 与 `eatlq.sender_feature_store_v1` 可直接查询
- `verify-wallet-features.sh` 会打印抽样 `sender_address`
- `sender_venue_trade_profile` 的 `trade_count` / `distinct_txs` / token diversity 与 `swap_trade_events_all` 一致
- `sender_feature_store_v1` 的 `venue_concentration_ratio` / `token_concentration_ratio` / `avg_trades_per_active_day` 与上游 profile views 计算一致

### 8.14.3 检查 wallet behavior views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-wallet-behavior.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%behavior%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%preference_profile%'"
```

预期：

- `018_wallet_behavior_views.sql` 被自动 apply
- `eatlq.sender_preference_profile_v1` 与 `eatlq.sender_behavior_labels_v1` 可直接查询
- `verify-wallet-behavior.sh` 会打印抽样 `sender_address`
- `sender_preference_profile_v1` 中的 `top_venue_*` / `top_token_*` 与上游 profile views 中的最大 trade bucket 一致
- `sender_behavior_labels_v1` 中的 `protocol_preference` / `activity_band` / `flow_bias` 与 `sender_feature_store_v1` 中的数值阈值规则一致

### 8.14.4 检查 training dataset views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-training-dataset.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%training_dataset%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%trade_day_rollup%'"
```

预期：

- `019_training_dataset_views.sql` 被自动 apply
- `eatlq.sender_trade_day_rollup_v1` 与 `eatlq.sender_training_dataset_v1` 可直接查询
- `verify-training-dataset.sh` 会打印抽样 `sender_address` 与 `activity_date`
- `sender_trade_day_rollup_v1` 的日级 count / diversity 与 `swap_trade_events_all` 按天聚合结果一致
- `sender_training_dataset_v1` 的 concentration ratio / protocol preference / activity band 与日级样本阈值规则一致

### 8.14.5 检查 outcome label views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-outcome-labels.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%outcome_labels%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%training_examples%'"
```

预期：

- `020_outcome_label_views.sql` 被自动 apply
- `eatlq.sender_forward_outcome_labels_v1` 与 `eatlq.sender_training_examples_v1` 可直接查询
- `verify-outcome-labels.sh` 会打印抽样 `sender_address` 与 `activity_date`
- `sender_forward_outcome_labels_v1` 中的 `next_day_*` 字段与 `sender_trade_day_rollup_v1` 的次日聚合结果一致
- `sender_training_examples_v1` 中的特征字段与 `sender_training_dataset_v1` 一致，label 字段与 `sender_forward_outcome_labels_v1` 一致

### 8.14.6 检查 training split views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-training-splits.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%matured%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%split%'"
```

预期：

- `021_training_split_views.sql` 被自动 apply
- `eatlq.sender_training_examples_matured_v1` 与 `eatlq.sender_training_examples_split_v1` 可直接查询
- `verify-training-splits.sh` 会打印抽样 `sender_address` 与 `activity_date`
- `sender_training_examples_matured_v1` 只包含小于当前最大 `activity_date` 的成熟样本
- `sender_training_examples_split_v1` 的 `split_bucket` / `dataset_split` 与 deterministic hash 分桶规则一致

### 8.14.7 检查 training quality views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-training-quality.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%split_summary%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%daily_dataset_summary%'"
```

预期：

- `022_training_quality_views.sql` 被自动 apply
- `eatlq.sender_training_split_summary_v1` 与 `eatlq.sender_training_daily_dataset_summary_v1` 可直接查询
- `verify-training-quality.sh` 会打印 split 级和 day 级抽样 summary
- `sender_training_split_summary_v1` 中的样本量与 label 覆盖率和 `sender_training_examples_split_v1` 聚合结果一致
- `sender_training_daily_dataset_summary_v1` 中的 train / validation / test 分布与日级聚合结果一致

### 8.14.8 检查 training readiness views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-training-readiness.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%readiness%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%label_balance%'"
```

预期：

- `023_training_readiness_views.sql` 被自动 apply
- `eatlq.sender_training_examples_ready_v1` 与 `eatlq.sender_training_label_balance_v1` 可直接查询
- `verify-training-readiness.sh` 会打印 ready 样本与 label balance 抽样
- `sender_training_examples_ready_v1` 中的 `supervised_target_class` / `next_day_activity_bucket` / `next_day_growth_bucket` 与上游 split 样本阈值规则一致
- `sender_training_label_balance_v1` 中的样本量与正例计数和 `sender_training_examples_ready_v1` 聚合结果一致

### 8.14.9 检查 inference input views

```bash
bash scripts/apply-clickhouse-init.sh
bash scripts/verify-inference-inputs.sh 3
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%inference_candidates%'"
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml exec -T clickhouse clickhouse-client --query "SHOW TABLES FROM eatlq LIKE '%inference_cohort_summary%'"
```

预期：

- `024_inference_input_views.sql` 被自动 apply
- `eatlq.sender_inference_candidates_v1` 与 `eatlq.sender_inference_cohort_summary_v1` 可直接查询
- `verify-inference-inputs.sh` 会打印最新交易日候选与 cohort summary 抽样
- `sender_inference_candidates_v1` 只包含当前最大 `activity_date` 的样本，且 `inference_priority_band` / gate 字段与阈值规则一致
- `sender_inference_cohort_summary_v1` 的样本量与 gate 统计和 `sender_inference_candidates_v1` 聚合结果一致

### 8.17 验收 Base L2 Aerodrome 数据覆盖

```bash
cd /opt/EATLQ/infra/compose
docker compose exec clickhouse clickhouse-client --database eatlq --query "
SELECT
  p.venue_label,
  count(r.tx_hash) AS events,
  min(r.block_number) AS first_block,
  max(r.block_number) AS last_block,
  round((max(r.block_number) - min(r.block_number)) / 43200.0, 1) AS days_covered
FROM eatlq.pool_registry p
JOIN eatlq.raw_evm_logs r ON lower(r.address) = lower(p.venue_address)
WHERE p.chain_id = 8453
GROUP BY p.venue_label
ORDER BY events DESC
FORMAT PrettyCompact"
```

预期（基于 2026-03-25 验收快照，后续持续增长）：

- 全部 9 个池均出现
- 所有 Aerodrome 池 `days_covered >= 30`
- 所有 Aerodrome 池 `last_block` 与 WETH/USDC 池的 `last_block` 差值 < 2000（约 1 小时）
- `events` 最大的池为 `WETH/USDC 0.30%`（>2.9M）
- `AERO/WETH [Aerodrome CL]` 事件数 > 500k

如果 Aerodrome 池的 `last_block` 明显落后（差值 > 10000），说明 `EVM_HTTP_URL` 被改回了 QuickNode 或公共 RPC 出现了问题，检查：

```bash
grep EVM_HTTP_URL /opt/EATLQ/infra/compose/.env
docker compose logs ingestor --tail=20 | grep fetched_logs
```

正常状态下 `fetched_logs` 每块应为 3–10（10 个地址），若持续为 0–1，说明 HTTP 端点对多地址过滤失效。

### 8.15 本地 Rust 检查

```bash
cargo check
cargo test -p ingestor
cargo test -p archiver
```

预期：

- 全部通过

## 补充说明

- 默认不要提交 `.claude/`
- 用户明确要求以后改完就推送
- 任何需要用户在 VPS 上执行的命令，尽量给成可直接复制的一两条
- 不要把 shell 提示符一起写进命令
- 如果新增 ClickHouse DDL，记得同时考虑：
  - `scripts/apply-clickhouse-init.sh`
  - 是否要补 rebuild / verify / inspect 脚本
  - `docs/phase1-runbook.md`
  - `docs/phase2-normalized-events-design.md`
  - `docs/whitepaper-v2-current.md`
