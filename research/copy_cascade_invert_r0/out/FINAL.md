# COPY_CASCADE_INVERT r0 — FINAL

**GO = false**. verdict = `VENDOR_LEDGER_GAP`

generated_at: 2026-09-17T08:18:32+00:00

## What was collected

- vendor source status: `{'debot': 'CAPTURED_WRONG_VENUE', 'fomo': 'SOURCE_BLOCKED_AUTH', 'gmgn': 'CAPTURED'}`
- board lineage: `OK`
- vendor rows / unique addresses: 3255 / 2183
- unique by source: {'debot': 1338, 'gmgn': 846}
- venue counts: {'base_bsc_spot': 1755, 'hyperliquid_perp': 1500}
- spot-reconcilable addresses: 846
- offline files copied: 15
- RPC used: True (public_https_only)
- EATLQ archive used: False

## Live reconstruct (public index, through today)

- Base status: `VENDOR_LEDGER_GAP` n_ok=378 both7=378 sign_agree_7d=0.8597883597883598 active=0.49504950495049505 pearson=-0.34828883304419894 head=51419202
- BSC status: `VENDOR_LEDGER_GAP` n_ok=532 both7=532 sign_agree_7d=0.34398496240601506 active=0.10963455149501661

## Debot board vs Hyperliquid own ledger (different venue)

- status: `RECONCILED_WITH_GAPS` venue=`hyperliquid_perp`
- clean rows: 683 sign_agree=0.986822840409956 within10%=0.7635829662261381 median_ratio=0.9999999999999993

## Offline tape reconcile (July FIFO, not contemporaneous)

- status: `VENDOR_LEDGER_GAP`
- both (vendor ∩ our FIFO): 0
- tape coverage: 0.0
- sign agree/disagree: 0 / 0

## Delay tax

- subset: `vendor_overlap`
- same-direction copy arm: `INCONCLUSIVE_SMALL_N`

## Arms

- Arm A passed: False (n=281, mean=-0.010480924952352621)
- Arm B passed: False (n=192, mean=1.2068945705338476, first_seen=FIRST_SEEN_WALLET_AGE_DAYS)

## Reasons

- July offline FIFO ∩ today's board is empty or mismatched (old tape)
- live Base GMGN vs public-index FIFO: VENDOR_LEDGER_GAP sign7=0.8597883597883598
- live BSC: VENDOR_LEDGER_GAP
- Debot open board is Hyperliquid perp, not Base/BSC spot; its spot smart-money board is SOURCE_BLOCKED_AUTH
- Debot vs Hyperliquid own ledger: RECONCILED_WITH_GAPS sign_clean=0.986822840409956 within10%=0.7635829662261381
- Fomo prod-api requires a Privy session; no anonymous board exists
- vendor∩tape delay-tax n below powered gate (do not close on n<20)
- atomic sell-sim skipped (INCONCLUSIVE_NO_RPC)
- Arm A did not pass preregistered gates
- Arm B did not pass preregistered gates
- BSC used GMGN activity hashes + public RPC receipts (no free Blockscout index)
- prospective window after captured_at is empty on this tape

Dune is overlay only and was not renamed to GMGN/Debot/Fomo.
Today's public board is fame_now, not historical fame.

