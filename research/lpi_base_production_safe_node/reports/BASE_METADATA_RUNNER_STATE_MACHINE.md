# Base Metadata Runner State Machine

Future `scripts/run_metadata_inventory.py` behavior (live mode not executed this round).

## Commit-before-query seal

Before any Docker/Redis/ClickHouse call:

1. `git status --porcelain` must be empty
2. HEAD commit and tree must exist
3. Critical runner/modules must be in HEAD and byte-identical to HEAD blobs
4. Seal recorded into run_metadata (`execution_source_commit`, `execution_source_tree_hash`)
5. Seal re-checked at end; HEAD/tree drift fails closed

Critical paths include runner, metadata inventory/ranking, metadata health gate, auth gate, evidence manifest/bundle, execution_seal.

## Round status values

```text
DRY_RUN_COMPLETE
BLOCKED_PRE_HEALTH
BLOCKED_PRE_UNKNOWN
FAILED_SCHEMA_QUERY
PARTIAL_SCHEMA_INVENTORY
SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_SAFE
SCHEMA_INVENTORY_COMPLETE_POST_HEALTH_BLOCKED
EXECUTION_SOURCE_UNSEALED
INTERNAL_ERROR
```

## Exit codes

```text
0  = complete success AND post-health safe
3  = access denied
10 = pre-health blocked
11 = pre-health UNKNOWN
20 = schema query failed
21 = schema inventory incomplete
30 = post-health blocked
31 = post-health UNKNOWN
40 = execution source unsealed
1  = internal error
```

Post-health blocked after a complete schema inventory must return **30**, not 0.

## Health evidence files (future)

```text
query_results/health_before_processes.tsv
query_results/health_before_merges.tsv
query_results/health_after_processes.tsv
query_results/health_after_merges.tsv
```

No overwrite of before with after.

## Command log

Unified sanitized records for docker inspect, Redis XINFO, and ClickHouse system queries.
Redundant `redis-cli --raw` probe removed; only one XINFO CONSUMERS call remains.
