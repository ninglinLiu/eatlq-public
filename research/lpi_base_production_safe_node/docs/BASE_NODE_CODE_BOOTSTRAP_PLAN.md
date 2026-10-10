# Base Node Code Bootstrap Plan

Status: `BLOCKED_MISSING_AUTHORITATIVE_CODE_BUNDLE` for formal source import  
Workspace gate: passed  
Database gate: closed

## Fixed workspace

```text
/root/lpi_base_production_safe_node/
├── source/          # authoritative source only after verification; currently empty
├── runtime/venv/    # isolated Python 3.12 venv; no project dependencies
├── docs/
├── reports/
├── scripts/
├── tests/           # Bootstrap infrastructure tests only
├── evidence/
├── export/
├── checkpoints/
├── logs/
└── var/
```

All code, caches, temporary files, evidence, test output, and future runtime output must remain under this root. `/opt/EATLQ`, its virtual environments, `.env`, logs, checkpoints, databases, Docker project, ports, Redis prefixes, and temporary paths are prohibited.

## Current stage decisions

| stage | state | reason |
|---|---|---|
| Isolated workspace | PASS | dedicated root exists outside all discovered repositories |
| Authoritative source receipt | BLOCKED | no Git commit delivery, git bundle, or manifest+SHA tar.zst was found |
| Untrusted prototype | QUARANTINED_BY_POLICY | 19 untracked files inventoried by metadata/hash only; not read, copied, imported, or executed |
| Dependency review | BLOCKED_FOR_PROJECT | no authoritative lock/dependency files supplied |
| Empty venv | PASS | isolated stdlib venv created without project package installation or network use |
| Bootstrap audit tooling | PASS | 5 offline synthetic tests passed |
| Authoritative project tests | NOT RUN | source and test suite absent |
| Base adapter scaffolding | BLOCKED | shared Schema and Adapter protocols are not verified |
| Database probe | CLOSED | explicitly outside Phase 1–2 and independently not ready |

## Receipt and validation sequence

After an approved Bundle is placed under this workspace:

1. take a short live-health snapshot;
2. compute the artifact SHA-256 without unpacking;
3. compare it to an out-of-band expected SHA-256;
4. validate every required Manifest field and version;
5. for git bundle, run `git bundle verify` without checkout;
6. list archive members without extraction and reject absolute paths, `..`, devices, FIFOs, hard-link escapes, and symlink escapes;
7. extract only into a new directory under `var/` with no overwrite of `source/`;
8. inventory file modes, executables, submodules, dependency files, install hooks, URLs, database defaults, Secret access, production paths, Docker/systemd/cron commands, and Robinhood fixed paths;
9. run `scripts/audit_bootstrap_candidate.py` against the extracted candidate;
10. compare source tree/hash and dependency lock hash with the Manifest;
11. take a post-validation health snapshot;
12. only after all checks pass, atomically designate the verified candidate as the `source/` baseline;
13. audit dependencies and network hosts before any install;
14. install only into `runtime/venv`, one process/thread, low priority, 384 MiB, bounded time;
15. run only offline tests with socket connections blocked and all caches under `var/`;
16. take post-install and post-test health snapshots.

No step authorizes a database connection, RPC/HTTP collection, Docker service, worker, dashboard, scheduler, Clanker query, Golden collection, or backfill.

## Resource wrapper

Future commands must use:

```text
nice = 19
io_class = idle
process_concurrency = 1
test_concurrency = 1
max_threads = 1
memory_limit <= 384 MiB
command_timeout <= 10 minutes
```

Thread variables must be fixed at 1. The environment must be constructed without production variables, and `PYTHONNOUSERSITE=1` must remain enabled.

## Base node boundary after source verification

Only after shared interfaces are verified may Base-specific empty implementations be added under the authoritative layout equivalent of `launchpad_intelligence/nodes/base/`. They must default all external access and backfill flags to false and expose bounded block range, projection, row limit, timeout, thread, read-byte, checkpoint, dry-run, and explain-only controls. No production endpoint, credential, username, database, or table name may be hardcoded.
