# PROTECTED_LIVE_ALPHA Inventory (read-only)

Audit UTC: 2026-07-18T13:47Z  
Project: `BASE_ALPHA_ATLAS_30_v0`  
Production root: `/opt/EATLQ`  
Compose project prefix: `eatlq-*`

## Role map

| Asset | Role | Identity | Status (snapshot) | RestartCount | Notes |
| --- | --- | --- | --- | --- | --- |
| `eatlq-executor-1` | Executor / risk / order / position | container + `python -u services/executor/src/main.py` | running since 2026-07-17T04:30Z | 0 | LIVE mode; no Docker healthcheck |
| `eatlq-signal-bridge-1` | Live Alpha signal source (KEYSTONE) | container + `bridge.py` | running since 2026-06-29 | 0 | Emits to Redis `signals`; queries ClickHouse |
| `eatlq-ingestor-1` | Market data ingest | container `ingestor` | Up healthy ~3w | 0 | Writes Redis streams |
| `eatlq-archiver-1` | Archive → ClickHouse | container `archiver` | Up healthy ~12d | 0 | Consumes discovery ticks |
| `eatlq-swap-poller-1` | Pool swap polling | container / host python | Up ~12d | 0 | High host CPU contributor |
| `eatlq-pool-discovery-1` | Pool discovery | container | Up ~4w | 0 | Seeds Redis `pool:discovered:*` |
| `eatlq-redis-1` | Queue / state | container redis:7.4 | Up healthy ~5w | 0 | Streams: `market_ticks`, `market_ticks_discovery`, `signals` |
| `eatlq-clickhouse-1` | Analytics / bridge features | container CH 24.12 | Up healthy ~4w | 0 | Shared with live bridge; often 1–2+ cores busy |
| `eatlq-postgres-1` | Relational state | container postgres:16 | Up healthy ~4w | 0 | Not probed beyond liveness |
| `eatlq-dashboard-1` | Local dashboard | `127.0.0.1:8090` | Up ~4w | 0 | Localhost only |
| `eatlq-telegram-bot-1` | Alerts | container | Up ~3w | 0 | Daily summary still firing |
| `eatlq-autoheal-1` | Container heal | container | Up healthy ~5w | 0 | |
| `eatlq-strategy-1` | Legacy strategy | exited 137 | Exited ~5w | 0 | Replaced by signal-bridge for live path |
| `eatlq-wallet-scorer-1` | Legacy | exited 137 | Exited ~5w | 0 | Not part of current live path |
| `eatlq-shadow-closer-1` | Legacy | exited 137 | Exited ~5w | 0 | Not part of current live path |

## Protected Redis keys / streams (do not write)

- Streams: `market_ticks`, `market_ticks_discovery`, `signals`, `signals_dead_letter`
- Panic: `SYSTEM_PANIC`
- Positions: `positions:*`
- Bridge dedupe / blacklist keys used by live bridge
- Any `exec:*` legacy keys

## Ports reserved by production / host

- `127.0.0.1:8090` — EATLQ dashboard
- `127.0.0.1:3000` — unrelated next-server (host)
- Container-internal Redis/CH/Postgres (not published broadly)

## Live Alpha economic identity (non-secret summary)

`PROTECTED_LIVE_ALPHA` for this host is the KEYSTONE path:

```text
pool-discovery → signal-bridge (CH features + ML p_fatal) → Redis signals → executor (LIVE)
```

Atlas candidates must treat this as a protected benchmark, not a code base to extend.

## Heartbeat signals used by this audit

| Signal | Source | Interpretation |
| --- | --- | --- |
| Executor container status + RestartCount | `docker inspect` | Process liveness |
| `signals` group lag/pending | Redis `XINFO GROUPS` | Executor consumption health |
| `market_ticks_discovery` tip age | Redis `XREVRANGE` | Market data freshness |
| `SYSTEM_PANIC` exists | Redis `EXISTS` | Buy block / drawdown latch |
| Archiver / ingestor Docker health | `docker ps` | Ingest+archive path |
| ClickHouse `system.processes` count | read-only SQL | Query pressure |

## Explicit non-actions

This inventory did **not** read `.env`, private keys, wallet secrets, API secrets, process environments, or production config bodies. No Redis/DB writes. No process signals.
