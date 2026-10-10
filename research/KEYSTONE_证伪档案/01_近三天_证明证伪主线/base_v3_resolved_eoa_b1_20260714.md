---
sop_applied: true
alpha_id: AR-B1-RESOLVED-EOA
family: F3/F7
next_gate: CHA_SIDE_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
---

# Base V3 B1: resolved-EOA attribution and golden reconciliation

**Status:** B1 complete, research-only. No service was started/restarted, no live config was changed, and no transaction was signed or sent. This report does not make a GO decision.

## Executive answer

The old `swap.sender` attribution is not a noisy wallet proxy; it is the wrong entity layer. In the high-coverage block window **46,389,458 through 46,417,105** (2026-05-23 20:31:03 UTC through 2026-05-24 11:52:37 UTC), direct sender produced only **280 identities** (273 after known-infrastructure removal), while canonical resolution produced **9,274 wallets**. The old non-infrastructure set missed **9,218 / 9,274 = 99.40%** of resolved wallets. **57,204 / 57,426 = 99.61%** of swap rows resolved to an address different from old sender or had a known router as sender.

Known router/aggregator senders directly contaminated **14,083 / 57,426 = 24.52%** of all swaps. In the old top-100 sender ranking, only 6 entries were known infrastructure, but they generated **14,076 / 56,517 = 24.91%** of top-100 activity. Universal Router alone collapsed 6,350 swaps from 1,718 resolved wallets into one apparent "wallet"; SwapRouter02 collapsed 4,399 swaps from 537 wallets; observed router `0x7747...` collapsed 3,084 swaps from 1,044 wallets.

**Is this enough to explain PF ~0.08 direct-copy failure?** Yes, the error is easily large enough to explain the failure mechanism: the old key selects/copies routers and shared execution contracts rather than the initiating accounts, and loses 99.4% of wallet identities. Therefore the old PF is not a valid test of wallet-following alpha. It is not yet proof that attribution is the *only* cause or that the strategy becomes profitable: a block-number-aligned replay using `resolved_eoa` is still required. The correct B1 verdict is **old direct-copy conclusion invalidated by attribution confounding; strategy merit remains untested**, not GO.

## Resolved attribution layer

Canonical policy is:

```text
first valid non-infrastructure address:
tx_from_by_hash.tx_from > swap.recipient > swap.sender > unresolved
```

The single source manifest is `configs/research/base_attribution_exclusions.json` (36 lower-case addresses, schema v1). It merges the previously scattered Base quote tokens, Uniswap routers, Aerodrome routers, 1inch/0x/Velora/CoW/Kyber/Bebop/OKX/Socket and observed aggregators, V3 NPM, Quoter/QuoterV2, Permit2, and ERC-4337 EntryPoint. Historical sprint-local lists remain audit evidence but are deprecated for new attribution work. `scripts/wallet_profile_v0/sender_semantics.py` now delegates to the canonical manifest.

`infra/clickhouse/init/041_base_v3_resolved_wallet_swaps.sql` installs:

- `eatlq.base_wallet_attribution_exclusions`: research authority table synced exactly from the manifest.
- `eatlq.base_v3_wallet_swaps_resolved`: reusable dynamic view analogous to `cha_wallet_swaps`.
- Provenance fields: `resolved_eoa`, `attribution_source`, `has_tx_from`, `old_sender_status`, `block_time_source`.
- Quote-aware fields: `target_trade_side`, target token/decimals, and wallet target-token delta.

The view does not claim recipient/sender fallbacks are RPC-verified EOAs. Consumers can require `attribution_source='tx_from'` for high-confidence analysis. `tx.from` is the transaction originator under the task's frozen priority; account-abstraction/bundler semantics remain a residual risk and are why golden addresses also require `eth_getCode=0x`.

## Real time and block window

All filtering and splitting use `block_number`. `resolved_block_time` uses `block_ts_map` when the exact block exists, otherwise the decoded swap's archiver `blockTimestamp`; arrival time is never used as a fallback. The selected window has zero missing real block times and uses the archiver timestamp because `block_ts_map` has no rows for these exact blocks.

The window was selected by coverage, not by date: it is the min/max block span of the current `tx_from_by_hash` backfill. It contains 57,426 swaps and 52,989 unique txs; **52,493 txs (99.06%)** have `tx.from`. Attribution sources by swap row are:

| Source | Swaps | Share |
|---|---:|---:|
| `tx_from` | 56,904 | 99.09% |
| `recipient` fallback | 431 | 0.75% |
| `sender` fallback | 10 | 0.02% |
| unresolved | 81 | 0.14% |

Important limitation: across the full 8.11M-tx V3 history, the current `tx_from_by_hash` contains only 52,493 hashes (about **0.65%**). The view works for the full history but older rows will mostly be lower-confidence recipient/sender fallback until block-number-based tx backfill is expanded. This report therefore makes bounded quantitative claims for the stated block window.

## Old versus resolved error

| Metric | Old sender | Resolved | Error / interpretation |
|---|---:|---:|---|
| Raw distinct identities | 280 | 9,274 | resolved has 33.1x as many identities |
| Non-infrastructure identities | 273 | 9,274 | 9,218 resolved wallets absent from old set (99.40%) |
| Quote-aware buys attributed to eligible wallet | 8,533 | 10,767 | old loses 2,234 (20.75%) after router removal |
| Quote-aware sells attributed to eligible wallet | 8,254 | 10,684 | old loses 2,430 (22.74%) after router removal |
| Known-router sender swaps | 14,083 | 0 as wallet identity | 24.52% direct contamination |
| Changed or known-router rows | 57,204 | n/a | 99.61% of all swap rows |

Buy/sell counts cover 21,483 quote-aware rows whose discovered pool metadata has exactly one canonical quote leg. Raw sender can preserve the global event count only by assigning those events to contracts; once infrastructure is removed, it drops about 21-23% of classifiable trades. The larger damage is entity collapse: thousands of unrelated wallets are aggregated under a few senders.

Top known contaminants:

| Old sender | Label | Swaps | Unique resolved wallets |
|---|---|---:|---:|
| `0x6ff5693b...d299b43` | Uniswap Universal Router | 6,350 | 1,718 |
| `0x2626664c...741e481` | Uniswap SwapRouter02 | 4,399 | 537 |
| `0x7747f8d2...47f2359` | observed Base router | 3,084 | 1,044 |
| `0xaee2b8d4...c03d396e` | observed aggregator | 184 | 27 |

## Golden reconciliation

Golden fixture: `tests/fixtures/base_v3_resolved_eoa_golden.json`. Both wallets route through Universal Router, have `tx.from` attribution, and returned empty code (`0x`) at block 46,417,105.

| Wallet | Resolved V3 count | Target-token net delta | Independent result |
|---|---:|---:|---|
| [`0x44e5...1b038`](https://basescan.org/address/0x44e5f8183162994e70f3d4a9235a8a1e2471b038) | 4 buys / 17 sells | 401,071.598661039602907575 | boundary `balanceOf` delta = exact raw `401071598661039602907575` |
| [`0x5ccf...67c6d`](https://basescan.org/address/0x5ccfc6e399003a8e2f68d9c7710f965b9cf67c6d) | 7 buys / 7 sells | 208,520.074714488298800513 | boundary `balanceOf` delta = exact raw `208520074714488298800513` |

Manual transaction checks:

- [Golden tx 1](https://basescan.org/tx/0x4da255c913b1648182d7f5ee44c6716420491c30486598855ea773fed251b801): Basescan shows block 46,392,878, `From=0x44e5...1b038`, `To=Universal Router`; local resolved row matches wallet, block and tx hash.
- [Golden tx 2](https://basescan.org/tx/0xe881d6db0f60479a7e71af503bf5b648cb8e2f1a0f25209ab97db315db71a893): Basescan shows block 46,401,455, `From=0x5ccf...67c6d`, `To=Universal Router`; local resolved row matches.
- Independent historical Base RPC `balanceOf` at block 46,389,457 and 46,417,105 matches each view-computed raw net delta exactly. This checks holdings independently of swap aggregation and avoids comparing current balances to a historical window.

## Regression result and operating boundary

Commands run successfully:

```bash
python3 -m unittest tests.test_base_v3_resolved_attribution -v
EATLQ_RUN_CH_GOLDEN=1 python3 -m unittest \
  tests.test_base_v3_resolved_attribution.GoldenRegressionTests.test_clickhouse_view_matches_golden -v
```

Result: 10 tests passed in the normal suite (1 CH integration test intentionally skipped without opt-in); the opted-in ClickHouse golden test passed for both wallets. Tests cover priority/fallback, infrastructure rejection, quote-leg-aware side, no arrival-time fallback, block-number-only analysis, fixture invariants, and live research-view reconciliation.

Research output is at `mvp/diagnostics/base_v3_resolved_eoa_b1_20260714.json`. Reproduce/sync the research layer with:

```bash
python3 scripts/wallet_profile_v0/base_v3_resolved_attribution.py \
  --install --start-block 46389458 --end-block 46417105 \
  --output mvp/diagnostics/base_v3_resolved_eoa_b1_20260714.json
```

`--install` touches only the new research exclusion table and research view. No live/executor/strategy service or configuration is changed. Await CHA-side review; do not promote, train, or trade from this result.
