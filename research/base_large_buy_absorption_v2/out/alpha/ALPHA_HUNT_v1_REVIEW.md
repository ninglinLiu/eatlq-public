# ALPHA_HUNT_v1_REVIEW

Generated: `2026-07-21T09:15:24Z`

## Leakage gate: **PASS**

## Live: **NO_GO** | Observer shadow proposed: **False**

## Per-family verdicts (net-of-cost, incremental over random)

| Family | Matched ATT | Ranker | Overall | ATT mean (WETH) | day p05 |
| --- | --- | --- | --- | ---: | ---: |
| actor | REFUTED | REFUTED | **REFUTED** | -0.001470 | -0.008008 |
| genesis | REFUTED | REFUTED | **REFUTED** | -0.001051 | -0.006037 |
| combined | UNCONFIRMED | REFUTED | **UNCONFIRMED** | 0.000367 | -0.001707 |

## Success bar

net-of-real-cost incremental return over RANDOM-spray, robust across walk-forward folds, BH-corrected, replicated on sealed holdout

## Stop rule

If both actor and genesis are REFUTED under matched+sealed bar net of cost, seal and stop — do not keep tuning.

Both families REFUTED → stop line: **True**

## Disposition

No family cleared PROVEN under the sealed + matched + net-of-cost bar. Do not upgrade to live. Both actor and genesis are REFUTED → **stop this line; do not keep tuning the same features.**

## Data-quality notes (honest caveats, not excuses)

- Leakage gate: **PASS** (as-of-T invariance + no forward refs + placebo).
- Outcome = `pnl_net` (realized kappa impact + LP fee), incremental over random spray.
- Actor cold-start rate ≈ **94.7%** in the fresh-pool 48h universe (few wallets close a prior round-trip before signal). Treatment = top decile among *informative* (`score > 0`) rows only.
- Deployer address join via `tx_from_by_hash` hit **1 / 16,201** pools → deployer-reputation leg was inert. Genesis score still ran on LP mint/burn + holder-growth proxies and was **REFUTED** on those features alone.
- Forbidden lifetime tables (`sender_pnl_summary_v1`, etc.) were not used.

## Artifacts

- `out/alpha/panel_with_alpha_features.parquet`
- `out/alpha/leakage_gate.json`
- `out/alpha/alpha_bakeoff.json`
- `out/alpha/ALPHA_HUNT_FINAL.json`
- `configs/freeze_alpha_v1.json` status = `SEALED_BOTH_REFUTED`
