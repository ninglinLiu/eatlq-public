# Clanker Alpha v0.3 Round-Trip Coverage Census

```text
implementation_commit = 68e5c40627d96004f927179a4c80d516b2aeefbb
closeout_commit = 24725417c82fe836e4d424f75cb98c7105e240c4
implementation_tree = d7499d120457fe2d6acf4583a5961a467e71572c
verdict = INSUFFICIENT_ROUND_TRIP_MARKET_DATA
run_id = 20260718T072130Z
network_calls = 0
n_launches = 200
launches_with_any_buy = 16
launches_with_any_sell = 16
independent_round_trip_launches_30m = 6
threshold = 30
pnl_authorized = false
```

## Route mapping (captured sealed txs)

```json
{
  "MAPPED_CUSTOM_ROUTER": 10,
  "MAPPED": 3,
  "AMBIGUOUS_MULTI_LEG": 2
}
```

Goldens: `golden/clanker_alpha_v03/` (n=5).

## Semantics

Corrected caller-delta signs from `reports/CLANKER_V02_SWAP_DIRECTION_ERRATA.md`.
Old inverted buy predicates are rejected by goldens.

## Next acquisition (if still insufficient)

```json
{
  "current_round_trips": 6,
  "gap_to_30": 24,
  "need": "trace_capable_public_eth_getTransactionReceipt_or_debug_trace_for_swap_txs",
  "suggested_budget": {
    "concurrency": 1,
    "max_calls": 200,
    "targets": "unique swap txs for launches that have buy but no sell in-window"
  },
  "why": "Sealed v0.2 swap logs classify buy/sell by corrected event signs, but many launches still lack an observed sell within 30m; expanding to receipt/trace would recover multi-leg router semantics and confirm external beneficiary without production DB access."
}
```

v0.2 evidence bytes were not modified.
