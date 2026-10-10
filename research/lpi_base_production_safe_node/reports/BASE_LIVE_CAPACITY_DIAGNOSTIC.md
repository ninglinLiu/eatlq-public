# Base Live Capacity Diagnostic

Diagnostic window: 2026-07-17 11:46:39–11:56:35 UTC  
Duration: 596 seconds  
Samples: 119 at 5-second cadence  
Execution policy: `nice -n 19`, `ionice -c3`, one bounded shell task, `ulimit -v 393216`, 600-second outer timeout  
Production services changed or signalled: none

```text
overall_live_state = SAFE_TO_PROCEED_FOR_OFFLINE_BOOTSTRAP_ONLY
ready_for_isolated_code_bootstrap = true
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
ready_for_full_history = false
ready_for_strategy_research = false
ready_for_paper_forward = false
ready_for_live_modification = false
```

This report supersedes the earlier interpretation that a single high-CPU snapshot implied a persistent node block. The earlier `PAUSED_LIVE_PRIORITY` was valid for that instant, but it is a dynamic gate, not a permanent host classification.

## 1. 本轮目标

Perform a bounded, read-only capacity diagnosis; identify CPU/IO attribution; establish existing canonical heartbeats; distinguish idle executor logging from a stalled loop; determine Swap direction and ClickHouse activity; identify reviewable old research processes without stopping anything.

## 2. 实盘 execution 健康

- All 12 observed EATLQ containers remained `Up` after the diagnostic.
- Executor container ID and PID remained stable. Its Redis consumer was sampled 19 times: idle 177–1,894 ms, average 1,046 ms, pending always 0. This proves the main loop continued issuing its idle blocking read.
- Executor operational logs contained no monitor-cycle failure, panic, position-open, or position-close event during the final 20 minutes. Given the configured daily Telegram heartbeat and no signals/positions, log silence is normal idle behavior, not evidence of a stalled loop.
- Ingestor health-file age never exceeded 1 second in the complete window.
- Archiver health-file age never exceeded 29 seconds in the complete window; Docker remained healthy.
- No OOM kill or container restart was observed.

Risk, order management, and position monitoring are integrated into the executor process rather than separate services. Their code path executes `_monitor_once` on a nominal 5-second cadence, but no independent positive heartbeat is exported. This prevents declaring their individual canonical heartbeats fully confirmed; see `docs/BASE_CANONICAL_HEARTBEATS.md`.

## 3. 本任务资源使用

The diagnostic itself was one low-priority bounded task. Every 30 seconds it ran one 1-second `pidstat` sample and one Redis `XINFO CONSUMERS` metadata read. It wrote small TSV/text evidence files under this workspace. No worker, daemon, service, container, or schedule remains running.

## 4. 实际完成

### Ten-minute host capacity

| metric | result |
|---|---:|
| CPU idle, full-window average | 51.845% |
| CPU idle, minimum 5-second sample | 0.68% |
| CPU idle, maximum 5-second sample | 94.17% |
| Samples below 10% idle | 11 of 119 |
| Longest continuous time below 10% idle | 5 seconds |
| Minimum 30-second rolling CPU-idle average | 35.037% |
| IO wait, average | 0.299% |
| IO wait, maximum 5-second sample | 2.14% |
| Available memory | 12.44–18.32 GiB |
| Swap used, first sample | 2,264,860 KiB |
| Swap used, final sample | 2,262,556 KiB |
| Swap net change | -2,304 KiB |
| `pswpout` during window | 0 pages |
| `pswpin` during window | 347 pages |
| Disk read | 24,248 sectors, approximately 11.84 MiB |
| Disk write | 5,562,192 sectors, approximately 2.65 GiB |
| Aggregate disk busy time | 35.388 seconds over 596 seconds |
| Root filesystem free | previously measured 108–109 GiB |

The full-window CPU gate passed: average idle exceeded 30%, the minimum 30-second rolling average remained above 35%, and no 30-second low-idle guard fired. Swap did not grow. The small `pswpin` with zero `pswpout` and declining used Swap indicates historical swapped pages being read back, not new memory pressure.

### CPU and IO attribution

Nineteen 1-second process samples show:

- ClickHouse server: average about 373% CPU, maximum about 784%; average read/write around 278/277 KiB/s in the sampled seconds. It is the dominant sustained host consumer.
- Production swap poller PID 3387816: average about 12.6% CPU, maximum about 64.4%.
- Cursor server PID 809169: average about 16.3% CPU, maximum about 92.3%; this is an interactive development service, not a confirmed disposable research job.
- Two transient Redis child processes appeared for one sample each at about 95% and 84% CPU and wrote about 36 and 30 MiB/s. Redis logs show 7 background-save starts and successful completions in 20 minutes, with no related error. These children are production persistence work and must not be stopped.
- Short `docker`, `runc`, `redis-cli`, and `clickhouse-client` processes correspond to existing container healthchecks and management activity; they are not safe-stop candidates.

### ClickHouse state

Three HTTP diagnostic attempts returned 403 and were not retried with credentials. No Secret was sought or read. A local container client already used by the production healthcheck was then used with `readonly=2`, `max_threads=1`, 64 MiB memory, 10 MiB read cap, 3-second execution cap, and 5-second command timeout.

At 11:59:41 UTC:

- active merges: 0;
- unfinished mutations: 0;
- active inserts: 0;
- one other SELECT visible besides the diagnostic query;
- `Merge` and `BackgroundMergesAndMutationsPoolTask` metrics were 0 at that instant.

At 12:00:07 UTC:

- two other SELECTs were active;
- maximum elapsed time was about 7.161 seconds;
- aggregate read was about 11.70 million rows / 1.22 GB;
- aggregate query memory was about 3.40 GB;
- 16 query-pipeline executor threads were active and scheduled;
- 48 MergeTree background executor threads were active/scheduled, although `system.merges` was empty.

Conclusion: the observed ClickHouse CPU was not caused by an active merge, mutation, or import at the point-in-time probes. It was associated with production SELECT workload and other MergeTree background executor work. There was no explicit queue metric proving queued queries, but the active production reads and thread occupancy are sufficient to keep the business-data probe closed now.

### Old research process review

One candidate requires owner review:

```text
PID 3939955
cwd=/root/closed_source_shadow_tracker
started=2026-07-07 05:52:19 UTC
CPU approximately 0%
RSS approximately 10 MiB
child PID 1256193, command name docker
```

It is not classified as safely stoppable because its owner/purpose and child Docker operation are unverified. Cursor/Codex sessions and all `/opt/EATLQ` or production-container processes are also not safe-stop candidates. Therefore `confirmed_safely_stoppable_old_research_processes = 0`.

## 5. 修改文件

- `reports/BASE_LIVE_CAPACITY_DIAGNOSTIC.md`
- `docs/BASE_CANONICAL_HEARTBEATS.md`
- `scripts/observe_live_capacity.sh`
- bounded raw evidence under `reports/raw/live_capacity_20260717T1055Z/` and `reports/raw/live_capacity_20260717T1146Z/`

No production file was edited.

## 6. 查询的数据源

- Linux `/proc` host/process/disk/memory counters.
- Docker status and targeted metadata.
- Existing Docker log streams, reduced locally to event counts without emitting log bodies.
- Redis operational metadata only: `XINFO CONSUMERS signals execution_group`; no signal entry or field was read.
- ClickHouse `system.processes`, `system.merges`, `system.mutations`, and `system.metrics` only; no business database/table was queried.
- Production source code and Compose variable names, read-only, to establish heartbeat semantics.

## 7. 执行的只读查询

- 19 Redis consumer metadata reads over the complete window.
- Three unauthenticated HTTP ClickHouse attempts returned 403.
- One container-local ClickHouse query failed at parse time because `currentQueryID()` is unsupported.
- Two corrected system-only ClickHouse queries completed within one second each and under all declared limits.
- Executor/Redis log bodies were processed only into aggregate pattern counts; no raw business event was reported.

## 8. 测试结果

- Monitor syntax: passed with `bash -n`.
- Complete monitor: exit code 0, 119 samples, `GUARD_NOT_TRIGGERED`.
- Automatic low-CPU guard: no 30-second breach.
- Swap-growth guard: no breach.
- IO-wait guard: no breach.
- Market-data/archiver freshness guard: no sustained breach.
- A preliminary 471-second run stopped on one transient missing archiver stat. Container PID/start time/restart count proved no restart; the guard was corrected to require 30 seconds of sustained missing/stale heartbeat, matching Docker retry semantics. The complete rerun had zero missing samples.

## 9. Golden 结果

`NOT_RUN_PHASE_GATED`

## 10. 数据质量

Host diagnostic quality: `A-` for the complete 596-second 5-second-cadence window. Process attribution is sampled for 1 second every 30 seconds and is therefore directional, not a complete CPU accounting. ClickHouse system state is point-in-time rather than a full query-history audit.

## 11. UNKNOWN 和缺失

- No independent positive heartbeat for successful risk checks, order reconciliation, or position monitor cycles.
- Signal bridge has no Docker healthcheck.
- ClickHouse queue latency and historical query attribution were not inspected; query text and query log were deliberately excluded.
- PID 3939955 ownership and purpose are unverified.
- Authoritative Launchpad Intelligence code commit/bundle is still missing.

## 12. 是否触发健康守卫

The complete diagnostic did not trigger a guard. The preliminary run triggered a conservative single-stat guard; production did not restart or become unhealthy, and the guard logic was corrected before the complete run.

## 13. 是否修改生产状态

No production code, configuration, process priority, service, container, database row, Redis key/stream, checkpoint, log, firewall, scheduler, wallet, order, or transaction was modified. Evidence:

- all production container IDs/statuses remained stable and `Up`;
- archiver/ingestor restart counts remained 0 and OOM flags false at the guard review;
- the diagnostic wrote only under `/root/lpi_base_production_safe_node`;
- Redis commands were informational reads; ClickHouse queries used `readonly=2` and only `system.*` tables;
- no Secret-bearing configuration or environment was read.

Operational metadata commands inevitably create transient client connections and server command counters; no persistent production data or configuration was changed.

## 14. 当前阻塞

Offline Bootstrap is no longer blocked by live capacity. Actual code import remains blocked until an authoritative Git commit, git bundle, or SHA-256 package is supplied and dependency files pass preflight.

Database probing remains paused because ClickHouse had concurrent production SELECTs and because risk/order/position success lacks independent canonical observability. No Clanker query is authorized by this report.

## 15. 下一步最小安全任务

The next permitted task is isolated, database-free code Bootstrap only:

1. verify an authoritative Git commit/bundle/package hash;
2. inspect dependency and install hooks without executing them;
3. import code into this workspace only;
4. create a local venv only after confirming no global or production dependency mutation;
5. run offline tests with one process/thread, 384 MiB, low CPU/IO priority, and a 10-minute timeout;
6. perform live-health checks before and after.

Do not perform a database schema probe until a just-in-time ClickHouse preflight shows no competing heavy SELECT workload and the missing heartbeat semantics are accepted or supplied by the production owner.

## Evidence files

- Complete samples: `reports/raw/live_capacity_20260717T1146Z/capacity_samples.tsv`
- Process samples: `reports/raw/live_capacity_20260717T1146Z/pidstat_samples.txt`
- Process aggregates: `reports/raw/live_capacity_20260717T1146Z/process_aggregates.txt`
- Executor consumer samples: `reports/raw/live_capacity_20260717T1146Z/executor_consumer_samples.txt`
- Guard result: `reports/raw/live_capacity_20260717T1146Z/guard_events.txt`
- Preliminary conservative run: `reports/raw/live_capacity_20260717T1055Z/`
