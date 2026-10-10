# Corrected Outcome Spec (V1.1)

## Principle

Illegal / non-canonical / preflight-rejected opportunities are **not** position losses.

| State | pnl_weth |
|---|---|
| ENTRY_PREFLIGHT_REJECT | 0 |
| ENTRY_TX_REVERT | -buy_gas_weth |
| BUY_TX_SUCCESS + EXIT_SUCCESS / TEMP_RECOVERED | weth_out - weth_in - buy_gas - sell_gas |
| BUY_TX_SUCCESS + EXIT_PERMANENT_UNSELLABLE | recoverable_weth - weth_in - gas (≈ −amt_in) |
| INFRASTRUCTURE_UNKNOWN | NULL (excluded from filled EV; reported separately) |

`total_pnl_weth` = wallet WETH delta under capital constraints.  
`sum_return_for_reference` = legacy sum(return) for comparison only.

Label: **HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC** (not a new sealed confirmation).
