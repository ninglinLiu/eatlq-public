# ALPHA_HUNT_v2_REVIEW

Generated: `2026-07-21T10:36:41Z`

## Leakage gate: **PASS**

## Live: **NO_GO** | Observer shadow proposed: **False**

## Per-leg verdicts (net-of-cost, incremental over random)

| Leg | Matched ATT | Ranker | Overall | ATT mean (WETH) | day p05 |
| --- | --- | --- | --- | ---: | ---: |
| actor_xpool | REFUTED | REFUTED | **REFUTED** | -0.001508 | -0.010358 |
| clone_family | REFUTED | REFUTED | **REFUTED** | -0.001619 | -0.005736 |
| combined | REFUTED | REFUTED | **REFUTED** | -0.002138 | -0.007363 |

## Success bar

net-of-real-cost incremental return over RANDOM-spray, robust across walk-forward folds, BH-corrected, replicated on sealed holdout

## Stop rule

If Leg A2 and Leg N are both REFUTED under matched+sealed+net-of-cost, seal and declare on-chain actor/launch-identity selection-alpha line closed — do not keep tuning.

Both primary legs (A2 + N) REFUTED → stop line: **True**

## Disposition

No leg cleared PROVEN under sealed + matched + net-of-cost. Do not upgrade to live. Both primary legs REFUTED → declare on-chain actor/launch-identity selection-alpha line closed; do not keep tuning.

## Notes vs v1

- v1 Leg A was crippled by 94.7% cold-start (48h-universe only); v2 rebuilt from 8.3M cross-pool WETH swaps (`uniswap_v3_swaps_decoded`, not the heavy resolved VIEW).
- Own-wallet cold-start still high (93.6%) because actor key in history is swap `recipient`, while panel `economic_actor` is often `tx_from`/EOA — but **score>0 = 70%** via pool-buyer aggregate, so the matched ATT (36k treat / 362k informative) was a powered test of co-buyer quality.
- Clone-family: 919 tokens → 552 distinct `code_hash` families; informative score>0 = 17.8%; still REFUTED.
- v1 deployer leg was dead; v2 replaces it with bytecode clone-family reputation.
- v1 seal (`SEALED_BOTH_REFUTED`) is untouched.

## Artifacts

- `out/alpha_v2/panel_with_alpha_features.parquet`
- `out/alpha_v2/leakage_gate.json` (PASS)
- `out/alpha_v2/alpha_bakeoff.json`
- `out/alpha_v2/ALPHA_HUNT_FINAL.json`
- `configs/freeze_alpha_v2.json` status = `SEALED_BOTH_REFUTED`
