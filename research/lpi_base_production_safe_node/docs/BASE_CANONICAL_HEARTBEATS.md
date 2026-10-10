# Base Canonical Heartbeats

Status captured: 2026-07-17 12:00 UTC  
Scope: existing production signals only; no production code or configuration change

## Heartbeat map

| component / responsibility | existing canonical signal | cadence / freshness | semantic meaning | observed result | confidence |
|---|---|---|---|---|---|
| Executor process | Docker container/PID liveness | continuous | process exists; does not prove loop progress | stable for full window | medium |
| Executor main loop | Redis consumer `executor-1` in group `execution_group` on stream `signals`; inspect consumer idle/pending only | loop uses blocking reads of about 2 seconds | low idle proves the loop reaches `XREADGROUP`; pending shows unacked backlog | 19 samples, idle 177–1,894 ms, pending 0 | high for loop liveness |
| Executor operator notification | `Keystone 心跳` Telegram message | `EXECUTOR_HEARTBEAT_SEC`, Compose default 86,400 seconds | daily liveness summary with open position count, daily PnL and panic state | no message/log expected in a two-hour idle interval | high semantics; not locally observed |
| Position monitor | executor `_monitor_once` | nominal `EXECUTOR_MONITOR_INTERVAL_SEC`, Compose default 5 seconds | inspects and manages open positions before the Redis read | code path confirmed; no independent positive timestamp exported | partial |
| Position-monitor failure | `monitor cycle failed (consecutive=N)` log plus Telegram alert after configured streak | on exception; alert threshold default 3 | negative heartbeat: detects repeated failed cycles | zero failure lines in final 20-minute aggregate | medium; absence is not positive proof |
| Risk checks | integrated executor preflight/external-risk path | event-driven when a candidate signal is processed | validates a candidate before execution | no independent idle heartbeat; no signals in window | unavailable during idle |
| Order management | integrated executor buy/sell/pending-recovery paths | event-driven; pending recovery nominally every 60 seconds | order/reconciliation work when relevant state exists | no independent success timestamp exported | partial |
| Market-data ingestion | `/tmp/ingestor_healthy` inside ingestor container; Docker freshness check `<2 minutes` | writer ticks/update on empty or successful batch; pool-health path may also touch file | ingestion writer/publisher loop remains responsive; separate stall watchdog exits after block fetch stops advancing | age 0–1 second across complete window; Docker healthy | high for writer liveness |
| Chain block progress | in-process `LAST_BLOCK_PROGRESS_MS` stall watchdog | watchdog evaluates every 30 seconds using configured stale/grace thresholds | successful `eth_getLogs` block fetch has occurred recently; exits process on sustained stall | process/container remained healthy; value is not externally exported | medium |
| Archiver | `/tmp/archiver_healthy`; Docker freshness check `<2 minutes` | updated after successful `XREADGROUP` even when empty and after successful flush | main Redis/ClickHouse archival loop is advancing; prevents idle false positives | age 0–29 seconds; Docker healthy | high |
| Signal bridge | container liveness and log progress only | unknown | bridge process exists; no Docker healthcheck | container remained Up; no canonical positive heartbeat found | low |
| ClickHouse service | Docker healthcheck `SELECT 1` | every 15 seconds, 10-second timeout, 5 retries | server accepts a trivial query | Docker healthy | high availability; does not prove spare capacity |
| Postgres service | Docker `pg_isready` | every 10 seconds | server accepts readiness connection | Docker healthy | high availability; does not prove application progress |
| Redis service | Docker `redis-cli ping` | every 10 seconds | Redis responds | Docker healthy | high availability; does not prove stream semantics |

## Executor log-silence determination

The executor's lack of routine log output for approximately two hours is consistent with normal idle operation:

1. its configured operator heartbeat defaults to once per day;
2. `_monitor_once` does not emit a success log on every 5-second cycle;
3. the Redis consumer idle value stayed below two seconds throughout 19 samples, proving repeated loop progress;
4. pending remained zero and no monitor-failure or panic line was counted;
5. the executor PID/container did not restart.

Therefore `executor_two_hour_log_silence = NORMAL_IDLE_WITH_LOOP_PROGRESS`, not a stopped executor.

## Canonical status by required responsibility

```text
execution_heartbeat = CONFIRMED_COMPOSITE
market_data_heartbeat = CONFIRMED
archiver_heartbeat = CONFIRMED
risk_heartbeat = BLOCKED_MISSING_POSITIVE_IDLE_SIGNAL
order_manager_heartbeat = PARTIAL_EVENT_DRIVEN_ONLY
position_sync_heartbeat = PARTIAL_NO_EXPORTED_SUCCESS_TIMESTAMP
```

The executor composite is:

```text
container_alive
AND redis_consumer_idle_ms < 10_000
AND pending_not_growing
AND no recent monitor_cycle_failed streak
AND no SYSTEM_PANIC escalation attributable to monitor failure
```

This is suitable for offline Bootstrap health checks. It is not yet strong enough for automated database micro-batch admission because a loop can continue reaching Redis even after a caught `_monitor_once` exception; only the negative failure log reveals that condition.

## Minimum missing information

To open automated micro-batch admission without changing production, the production owner should identify any existing local metric, file, or state key that records:

- timestamp of the last fully successful `_monitor_once` cycle;
- timestamp/result of the last applicable risk preflight, explicitly distinguishing `IDLE_NO_SIGNAL` from failure;
- timestamp/result of order/pending reconciliation, explicitly distinguishing `NO_OPEN_ORDER` from failure;
- timestamp/result of position synchronization, explicitly distinguishing `NO_OPEN_POSITION` from failure;
- canonical signal-bridge progress indicator.

If no such signals exist, this report does not authorize adding them to production. A separate, explicit production-change approval would be required.

## Read-only observation procedure

For future pre/post checks, without reading event contents:

1. record Docker container ID, PID, StartedAt, RestartCount, OOMKilled, and health;
2. stat ingestor and archiver health-file mtimes through `/proc/<pid>/root`, allowing Docker-equivalent retries rather than failing on one atomic-write race;
3. read only Redis `XINFO CONSUMERS` metadata for the executor consumer; do not call `XREAD`, `XRANGE`, or inspect fields;
4. count executor error-pattern categories without emitting log bodies;
5. apply CPU/Swap/IO guards and stop only the research task if they fire;
6. never signal, reprioritize, restart, attach, debug, or trace a production process.
