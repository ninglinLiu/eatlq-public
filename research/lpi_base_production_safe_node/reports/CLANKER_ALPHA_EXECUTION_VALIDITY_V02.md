# Clanker Alpha Execution-Validity v0.2

```text
verdict = INSUFFICIENT_MARKET_DATA
run_id = 20260717T172757Z
implementation_commit_pre_collection = 2959e3a8c5f81483d43886c48a7ff4d8692a0f0c
implementation_tree_pre_collection = 3268e2f54823e63b56b4879116a1fb5dc5b6f0a3
method_sha256 = d7753fc208c96024caeb95b42be404a235edf7ae4c68468e41a4e6f47e74d83e
hypotheses_sha256 = acf67c817bc3dde786913a2441f54f78ad55f1d324867d24ad3c68250e3b8f6f
v01_excluded_to_block = 48712326
window = [48713150, 48716765]
n_launches = 200
discovery_n = 120
holdout_n = 80
executable_holdout_n = 0
rpc_calls = 919
rpc_bytes = 6030488
eth_usd = 1842.6
eth_usd_source = blockscout_stats:20260717T172757Z
max_entry_wait_blocks = 5
swap_coverage_complete_pools = 200/200
```

## Status mix (full sample)

{
  "EXIT_QUOTE_UNAVAILABLE": 41,
  "NO_TRADE_NO_QUOTE": 159
}

## Holdout universe

{
  "launches": 80,
  "no_trade_rate": 0.8375,
  "observed_swaps_pools": 11,
  "quote_availability": 0.0,
  "status_counts": {
    "EXIT_QUOTE_UNAVAILABLE": 13,
    "NO_TRADE_NO_QUOTE": 67
  }
}

## Conditional-on-executable (holdout)

{
  "n": 0
}

## Strategy-level (NO_TRADE PnL=0)

- all: {'bootstrap_mean_ci95': [0.0, 0.0], 'coverage_selected': 0.8375, 'expected_net_pnl': 0.0, 'max_single_loss': 0.0, 'mean': 0.0, 'median': 0.0, 'n': 67, 'p10': 0.0, 'p90': 0.0, 'profit_factor': nan, 'skipped_non_pnl': 13, 'win_rate': 0.0}
- H1: {'coverage_selected': 0.0, 'n': 0, 'skipped_non_pnl': 0}
- H2: {'bootstrap_mean_ci95': [0.0, 0.0], 'coverage_selected': 0.075, 'expected_net_pnl': 0.0, 'max_single_loss': 0.0, 'mean': 0.0, 'median': 0.0, 'n': 6, 'p10': 0.0, 'p90': 0.0, 'profit_factor': nan, 'skipped_non_pnl': 0, 'win_rate': 0.0}
- H3: {'bootstrap_mean_ci95': [0.0, 0.0], 'coverage_selected': 0.75, 'expected_net_pnl': 0.0, 'max_single_loss': 0.0, 'mean': 0.0, 'median': 0.0, 'n': 60, 'p10': 0.0, 'p90': 0.0, 'profit_factor': nan, 'skipped_non_pnl': 12, 'win_rate': 0.0}

## Quote notes

- Historical `V4Quoter` often reverts on Clanker dynamic-fee hooks; some entry quotes succeeded via quoter at intended entry.
- Exit quotes within `[intended_exit, intended_exit+5]` were usually unavailable → `EXIT_QUOTE_UNAVAILABLE` (inventory **not** forced to zero).
- Cross-checks written: `evidence/clanker_alpha_v02/quote_cross_checks.json` (n=12).
- v0.1 bytes untouched; see `reports/CLANKER_ALPHA_V01_ERRATA.md`.

## Verdict rationale

`INSUFFICIENT_MARKET_DATA` because confirmation holdout executable entries = **0** (< 30). This is not an `EDGE_NOT_SUPPORTED` claim.
