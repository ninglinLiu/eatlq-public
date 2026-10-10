# Base Greenfield V4 Orthogonal Hypothesis Round — Reproduce

Parent: V3 `LOTTERY_DEPENDENT_REJECT`  
Phase: `V4_ORTHOGONAL_HYPOTHESIS_ROUND`  
`deploy_allowed=False`  
Expand-search-after-failure: **forbidden**  
One-shot orthogonal round only.

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v4.run_orthogonal 2>&1 | tee out_v4/RUN.log
```

## Re-seal

- method: salted_signal_hash
- salt: `V4_RESEAL_20260720_ORTHOGONAL`
- V3 INTERNAL_HOLDOUT/UNTOUCHED ids burned out of V4 confirmation

## Frozen configs

- `configs/v4_orthogonal_freeze.json`
- `configs/v4_hypothesis_registry.json`
