# Compose service profiles (MM-09)

Generated at: 2026-05-02  
Scope: documentation-only. No `docker compose up`, no ClickHouse/Archiver startup requirement stated here.  
Source file: `infra/compose/compose.yaml`  
MM reference: MM-09 (Hybrid Architecture Compose Profiles)

---

## Rule: ClickHouse and Archiver are not “always-on” narratively

**Stable contract phrase:** **ClickHouse and Archiver are not mandatory 24/7 services for every deployment narrative.**

In `infra/compose/compose.yaml`, **clickhouse** and **archiver** are defined **without** a `profiles:` block, so a default `docker compose` project includes them. Operationally, teams may:

- Run a **narrow Redis + ingestor (+ strategy/executor profiles)** loop for research, or
- Keep OLAP components **stopped** when not needed,

without contradicting the whitepaper’s “pragmatic single-node / optional OLAP” intent. **Do not document ClickHouse or Archiver as unconditionally always-on** for all environments.

---

## Service inventory (by operational class)

| Class | Services | Notes |
| --- | --- | --- |
| **Active / core data plane (default project)** | `redis`, `postgres`, `ingestor` | Redis + ingestor are the minimal streaming spine; Postgres for app OLTP. |
| **OLAP / archival (default project, optional operationally)** | `clickhouse`, `archiver` | Archiver **depends_on** ClickHouse; pausing CH implies archiver is not useful. **Not** “always-on” in documentation claims. |
| **Strategy profile** `strategy` | `strategy`, `pool-discovery`, `swap-poller`, `shadow-closer`, `wallet-scorer` | Strategy and satellite workers; several **depend_on** ClickHouse — if CH is down, these profiles may fail health/depends — treat CH as part of this profile’s **data** dependency, still not a universal 24/7 mandate. |
| **Executor profile** `executor` | `executor` | Consumes `signals`; depends on Redis only in compose. |
| **Dashboard profile** `dashboard` | `dashboard` | Observational; Redis only. |
| **Backfill profile** `backfill` | `backfill`, `backfill-enqueue-gap`, `backfill-discover-gaps`, `backfill-scheduler` | Batch / maintenance; **roadmap / ops** style, not the live tick loop. |
| **Support (default project)** | `autoheal` | Container health sidecar. |

---

## Profile activation (reference only)

- Strategy stack: `docker compose --profile strategy ...` (exact command in `Operations.md`).
- Executor: `--profile executor`.
- Dashboard: `--profile dashboard`.
- Backfill jobs: `--profile backfill`.

This file does not authorize running any profile.

---

## Forbidden

- Do not state that every installation must run ClickHouse + Archiver continuously.
- Do not use this table to claim whitepaper full parity.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
