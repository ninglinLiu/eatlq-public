# Base Greenfield V3 Narrow-Market Rescue — Reproduce

Role: **Base Uniswap New-Pool Narrow-Market Rescue Research Agent**  
Phase: `V3_NARROW_MARKET_RESCUE`  
`deploy_allowed=False`  
Venue rejection: **forbidden**

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v3.run_rescue 2>&1 | tee out_v3/RUN.log
```

## Frozen inputs

- `configs/v3_rescue_freeze.json`
- `configs/v3_hypothesis_registry.json`
- `configs/hard_bans.json`
- Source panel: `out_v2/PHASE0_CENSUS_OPPORTUNITIES.parquet`
- Source swaps: `out_v2/cache/label_design_swaps_126h.parquet`

## Capital

- initial_usd=1000.0 @ eth_usd_freeze=3000.0
- initial_weth=0.3333333333333333
- monthly withdraw target USD=1000.0
- compound_profits=False

## Outputs

- `out_v3/RESCUE_FINAL.json`
- `out_v3/HYPOTHESIS_LEDGER.csv`
- `out_v3/OOF_SCORECARD.csv`
- `out_v3/MONTHLY_WITHDRAW.csv`
- `out_v3/WALLET_EQUITY_CURVE.parquet`
- `out_v3/RIGHT_TAIL_AUDIT.csv`
- `out_v3/DIAGNOSTIC_PHASE0_CROSSWALK.csv`

LatchPi features: disabled (`DATA_LINEAGE_INSUFFICIENT` for narrative hypotheses).
