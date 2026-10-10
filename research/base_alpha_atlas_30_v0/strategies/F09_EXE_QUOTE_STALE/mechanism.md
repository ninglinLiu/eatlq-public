# F09_EXE_QUOTE_STALE Mechanism

If mid moves >=x without swaps for g seconds (stale), trade mean-reversion toward last trade price when swaps resume.

Counterparty: stale_arbitrageurs_slow

## Why distinct
Latency/staleness microstructure; not trend.

## Falsification
inject fake stale flags
