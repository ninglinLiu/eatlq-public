# Base Bounded Query Policy

Status: offline planner hardened; business-table execution **not** authorized.

## Hard requirements (offline-enforced on plans)

```text
readonly = true
strict SQL identifiers (schema.table / columns / partition key)
no whitespace, quotes, comments, or expression injection in identifiers
block span <= 1000
LIMIT <= 1000
max_threads == 1
1 <= statement_timeout_sec <= 10
1 <= max_bytes_to_read <= 8388608
dry_run = true
explain_only = true
execution_authorized = false
checkpoint under workspace checkpoints/ or var/tmp/ (no symlink escape)
```

## Offline-enforced vs future DB-side settings

| constraint | offline planner | future DB session (not implemented) |
|---|---|---|
| identifier syntax | enforced | n/a |
| block span / LIMIT caps | enforced | should also be enforced server-side |
| max_threads=1 | plan field only | must be session setting |
| timeout / max_bytes | plan field only | must be session setting |
| readonly | plan field only | must be readonly=2 or equivalent |

`estimated_read_bytes` is `offline_unverified_heuristic`, never a measured ClickHouse read.

## Tooling

```text
scripts/bounded_db_probe.py
launchpad_intelligence/query/preflight.py
```

`--execute` remains denied while access defaults are closed. No ClickHouse transport exists.

## Stage gate

```text
ready_for_bounded_database_probe = false
```
