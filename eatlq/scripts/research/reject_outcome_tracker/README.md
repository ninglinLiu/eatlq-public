# Reject-Outcome Tracker

Purpose: for every pretrade rejection the live executor makes (honeypot/closed-source
flags, sim-unavailable, cluster blacklist, friction, depth, etc.), record what actually
happened to that pool's price afterwards. This is the "would we have missed a winner or
dodged a rug?" dataset that never existed before (see 2026-07-05/07 conversation:
`closed_source`/`external_risk` had never been backtested, on/off, against real outcomes —
only current-state scanner data, no historical snapshot).

## Safety model (read this before touching anything)

This tool is **strictly observational** and cannot affect live trading:

- **No RPC calls, no wallet, no chain writes.** It never imports `web3` or touches any
  key/secret. It only shells out to `docker logs`, `docker exec clickhouse-client`, and
  `docker exec psql`.
- **No writes to any existing table.** It only writes to a brand-new, isolated table:
  `app.reject_outcome_tracking` (see `schema.sql`). It never touches `positions:*`,
  `signals`, `exec:*`, or any executor/strategy config.
- **Does not touch `services/executor` or `services/signal_bridge` code or env.** Entry/exit
  logic, gates, and thresholds are completely untouched by this tool.
- Runs as a **separate cron job** (`run.sh`, added to `infra/cron/crontab.sealed` under
  "ALLOWED: free-RPC maintenance"), independent of the live services' own scheduling.

## What it does

1. `ingest.py` — tails `docker logs eatlq-executor-1` since the last checkpoint, finds
   lines like `executor BUY blocked by pretrade gate pool=0x... reason=...`, classifies the
   reason into a category (`closed_source_high`, `sim_unavailable`, `cluster_blacklist`,
   `friction`, `depth`, `external_risk_other`, `honeypot_other`, `other`), looks up pool
   metadata from the (read-only) `pool:discovered:*` Redis hash, and inserts one row per
   rejection into `app.reject_outcome_tracking`.
2. `snapshot.py` — for every tracked row not yet finalized, queries ClickHouse's swap
   archive (already collected by the live ingest/archiver pipeline — no extra RPC) for the
   token's price (via tick) at +1h/+6h/+24h/+72h after rejection, the peak/trough excursion,
   and whether the pool is still trading. Marks a row `done` once 72h have elapsed.
3. `report.py` — prints a summary table grouped by rejection category: sample size, average
   24h return, average peak return, how many "mooned" (>+20% at 24h), how many pools died.

## Running manually

```bash
cd /opt/EATLQ
python3 scripts/research/reject_outcome_tracker/ingest.py
python3 scripts/research/reject_outcome_tracker/snapshot.py
python3 scripts/research/reject_outcome_tracker/report.py
```

## Deployed schedule

`*/15 * * * * /opt/EATLQ/scripts/research/reject_outcome_tracker/run.sh` (see
`infra/cron/crontab.sealed`). Logs to `logs/reject_outcome_tracker.log`.

## Interpreting results (once enough samples accumulate)

- `reject_category = closed_source_high`, high average 24h/peak return, low death rate
  → the closed-source+high-severity gate is likely too aggressive (false-positive heavy),
    supports the idea of downgrading it to a canary/small-size allow instead of hard reject.
- Low/negative average return, high death rate → the gate is earning its keep; leave it as
  a hard reject.
- **Do not act on this after a handful of samples.** The whole reason this tool exists is
  that a single anecdote (ALM-PROMO) proves nothing; wait for at least ~30-50 rows per
  category before drawing conclusions, per the same statistical-rigor standard used
  elsewhere in this codebase (see `mvp/audits/failed_no_7d_alpha_guard.md`).
