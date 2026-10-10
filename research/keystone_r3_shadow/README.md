# KEYSTONE R3 Shadow

## Current version

**`r3.0.1-shadow`** — see [`r3.0.1/`](r3.0.1/)

Status: `CLOCK_STARTED` (2026-08-21)

Clock is armed. Confirmatory writes go to `ledger/r3_preoutcome_events.jsonl` via `r3.0.1/shadow_observer.py`.
Shadow only: capital=0, no Redis BUY, no executor. Production bridge/executor are a separate process and were not changed.

## Superseded

| Artifact | Status |
|----------|--------|
| `R3_PREREGISTRATION.json` (root, r3.0.0) | `SUPERSEDED` — hash ambiguity / incomplete freeze |
| `shadow_recorder.py` (root) | `SUPERSEDED` — use `r3.0.1/shadow_recorder.py` |
| Any pre-r3.0.1 ledger rows | `R3_PILOT_PRE_FREEZE` — excluded from confirmatory sample |

## Arm confirmatory clock (human only)

```bash
cp HUMAN_ARM.template.json HUMAN_ARM.json
# edit acknowledgements, set clock_started=true
```

`HUMAN_ARM.json` is present (`clock_started=true`). Observer: `python3 r3.0.1/shadow_observer.py`.
