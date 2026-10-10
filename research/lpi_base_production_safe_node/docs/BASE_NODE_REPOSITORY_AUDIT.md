# Base Node Repository Audit

Status: `BLOCKED_MISSING_CODE_BASELINE`

## Discovered Git repositories

| repository | role assessment | branch / commit | state | worktrees | decision |
|---|---|---|---|---|---|
| `/opt/EATLQ` | live production execution/collection/data repository | `sync-before-bnb-local-lab-20260511` / `90077ef020315e2490833c328d46941d6a24d996` | dirty; 30 tracked status entries including executor, ingestor, signal bridge, pool discovery, Compose and audit/diagnostic files | one | `must_never_touch`; no LPI development |
| `/root/quant_research_os/web` | independent research/web repository | `main` / `20b62fead8041e025a3c530957000b0b2de780b4` | dirty tracked web files; `launchpad_intelligence/` is untracked | one | candidate evidence only; not a verified baseline |
| `/opt/eatlq-research-archive` | separate research archive | `main` / `8b8aa06dab104c6f0ef60ba75976050abddfe028` | no tracked changes reported by bounded check | one | read-only until ownership and scope verified |
| `/root/.codex/.tmp/plugins` | tool/plugin cache | not audited further | tool-managed | unknown | out of scope |

## Production repository observations

Filename-level inspection of the Git index found:

- ClickHouse initialization/schema files `001_raw_events.sql` through multiple normalized, registry, decoded, wallet, training, outcome, and discovery views;
- Postgres initialization and backfill-state schema files;
- Base research scripts under `scripts/research/base_lefttail/` and other research scripts;
- a tracked backfill watermark at `mvp/state/base_v3_backfill_watermark.json`;
- production executor, ingestor, archiver, signal bridge, pool discovery, dashboard and operational audit assets.

No file content, database content, or unbounded untracked-file scan was performed. The dirty production tree is a hard isolation reason regardless of whether its changes are intentional.

## Existing Launchpad Intelligence candidate

`/root/quant_research_os/web/launchpad_intelligence` contains 19 working-tree files, including YAML configs, `pyproject.toml`, README, and Python modules for registry, discovery, attribution, models, and lifecycle.

However:

- `git ls-files launchpad_intelligence` returned zero files;
- `git status` reports `?? launchpad_intelligence/`;
- `git ls-tree HEAD launchpad_intelligence` returned no tree object;
- no commit, tree hash, source repository, authoritative branch, bundle hash, schema version, adapter API version, manifest version, test count, or source test result can be attributed to these files;
- no Golden validator, Base adapter, evidence exporter, or tests were identified in the bounded filename listing.

Therefore this directory must not be copied or treated as the master-node shared code baseline. Phase 2 remains `BLOCKED_MISSING_CODE_BASELINE`.
