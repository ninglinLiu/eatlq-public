---
sop_applied: true
alpha_id: AR-B4-QV2-ATOMIC-SELLSIM
family: F4/F7
next_gate: HUMAN_RESULT_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
---

# B4 QuoterV2 vs atomic sell-sim preregistration (frozen before outcome calls)

This document operationalizes `/root/quant_research_os/reports/B4_task_spec_quoterv2_vs_sellsim_20260714.md` without changing its hypothesis or thresholds. The run is research-only. It cannot authorize GO, training, shadow/live routing, signing, sending, broadcasting, deployment, or a service/configuration change.

## Frozen sample construction

- Fixed input is exactly `1_000_000_000_000_000` wei WETH (0.001 WETH).
- Cached direct-honeypot cases are every `_external_risk_current.json` record whose cached `raw.honeypot.honeypotResult.isHoneypot` is true and whose selected V3 pair has a WETH leg. The pin block is `floor((checked_at - 1686789347) / 2)`, the already-validated Base 2-second block-time affine map. Cached sell tax at least 90% maps to `sell_tax`; other direct cases map conservatively to `transfer_hook_or_unknown`.
- Confirmed-rug cases come from `_labeled_universe.json`. The first 30 `lp_pull` rows ordered by `(flip_block, pool)` are evaluated at `flip_block - 1`; the two available `transfer_toggle` rows are evaluated at `flip_block`. No result-dependent replacement is allowed.
- Good controls come only from shard rows labeled `clean` (forward scan did not find a rug). ClickHouse must additionally show at least 3 target-token-in V3 swaps from at least 2 distinct `tx_from` addresses during `(entry_block, entry_block + 7200]`. The first 30 ordered by `(entry_block, pool)` are used; no outcome-dependent replacement is allowed.
- Time sensitivity uses the first 3 selected LP-pull cases ordered by `(flip_offset, flip_block, pool)`, once at `flip_block - 1` and once at `flip_block`. This pair is descriptive and is not duplicated into the primary divergence denominator.
- Missing archive state, missing fee/WETH leg, a failed atomic buy (so no actually received target amount exists), or an RPC/ABI failure is reported as non-evaluable. It is never coerced to agreement or divergence.

## Frozen execution and outcome definitions

- The atomic leg is one historical `eth_call` with ephemeral code state override. The helper owns 0.001 native ETH only inside that call, wraps it, uses SwapRouter02 to buy target, measures actual target balance, approves and sells that full measured balance, then measures WETH. All token transfers and hooks execute in the EVM. No state persists.
- QuoterV2 is called only after the atomic leg supplies the actual received target integer.
- `q_tax = 1 - q_weth_out / input_weth`; QuoterV2 is healthy iff it does not revert and `q_tax < 0.10`.
- `atomic_tax = 1 - atomic_weth_back / input_weth`; the atomic sell is bad iff it reverts/does not complete or `atomic_tax >= 0.90`.
- A divergence is exactly `quoter_healthy == true AND atomic_bad == true`. Boundary values 10% and 90% are not rounded before classification.
- Wilson two-sided 95% intervals use `z = 1.959963984540054`. H0 is rejected only if the known-honeypot evaluable divergence interval has lower bound strictly greater than 5%. Otherwise the output says not rejected/inconclusive; it never says GO.
- Rates are reported by group and mechanism with `n_selected`, `n_evaluable`, and failure counts. Small or zero evaluable denominators remain visible.

## Golden and reproducibility freeze

- Golden cases are one direct cached sell-tax case when available, one deterministic good control, and one deterministic LP-pull pre-pull case.
- Each golden atomic call and Quoter call is repeated. Raw return bytes must match exactly (`==`). Successful sell integers must satisfy `routerSellOut == wethAfterSell - wethAfterBuy`; no tolerance is allowed. For an untaxed successful buy, `routerBuyOut == targetAfterBuy - targetBefore`; tax cases retain both integers without forcing equality.
- Each atomic return records the pin block and `BLOCKHASH(pin_block - 1)` from inside the same `eth_call`. Outputs also record chain id, RPC endpoint SHA-256 fingerprint (never credentials), helper runtime SHA-256, source hashes, input-manifest hash, and final artifact hashes.

Frozen on 2026-07-14 UTC before outcome calls.
