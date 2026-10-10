# base_large_buy_absorption_v2

Preregistered primary: **5s absorption / 0s latency**. Diagnostic replay only.

## Run

```bash
cd /root/base_large_buy_absorption_v2
python3 -m src.coverage_diag          # Phase 0 only
python3 -m src.run_all_v2             # full Phases 0–5
```

## Key outputs
- `out/diag/DATA_COVERAGE_DIAGNOSTIC.md`
- `out/pnl_decomp.json` / `PNL_DECOMP.md`
- `out/sensitivity.csv` (do not use to pick params)
- `out/ranking.json`
- `out/LARGE_BUY_ABSORPTION_v2_REVIEW.md`
