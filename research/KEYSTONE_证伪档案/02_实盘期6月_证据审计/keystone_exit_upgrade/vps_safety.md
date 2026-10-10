# VPS / ClickHouse 安全审查 — 出场 v1 研究

**原则：实盘优先。** 研究任务不得挤占 live 路径的 ClickHouse、RPC、CPU。

## 硬件快照（2026-06-20）

| 资源 | 值 | 研究限额 |
|------|-----|----------|
| CPU | 8 核，load ~4–5 | 单查询 max_threads=1 |
| RAM | 23 GiB，available ~18 GiB | CH 等待阈值 RSS **1.8 GiB** + live 重查询；硬中止 **6 GiB** |
| Disk | 193G，34% used | 输出仅 TSV/JSON，<50MB |
| Swap | 4 GiB，0 used | 不依赖 swap |

## Live 依赖（不可干扰）

| 服务 | ClickHouse 用法 | 风险 |
|------|-----------------|------|
| **signal-bridge** | 每 30s 全表 `GROUP BY pool` 扫 t0（~10M rows，~3.5GB mem） | **高** — 研究需避让 |
| archiver / ingestor | INSERT raw_evm_logs | 中 — 不跑全表 scan |
| pool-discovery | INSERT discovered_pools | 低 |

## 研究脚本护栏

`fetch_fwdbuckets_safe.py`:

1. **单池** `pool_address='...'` 过滤（走索引，~1–2s/池）
2. `SET max_memory_usage=1GiB, max_execution_time=90, max_threads=1`
3. CH RSS > 1.8GiB **且** live 重查询进行中 → **等待**（最多 10min）；无竞争时可继续
4. RSS > 6GiB → **硬中止**，留 checkpoint 续跑
5. 池间 pause **3s**；743 gated 池 ≈ **45–60 min**
6. **只读** swaps 表；无 INSERT/ALTER

`exit_upgrade_v1_walkforward.py`:

- **零 ClickHouse**（用缓存 TSV/JSON）
- **零 RPC**（用已有 `_sellcurve.json`）

## 未修改

- `services/executor/`
- `services/signal_bridge/`
- `infra/compose/.env`

## 建议运行窗口

若 bridge 扫描导致 CH CPU >150% 持续，将 fetch 放到 **UTC 低活跃段** 或手动暂停 bridge（需你同意，默认不暂停 live）。
