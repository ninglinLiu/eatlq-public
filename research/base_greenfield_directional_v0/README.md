# Base Greenfield Directional Alpha v0

Isolated research project. Does **not** modify production KEYSTONE.

```bash
/root/base_greenfield_directional_v0/.venv/bin/python src/run_all.py
```

Outputs:
- `out/panel/` — PIT greenfield panel
- `out/labels/` — frozen horizon/barrier + labels
- `out/predictions/oof_*.parquet` — OOF only
- `out/metrics/oof_scorecard.csv` — controls + formal routes
- `out/FINAL_GREENFIELD.json` — ≤2 candidates or `NO_GREENFIELD_ALPHA_FOUND`
