# Codex Review Request — Clanker Alpha Feasibility v0.1

Cursor completed the isolated public-RPC Clanker alpha feasibility study.

## Identity

```text
study = CLANKER_ALPHA_FEASIBILITY_STUDY_V01
run_id = 20260717T154007Z
factory = 0xE85A59c628F7d27878ACeB4bf3b35733630083a9
factory_version = 4
defaults_json_untouched = true
production_clickhouse_queried = false
wallet_or_orders = false
```

## Ten adjudication questions

1. **Factories/versions verified:** Base Clanker v4 `0xE85A59c628F7d27878ACeB4bf3b35733630083a9` (official README + Blockscout verified + live log/ABI match).
2. **Still UNKNOWN:** deployment tx hash; all pre-v4 factories/event layouts.
3. **Sample window:** blocks [48705301, 48712326] (24h buffer before tip).
4. **Launches:** n=120 (see evidence); swaps/features/labels recorded per token.
5. **Buy-all holdout expected net PnL (primary base $250):** -5.643583001405433
6. **Hypotheses discovery vs holdout (primary mean):**
   - H1 disc=None hold=None
   - H2 disc=-0.16810069635341587 hold=-0.2185828503097334
   - H3 disc=None hold=None
7. **Most realistic point:** pre-registered entry+3b / exit+30m / $250 / base cost (sensitivities in evidence).
8. **Costs:** optimistic/base/stressed slippage+impact+gas; marks from Uni v4 Swap sqrtPriceX96; missing entry/exit not dropped.
9. **Strongest counterexample:** {"block_number": 48710987, "features": {"block_number": 48710987, "buy_sell_imbalance_pre_entry": 0.0, "h3_buy_vol": 0.0, "h3_extreme_share": 1.0, "h3_net_imbalance": 0.0, "log_index": 206, "max_buyer_share": 0.0, "n_swaps_24h": 1, "paired_token": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "prior_launches_admin": 0, "prior_launches_sender": 0, "token": "0xa1fdf27f88d890d100916b86c022703ec8a00b07", "top3_buyer_share": 0.0, "unique_buyers_pre_entry": 0}, "net_pnl_usd": -250.0046046, "status": "NO_EXIT_MARK_ZERO", "token": "0xa1fdf27f88d890d100916b86c022703ec8a00b07"}
10. **Verdict:** `EDGE_NOT_SUPPORTED`

## Artifacts

- reports/CLANKER_ALPHA_HYPOTHESES_V01.md
- config/clanker_verified_registry.v0.1.json
- reports/CLANKER_CONTRACT_SOURCE_DOSSIER.md
- reports/CLANKER_ALPHA_FEASIBILITY_V01.md
- reports/CLANKER_ALPHA_HOLDOUT_RESULTS_V01.md
- evidence/clanker_alpha_v01/
- golden/clanker_alpha/
- export/CODEX_HANDOFF_CLANKER_ALPHA_V01.md

Please adjudicate PASS / PASS_WITH_CORRECTIONS / FAIL against the economic standard (not test count).
