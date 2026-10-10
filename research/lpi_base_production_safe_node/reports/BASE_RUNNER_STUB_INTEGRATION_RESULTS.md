# Base Runner Stub Integration Results

All production-adjacent commands in tests use injected stub executors.
No real docker/redis/clickhouse-client invocations.

## Coverage

- clean initial seal success
- unrelated untracked rejected at executor call_count=0
- allowlisted evidence after seal passes re-check
- critical tracked mutation fails re-check
- real blob mismatch via assume-unchanged + byte change
- HEAD/tree drift fails
- outside-allowlist untracked fails
- symlink in allowlisted output fails
- docker/redis/clickhouse command_class recorded via executor
- pre-health blocked => exit 10 semantics
- schema complete + post-health blocked => exit 30; four health TSV names present
- post-health UNKNOWN => exit 31
- partial schema => 20/21, not complete-safe
- bundle export provenance fields contract

## Offline suite

See timestamped `reports/raw/offline_tests_*` from this closeout round.

## Latest offline suite

 — see combined.txt for counts/RSS/sockets.
