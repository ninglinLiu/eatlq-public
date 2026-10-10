# LASZLO Data Pipeline

Source: `v2/pipeline-flow.svg` · kind: `flow`

**LASZLO DATA PIPELINE**

```text
Base L2 (Alchemy WS/HTTP)
  → Ingestor
    → market_ticks (Redis Streams)
      → Strategy (XGBoost v3)
        → Executor (swap + ledger)
          → Ledger: trade_ledger.jsonl + dashcam.csv
```

- Phase 6: label → train → holdout · Pe ≥ 72%
- Risk gates: stale head · drawdown · SELL_ONLY · `fire_thr = max(model, 0.68)`

