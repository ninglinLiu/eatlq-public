# Atomic Label Spec (V1 Profit Validation)

## Production execution assumptions (frozen)

| Item | Value |
|---|---|
| Router | SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481` |
| Quoter | QuoterV2 `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` |
| amt_in | `16666666666666666` wei (= $50 @ 3000; within 0.02 ETH executor cap) |
| entry_delay | +1 block after decision_block |
| gas_debit | 0.0002 WETH round-trip (debit on success path) |
| slippage | amountOutMinimum = 0 in sim (atomic eth_call) |
| allowance | stateDiff grant to router for probe |
| recipient | probe `0x…dEaD` |
| retry | INFRASTRUCTURE_UNKNOWN only: +0,+5,+30,+150 blocks |
| max hold | H=3600s with TB_A barriers for exit timing |

## Status taxonomy

- SELL_SUCCESS
- DETERMINISTIC_REVERT
- ECONOMICALLY_UNSELLABLE (tax ≥ 90% vs amt_in)
- INFRASTRUCTURE_UNKNOWN

## Primary outcome

`realized_net_weth_return = weth_out / amt_in - 1 - gas_debit` on SELL_SUCCESS;
`-1.0` on deterministic/economic failure; NULL on INFRASTRUCTURE_UNKNOWN (excluded from EV denominators requiring labels).

PIT forward ticks are **comparison only** (exit timing / MFE/MAE), never Primary PnL.
