# LASZLO Iteration Loop (Phase 6 RL-style)

Source: `v2/rl-iteration-loop.svg` · kind: `flow`

**PHASE 6 — RL-STYLE ITERATION** (center gate: EDGE / 72% P)

```text
Ingest (soak / dashcam)
  → Label (tri / QC)
    → Train (holdout gate)
      → Paper (no BUY if below thr)
        → Ledger (replay / audit)
          → Diagnose (edge / worst)
            → (loop back)
```

