# Base Production Execution Inventory

Audit time: 2026-07-17 10:48–10:53 UTC  
Overall status: `PAUSED_LIVE_PRIORITY`  
Production project: Docker Compose `eatlq`  
Compose entrypoint: `/opt/EATLQ/infra/compose/compose.yaml`  
Production repository: `/opt/EATLQ`  

Role assignments below are inferred only from container/service names and existing Docker health metadata. No application config, environment variable, log body, database, Redis, RPC, wallet, or Secret was read.

| component_name | component_role | pid_or_container | start_command | working_directory | config_path | data_path | log_path | checkpoint_path | network_endpoints | database_dependencies | expected_resource_profile | health_signal | must_never_touch | confidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| eatlq-executor-1 | Live execution / order path (name-derived) | container `b3f77d…`; PID 1255380 | Python executable; arguments intentionally not read | `/app` | Compose entrypoint above; application config unknown | container filesystem; mounts none reported | Docker JSON log under `/var/lib/docker/containers/b3f77d…/` | `UNKNOWN_PRODUCTION_BOUNDARY` | internal `eatlq_eatlq-net`, 172.18.0.10 | `UNKNOWN_PRODUCTION_BOUNDARY` | Unknown | container alive; no Docker healthcheck; log did not advance in sample | yes | medium for role; low for health |
| eatlq-signal-bridge-1 | Signal transport (name-derived) | `ab7dc8…`; PID 3543651 | Python executable; arguments not read | `/app` | Compose entrypoint | container filesystem | Docker JSON log `ab7dc8…`; advanced | `UNKNOWN_PRODUCTION_BOUNDARY` | internal 172.18.0.7 | unknown | Unknown | alive; log advanced | yes | medium |
| eatlq-ingestor-1 | Market/on-chain ingestion (name-derived) | `39cfe9…`; PID 1139890 | `ingestor`; arguments not read | `/app` | Compose entrypoint | container filesystem | Docker JSON log `39cfe9…`; advanced | `UNKNOWN_PRODUCTION_BOUNDARY` | internal 172.18.0.9 | ClickHouse/Redis likely but unverified | Unknown | Docker healthy; log advanced | yes | medium |
| eatlq-pool-discovery-1 | Pool discovery / collection (name-derived) | `efd1bb…`; PID 3517052 | Python executable; arguments not read | `/app` | Compose entrypoint | container filesystem | Docker JSON log `efd1bb…` | `UNKNOWN_PRODUCTION_BOUNDARY` | internal 172.18.0.12 | unknown | Unknown | alive; no healthcheck; no log advance in short sample | yes | medium for role; low for health |
| eatlq-swap-poller-1 | Swap polling / market collection (name-derived) | `c34eda…`; PID 3387816 | Python executable; arguments not read | `/app` | Compose entrypoint | bind `/opt/EATLQ/services/pool_discovery/src` -> `/app/services/pool_discovery/src` | Docker JSON log `c34eda…`; advanced | `UNKNOWN_PRODUCTION_BOUNDARY` | internal 172.18.0.8 | unknown | Unknown | alive; log advanced | yes | medium |
| eatlq-archiver-1 | Archival / persistence (name-derived) | `f15aa2…`; PID 2600375 | `archiver`; arguments not read | `/app` | Compose entrypoint | container filesystem | Docker JSON log `f15aa2…`; advanced | `UNKNOWN_PRODUCTION_BOUNDARY` | internal 172.18.0.11 | ClickHouse likely but unverified | Unknown | Docker healthy; log advanced | yes | medium |
| eatlq-clickhouse-1 | Production ClickHouse | `42b3ad…`; PID 1727517 | clickhouse-server; arguments not read | image default | `/opt/EATLQ/infra/clickhouse/config/config.xml` and init directory | volume `/var/lib/docker/volumes/eatlq_clickhouse_data/_data` | Docker JSON log `42b3ad…`; internal logging may differ | database-managed | internal 172.18.0.5; container ports 8123/9000/9009 not host-published | production data store | Unknown; 772 threads observed; RSS varied materially | Docker healthy | yes | high identity; low workload boundary |
| eatlq-postgres-1 | Production Postgres | `dc140b…`; PID 1727523 | postgres; arguments not read | `/` | init bind `/opt/EATLQ/infra/postgres/init` | volume `/var/lib/docker/volumes/eatlq_postgres_data/_data` | Docker JSON log `dc140b…` | database-managed | internal 172.18.0.6; 5432 not host-published | production data store | Unknown | Docker healthy | yes | high |
| eatlq-redis-1 | Production Redis | `4a0fac…`; PID 886608 | redis-server; arguments not read | `/data` | Compose entrypoint; exact config unknown | volume `/var/lib/docker/volumes/eatlq_redis_data/_data` | Docker JSON log `4a0fac…` | Redis persistence path in volume; exact file unknown | internal 172.18.0.2; 6379 not host-published | production queue/cache/state | Unknown | Docker healthy | yes | high identity; low semantics |
| eatlq-dashboard-1 | Existing monitoring/dashboard | `43dbe9…`; PID 1532301 | Python executable; arguments not read | `/app` | Compose entrypoint | container filesystem | Docker JSON log `43dbe9…` | none identified | 127.0.0.1:8090 -> 8090; internal 172.18.0.4 | unknown | Unknown | alive; no healthcheck | yes | medium |
| eatlq-telegram-bot-1 | Existing notification bot | `d74f33…`; PID 2379930 | Python executable; arguments not read | `/app` | Compose association label absent; name indicates EATLQ | container filesystem | Docker JSON log `d74f33…` | unknown | internal 172.18.0.3 | unknown | Unknown | alive; no healthcheck | yes | medium |
| eatlq-autoheal-1 | Container auto-healing control plane | `6bc084…`; PID 3466710 | image entrypoint; arguments not read | `/` | Docker metadata | bind `/var/run/docker.sock` | Docker JSON log `6bc084…` | none identified | `eatlq_default`, 172.19.0.2 | Docker daemon | Unknown | Docker healthy | yes | high |
| caddy.service | Public reverse proxy | systemd PID 860 | not read | unknown | not read | unknown | existing system logs, not read | none identified | public 80/443; admin 127.0.0.1:2019 | unknown | Unknown | systemd running | yes | high identity |

Additional production-boundary findings:

- No Supervisor or PM2 task was reported by their status commands.
- Root crontab contains 5 active entries; command bodies were deliberately not displayed or stored.
- `/etc/crontab` has 5 active lines; `/etc/cron.d/e2scrub_all` 2, `staticroute` 3, `sysstat` 3. Their semantics remain `UNKNOWN_PRODUCTION_BOUNDARY`.
- Production checkpoint paths, risk process identity, order-manager identity, position-sync identity, RPC/WS endpoints, health thresholds, and database dependency edges remain unknown.
- All listed production assets and `/opt/EATLQ` are `must_never_touch` until an owner-supplied boundary map is verified.
