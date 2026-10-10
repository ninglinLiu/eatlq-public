# BASE_ALPHA_ATLAS_PANEL_BUILD_PREFLIGHT

```text
ready_for_shared_panel_build = false
ready_for_large_file_copy = false
ready_for_production_database_access = false
```

## Preflight checklist (all must be true later)

1. `SAFE_TO_BEGIN` style host gate passes without idle<10% streaks
2. ClickHouse not in multi-core research contention (live bridge load acknowledged)
3. Offline sources for Wave1 columns identified with sha256 after **small** header-only probes
4. Builder runs nice=19 ionice-idle, threads=1, mem<=384MB, timeout<=20m, checkpointable
5. Output only under `/root/base_alpha_atlas_30_v0/shared_panel/`
6. No strategy runner reads raw production tables

## Current blockers

- Phase -1–0 resource pause still valid for heavy IO
- No verified offline parquet panel exists
- Several Wave2/Parking fields require mints/wallet/fee tapes

## Allowed now

- This design document
- Synthetic fixtures for interface tests

## Forbidden now

- Building `base_alpha_atlas_shared_panel.parquet`
- Copying 100MB+ diagnostics into `normalized/`
- Querying `eatlq.uniswap_v3_swaps_decoded` or related business tables
