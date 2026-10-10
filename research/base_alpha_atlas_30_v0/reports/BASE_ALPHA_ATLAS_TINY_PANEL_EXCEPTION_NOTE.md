# BASE_ALPHA_ATLAS_TINY_PANEL_EXCEPTION_NOTE

UTC note written: 2026-07-18T14:31Z  
Artifact: `shared_panel/tiny_wave1/` (not deleted, not rebuilt this stage)

## Facts

1. **Strict Gate was false** at construction time (`checkpoints/gate_c_baseline/summary.json`):
   - `STRICT_GATE_PASS = false`
   - idle_mean ≈ 58.8% (mean rule alone would pass)
   - idle_min ≈ 1.27%; samples with idle &lt; 10% = 2; max consecutive streak = 1 (~30s)
   - Swap used bytes did not increase

2. **Why Tiny Panel was still built**
   - Human ordered A→B→C “全部都跑，不影响实盘”
   - Builder used **only** project-local `normalized/wave1_probe/` (~16MB already extracted)
   - No production ClickHouse business queries, no RPC/WS, no Redis writes
   - Process launched with `nice -n 19 ionice -c3`, single Python process

3. **Inputs**
   - `base_b5_real_swaps_head50k.tsv`
   - `run004_v3r6_candidate_pool_head20k.csv`
   - (OHLCV smoke files present in probe dir but not required for this panel)

4. **Outputs**
   - `base_alpha_atlas_tiny_wave1_panel.jsonl` ≈ 3.5 MiB
   - `base_alpha_atlas_tiny_wave1_panel.csv` ≈ 1.7 MiB
   - `manifest.json` sha256 `49937534aeee70fd7c2d35b23116ca205c5e3703aedf07f3aee0dc205ef987b5`
   - n_bars = 8419, n_pools = 30

5. **Runtime / resource envelope (as run)**
   - Wall time: ~2s class
   - Threads: 1
   - nice=19, ionice=idle
   - No resident service started

6. **Production containers**
   - Before/after ABC and at Stage1A start: `executor` / `signal-bridge` / `ingestor` = `running`, RestartCount=`0`
   - Redis `signals` pending = `0` at Stage1A start

## Binding interpretation

```text
THIS EXCEPTION DOES NOT AUTHORIZE:
  - full shared panel builds when Strict Gate fails
  - tiny/full backtests when ready_for_tiny_backtest=false
  - production database access
  - skipping future health gates
```

Tiny Panel remains an **engineering / early Discovery fixture** only. Stage 1A will not rebuild it.
、