# Codex Review Request — Clanker Alpha v0.3 Round-Trip Census

## Identity

```text
study = CLANKER_ALPHA_V03_ROUNDTRIP_CENSUS
implementation_commit = 68e5c40627d96004f927179a4c80d516b2aeefbb
implementation_tree = d7499d120457fe2d6acf4583a5961a467e71572c
closeout_commit = 24725417c82fe836e4d424f75cb98c7105e240c4
run_id = 20260718T072130Z
verdict = INSUFFICIENT_ROUND_TRIP_MARKET_DATA
independent_round_trip_launches_30m = 6
network_calls = 0
pnl_computed = false
v02_bytes_modified = false
```

## Deliverables

- launchpad_intelligence/clanker_alpha/v03_semantics.py
- launchpad_intelligence/clanker_alpha/v03_route_decode.py
- scripts/run_clanker_alpha_v03_roundtrip_census.py
- tests/test_clanker_alpha_v03.py
- golden/clanker_alpha_v03/
- evidence/clanker_alpha_v03/
- reports/CLANKER_ALPHA_V03_ROUNDTRIP_CENSUS.md
- export/CODEX_HANDOFF_CLANKER_ALPHA_V03.md

## Route mapping

```json
{
  "AMBIGUOUS_MULTI_LEG": 2,
  "MAPPED": 3,
  "MAPPED_CUSTOM_ROUTER": 10
}
```

## Census

```json
{
  "independent_round_trip_launches_30m": 6,
  "launches_with_any_buy": 16,
  "launches_with_any_sell": 16,
  "n_launches": 200,
  "pnl_authorized": false,
  "round_trip_window_blocks": 900,
  "source_launches_sha256": "f35335d150a0e0501d0ba9e198e4cbb1198820667a4898aef1b6796813709aac",
  "source_swaps_sha256": "fd62ea249ef2bdb50284564c4c8252ca4f35fc40814c987493c9f78510911a5e",
  "threshold": 30,
  "verdict": "INSUFFICIENT_ROUND_TRIP_MARKET_DATA"
}
```

## Next acquisition (if Codex agrees)

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

Please adjudicate PASS / PASS_WITH_CORRECTIONS / FAIL.
