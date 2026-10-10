# Clanker Alpha Holdout v0.2

```text
verdict = INSUFFICIENT_MARKET_DATA
holdout_eval = ONCE
executable_n = 0
window = [48713150, 48716765]
```

## Universe
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

## Conditional executable
{
  "n": 0
}

## Strategy-level (NO_TRADE=0)
all: {
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
H1: {
  "coverage_selected": 0.0,
  "n": 0,
  "skipped_non_pnl": 0
}
H2: {
  "bootstrap_mean_ci95": [
    0.0,
    0.0
  ],
  "coverage_selected": 0.075,
  "expected_net_pnl": 0.0,
  "max_single_loss": 0.0,
  "mean": 0.0,
  "median": 0.0,
  "n": 6,
  "p10": 0.0,
  "p90": 0.0,
  "profit_factor": NaN,
  "skipped_non_pnl": 0,
  "win_rate": 0.0
}
H3: {
  "bootstrap_mean_ci95": [
    0.0,
    0.0
  ],
  "coverage_selected": 0.75,
  "expected_net_pnl": 0.0,
  "max_single_loss": 0.0,
  "mean": 0.0,
  "median": 0.0,
  "n": 60,
  "p10": 0.0,
  "p90": 0.0,
  "profit_factor": NaN,
  "skipped_non_pnl": 12,
  "win_rate": 0.0
}

## Discovery reference
{
  "launches": 120,
  "no_trade_rate": 0.7666666666666667,
  "observed_swaps_pools": 20,
  "quote_availability": 0.0,
  "status_counts": {
    "EXIT_QUOTE_UNAVAILABLE": 28,
    "NO_TRADE_NO_QUOTE": 92
  }
}
