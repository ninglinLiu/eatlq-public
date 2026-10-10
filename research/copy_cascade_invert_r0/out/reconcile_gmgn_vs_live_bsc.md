# GMGN vs live bsc FIFO

status: `VENDOR_LEDGER_GAP`
generated_at: 2026-09-17T07:27:23+00:00

- public RPC head: `https://bsc-dataseed.binance.org` #122368528
- native USD spot: 725.4904023816368
- addresses / ok / fail: 532 / 532 / 0
- lookback: 40d
- 7d both / sign agree / disagree: 532 / 183 / 38
- 7d sign-agree rate: 0.34398496240601506 (active |pnl|>=$1: 0.10963455149501661 n=301)
- 1d / 30d sign-agree rate: 0.36278195488721804 / 0.31390977443609025
- 7d Pearson all / active: 0.12576601215329594 / 0.10443694829313806
- 7d median |our-vendor| USD: 5.6254798460019
- wallets with transfer-in/out: 77
- wallets with unmatched sells: 107
- truncated: 422

vendor = GMGN walletNew realized_profit_* USD; ours = public Blockscout ERC-20 + tx WETH mint/burn FIFO, daily native USD from Blockscout market chart. Token-token (non-ETH/stable) quotes are transfer-contaminated and under-count vs GMGN.

Gaps: SIGN_DISAGREEMENT_ACTIVE, ZERO_VS_NONZERO

This is contemporaneous reconstruct. July offline tape is not used here.
GO remains false.

