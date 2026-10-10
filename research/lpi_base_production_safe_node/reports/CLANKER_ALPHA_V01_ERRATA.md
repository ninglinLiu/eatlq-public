# Clanker Alpha v0.1 Errata (Codex FAIL / REJECT_IMPLEMENTATION)

```text
status = ERRATA_SEALED
codex_adjudication = FAIL / REJECT_IMPLEMENTATION
corrected_economic_state = INSUFFICIENT_MARKET_DATA
v01_bytes_modified = false
```

This document does **not** rewrite v0.1 artifacts. Original bytes remain unchanged.
Machine inventory: `evidence/clanker_alpha_v01/V01_ARTIFACT_SHA256.json`.

## Corrected economic state

The v0.1 headline verdict `EDGE_NOT_SUPPORTED` is **withdrawn** as an economic claim.

```text
v0.1_corrected_state = INSUFFICIENT_MARKET_DATA
ready_for_clanker_alpha_claim = false
```

v0.1 remains useful only as development / decoder / factory-identity evidence.

## Confirmed defects

1. **Holdout mix-up:** Of 48 holdout launches (primary `3b|30m|base|$250`), sealed recount is **5 FILLED / 42 NO_ENTRY / 1 NO_EXIT_MARK_ZERO**. These were pooled into one “buy-all” mean.
2. **Timeline inversion (FILLED):** Holdout token `0x0f1e1ad1fec433f2e421bada92269bdc799aab07` used entry Swap at block **48712941** after intended exit **48712030**.
3. **Timeline inversion (NO_EXIT −250):** Token `0xa1fdf27f88d890d100916b86c022703ec8a00b07` entry mark **48714584** after intended exit **48711890**, then inventory forced to zero.
4. **NO_ENTRY billed as gas loss:** Missing quotes were assigned `-entry_gas` and averaged into strategy PnL.
5. **sqrtPriceX96 ≠ executable quote:** Post-swap marks are not exact-input fills for $100/$250/$500 and ignore hook/dynamic fee impact.
6. **Buyer identity error:** `PoolManager.Swap.sender` was treated as economic buyer.
7. **Incomplete 24h coverage:** Probe + sparse late check left middle ranges unscanned; `n_swaps_24h` overstated completeness.
8. **Holdout contamination:** The 120-launch window and multiple runner attempts are development data only.
9. **Economic adjudication correction:** Replace v0.1 `EDGE_NOT_SUPPORTED` with `INSUFFICIENT_MARKET_DATA`.

## Artifact SHA256 (immutable)

| path | sha256 | bytes |
|---|---|---|
| `reports/CLANKER_ALPHA_HYPOTHESES_V01.md` | `08a825289dd2eb2af2179e38b262787b72f3663f792380155cbdba680127483f` | 4977 |
| `reports/CLANKER_ALPHA_FEASIBILITY_V01.md` | `4b0038af43b96bdfb907bb2d8344db41b27ef5d690501c9efb1b0c2118af6872` | 3436 |
| `reports/CLANKER_ALPHA_HOLDOUT_RESULTS_V01.md` | `fa8200e444694d1a6eeab3de46214df2f41aea42c5c6f6eccd380470cedd372f` | 1491 |
| `reports/CLANKER_CONTRACT_SOURCE_DOSSIER.md` | `585fe851e091c784b03fb995348b8125291430d06c22ff6963b017f2bd8e7d3e` | 2953 |
| `config/clanker_verified_registry.v0.1.json` | `aff806ebbcb3a55b60861c5dbbbe011e07e41bb044067d26b2442524319a016e` | 4075 |
| `export/CODEX_HANDOFF_CLANKER_ALPHA_V01.md` | `b41395084b54f368f9073427c45130c664089f9dd51b427c3b6555b0eb7d36d0` | 2396 |
| `evidence/clanker_alpha_v01/launches.json` | `68ead4b829f165627be322d5442fbedf582ba9b337c88d62f2ab6188a19ce7ac` | 561655 |
| `evidence/clanker_alpha_v01/features.json` | `ed1bb5249d5ac32632d661ebb6d44a917023fbdca65dcf7bba9dbdd2effd250a` | 62012 |
| `evidence/clanker_alpha_v01/labels_primary_grid.json` | `9995cbe2b998d633eead41d98e9be442f8e3245bad91cfb15272eafef62c1abe` | 7672338 |
| `evidence/clanker_alpha_v01/swaps_by_token.json` | `82a82b43319cdfcf66e0c34637be98d7d6fabde6f8a7f5f8020613968c945188` | 42302 |
| `evidence/clanker_alpha_v01/cohort_results.json` | `18dad8b99360213e6897773ac3db5221c5f31122f259874fae41755c248e7556` | 68216 |
| `evidence/clanker_alpha_v01/rpc_journal.json` | `f8813d7ffb08d97ca5114279fd23829705709961c3d5e7984d06ed873cfcec46` | 247674 |
| `evidence/clanker_alpha_v01/discovery_thresholds.json` | `f3f429df1b0d326b363a51181450a2b7dd32f04a4938be8d80e87297f2ca9b9a` | 157 |

## Next study

`CLANKER ALPHA EXECUTION-VALIDITY RECOVERY v0.2` — new methods committed before any non-overlapping confirmation window.
