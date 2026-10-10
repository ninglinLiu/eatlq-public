# Base Bootstrap Health Comparison

This document separates **observed health facts** from **stage authorization**.
They must not be collapsed into a single PASS/PAUSE label.

```text
round_health_observation = STABLE_NO_RESTART_NO_PENDING_GROWTH
next_stage_authorization = NOT_AUTHORIZED_FOR_DATABASE_PROBE
gate_decision = KEEP_DATABASE_GATE_CLOSED
```

## Round health observation (facts only)

Derived from sealed snapshots (see Raw evidence). No new live probes were taken in the correction round.

| metric | before | after |
|---|---|---|
| timestamp_utc | 2026-07-17T13:11:03Z | 2026-07-17T13:15:43Z |
| load_1m | 7.25 | 7.76 |
| MemAvailable_kb | 18228392 | 17353264 |
| swap_used_kb | 2249756 | 2248476 |
| swap_delta_kb |  | -1280 |
| disk_free_bytes | 115866628096 | 115919515648 |
| executor pending | 0 | 0 |
| executor idle_ms | 707 | 1872 |
| clickhouse active queries | 0 | 1 |
| clickhouse active merges | 0 | 0 |

### Container RestartCount

```text
all listed eatlq-* containers: 0 -> 0 (delta=0)
```

### Observation summary

* Executor consumer pending remained 0; idle advanced.
* No RestartCount increase.
* Swap did not grow (delta -1280 KiB).
* One ClickHouse active query was visible in the after snapshot; this is an observation only, not attributed as a production fault caused by this task, and not used to authorize further DB work.
* These facts support: `round_health_observation = STABLE_NO_RESTART_NO_PENDING_GROWTH`.
* These facts do **not** alone prove absolute absence of every possible production write.

## Next-stage authorization

```text
next_stage_authorization = NOT_AUTHORIZED_FOR_DATABASE_PROBE
```

Reasons (independent of health observation):

* Codex review required SQL identifier hardening and evidence sealing first.
* `database_access_enabled` remains false.
* Clanker candidate table remains logical/unbound.
* No EXPLAIN/business-table/metadata probe is approved.

## Gate decision

```text
gate_decision = KEEP_DATABASE_GATE_CLOSED
ready_for_bounded_database_probe = false
ready_for_micro_batch_backfill = false
```

Do not interpret a stable health observation as `SAFE_TO_PROCEED` into database probe work.

## Raw evidence (sealed)

```text
before = reports/raw/bootstrap_health_pre_20260717T1311Z/snapshot.txt
after  = reports/raw/bootstrap_health_post_20260717T1315Z/snapshot.txt
register = evidence/RAW_EVIDENCE_REGISTER.md
inventory = evidence/raw_evidence_inventory.tsv
```
