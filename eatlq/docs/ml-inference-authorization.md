# ML Inference Authorization (placeholder tiers)

Generated at: 2026-05-02  
Scope: **documentation-only** static contract scaffold. Defines authorization **tiers**, not approvals. No inference promotion is granted here.

---

## Current implementation alignment

- **`pool_score`** model scoring remains **shadow-only** (writes to shadow streams only)—see **`docs/signal-path-contract.md`** and **`tests/test_whitepaper_v2_static_contracts.py`** (AST invariant).
- **`smart_money`** is the heuristic path that may emit payloads on the executable **`signals`** stream—but **smart_money executable path is not live authorization**; **`EXECUTOR_DRY_RUN`**, **`LIVE_ALLOWED`**, and economic gates still dictate money risk.

---

## Inference tiers (planning scaffold)

| Tier | Meaning | Prerequisites (all must hold) |
| --- | --- | --- |
| **shadow-only** | Default today for **`pool_score`**, diagnostic writes only | Maintain current AST + checklist reviews |
| **paper-authorized** | Hypothetical paper/dry envelopes | Super **`FAILED_NO_7D_ALPHA`** verdict per **`docs/phase6-resolution-criteria.md`**, fulfill **`docs/ml-training-gate.md`**, populate honest **ML monitoring gate** payloads (`mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P5`) with **`decision_flags` default false**, plus session-specific human booleans—not automatic |
| **live-authorized** | Capital-at-risk inference coupling | **`paper-authorized`** lineage **plus** new economic artifact showing sustained positive expectancy after execution costs |

**Paper/live tiers require superseding `FAILED_NO_7D_ALPHA` and ML monitoring gate honesty** together; documents alone cannot flip tiers.

---

## Model metrics caveat

**Model metrics cannot authorize promotion alone**—`promoted`, `accuracy_gate_passed`, **`uplift_gate_passed`**, or similar JSON fields never override **`mvp/audits/failed_no_7d_alpha_guard.md`** or this tier table.

---

## Forbidden

- Do not elevate **`pool_score`** to **`signals`** from this Markdown.
- Do not cite metrics JSON as unlocking live trading.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
