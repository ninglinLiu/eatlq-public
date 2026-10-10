# Clanker Alpha Feasibility v0.1

```text
verdict = EDGE_NOT_SUPPORTED
run_id = 20260717T154007Z
factory = 0xE85A59c628F7d27878ACeB4bf3b35733630083a9
version = 4
n_launches = 120
window_blocks = [48705301, 48712326]
discovery_n = 72
holdout_n = 48
primary = entry+3 blocks / exit+30m / notional $250 / cost=base
block_seconds_est = 2.0000
eth_usd_freeze = 1820.0
rpc_calls = 605
rpc_response_bytes = 138293
```

## Method (short)

- Universe: verified Clanker v4 `TokenCreated` only (see contract dossier).
- Prices: Uniswap v4 PoolManager `Swap` `sqrtPriceX96` marks; missing entry → `-gas`; missing exit after entry → inventory marked 0.
- Costs: optimistic/base/stressed slippage, impact, gas (see `evidence/clanker_alpha_v01/`).
- Split: time-ordered 60% discovery / 40% holdout; thresholds fit on discovery only.
- Hypotheses frozen in `reports/CLANKER_ALPHA_HYPOTHESES_V01.md` before PnL.

## Buy-all (base / primary)

| split | summary |
|---|---|
| discovery | n=72 cov=1.00 mean=-0.4601 med=-0.0023 win=0.014 p10=-2.8552 p90=-0.0023 maxLoss=-4.2438 PF=0.01655785139948969 CI=[-0.7404002718608264, -0.18437274163861772] |
| holdout | n=48 cov=1.00 mean=-5.6436 med=-0.0023 win=0.000 p10=-3.9729 p90=-0.0023 maxLoss=-250.0046 PF=0.0 CI=[-16.32685367710601, -0.18704193672289726] |

## Hypotheses on holdout (primary)

| hyp | summary |
|---|---|
| H1 | n=0 |
| H2 | n=41 cov=0.85 mean=-0.2186 med=-0.0023 win=0.000 p10=-0.0023 p90=-0.0023 maxLoss=-4.4589 PF=0.0 CI=[-0.6500300241651684, -0.0023022999999999997] |
| H3 | n=0 |

## Discovery thresholds (frozen after discovery fit)

```json
{
  "h1_unique_buyers_ge": 0.0,
  "h1_max_buyer_share_le": 0.0,
  "h3_imbalance_ge": 0.0,
  "h3_extreme_share_le": 1.0,
  "primary_key": "3b|30m|base|250"
}
```

## Strongest counterexample (holdout, primary)

```json
{
  "token": "0xa1fdf27f88d890d100916b86c022703ec8a00b07",
  "block_number": 48710987,
  "net_pnl_usd": -250.0046046,
  "status": "NO_EXIT_MARK_ZERO",
  "features": {
    "token": "0xa1fdf27f88d890d100916b86c022703ec8a00b07",
    "block_number": 48710987,
    "log_index": 206,
    "unique_buyers_pre_entry": 0,
    "max_buyer_share": 0.0,
    "top3_buyer_share": 0.0,
    "buy_sell_imbalance_pre_entry": 0.0,
    "prior_launches_admin": 0,
    "prior_launches_sender": 0,
    "h3_net_imbalance": 0.0,
    "h3_extreme_share": 1.0,
    "h3_buy_vol": 0.0,
    "n_swaps_24h": 1,
    "paired_token": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
  }
}
```

## Market microstructure (why edge fails)

Primary grid `3b|30m|base|$250` on the sealed 120-launch sample:

| status | n |
|---|---|
| NO_ENTRY (no Swap mark at/after entry) | 104 |
| FILLED | 15 |
| NO_EXIT_MARK_ZERO | 1 |
| pools with any Swap in 24h | 16 / 120 |

Most launches never offer an executable Uni v4 Swap mark by entry+3 blocks. Surviving fills are still net-negative after base costs on holdout (win rate 0). H1/H3 cohorts are empty because pre-entry buyer features are almost always zero — the signals do not fire, which is itself a rejection of those hypotheses on this window.

## Verdict rationale

`EDGE_NOT_SUPPORTED` under pre-registered gates: holdout base-cost expectancy is negative for buy-all and H2; H1/H3 have no selectable holdout cohort; no multi-delay positive edge remains after costs.

Evidence directory: `evidence/clanker_alpha_v01/`.
Defaults (`config/defaults.json`) remain all-closed; production ClickHouse was not queried.
