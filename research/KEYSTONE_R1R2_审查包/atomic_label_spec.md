# Atomic Label Spec (R2 frozen)

## Status taxonomy

| status | meaning |
|---|---|
| SELL_SUCCESS | `exactInputSingle` eth_call at gas_cap returns WETH out > 0 |
| DETERMINISTIC_REVERT | eth_call returns RPC error / revert at gas_cap |
| ECONOMICALLY_UNSELLABLE | call succeeds but tax >= 90% (WETH out / fair < 0.10) |
| INFRASTRUCTURE_UNKNOWN | balance/allowance slot undetectable, archive missing, or transport failure — **not** token fatal |

## Primary endpoint

Under frozen KEYSTONE exit (hard -50%, hold 14400s, trailing OFF):

1. Reconstruct token_in via production-equivalent buy path at entry_block (QuoterV2 buy used only to size inventory; sell label is atomic).
2. At exit_block (causal trail/hold from priced path), run atomic sell sim.
3. If SELL_SUCCESS: `atomic_net = weth_out / amt_in - 1`.
4. If DETERMINISTIC_REVERT or ECONOMICALLY_UNSELLABLE after retry schedule: `-100%`.
5. If INFRASTRUCTURE_UNKNOWN after retries: `UNKNOWN` (excluded from EV denominators that require labels; reported separately).

QuoterV2 markouts are comparison columns only.

## Secondary barriers (atomic net path only)

- TB_A: +50% / -35%
- TB_B: +100% / -50%
- FATAL_FIRST: atomic unsellable / -100% before TP

## Point-in-time

All features and residualizers use information available at decision time t0+600s.
Residualizers fit on chronologically prior train fold only.
