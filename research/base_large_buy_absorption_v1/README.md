# LARGE_BUY_ABSORPTION_v1

Isolated Base Uniswap V3 **order-flow** candidate (relative large buy + 5m absorption).
Not smart money. Legacy alias: `WHALE_EVENT` (v0).

## Run diagnostic replay

```bash
cd /root/base_large_buy_absorption_v1
python3 -m src.run_all_v1
```

## Outputs

- `out/LARGE_BUY_ABSORPTION_v1_REPLAY.json`
- `out/LARGE_BUY_ABSORPTION_v1_REVIEW.md`
- `out/FREEZE_READY.json` (if gate passes)
- `out/PROSPECTIVE_T0.json` (if gate passes)
- Shadow recorder: `python3 -m src.shadow_recorder` (observer-only)

## Governance

- Historical run = `DIAGNOSTIC_REPLAY` (no OOS inheritance from v0)
- Live trading forbidden until prospective shadow clears
- Do not retune thresholds after seeing replay results
