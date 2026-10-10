# KEYSTONE R2 Reproduce

Version: `r2.0.0`

```bash
cd /root/keystone_r2_atomic
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 src/r2_pipeline.py
```

Requirements:
- Docker ClickHouse `eatlq-clickhouse-1` (optional for confirmation census)
- Archive RPC in `/tmp/rpc_url.txt` or GL5v3 archive URL (read-only eth_call)
- R1 panel at `/root/alpha_discovery_keystone_r1/out/panel_r1.csv`
- Realgas cache `/opt/EATLQ/scripts/research/base_lefttail/_realgas_replay.json`
- GL4 ledger `/opt/EATLQ/mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json`

Does **not** modify `/opt/EATLQ/services/signal_bridge/` or executor config.
Does **not** deploy.

## Expected conclusion gate

See `FINAL_R2.json` → `conclusion`.
Calibration must be trustworthy or pipeline emits ATOMIC_LABEL_NOT_TRUSTWORTHY.
