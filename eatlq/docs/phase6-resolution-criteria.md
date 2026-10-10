# Phase 6 Alpha Hunt Resolution Criteria

Generated at: 2026-05-02  
Scope: documentation-only; planning only. No Phase 6 authorization, no implementation.  
MM reference: MM-13 (Phase 6 Alpha Hunt Resolution Plan)  
Parent plan: `mvp/audits/whitepaper_v2_missing_modules_plan.md`  
Controlling verdict: `mvp/runs/run-004-v2-7d-alpha-readout.md` — FAILED_NO_7D_ALPHA  
Active guard: `mvp/audits/failed_no_7d_alpha_guard.md`

---

**THIS DOCUMENT DOES NOT AUTHORIZE PHASE 6.**

It defines the supersession rule: what new evidence must exist, and what authorization steps
must occur, before the `FAILED_NO_7D_ALPHA` guard can hypothetically be lifted. The guard
remains active until every criterion here is met.

---

## Current Guard State

| Guard | Value |
|---|---:|
| `PHASE6_ALLOWED` | false |
| `LIVE_ALLOWED` | false |
| `RETRAIN_ALLOWED` | false |
| `LABELING_ALLOWED` | false |
| `TRAINING_ALLOWED` | false |
| `WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED` | false |

These flags will not change by virtue of this document's existence.

---

## Supersession Rule

Controlling artifacts must declare the supersession line verbatim when intended to supersede:

supersedes: FAILED_NO_7D_ALPHA

The `FAILED_NO_7D_ALPHA` guard can only be superseded by a **new scoped economic readout
artifact** that satisfies ALL of the following criteria simultaneously:

### Criterion 1 — New artifact, not a reinterpretation

The superseding artifact must be a **new** run artifact at a path of the form
`mvp/runs/run-XXX-*-readout.md` (where XXX is a run number later than 004).

Reinterpreting `mvp/runs/run-004-v2-7d-alpha-readout.md` does not constitute supersession.
Updating model metrics does not constitute supersession.

### Criterion 2 — Explicit supersession declaration

The new artifact must contain the explicit phrase:

> `supersedes: FAILED_NO_7D_ALPHA`

(or a clearly equivalent machine-readable field in its companion summary JSON, e.g.
`"supersedes_verdict": "FAILED_NO_7D_ALPHA"`).

A readout that does not explicitly declare supersession does not lift the guard, regardless
of its economic results.

### Criterion 3 — Positive holdout economics under execution-cost assumptions

The new readout must demonstrate **positive** holdout economics:

- `holdout_mean_net_return > 0` across the full holdout set (not only a favourable subset).
- `profit_factor > 1.0` (gross profit / gross loss > 1).
- These numbers must be calculated **after** deducting execution costs (swap fees, gas, slippage
  assumptions) comparable to or stricter than those used in Run-004.
- Cherry-picked sub-buckets do not satisfy this criterion.

### Criterion 4 — Minimum scoped duration

The new readout must cover a period of at least 7 days of live-equivalent or live data.
Synthetic bootstrap results do not satisfy this criterion.

### Criterion 5 — Label and monitoring gate lineage

The new readout must be traceable to:

- A label run that satisfies `docs/label-semantics-freeze.md`.
- A monitoring gate artifact that satisfies
  `mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P5`.

### Criterion 6 — No model-metrics-only claim

Model metrics (`promoted=true`, `accuracy_gate_passed=true`, `uplift_gate_passed=true`, or
any similar field) in `models/*_metrics.json` do NOT constitute supersession, regardless of
their values. The guard can only be lifted by an economic readout, not by a model score.

---

## What Happens After Supersession

If a new artifact satisfies all six criteria above, the following sequence is required:

1. Update `mvp/audits/failed_no_7d_alpha_guard.md` to note the supersession, citing the new
   readout path.
2. Create a new guard document for the current phase (e.g. `mvp/audits/current_phase_guard.md`)
   that resets all blocked flags to their new authorized values.
3. Explicitly re-evaluate each of the following documents before any runtime action:
   - `docs/signal-path-contract.md` (update `pool_score` status if newly authorized)
   - `docs/ml-training-gate.md` (re-evaluate whether training is now authorized)
   - `docs/label-semantics-freeze.md` (re-evaluate whether labeling is now authorized)
4. Receive explicit per-session human authorization for each specific action (label, retrain,
   shadow, paper, live) separately.

No single superseding artifact automatically unlocks all actions. Each action requires its
own authorization.

---

## What Does Not Lift the Guard

The following actions or artifacts do NOT lift the `FAILED_NO_7D_ALPHA` guard:

- Updating `models/*_metrics.json`.
- Adding or changing strategy heuristics.
- Running more dashcam data collection.
- Creating documentation (including this document).
- Reaching a certain volume of rows in `logs/dashcam.csv`.
- Any Cursor agent action unless it produces the required economic readout artifact.
- Any claim that "the system is now ready" without the artifact evidence above.

---

## Rationale

The `FAILED_NO_7D_ALPHA` verdict means the 7D economic test found no execution-cost-adjusted
positive expected value across any tested policy. This is not a "weak edge needs 30D
confirmation" result — it is a "no edge found" result. Starting Phase 6 (data → label →
retrain → shadow → live) before finding a positive edge would repeat the same loop without
new evidence.

The supersession criteria above are designed to require new evidence from a new run, not a
reinterpretation of existing evidence.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
