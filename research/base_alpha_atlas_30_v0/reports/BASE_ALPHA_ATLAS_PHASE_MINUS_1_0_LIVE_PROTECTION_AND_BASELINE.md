# BASE_ALPHA_ATLAS_30_v0

## Phase -1–0: Live Alpha Protection, Resource Baseline and Research Boundary

Audit window UTC: `2026-07-18T13:46:58Z` → `2026-07-18T13:58:56Z`  
Project root: `/root/base_alpha_atlas_30_v0`  
Protected production: `/opt/EATLQ` + `eatlq-*` containers

```text
overall_status = PAUSED_LIVE_PRIORITY
SAFE_TO_BEGIN_OFFLINE_RESEARCH = false
blocking_statuses = BLOCKED_RESOURCE_RISK_CPU_IDLE
plan_verdict = PAUSED_LIVE_PRIORITY
```

---

### 1. 本轮目标

只完成：

1. 只读识别 `PROTECTED_LIVE_ALPHA` 与生产资源边界  
2. 10 分钟被动健康基线  
3. 隔离研究目录落盘  
4. 离线数据路径盘点（文件名级）  
5. 给出是否允许启动离线研究的门禁裁决  

**未做：** 共享 Panel 构建、策略回测、生产 DB 导出、RPC、Stage 0 策略卡批量写作、任何实盘修改。

---

### 2. PROTECTED_LIVE_ALPHA 边界

当前实盘 Alpha 路径（非 secret 摘要）：

```text
pool-discovery → signal-bridge (ClickHouse features + ML p_fatal)
              → Redis stream `signals`
              → executor (LIVE, dry_run=False)
```

| 组件 | 角色 | 基线结果 |
| --- | --- | --- |
| `eatlq-executor-1` | 执行 / 风控 / 仓位 | `running`, RestartCount=`0`, 窗口内不变 |
| `eatlq-signal-bridge-1` | Live 信号源 | `running`, RestartCount=`0` |
| `eatlq-ingestor-1` | 行情采集 | `healthy`, 不变 |
| `eatlq-archiver-1` | 归档入 CH | `healthy` |
| `eatlq-redis-1` | 队列与状态 | `healthy`; `signals` lag=`0` pending=`0` |
| `eatlq-clickhouse-1` | Bridge 特征查询 | `healthy`，但持续高 CPU（均值约 380%≈3.8 核） |
| `SYSTEM_PANIC` | 买侧急停闩 | 窗口内始终 `EXISTS=0` |
| `eatlq-strategy-1` 等 | 旧链路 | 已退出数周；现网不以之为信号源 |

详细表见 `docs/inventory/PROTECTED_LIVE_ALPHA_INVENTORY.md`。

**明确禁止（本项目）：** 修改 `/opt/EATLQ` 代码/配置/阈值/风控/仓位/Universe/RPC；读写密钥；签名下单；写 Redis/生产 DB；重启或 attach 实盘进程；建 cron/systemd；占生产端口/Compose 名。

---

### 3. 隔离目录

已确认无冲突并创建：

```text
/root/base_alpha_atlas_30_v0/
├── docs/ inventory safety gates
├── reports/
├── strategies/
├── shared_panel/
├── normalized/
├── checkpoints/   # health baseline artifacts only
├── logs/
├── tests/
├── orchestration/
└── review/
```

与 `/root/lpi_base_production_safe_node` 并存、不合并。不使用生产 venv / `.env` / Redis 前缀 / 生产日志目录。

---

### 4. 10 分钟被动健康基线

采样：21 点，约每 30s；产物：

- `checkpoints/health_baseline_raw/samples.log`
- `checkpoints/health_baseline_summary.json`

| 指标 | 结果 | 门禁 |
| --- | --- | --- |
| CPU idle 均值 | **44.97%** | 要求 >30% → 通过 |
| CPU idle 最小 | **0.00%** | 自动暂停：连续 30s <10% |
| idle <10% 样本数 | **7 / 21** | 失败 |
| 最长连续 idle<10% | **3 个样本（≈90s）** | 触发 `PAUSED_LIVE_PRIORITY` |
| Swap | 2.176GB → 2.175GB（Δ −786KB） | 未增长 → 通过 |
| IO wait 均值 / 最大 | 0.11% / 1.24% | 低 → 通过 |
| MemAvailable | 全程 ≈15–17 GiB | 安全 |
| Disk avail | ≈106 GiB | 安全 |
| Executor / bridge / ingestor | 状态与 RestartCount 不变 | 通过 |
| `signals` pending/lag | 0 / 0 | 通过 |
| discovery tip age | max 13s | 行情仍在推进 |
| ClickHouse concurrent queries | max 3 | 无严重排队 |
| ClickHouse CPU | mean ≈380%, max ≈878% | **实盘 Bridge 相关压力，研究不得叠加** |
| Load1 | mean 5.34 / max 7.33（8 核） | 偏高 |

---

### 5. 离线数据盘点（文件名级，未读内容做特征）

| 类别 | 观察 | Atlas 可用性 |
| --- | --- | --- |
| 现成 Parquet panel | **未发现**可用的共享 Base panel parquet | Panel 需新建；当前禁止构建 |
| 诊断 CSV/JSON/TSV | `/opt/EATLQ/mvp/diagnostics` 大量历史产物（最大单文件约 325MB） | 仅可在 Gate 通过后、只读复制到本项目 `normalized/` 再分析 |
| `dashcam.csv` | 126MB，mtime 2026-06-23 | 陈旧；非实时 |
| LPI 节点 | `/root/lpi_base_production_safe_node` | 另一隔离项目；可参考报告，不共用运行时 |
| 生产 ClickHouse 大表 | 存在（live bridge 正在查询） | `production_database_access=false`；全表 GROUP BY 已证实会打满 CH，**禁止** Atlas 重扫 |

清单：`docs/inventory/offline_data_paths.md`

---

### 6. 本任务资源使用

- 短时只读：`docker ps/inspect/stats`、`redis-cli` 读元数据、`clickhouse-client` 仅 `system.processes` count  
- 10 分钟被动采样器（写 `/tmp` 后拷入本项目 checkpoints）  
- 仅在 `/root/base_alpha_atlas_30_v0` 创建文档与目录  
- **无** 重型回测、无 Panel 构建、无生产写、无 cron

---

### 7. 门禁裁决

```text
SAFE_TO_BEGIN_OFFLINE_RESEARCH = false
```

**原因：** 虽 10 分钟 idle **均值**高于 30%，但窗口内多次出现 idle <10%，最长连续约 90 秒，已命中计划自动暂停条件：

> CPU idle 连续 30 秒低于 10% → 立即停止研究进程 → `PAUSED_LIVE_PRIORITY`

叠加事实：ClickHouse 被 live `signal-bridge` 周期性重查询打到数核占用；此时任何共享 Panel 构建或生产只读大导出都无法证明“对实盘无可感知影响”。

因此默认停止研究推进。

---

### 8. 是否修改生产状态

否（本轮命令范围内）：

- 未改 `/opt/EATLQ`  
- 未重启/停止/signal 任何实盘容器  
- 未写 Redis / Postgres / ClickHouse 用户表  
- 未改 cron / systemd / compose  
- 未触碰密钥与交易接口  

限制：不能证明其他并发主体（Cursor/Codex/人工）未改动生产。

---

### 9. 明确未完成（被门禁挡住）

- Stage 0：30 条精确策略卡  
- 正交性矩阵初审（需策略卡）  
- 共享 Panel **构建**（计划文档可随后补，构建禁止）  
- 任何 Discovery / Walk-forward / 红队  

---

### 10. 下一步最小安全动作

1. **保持 `PAUSED_LIVE_PRIORITY`**，不启动 Panel / 回测。  
2. 待主机出现**连续 ≥10 分钟**且：
   - idle 均值 >30%  
   - **无** idle<10% 持续 ≥30s  
   - Swap 不升  
   - CH 查询压力回到更低稳态  
3. 重新跑同一套被动基线；仅当新报告写出  
   `SAFE_TO_BEGIN_OFFLINE_RESEARCH = true`  
   后，才允许：
   - 从已有 diagnostics **复制**到 `normalized/`（不碰生产写）  
   - 编写 Stage 0 策略卡与 Panel **计划**  
   - 在资源预算内构建离线 Panel（仍禁止扫生产全表）  

在此之前，允许的工作仅限：完善本项目文档、人工审阅已有脱敏报告路径名、等待更干净的主机窗口。
