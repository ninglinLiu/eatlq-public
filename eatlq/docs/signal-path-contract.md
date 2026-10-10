# Signal Path Contract

Generated at: 2026-05-02  
Scope: documentation-only; static contract. No RPC, no runtime, no implementation authorization.  
MM reference: MM-03 (Fusion Layer Signal Path Contract)  
Parent plan: `mvp/audits/whitepaper_v2_missing_modules_plan.md`  
Contracts JSON: `mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P4_SIGNAL_PATH_EXECUTION_BOUNDARY`  
Machine-enforceable test: `tests/test_whitepaper_v2_static_contracts.py::test_signal_path_boundary_is_static`

---

## Purpose

This document is the single human-readable reference for the boundary between the two signal
generation paths in `services/strategy/src/main.py`. It is authoritative for code review.
Any change to signal routing must be reviewed against this document.

---

## Signal Path Table

| Path | Runtime status | Allowed sink | Forbidden sink | Source lines |
|---|---|---|---|---|
| `smart_money` | **executable signal path** | `cfg.signal_stream` (`"signals"`) | `shadow:signals` as primary | `main.py` ~L1165–1175 |
| `pool_score` | **shadow-only** | `SHADOW_LIST_KEY` = `"shadow:signals"` | `cfg.signal_stream` | `main.py` ~L1218–1223 |

---

## Detailed Constraints

### `smart_money` path

- Writes BUY payloads to `cfg.signal_stream` via `consumer.xadd(cfg.signal_stream, ...)`.
- Sets `"signal_source": "smart_money"` in payload.
- Also writes a shadow copy via `_write_shadow_signal(...)` for diagnostic purposes.
- **This path can emit signals that the executor will consume.**
- **not live authorized** — executable path only means payloads may reach the executor stream;
  it does **not** authorize live trading money or promotion.
- **Being executable does NOT mean live is authorized.** Live authorization requires a separate
  positive economic readout that supersedes `FAILED_NO_7D_ALPHA`.

### `pool_score` path

- Is preceded by the inline code comment:
  `pool_score is FROZEN from execution — shadow-only until labels, calibration, and EV threshold are validated.`
  (source: `services/strategy/src/main.py`)
- Writes ONLY to `SHADOW_LIST_KEY` (`"shadow:signals"`) via `_write_shadow_signal(...)`.
- Does **not** call `consumer.xadd(cfg.signal_stream, ...)`.
- The static test `test_signal_path_boundary_is_static` enforces this at the AST level:
  it asserts that no `xadd(cfg.signal_stream, ...)` call appears inside or after the
  `Pool-Level Score Path` block.

---

## Whitepaper Context

The whitepaper describes a "Fusion layer" that emits `final_score` and controls signal
decisions. The current implementation has two separate paths:

1. A heuristic smart-money path that uses wallet membership and pool age checks.
2. A model-scored pool path that computes `confidence` from XGBoost features.

These are **not** fused. The model path is frozen to shadow because the `FAILED_NO_7D_ALPHA`
economic readout has not been superseded.

**Do not claim:** "Model fusion is driving executable production signals."  
**Allowed claim:** "Smart-money heuristic path emits executable signals; model pool-score path
is shadow-only."

---

## Forbidden Actions

- Do not promote `pool_score` to write to `cfg.signal_stream`.
- Do not describe the pool-score model as generating live execution signals.
- Do not change the signal path routing without updating this document and the contracts JSON.
- Do not use signal path changes as evidence that Phase 6 can continue.

---

## Change Authorization

To change the `pool_score` path from shadow to executable, ALL of the following must be true:

1. `mvp/audits/failed_no_7d_alpha_guard.md` guard is superseded by a new scoped economic
   readout (see `docs/phase6-resolution-criteria.md`).
2. ML Monitoring Gate (`mvp/audits/whitepaper_v2_minimal_patch_contracts.json::P5`) is
   populated with all `decision_flags=false`, then a subsequent readout passes the gate.
3. ML Training Gate (`docs/ml-training-gate.md`) preconditions are all satisfied.
4. This document and `P4_SIGNAL_PATH_EXECUTION_BOUNDARY` are explicitly updated to reflect
   the new authorization status.
5. `test_signal_path_boundary_is_static` is updated to reflect the new contract.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
