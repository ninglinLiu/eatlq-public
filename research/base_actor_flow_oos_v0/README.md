# Base V3 Actor-Flow OOS Harness v0

Isolated research project. Does **not** modify production KEYSTONE or reopen the old wallet-ranking / copy pipeline.

```bash
/root/base_actor_flow_oos_v0/.venv/bin/python -m src.run_all 2>&1 | tee logs/RUN.log
```

## Phases

1. **Actor resolution foundation** — tier A/B/C/D economic actors; hard gate on sampled error ≤ 2%
2. **PIT panel + identity scores** — matured-episode traits only
3. **Six-arm walk-forward OOS** — `POOL_ONLY` / `DIAMOND` / `WHALE_EVENT` / `SMART_ALPHA` / `BOT_REGIME` / `INDEPENDENT_CONSENSUS`
4. **Dual verdict** — `INFORMATION_EDGE_GO` vs `COPY_EDGE_GO`

## Outputs

- `out/phase1/RESOLUTION_GATE.json` — foundation gate
- `out/panel/events.parquet` — PIT event panel
- `out/scores/actor_traits.parquet` — matured identity traits
- `out/oos/scorecard.csv` — arm scorecard
- `out/ACTOR_FLOW_FINAL.json` — dual verdict
- `ACTOR_FLOW_审查包_完整合并.md` — review package

`deploy_allowed=false` always.
