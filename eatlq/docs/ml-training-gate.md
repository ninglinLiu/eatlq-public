# ML Training Gate Contract

Generated at: 2026-05-02  
Scope: documentation-only; static contract. No training, no implementation authorization.  
MM reference: MM-11 (ML Training Gate Contract)  
Parent plan: `mvp/audits/whitepaper_v2_missing_modules_plan.md`  
Active guard: `mvp/audits/failed_no_7d_alpha_guard.md`

---

## Current Status

**RETRAIN_ALLOWED = false**

This flag must remain false until every precondition in this document is met. The `FAILED_NO_7D_ALPHA`
guard (`mvp/audits/failed_no_7d_alpha_guard.md`) is active. No model metric — including
`promoted=true`, `accuracy_gate_passed=true`, or `uplift_gate_passed=true` — can override this gate.

---

## Purpose

This document enumerates the ordered preconditions that must ALL be satisfied before any
training run via `scripts/retrain_model.py` (or equivalent) is authorized. It does not
authorize training. It is a checklist for a future authorized session.

---

## Precondition Checklist

Training is not authorized unless ALL of the following are true in a single authorized session:

### Gate 0 — FAILED_NO_7D_ALPHA superseded

`mvp/audits/failed_no_7d_alpha_guard.md` must be superseded by a new scoped economic readout
that explicitly states it supersedes the current FAILED_NO_7D_ALPHA verdict.

See `docs/phase6-resolution-criteria.md` for the supersession rule.

- Required artifact: a new `mvp/runs/run-XXX-*-readout.md` that explicitly contains
  `supersedes: FAILED_NO_7D_ALPHA` and shows positive holdout economics.
- This artifact must be approved before any other gate is evaluated.
- Model metrics alone (`promoted=true`, etc.) do not satisfy this gate.

### Gate 1 — Label Semantics Verified

`docs/label-semantics-freeze.md` semantics must be verified unchanged:

- Exit horizon is still 1800 seconds.
- No batch RPC regression for quote calls.
- Output schema is compatible with `retrain_model.py`.
- The label run's preflight smoke test is documented.

### Gate 2 — Clean Label Dataset Available

A labeled dataset must exist with the following minimum properties:

- Produced by `scripts/label_dashcam_executable.py` with frozen semantics.
- `label_success_rate` ≥ the threshold defined in the ML Monitoring Gate schema
  (`mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P5`).
- `clean_rows` ≥ a documented minimum (to be set in the monitoring gate artifact, not here).
- The dataset's `LABEL_EXIT_HORIZON_SEC` and dedup parameters are recorded in the run artifact.

### Gate 3 — ML Monitoring Gate Populated

A monitoring gate artifact must exist that:

- Conforms to the schema in `mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P5`.
- Has `schema_version: "v1"`.
- Has ALL `decision_flags.*=false` (the monitoring gate confirms the state of the data;
  it does not itself authorize training — that is this document's role).
- Documents holdout economics: `holdout_mean_net_return`, `holdout_positive_rate`,
  `profit_factor`.
- Documents concentration: `pool_concentration_top1`, `pool_concentration_top5`,
  `token_concentration_top1`, `token_concentration_top5`.
- Documents data quality: `dead_letter_count`, `rpc_failure_rate`.

### Gate 4 — Explicit Per-Session Authorization

A training run requires an explicit `RETRAIN_ALLOWED=true` flag set in a per-session
authorization artifact. This artifact must:

- Reference the superseding economic readout (Gate 0).
- Reference the monitoring gate artifact (Gate 3).
- Be created in the same session as the training run.
- Not be carried forward from a prior session.

---

## Training Script Reference

Script: `scripts/retrain_model.py`  
Output location: `models/` directory  
Version naming convention: follow existing `signal_model_realtime_v*.json` pattern  
Metrics output: must include all fields from `models/signal_model_realtime_v2_metrics.json`
as a minimum; add `training_gate_artifact` field citing the Gate 3 monitoring gate.

---

## What Training Produces vs What It Authorizes

| Produced by training | Does it authorize live? |
|---|---|
| `models/signal_model_realtime_v*.json` | No |
| `models/*_metrics.json` with `promoted=true` | No |
| A holdout accuracy above threshold | No |

Live authorization is a separate gate that requires a new positive economic readout, not
just a model with good holdout accuracy.

---

## Relationship to Signal Path Contract

Even after training is authorized:

- `pool_score` path remains shadow-only until `docs/signal-path-contract.md` is explicitly
  updated.
- `smart_money` path remains executable-but-not-live until a separate live gate passes.

---

## What This Document Does Not Authorize

- This document does not authorize running `scripts/retrain_model.py`.
- This document does not authorize labeling.
- This document does not authorize live or shadow promotion.
- This document does not supersede `mvp/audits/failed_no_7d_alpha_guard.md`.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
