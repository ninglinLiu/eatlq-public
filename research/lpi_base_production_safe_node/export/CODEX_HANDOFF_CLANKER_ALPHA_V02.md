# Codex Review Request — Clanker Alpha Execution-Validity v0.2

## Identity

```text
study = CLANKER_ALPHA_EXECUTION_VALIDITY_V02
run_id = 20260717T172757Z
implementation_commit = 2959e3a8c5f81483d43886c48a7ff4d8692a0f0c
implementation_tree = 3268e2f54823e63b56b4879116a1fb5dc5b6f0a3
evidence_commit = cf65a01b364b84c473d48d240069a92861737a70
evidence_tree = bbb886e5e2dfe1acb3863805b4fb3c143097e231
method_sha256 = d7753fc208c96024caeb95b42be404a235edf7ae4c68468e41a4e6f47e74d83e
hypotheses_sha256 = acf67c817bc3dde786913a2441f54f78ad55f1d324867d24ad3c68250e3b8f6f
verdict = INSUFFICIENT_MARKET_DATA
executable_holdout_n = 0
v01_errata = reports/CLANKER_ALPHA_V01_ERRATA.md
v01_bytes_modified = false
v01_corrected_state = INSUFFICIENT_MARKET_DATA
defaults_json_untouched = true
production_db = false
wallet_or_orders = false
```

## Window

```text
v01_excluded_to_block = 48712326
launch_from = 48713150
launch_to = 48716765
n_launches = 200
discovery_n = 120
holdout_n = 80
rpc_calls = 919
rpc_bytes = 6030488
eth_usd = 1842.6
eth_usd_source = blockscout_stats:20260717T172757Z
max_entry_wait_blocks = 5
swap_coverage_complete = 200/200 pools
```

## Holdout snapshot

```json
{
  "universe": {
    "launches": 80,
    "no_trade_rate": 0.8375,
    "observed_swaps_pools": 11,
    "quote_availability": 0.0,
    "status_counts": {
      "EXIT_QUOTE_UNAVAILABLE": 13,
      "NO_TRADE_NO_QUOTE": 67
    }
  },
  "conditional_executable": {
    "n": 0
  },
  "strategy_all": {
    "bootstrap_mean_ci95": [
      0.0,
      0.0
    ],
    "coverage_selected": 0.8375,
    "expected_net_pnl": 0.0,
    "max_single_loss": 0.0,
    "mean": 0.0,
    "median": 0.0,
    "n": 67,
    "p10": 0.0,
    "p90": 0.0,
    "profit_factor": NaN,
    "skipped_non_pnl": 13,
    "win_rate": 0.0
  }
}
```

## Why this verdict

Confirmation holdout executable entries = 0 (< 30).
Under the pre-registered 5-block max entry wait and no forced zero on missing exits, the confirmation window does not support `EDGE_NOT_SUPPORTED` or `EDGE_SUPPORTED`.

## Artifacts

- reports/CLANKER_ALPHA_V01_ERRATA.md
- reports/CLANKER_ALPHA_HYPOTHESES_V02.md
- reports/CLANKER_ALPHA_METHOD_V02.md
- reports/CLANKER_ALPHA_EXECUTION_VALIDITY_V02.md
- reports/CLANKER_ALPHA_HOLDOUT_V02.md
- evidence/clanker_alpha_v02/
- export/CODEX_HANDOFF_CLANKER_ALPHA_V02.md

Please adjudicate PASS / PASS_WITH_CORRECTIONS / FAIL.
