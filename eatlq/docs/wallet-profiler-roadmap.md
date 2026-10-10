# Wallet Profiler Roadmap (placeholder)

Generated at: 2026-05-02  
Scope: **documentation-only**, **roadmap-only**. No PostgreSQL migrations, no new collectors, no strategy changes.

---

## Scope rule

This file is **roadmap-only**—it defines narrative boundaries for a future **`wallet_profiles` / wallet profiler** capability. It **does not** implement profiling, ingestion, or database schema changes.

---

## Current runtime substitute (explicitly incomplete)

Redis sets **`wallet:smart_money`** and **`wallet:sniper`** are **not equivalent** to a full **`wallet_profiles`** table plus whitepaper-grade profiling.

Those sets only support narrow strategy heuristics. They **must not** be described as parity with a four-archetype wallet profiler backed by Postgres.

---

## Future archetypes (target narrative, no implementation promise)

Future documentation may standardize personas under headings such as **Smart Money**, **Sniper**, **Diamond Hand**, and **Paper Hand** once a Postgres-backed contract exists (`mvp/audits` will require a migration path before parity claims).

---

## Authorization

- **`no runtime collection authorization`** derives from backlog + **`FAILED_NO_7D_ALPHA`**: while **`mvp/audits/failed_no_7d_alpha_guard.md`** is active, spinning up pipelines that widen wallet intelligence for production or promotion is **`blocked by FAILED_NO_7D_ALPHA for runtime use`** regardless of wording in backlog tables.
- **`IMPLEMENTATION_ALLOWED=false`** mirrors `mvp/audits/whitepaper_v2_missing_modules_plan_summary.json`.

See also: **`mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P2_WALLET_PROFILES_DECISION`** (still **roadmap-only** tier).

---

## Forbidden

- Do not create wallet profiler binaries, dashboards, cron jobs, or migrations from this roadmap alone.
- Do not cite this document as proving whitepaper profiler parity.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
