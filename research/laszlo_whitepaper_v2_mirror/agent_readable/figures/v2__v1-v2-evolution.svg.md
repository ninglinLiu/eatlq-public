# V1 Blueprint → V2 Battle-tested

Source: `v2/v1-v2-evolution.svg` · kind: `table`

**V1 BLUEPRINT → V2 BATTLE-TESTED**

| Dimension | V1.0 | V2.0 |
|-----------|------|------|
| Chain | Ethereum Mainnet | Base L2 (OP Stack) |
| MEV | Flashbots Bundle | EIP-1559 Gas + 3% slippage cap |
| Honeypot | Concept only | Quoter V2 eth_call simulate |
| Stops | *(row incomplete in SVG; v2 has)* | slot0() every 2s · triple stop |
| ML | Synthetic XGBoost | v1 synth → v2 labeled auto-switch |
| Ingest | pendingTransactions | eth_subscribe logs · V3 Swap ABI |
| Features | ClickHouse / Redis ZSET | feat2:{symbol} Redis Hash buckets |
| Drawdown | Manual kill switch | Auto 10% daily halt (UTC reset) |

