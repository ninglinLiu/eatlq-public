# Resource Budget — BASE_ALPHA_ATLAS_30_v0

## Defaults (hard)

```text
max_heavy_workers = 2          # raise to 3 only after proven safe stage run
nice = 19
io_class = idle                # ionice -c3
threads = 1
process_concurrency = 1
memory_limit <= 384 MiB
command_timeout <= 20 minutes
production_database_access = false
production_rpc_reuse = false
```

## Thread env (every job)

```text
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
NUMEXPR_NUM_THREADS=1
POLARS_MAX_THREADS=1
RAYON_NUM_THREADS=1
```

## Auto-pause → PAUSED_LIVE_PRIORITY

Stop all research workers immediately if any:

- Executor heartbeat / container stop or RestartCount change
- `signals` pending sustained growth
- Ingest / archive / risk-position path appears down
- Any protected `eatlq-*` container exit/restart
- CPU idle < 10% for continuous 30s
- Swap used bytes increase vs baseline open
- IO wait materially worsens vs baseline
- Available memory enters danger zone (< 2 GiB available)
- ClickHouse query queue / concurrent queries clearly elevated vs baseline
- RPC/WS health worsens (if observable without secrets)
- This project exceeds its own budget
- Impact on live cannot be judged

On pause: do **not** intervene in production services.

## Allowed start gate (offline research)

From Phase -1–0 10-minute passive baseline:

- Executor main path advancing / alive
- Pending not abnormally growing
- Risk / market / position path alive
- No production container restarts in window
- Mean CPU idle over 10 minutes **> 30%**
- Swap not growing
- IO wait low
- Disk safe
- ClickHouse without material query pile-up
