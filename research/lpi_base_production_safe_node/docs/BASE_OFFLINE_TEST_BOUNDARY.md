# Base Offline Test Boundary

## Allowed after authoritative source verification

- Schema validation;
- Registry behavior;
- Adapter protocol conformance using synthetic sources;
- Evidence Manifest and Remote Bundle validation;
- Golden Validator synthetic fixtures;
- Lifecycle state machine and UNKNOWN preservation;
- static compilation;
- Secret scanning;
- file/path isolation;
- access-default tests proving database/RPC/business-table/backfill flags are false.

## Prohibited

- RPC, HTTP, ClickHouse, Postgres, Redis integration, or any production endpoint;
- Redis stream contents, orders, positions, wallet data, or live configuration;
- Docker Compose, dashboard, worker, service, cron, or systemd start;
- historical backfill, real Golden collection, Clanker discovery, paper forward, or live simulation.

## Fail-closed runtime

Offline tests must run with:

- sanitized environment and `PYTHONNOUSERSITE=1`;
- all thread variables set to 1;
- `PYTHONPYCACHEPREFIX` and `TMPDIR` under this workspace;
- one process, no pytest workers, low CPU/IO priority, 384 MiB memory, 10-minute timeout;
- socket `connect`, `connect_ex`, and `create_connection` blocked by the offline test guard;
- no external command that provides network or service access.

Any test requiring credentials, endpoint values, external fixtures, integration markers, Docker, or a live database must fail closed and remain unexecuted.

## Current result scope

Five Bootstrap-infrastructure synthetic tests passed. They validate the future candidate auditor, escaping symlink detection, production-path/service-command blocking, network-host inventory without connection, dependency counting, and socket fail-closed behavior.

They are not the authoritative Launchpad Intelligence test suite. Therefore:

```text
bootstrap_infrastructure_offline_tests_passed = true
authoritative_project_offline_tests_passed = false
ready_for_offline_tests = false
```

The final field remains false until the authoritative source and its declared tests are verified and pass inside this boundary.
