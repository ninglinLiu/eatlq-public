# BASE_ALPHA_ATLAS_ORTHOGONALITY_DESIGN_AUDIT

Mechanism-level audit (Phase 0.5). No trade-overlap statistics computed (no data).

Exact triple collisions (mechanism+entry+exit): 0
REPLACE_REQUIRED flags: 0

## Pairwise focused checks

| A | B | identical_mechanism | identical_entry | identical_exit | verdict | note |
| A | B | identical_mechanism | identical_entry | identical_exit | verdict | note |
|---|---|---|---|---|---|---|
| F02_LP_POST_MINT_DRIFT | F06_REV_LIQ_REINJECT | False | False | False | DISTINCT_OK | mint-related but silence+resume joint vs mint-only |
| F02_LP_POST_MINT_DRIFT | F08_EVT_MINT_BURST | False | False | False | DISTINCT_OK | single mint z vs multi-owner burst |
| F01_OFS_SIGN_RUNLEN | F01_OFS_INTERARRIVAL | False | False | False | DISTINCT_OK | run length vs inter-arrival compression |
| F03_VOL_COMPRESS_EXPAND | F03_VOL_JUMP_CONTINUE | False | False | True | DISTINCT_OK | compress-expand vs jump |
| F04_XS_SAME_AGE_RS | F08_EVT_POOL_AGE_RULE | False | False | False | DISTINCT_OK | RS vs deterministic age+impact rule |
| F10_PLP_HIGHVOL_LOWTREND | F10_PLP_EVENT_WINDOW | False | False | False | DISTINCT_OK | screen LP vs event-scheduled LP |
| F06_REV_SILENCE_FIRST_TRADE | F06_REV_VOL_BEFORE_PRICE | False | False | False | DISTINCT_OK | first trade vs vol-before-price |

## Family internal distinctness

Each family contains 3 strategies with distinct primary mechanism axes:

1. F01: run-length / size-sequence / inter-arrival
2. F02: post-mint / post-burn / depth-lead
3. F03: compress-expand / jump / failed-break fade
4. F04: leader→second / same-age RS / old→new migrate
5. F05: breadth / dilution avoidance / gas congestion
6. F06: silence first trade / vol-before-price / liq re-inject
7. F07: sync new wallets / early-holder release / HHI decline
8. F08: age rule contrast / mint burst / fee-tier migration
9. F09: impact asymmetry / notional curve / stale quote
10. F10: fee vs IL / highvol-lowtrend LP / event-window LP

## Live Alpha orthogonality

PROTECTED_LIVE_ALPHA (KEYSTONE): young-pool window + CH features + ML `p_fatal` → Redis signals.
Atlas strategies that share young-pool timing (esp. `F04_XS_SAME_AGE_RS`, `F08_EVT_POOL_AGE_RULE`) are marked high live_conflict_risk and excluded from Wave 1 even if statistically interesting later.

## Verdict

`ORTHOGONALITY_DESIGN_PASS`

