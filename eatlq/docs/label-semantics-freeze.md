# Label Semantics Freeze Contract

Generated at: 2026-05-02  
Scope: documentation-only; static contract. No labeling, no RPC, no implementation authorization.  
MM reference: MM-10 (ML Labeling Semantics Freeze Contract)  
Parent plan: `mvp/audits/whitepaper_v2_missing_modules_plan.md`  
Active guard: `mvp/audits/failed_no_7d_alpha_guard.md` — labeling is blocked while
`FAILED_NO_7D_ALPHA` is active.

---

## Purpose

This document pins the label semantics for the EATLQ dashcam labeling pipeline. No label run
may proceed without first verifying that these semantics are unchanged. Any modification to
these semantics must update this document and receive explicit human authorization.

---

## Controlling Artifact

`FAILED_NO_7D_ALPHA` verdict in `mvp/runs/run-004-v2-7d-alpha-readout.md` is active.  
**No label run is authorized while this guard is active.**  
See `docs/phase6-resolution-criteria.md` for the conditions that must be met before the guard
can be superseded.

---

## Primary Labeler

`scripts/label_dashcam_executable.py` — the author's Phase 2 mainline labeler.

This script supersedes `scripts/label_dashcam.py` for production use. The `label_dashcam_fast.py`
variant must not be used for training without explicit authorization and a documented equivalence
check.

---

## Frozen Label Parameters

These parameters are frozen. Any change requires this document to be updated and the change
to be explicitly authorized.

| Parameter | Frozen value | Source |
|---|---|---|
| Exit horizon | 1800 seconds (30 minutes) | `LABEL_EXIT_HORIZON_SEC=1800` in `label_dashcam_executable.py` |
| Dedup bucket | 300 seconds (5 minutes) | `LABEL_BUCKET_SEC=300` |
| Label column name | `label` (binary 0/1) | Output schema in `label_dashcam_executable.py` |
| Output schema | Compatible with `retrain_model.py` | Documented in script docstring |
| Minimum sample age | `exit_horizon_sec + 120` seconds | Samples too young for exit horizon are dropped |
| Notional size | 0.0005 ETH (`LABEL_NOTIONAL_WEI`) | Quote simulation reference size |
| Chain ID | 8453 (Base) | `CHAIN_ID=8453` |
| Pool type whitelist (default) | `v2,v3` | `LABEL_POOL_TYPES` default in `label_dashcam_executable.py` |
| Max sample age (default) | `0` = no max-age filter when unset | `LABEL_MAX_AGE_DAYS`; Phase 6 orchestration typically sets `30` to drop very old samples where pools are likely dead or quotes unreliable |

---

## Pool type scope (Base — frozen defaults)

**Stable contract phrase — LABEL_POOL_TYPES default: `v2,v3` (Base Uniswap V2–style and Uniswap V3–style pools in `discovered_pools` metadata).**

Within this freeze, only types resolvable by the executable labeler’s `label_v2` / `label_v3` paths (and the script’s default whitelist) are in scope for *authoritative* executable labeling. **Aerodrome (`aero_cl` / `aero_vamm`) and Uniswap V4–class pools are not part of the current freeze**; using them in production labeling requires a **new** freeze document and explicit authorization (do not treat out-of-scope types as silently `label=0`).

---

## Label Definition

A label of `1` means: the pool's simulated exit quote at `signal_timestamp + 1800s` returns
a positive net return after deducting `ROUND_TRIP_COST = 0.006` (0.6% round-trip cost
covering swap fees and estimated slippage).

A label of `0` means: a row was **written** to the labeled CSV and the net return after costs was not positive (including failed exit economics encoded as a written row with `label` 0 where applicable).

**Quote failures and skipped rows:** when the labeler cannot obtain a valid quote (`label < 0` / quote-failure reasons in worker stats), **that sample is not written to the labeled dataset output.** Those rows are **skipped (omitted)**, not coerced to `label=0`. **Stable contract phrase — quote_failed rows are omitted from the labeled CSV output (not written as label=0).**

Samples too young for the exit horizon are dropped earlier in the pipeline (not written).

---

## Timestamp → block grid (labeler-internal)

**Stable contract phrase — ts_to_block grid construction is labeler-internal frozen semantics.**

The executable labeler maps signal timestamp and exit timestamp to chain blocks via a batched `eth_getBlockByNumber` interpolation grid and `ts_to_block`. That construction is part of the frozen labeling semantics; changes require updating this document and explicit re-labeling authorization.

---

## LABEL_MAX_AGE_DAYS (default and rationale)

**Stable contract phrase — LABEL_MAX_AGE_DAYS default 0 means no max-age drop when unset; orchestrated runs may set 30d to drop stale samples.**

- **Default `0`:** Standalone runs impose no upper age bound beyond the “young for horizon” filter.
- **Typical orchestration `30`:** Drops very old dashcam rows so pools that are likely dead or structurally changed do not dominate failed-quote noise. See `scripts/phase6_weekly_cycle.sh` / `Operations.md` for the weekly pipeline default.

---

## RPC Call Pattern — Frozen

**Stable contract phrase:** `batch rpc` for per-row quote estimation is forbidden (use concurrent single-call JSON-RPC per worker instead).

**Batch RPC for quote calls is FORBIDDEN.**

The historical RPC batch failure (documented in
`mvp/runs/run-004-v2-live-equivalent-7d-pilot.md`) showed that using JSON-RPC batch for
quote calls produced large-scale label errors. The fix was to switch to concurrent
single-call RPC via `ThreadPoolExecutor`.

Allowed RPC patterns:
- `ThreadPoolExecutor` with `LABEL_WORKERS` (default 20) workers, each making single
  `eth_call` quote requests.
- Batched `eth_getBlockByNumber` requests for block-timestamp interpolation table construction
  (one-time setup scan, not per-row quote).

Forbidden RPC patterns:
- JSON-RPC batch calls for per-row quote estimation.
- Batch calls that group multiple `eth_call` invocations for quotes into a single HTTP request.

---

## Label Drift — Forbidden

The following changes constitute "label semantics drift" and are forbidden without
full re-labeling authorization:

1. Changing `LABEL_EXIT_HORIZON_SEC` to any value other than 1800.
2. Changing the net-return formula (e.g. altering `ROUND_TRIP_COST`).
3. Changing the dedup logic (bucket size, uniqueness key).
4. Changing the label column name or output CSV schema.
5. Using a different quote method (e.g. switching from QuoterV2 to slot0 price for labels).
6. Filtering samples by criteria not present in the frozen script.
7. Writing quote-failed / `label < 0` samples into the labeled CSV as `label=0` (they must remain **omitted** per Sprint028A semantics).
8. Extending authoritative executable labeling to Aerodrome / Uniswap V4 pool types **without** a new freeze document and authorization.

---

## Pool Type Whitelist

The **`LABEL_POOL_TYPES` environment variable defaults to `v2,v3`** in `label_dashcam_executable.py`. Narrower whitelists (e.g. `"v2"` only) change the sample distribution; any non-default value must be **explicitly documented** in the labeling run artifact.

---

## Future Authorization Requirements

Before any label run can be authorized, ALL of the following must hold:

1. `FAILED_NO_7D_ALPHA` guard is superseded (see `docs/phase6-resolution-criteria.md`).
2. **Current `LABELING_ALLOWED=false`** in all audited JSON summaries today. Only after supersession may a separate human/session artifact record **`future_authorization_may_set_LABELING_ALLOWED_true_only_after_supersession`** (explicit booleans in that artifact—not this Markdown, not metrics alone).
3. `LABEL_RPC_URL` points to an archive node that has been verified to respond to
   `eth_call` (QuoterV2) for the required historical block range without 429 errors.
4. All frozen parameters above are verified unchanged (or the change is documented and
   a full re-labeling is planned).
5. A preflight sample smoke test (small `LABEL_LIMIT=N`) is run and documented before
   a full labeling run.

---

## What This Document Does Not Authorize

- This document does not authorize running `label_dashcam_executable.py`.
- This document does not authorize training.
- This document does not authorize any live or shadow execution.
- This document does not supersede `mvp/audits/failed_no_7d_alpha_guard.md`.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
