# Retail Status Quo vs LASZLO

Source: `v2/retail-vs-institutional.svg` · kind: `comparison`

**RETAIL STATUS QUO vs LASZLO INSTITUTIONAL STANDARD**

| Dimension | RETAIL | LASZLO |
|-----------|--------|--------|
| Data filter | Noisy transfers · 15s polling | Blacklist + whale USD filter |
| Ingest latency | (tabs / polling) | WS ingest · Redis Streams · <20ms |
| Tooling | Nansen + TV + Uniswap tabs | Ingest → Strategy → Executor loop |
| MEV / gas | Public mempool · MEV sandwich | Base L2 gas · 3% slippage hard cap |
| Exits | Manual stop · forgot positions | Trailing · Time · Hard stop (chain) |
| Form factor | Fragmented tools · context loss | Single-tenant weapon system |

