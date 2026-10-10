# COPY_CASCADE_INVERT r0 — CHARTER

Status: ACTIVE RESEARCH — DEPLOY FORBIDDEN  
Project: `/root/copy_cascade_invert_r0`  
`deploy_allowed` is always `false`.

## Hypothesis

Public GMGN / Debot / Fomo “daily green” wallets are a crowding sensor, not a
copyable teacher. Feasible use of the same addresses is:

- **Reconcile** vendor display PnL against contemporaneous public-index FIFO
  (public Base/BSC HTTPS RPC + Blockscout/equivalent) and against the July
  offline FIFO / goldens. EATLQ archive RPC and ClickHouse scans stay banned.
- **Arm A**: fade a copy cascade after point-in-time fame.
- **Arm B**: follow synchronized *unfamous* new wallets (cohort sync ≠ copy).

## Hard bans

- Follow Debot / Fomo / GMGN copy-trade queues.
- Rank-by 1d/7d/30d PnL or winrate as an entry signal.
- Treat today’s public board as historical fame (lookahead).
- Substitute Dune or internal `smart_money` CSVs for a vendor board.
- Production ClickHouse full-table scans, EATLQ archive/RPC, `eth_call`
  against private nodes, writes to `/opt/EATLQ`, Redis, live signals.
  Public `https://mainnet.base.org` / Blockscout-class index is allowed
  only for contemporaneous reconstruct.
- Trigger-price trailing as primary economic proof.
- Lowering k / coverage gates after seeing results.

## Universe

- Boards: Base + BSC public pages.
- Replay tape: Base offline dumps plus contemporaneous public-index FIFO.
- BSC without a usable public index: `INCONCLUSIVE_BSC_NO_LEDGER`.

## Honest closeout

Missing boards → `SOURCE_BLOCKED` per vendor; all three empty →
`DATA_LINEAGE_INSUFFICIENT` and stop merit claims. Ledger mismatch →
`VENDOR_LEDGER_GAP`. Tax ≥ source edge at reachable delay → same-direction
copy arm permanently closed. `GO` is never self-assigned.
