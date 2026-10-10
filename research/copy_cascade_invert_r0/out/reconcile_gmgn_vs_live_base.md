# GMGN vs live base FIFO

status: `VENDOR_LEDGER_GAP`
generated_at: 2026-09-17T07:27:23+00:00

- public RPC head: `https://mainnet.base.org` #51419202
- native USD spot: 2443.333038894916
- addresses / ok / fail: 378 / 378 / 0
- lookback: 40d
- 7d both / sign agree / disagree: 378 / 325 / 24
- 7d sign-agree rate: 0.8597883597883598 (active |pnl|>=$1: 0.49504950495049505 n=101)
- 1d / 30d sign-agree rate: 0.9259259259259259 / 0.7698412698412699
- 7d Pearson all / active: -0.34828883304419894 / -0.3492623305225864
- 7d median |our-vendor| USD: 0.0
- wallets with transfer-in/out: 304
- wallets with unmatched sells: 179
- truncated: 33

vendor = GMGN walletNew realized_profit_* USD; ours = public Blockscout ERC-20 + tx WETH mint/burn FIFO, daily native USD from Blockscout market chart. Token-token (non-ETH/stable) quotes are transfer-contaminated and under-count vs GMGN.

Gaps: SIGN_DISAGREEMENT_ACTIVE, HIGH_TRANSFER_CONTAMINATION, HIGH_UNMATCHED_SELLS

This is contemporaneous reconstruct. July offline tape is not used here.
GO remains false.

