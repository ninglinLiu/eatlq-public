# Base Node Current State

Captured at: 2026-07-17 10:52:55 UTC

```text
overall_status = PAUSED_LIVE_PRIORITY
phase_minus_1_gate = BLOCKED_RESOURCE_RISK
production_boundary_gate = BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY
shared_code_gate = BLOCKED_MISSING_CODE_BASELINE
SAFE_TO_PROCEED = false
```

## Current facts

- Production is the Docker Compose project `eatlq`, running 12 containers (11 reported in `docker compose ls`, plus an EATLQ-named Telegram bot without the same Compose labels).
- The production repository is `/opt/EATLQ`, branch `sync-before-bnb-local-lab-20260511`, commit `90077ef020315e2490833c328d46941d6a24d996`, with 30 tracked status entries. It is not clean and must not be used for Launchpad Intelligence development.
- Production databases are Dockerized ClickHouse 24.12, Postgres 16-alpine, and Redis 7.4-alpine with named production volumes.
- Only the dashboard is host-published, on 127.0.0.1:8090. Database ports are container-internal. Caddy exposes 80/443 and SSH exposes 22.
- Host CPU pressure crossed the safety guard. Swap is materially occupied even though no ongoing swap-in/out was seen.
- Executor continuity is visible only as process/container liveness. A live execution heartbeat, risk heartbeat, order heartbeat, or checkpoint could not be identified safely.

## Isolated path decision

Reserved audit path:

```text
LPI_BASE_ROOT=/root/lpi_base_production_safe_node
```

Evidence at selection time:

- the path did not previously exist;
- `/root/.env` did not exist;
- the path is outside `/opt/EATLQ` and outside every discovered Git repository;
- `/root` is mode 0700 and owned by root;
- it shares the same root filesystem with production, so disk/IO isolation is not physical.

Only `docs/` and `reports/` were created for this audit. The path is `RESERVED_NOT_RELEASED`: it is not approved for code, venv, data, logs, checkpoints, jobs, services, containers, or queries until `SAFE_TO_PROCEED` is explicitly achieved.

## Next gate

The smallest safe next action is a later passive re-observation when host CPU has sustained headroom, plus owner confirmation of the canonical executor/risk/order/position health signal and checkpoint location. No Bootstrap or data-source mapping is authorized now.
