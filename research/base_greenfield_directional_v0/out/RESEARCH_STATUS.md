# Greenfield research status

- Panel: `out/panel/greenfield_panel_15-60m.parquet` (2399 rows, 764 pools, PIT UniV3 WETH)
- Label freeze: H=3600s, barrier=TB_A (+50%/-35%) — `configs/design_freeze.json`
- OOF scorecard: `out/metrics/oof_scorecard.csv`
- Final: `out/FINAL_GREENFIELD.json`
- Deploy: forbidden; SEALED_HOLDOUT unused for tuning

Primary candidate: `G2_HGB_COMPETING` / `DISCOVERY_LEAD`  
Beats `C1_PRICE_ONLY` OOF EV; does **not** beat `C3_SIMPLE_MOMENTUM`; top5 concentration high; rank_ic undefined (mostly singleton decision slices).