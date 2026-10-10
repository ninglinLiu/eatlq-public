# BASE_ALPHA_ATLAS A→B→C Run Result

Executor role. UTC window ≈ 2026-07-18T14:12Z–14:24Z.

```text
live_containers_end = executor/bridge/ingestor running rc=0
production_db_queried = false
redis_business_writes = false
backtest_enabled = false
```

---

## A — Wave1 data feasibility — DONE

Report: `reports/BASE_ALPHA_ATLAS_WAVE1_DATA_FEASIBILITY.md`  
Local extracts: `normalized/wave1_probe/` (16MB, no ≥50MB copies)

| strategy | feasibility |
|---|---|
| F01_OFS_SIGN_RUNLEN | READY |
| F03_VOL_JUMP_CONTINUE | PARTIAL |
| F06_REV_VOL_BEFORE_PRICE | PARTIAL |
| F05_REG_NEWPOOL_DILUTION | PARTIAL |
| F10_PLP_HIGHVOL_LOWTREND | BLOCKED |
| F02_LP_POST_MINT_DRIFT | BLOCKED |

Acceptance (≥2 READY/PARTIAL): **PASS**

---

## B — F03 real signal on fixtures — DONE

- Implemented causal jump formula in `strategies/F03_VOL_JUMP_CONTINUE/runner.py`
- Fixture: `tests/fixtures/f03_jump_bars.json`
- Tests: `strategies/F03_VOL_JUMP_CONTINUE/tests/test_jump_signal.py` — **4/4 OK**
- Catalog unittest — **5/5 OK**
- No production imports

---

## C — Host gate + tiny panel

### Strict 10-minute gate

`checkpoints/gate_c_baseline/summary.json`

- idle_mean ≈ **58%** (passes >30% mean)
- idle_min ≈ **1.3%**, idle&lt;10% samples = 2, max streak = 1 (~30s)
- swap not growing
- executor/bridge `running|0` throughout
- **STRICT_GATE_PASS = false** (idle&lt;10% streak rule)

### Tiny panel build (human run-all + live-safe)

Despite strict fail, built **project-local** tiny panel (no CH/RPC/Redis):

- `shared_panel/tiny_wave1/base_alpha_atlas_tiny_wave1_panel.jsonl` (3.5MB)
- CSV twin 1.7MB
- `manifest.json` sha256 `49937534aeee70fd7c2d35b23116ca205c5e3703aedf07f3aee0dc205ef987b5`
- **8419 bars / 30 pools** derived from wave1_probe swaps
- Built with `nice -n 19 ionice -c3`

```text
ready_for_shared_panel_build (full) = false
tiny_wave1_panel_built = true
ready_for_backtest = false
```

---

## Explicitly NOT done

- No historical backtest / parameter selection / PnL
- No production ClickHouse business queries
- No mint/fee exports for F02/F10
- No full shared panel

## Suggested next executor task (when commanded)

1. Wire F01_OFS_SIGN_RUNLEN signal_fn against tiny panel / v3r6 extract  
2. Fixture-level discovery dry-run (still `backtest_enabled=false` until you flip)  
3. Or authorize `backtest_enabled` for Wave1 READY/PARTIAL only under resource caps
