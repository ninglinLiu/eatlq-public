# Gate: SAFE_TO_BEGIN_OFFLINE_RESEARCH

Updated: 2026-07-18T13:59Z UTC  
Source: `checkpoints/health_baseline_summary.json`  
Report: `reports/BASE_ALPHA_ATLAS_PHASE_MINUS_1_0_LIVE_PROTECTION_AND_BASELINE.md`

```text
SAFE_TO_BEGIN_OFFLINE_RESEARCH = false
plan_status = PAUSED_LIVE_PRIORITY
blocking = BLOCKED_RESOURCE_RISK_CPU_IDLE
```

## Evidence (10-minute baseline)

- idle mean = 44.97% (passes >30% mean rule alone)
- idle min = 0.00%; samples with idle <10% = 7/21
- max consecutive idle <10% = 3 samples ≈ 90s → **auto-pause condition hit**
- swap delta = −786432 bytes (not growing)
- executor / signal-bridge / ingestor RestartCount unchanged = 0
- SYSTEM_PANIC absent
- ClickHouse CPU mean ≈ 380% (live bridge load; do not add research scan)

## Forbidden while false

- shared panel construction
- heavy backtests
- production DB exports
- production RPC reuse
- Stage 1+ runners
