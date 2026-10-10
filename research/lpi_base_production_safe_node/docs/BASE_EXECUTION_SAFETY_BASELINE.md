# Base Execution Safety Baseline

Observation window: 2026-07-17 10:48:56–10:52:58 UTC  
Active passive sub-window: 2026-07-17 10:50:31–10:50:47 UTC  
Status: `BLOCKED_RESOURCE_RISK` and `BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY`

## Host baseline

| metric | observed |
|---|---|
| CPU | 8 logical cores |
| Memory | 23 GiB total; 16–18 GiB available during later samples |
| Swap | 4.0 GiB total; about 2.2 GiB already used; no active swap-in/out in the initial vmstat sample |
| Root filesystem | ext4, 193 GiB; 85 GiB used; 108–109 GiB free; 44–45% used |
| Initial load average | 7.28 / 5.67 / 4.91 |
| Final load average | 6.46 / 6.50 / 5.44 |
| Initial disk iowait | 0–0.39% in sampled intervals |
| Instantaneous CPU guard sample | average 76.85% user + 20.67% system, 2.01% idle; one interval reached 0% idle |
| Network delta over passive sub-window | about 214 KiB RX and 308 KiB TX across non-loopback interfaces; no traffic was injected |

The CPU guard crossed the safe threshold. No historical scan, database query, repository content scan, dependency install, test, or service was allowed after that result.

## Production process continuity

All 12 observed EATLQ containers retained the same container IDs and remained `Up` through the before/after checks. Existing Docker health remained `healthy` for archiver, ingestor, ClickHouse, Postgres, Redis, and autoheal.

Log metadata, without reading bodies:

- Advanced during the short window: signal bridge, archiver, ingestor, swap poller.
- Did not advance during the short window: executor, Telegram bot, pool discovery, ClickHouse Docker JSON log, Postgres Docker JSON log, dashboard, Redis Docker JSON log, autoheal.
- The executor has no Docker healthcheck. Its Docker log mtime was approximately two hours old at observation time. This does not prove failure, but it prevents a positive execution-health assertion.
- No safe, known checkpoint or open-order heartbeat path was identified.

## Required baseline fields

| field | result |
|---|---|
| execution_process_alive | yes, container/PID alive |
| execution_process_cpu/rss/threads | PID 1255380; about 0.1% lifetime CPU, 76,032 KiB RSS, 1 thread in sampled `ps` output |
| market_data_process_alive | inferred ingestor/pool discovery/swap poller alive; exact boundary unverified |
| risk_process_alive | `UNKNOWN_PRODUCTION_BOUNDARY` |
| order_manager_alive | `UNKNOWN_PRODUCTION_BOUNDARY` beyond executor container identity |
| database_health | Docker health reports ClickHouse and Postgres healthy; no query performed |
| redis_health | Docker health reports healthy; no Redis command performed |
| rpc_or_ws_health_if_visible | not safely visible; not probed |
| host_load_1m / 5m | range 5.36–7.28 / 5.64–6.50 |
| available_memory | approximately 15.0–18.9 GB in byte samples |
| swap_used | approximately 2.324–2.325 GB |
| disk_free | 108–109 GiB |
| disk_io_wait | low in samples, but CPU saturation observed |
| network_rx / tx | counters advanced naturally; no request injection |
| open_order_or_heartbeat_progress | unknown |
| latest_checkpoint_mtime | unknown |
| latest_log_mtime | captured only as metadata; mixed progress, executor stale relative to window |
| error_rate_from_existing_logs | unknown; log bodies deliberately not read |

## Gate decision

`SAFE_TO_PROCEED` is **not** granted. Phase 1, Bootstrap, source mapping, and all database work remain prohibited until both resource pressure and execution-heartbeat uncertainty are resolved by a later passive audit or owner-provided health signal.
