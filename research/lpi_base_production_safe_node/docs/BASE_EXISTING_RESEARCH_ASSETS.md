# Base Existing Research Assets

Audit scope: filenames and repository metadata only; no data contents or code contents were read.

## Potential Base assets

| asset | location | observed evidence | usability now |
|---|---|---|---|
| Production ClickHouse | Docker volume `/var/lib/docker/volumes/eatlq_clickhouse_data/_data` | healthy container; numerous tracked schema/view filenames | prohibited; no query performed; schema and workload boundaries unverified |
| Production Postgres | Docker volume `/var/lib/docker/volumes/eatlq_postgres_data/_data` | healthy container; tracked init/backfill filenames | prohibited; no query performed |
| Production Redis | Docker volume `/var/lib/docker/volumes/eatlq_redis_data/_data` | healthy container | prohibited; semantics and prefixes unverified |
| Base backfill watermark | `/opt/EATLQ/mvp/state/base_v3_backfill_watermark.json` | tracked filename | production checkpoint; must never touch; content not read |
| Base left-tail research | `/opt/EATLQ/scripts/research/base_lefttail/` | tracked filenames for forward paths, pools, pricing, sizing and walk-forward analysis | production repository asset; no execution or reuse |
| Broader research/failure records | `/root/quant_research_os/{weekly,monthly,global,state}` | filenames include failure JSONL and state checkpoints | chain relevance unverified; not treated as Base source |
| Untracked LPI prototype | `/root/quant_research_os/web/launchpad_intelligence/` | 19 untracked files | provenance missing; cannot bootstrap from it |
| Research archive | `/opt/eatlq-research-archive` | clean Git metadata at bounded check | contents not audited due resource guard |

## Bounded offline-snapshot check

A bounded filename scan of `/root/quant_research_os` and `/opt/eatlq-research-archive`, pruning `.git`, `node_modules`, and `.next`, did not identify Parquet, Arrow, Feather, DuckDB, or SQLite files in the reported result set. This is not proof of absence across the whole host because the scan was depth-bounded and stopped under the resource guard.

## Platform-specific assets

No verified Clanker, Doppler, Flaunch, Zora Coins, Bankr, or Virtuals/Genesis code/data asset was identified in the production Git index filename filter or the bounded LPI prototype listing. Contract registries and event semantics remain unverified. No external evidence lookup was performed in this round.

## Data-source decision

No local data source is approved for Launchpad Intelligence access. ClickHouse/Postgres/Redis remain production-only until a bounded, cancellable, owner-approved read-only route and resource budget are demonstrated. Phase 4 has not started.
