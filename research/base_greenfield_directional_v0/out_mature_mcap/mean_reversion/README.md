# Mature-token simple mean-reversion probe

Generated: `2026-07-20T12:37:03Z`

## OOS totals by strategy (sum across tokens)

```
  strategy  n_tokens  n_trades  total_pnl_usd  median_token_pnl  n_tokens_profitable
DIP10_168H         1         0       0.000000          0.000000                    0
DIP20_168H         1         0       0.000000          0.000000                    0
MA72_BELOW         1         0       0.000000          0.000000                    0
 BH_ALWAYS         1         1      -0.889505         -0.889505                    0
   Z72_BUY         1         2      -2.619115         -2.619115                    0
    RSI_OS         1         5      -6.862238         -6.862238                    0
   Z24_BUY         1         7      -9.406492         -9.406492                    0
```

## Per-token best OOS strategy

```
symbol   strategy  n_trades  total_pnl_usd  win_rate
 cbETH DIP10_168H         0            0.0       NaN
```

## Caveats
- Price from UniV3 WETH pool ticks (some DS pairs are Aerodrome-primary).
- Friction = 40bps RT + $0.5 gas; not full atomic simulation.
- `deploy_allowed=false`.

