---
sop_applied: true
alpha_id: AR-B3-BASE-2MIN-ACCEL
family: F1/F6/F7
next_gate: HUMAN_PREREGISTRATION_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
---

# B3 Base 2-minute acceleration falsification: frozen preregistration v1

**Status: `FROZEN_APPROVED_FOR_V1` after Attachment II corrections. Research-only, no GO.** This document freezes the B3 design before the authorized bounded Transfer backfill and return backtest. No order, signature, broadcast, live/executor/strategy restart, live configuration change, labeling, training, or model promotion is authorized.

Any change to a number or rule below requires a new preregistration version, an explicit diff, and human approval before backfill/backtest. Results from a later run may not be used to edit v1.

## 1. Locked universe and RPC boundary

- Chain: Base, `chain_id=8453`.
- Universe: `discovered_pools FINAL`, `has_weth_leg=1`, and an exact `block_ts_map(created_block)` row.
- Locked time range: `[2026-02-27 05:41:15 UTC, 2026-06-08 18:29:40 UTC)`.
- Locked size before fresh-token classification: **3,331 pools / 3,330 target tokens**.
- Do not expand `block_ts_map`, the time range, or the 51,225-token full inventory.
- Transfer budget: approximately **9,992 `eth_getLogs` calls**, excluding retries/provider-mandated splits. An optional one-time probe is capped at 500 calls and requires a separate go-ahead before use.
- No token-genesis binary search and no unbounded deployment scan.
- Fetch target-token logs once over approximately one day before `created_block` through the 5-minute snapshot. Fetch WETH pool-in and pool-out logs once each over pool creation through the 5-minute snapshot. The 2-minute main test and 5-minute control reuse those responses.

### Fresh token versus old-token/new-pool

Use a bounded lookback of **43,200 Base blocks** (approximately one day at two seconds/block), floored at block zero.

Classify `old_token_new_pool=1` if either condition is true:

1. the same target token has an earlier locally discovered pool whose creation precedes this pool by more than **10,800 blocks** (approximately 6 hours); or
2. a target-token Transfer exists in the bounded lookback before `created_block - 10,800`.

Classify `fresh_token=1` only when neither old-token condition is true and the RPC range completed without truncation/error. A pre-mint or airdrop first seen within 6 hours of pool creation remains fresh but its `premint_address_count` and `premint_amount_raw` are reported. Provider truncation/error produces `freshness_unknown`, which is excluded from the main universe and reported separately. Old-token/new-pool and freshness-unknown pools never enter the main PASS gate.

This is a bounded detector: an old but dormant token can evade it. That limitation is reported and tested in golden cases; it is not repaired with a genesis scan.

## 2. Frozen non-overlapping windows and external regimes

Window boundaries are calendar/exogenous boundaries chosen before Transfer or return data are available:

| Window | UTC interval, end exclusive | B3 pools | External regime |
|---|---|---:|---|
| W1 | 2026-02-27 05:41:15 → 2026-04-01 00:00:00 | 407 | ETH sideways; pool creation and young-pool turnover collapse |
| W2 | 2026-04-01 00:00:00 → 2026-05-01 00:00:00 | 1,547 | ETH sideways; pool supply expansion; low-level/neutral young turnover |
| W3 | 2026-05-01 00:00:00 → 2026-06-08 18:29:40 | 1,377 | ETH bear; pool supply expansion; young turnover expansion |

The intervals are half-open and have empty pairwise intersections. A regression test must assert the intersections are exactly empty.

### 2.1 Exogenous metric definitions

1. **ETH trend:** Coinbase Exchange public `ETH-USD` daily candles. Boundary price is the arithmetic mean of daily closes for the first and last three complete UTC dates. `ETH_return=end/start-1`. Bull is `>= +10%`; bear is `<= -10%`; otherwise sideways.
2. **Base pool creation rate:** all `discovered_pools FINAL` rows whose exact created block is present in the already locked map, divided by calendar days. Within-window expansion is second-half rate / first-half rate `>=1.25`; contraction is `<=0.80`; otherwise neutral.
3. **Young-pool turnover:** WETH amount in real swaps during each locked WETH pool's first 24 hours, summed by creation cohort and divided by calendar days. Expansion/contraction use the same 1.25/0.80 ratios. Total and top-one-pool share are both reported so concentration cannot masquerade as broad market activity.

These metrics do not use B3 returns, true reserve acceleration, true holder acceleration, or post-entry outcomes. Coinbase documents a maximum of 300 candles per request; this request contains 103 daily rows. Exact boundary candles and the retrieval hash are frozen in `mvp/diagnostics/base_2min_accel_b3_regime_fixture_20260714.json`.

### 2.2 Observed external metric values

| Window | ETH start→end | ETH return | all mapped pools/day | pool 2nd/1st | young WETH/day | young volume 2nd/1st | top-one volume share |
|---|---:|---:|---:|---:|---:|---:|---:|
| W1 | $1,944.67 → $2,037.06 | +4.75% | 12.423 | 0.0049 | 520.699 | 0.0008 | 8.50% |
| W2 | $2,083.50 → $2,265.98 | +8.76% | 56.800 | 4.9373 | 11.769 | 1.1465 | 46.58% |
| W3 | $2,311.64 → $1,613.82 | -30.19% | 46.891 | 1.8949 | 24.550 | 2.9487 | 24.87% |

The locked sample is **not a single regime**. It contains two different sideways-ETH launch regimes and one clear ETH-bear regime. W1's high turnover is concentrated in its first half; W2 has a large pool-supply wave but almost no 2-minute acceleration opportunity; W3 combines a broad ETH drawdown with recovering launch activity.

The local WETH/USDC decoded series independently gives W1 `+6.22%` and W2 `+8.85%`, but has no data from 2026-05-27 through the W3 end. It is a cross-check only; Coinbase is the frozen primary ETH regime source because it covers all three boundaries.

## 3. Frozen feature definitions and thresholds

All event inputs must have `created_block <= block_number <= snapshot_block(T)`. `snapshot_block(T)` is the greatest mapped block whose real `block_time <= created_time+T`. There is no `received_at_ms` or arrival-time fallback.

### 3.1 Main T=2 minutes

- `volume_1`: normalized WETH amount in `[created_block, snapshot_block(60s)]`.
- `volume_2`: normalized WETH amount in `(snapshot_block(60s), snapshot_block(120s)]`.
- **Total-volume gate:** `volume_1 + volume_2 >= 0.01 WETH`.
- **Volume acceleration:** require `volume_1 > 0`; pass when `volume_2 / volume_1 >= 2.0`.
- `reserve_60`, `reserve_120`: WETH transfer-net into/out of the pool from `created_block` through the respective snapshots.
- **Reserve acceleration:** require `reserve_60 > 0`; pass when `reserve_120 / reserve_60 >= 2.0`.
- Holder balances at pool creation include the bounded pre-mint lookback and apply all exclusions in `base_attribution_exclusions.json`, plus pool, token, dead address, WETH/stables.
- `new_holders_1`: addresses with balance `<=0` at creation and `>0` at 60 seconds.
- `new_holders_2`: addresses with balance `<=0` at 60 seconds and `>0` at 120 seconds. An address that received and fully exited before the boundary is not a holder.
- **Holder acceleration:** require `new_holders_1 > 0`; pass when `new_holders_2 / new_holders_1 >= 2.0`.
- The main real signal requires the volume gate and all three acceleration conditions. `holders_all` is primary; `holders_eoa_only` is sensitivity-only and cannot replace it after results are seen.
- The 5-minute run is a control, not a replacement main test. It compares `[created_block, snapshot_block(150s)]` with `(snapshot_block(150s), snapshot_block(300s)]`; volume, reserve, and newly-positive-holder ratios use the same `>=2.0` thresholds and the same 0.01 WETH total-volume gate.

The 0.01 WETH gate is an economic capacity gate for a fixed **0.001 WETH research notional**: aggregate early volume must be at least ten times notional. Entry must additionally fit inside the selected real swap's own WETH amount.

### 3.2 Planning-only hit-count estimate

No Transfer feature has been computed. The capacity estimate therefore reports two non-return quantities:

- `pretransfer upper bound`: pools passing the 0.01 WETH gate and true swap-volume acceleration;
- `planning proxy`: additionally requires distinct real-swap recipients in minute two / minute one `>=2`.

The planning proxy is **not** the RH transfer-recipient proxy and is forbidden from entering the backtest feature table. It is only a workload estimate.

| Window | Pools | Volume gate | Volume acceleration | Pre-Transfer upper bound | Planning proxy estimate |
|---|---:|---:|---:|---:|---:|
| W1 | 407 | 155 | 20 | 18 | **17** |
| W2 | 1,547 | 0 | 0 | 0 | **0 (`NO_OPPORTUNITY`)** |
| W3 | 1,377 | 413 | 185 | 162 | **57** |

W1 is already below 20 at the pre-Transfer upper bound; W2 cannot produce a main signal because it has no qualifying 2-minute volume acceleration; W3 is the only window expected to have `n>=20`. This is frozen data reality. The volume gate will not be lowered and windows will not be moved to manufacture density.

## 4. Frozen entry, exit, and cost rules

- Signal time: `created_time + 120s`.
- **Entry latency delta: 120 seconds**, chosen by rounding the measured ingest p95 of approximately 99 seconds upward. Entry eligibility begins at `created_time + 240s`; delta zero is forbidden.
- Entry price: the next real swap after eligibility, never QuoterV2, tick, sqrt-price midpoint, or a synthetic quote.
- Entry-fill deadline: 30 minutes after eligibility. No qualifying swap means unfilled. Primary intention-to-treat reporting retains it with realized return zero; filled-only results are secondary.
- Fixed research notional: 0.001 WETH. The entry swap's WETH amount must be at least this size; otherwise the opportunity is unfilled rather than silently resized.
- **TP ladder:** +30%, +60%, +120%, one third each, active for 60 minutes after entry. A real sell-direction swap at or through a target fills at most one rung. Multiple rungs may not be paper-filled from one swap.
- At `entry+60m`, unsold remainder seeks the first real sell-direction swap through `entry+120m`. If none exists by that terminal cutoff, or sellability/reserve collapses, the remaining fraction realizes -100%.
- The uncapped variant has no TP fills, holds to `entry+60m`, and uses the identical terminal sell window and -100% rule.
- `ROUND_TRIP_COST=0.006` is removed. It materially understates this tiny position's cost.
- Primary gross return is formed only from the selected real swaps' normalized token/WETH amount ratios. Their average execution prices already embed the venue fee and that real swap's pool slippage; QuoterV2 and midpoint prices remain forbidden.
- Absolute gas is **USD 0.075 per attempted transaction leg**. A one-buy/one-sell round trip costs USD 0.15; ladder exits add USD 0.075 per extra attempted exit leg. Failed sell attempts still incur gas. Gas-return drag is `gas_usd / (0.001 WETH * Coinbase ETH-USD daily close at entry UTC date)` and is subtracted from opportunity return. Across the locked ETH-price range, a standard USD 0.15 round trip is roughly 6.5%--9.3% of notional, before any additional exit legs.
- For audit decomposition, V3 records the selected swap's average execution price versus its same-row post-swap marginal price from `sqrt_price_x96`; V2 records average execution-price movement versus the nearest prior real swap. This observed slippage is reported, but is not subtracted a second time because the primary entry/exit prices already use the real average execution amounts.
- Cost-adjusted return equals real-swap gross realized return minus absolute gas drag. No row with a failed sell is deleted.
- The conditional group, all-age-2m baseline, and volume-gate-only baseline use identical entry, fill, exit, and cost rules.

## 5. Frozen right-tail decision gate

All means below are arithmetic means of opportunity-level cost-adjusted realized returns, including unfilled entries at zero and filled but unsellable remainder at -100%.

### 5.1 PASS_RESEARCH

`PASS_RESEARCH` requires **all** conditions below. It is a research verdict only and is not GO/live/training authorization.

1. **Pooled edge:** real-signal pooled mean `>0` and pooled mean minus the same-window-weighted unconditional baseline mean `>0`.
2. **Cross-regime non-collapse:** at least two distinct-regime windows have `n>=20`; in every such evaluable window, real-signal mean is `>=-10%` and is `>=` that window's unconditional baseline mean. At least two evaluable regimes must also have both mean `>0` and alpha delta `>0`.
3. **Broad right tail:** `top1pct_positive_contribution <=15%`, where the numerator is positive PnL from the best `ceil(0.01*n)` opportunities and the denominator is total positive PnL. After removing those opportunities, pooled mean and pooled alpha delta must both remain `>0`.
4. **Monte Carlo:** 10,000 stratified bootstrap paths, seed `20260714`, resampling with replacement inside each window while preserving each window's trade count and chronological window order. Start equity is 100R and each filled trade risks fixed 1R; unfilled opportunities add 0R. Terminal-equity p5 must be `>100R`, and maximum-drawdown p95 must be `<=20R`.

Given the frozen pre-Transfer bounds, W1 cannot reach `n>=20` and W2 is `NO_OPPORTUNITY`; therefore v1 cannot earn a clean cross-regime PASS unless the inventory estimate is proven wrong by a reproducible bug fix. This limitation is not a reason to change thresholds after backfill.

### 5.2 NEEDS_REVIEW

Verdict priority is frozen as **sample adequacy before merit**. For each window, first compute `n`. A window with `n<20` is untested and can only be `INCONCLUSIVE/NEEDS_REVIEW`; its observed mean, alpha delta, concentration, or Monte Carlo result may be reported descriptively but may not trigger FAIL.

Return `NEEDS_REVIEW`, never PASS or FAIL, for an underpowered scope if any of these applies:

- fewer than two distinct-regime windows have `n>=20`;
- any window has `1<=n<20`; `n=1..4` receives no directional conclusion; `n=0` is `NO_OPPORTUNITY` rather than a zero return;
- real signal otherwise passes but real/proxy hit sets have Jaccard `<0.50`, hit counts differ by `>=50%`, or pooled means differ by `>=10` percentage points;
- any standard-token golden integer equality fails, any nonstandard-token mismatch is unquantified, or any RPC range is incomplete;
- `holders_all` versus `holders_eoa_only` hit counts differ by `>=25%` in any window;
- freshness is unknown for `>=5%` of the locked universe.

### 5.3 FAIL

Evaluate FAIL rules only on windows with `n>=20`. In v1, W3 is the preregistered powered single-window probe. Return `FAIL` only if at least one of these powered-scope falsification conditions occurs:

- a powered window's real-signal cost-adjusted mean is `<=0`;
- a powered window's real-signal alpha delta versus its unconditional baseline is `<=0`;
- specifically, W3 has `n>=20` and either its mean or alpha delta is non-positive (`W3_COLLAPSE`);
- a powered window's top-1%-removed mean or alpha delta is non-positive, showing that its apparent positive mean was entirely tail-supported;
- any feature uses a block after its snapshot, arrival-time fallback, a volume/liquidity proxy in the real version, or distinct recipients in place of true positive balances.

Top-1% contribution above 15% or a powered-window Monte Carlo miss is `NEEDS_REVIEW` unless the corresponding powered-window mean/delta is non-positive under the rules above. Small/zero opportunity counts always outrank observed return signs: they cause NEEDS_REVIEW/INCONCLUSIVE, never FAIL. They prevent PASS and prevent any claim of cross-regime stability.

## 6. Reproduction anchors and pre-backfill stop

- Frozen metric fixture: `mvp/diagnostics/base_2min_accel_b3_regime_fixture_20260714.json`.
- Schema authority: `infra/clickhouse/init/003_normalized_events.sql`, `013_decoded_views.sql`, `026_discovered_pools.sql`, `035_block_ts_map.sql`.
- B2 reusable implementation: `scripts/wallet_profile_v0/base_transfer_net_attribution.py::fetch_target_transfers_logs` and research tables in `042_base_cha_wallet_swaps.sql`.
- ETH source retrieval:

```bash
curl -fsS --get 'https://api.exchange.coinbase.com/products/ETH-USD/candles' \
  --data-urlencode 'start=2026-02-27T00:00:00Z' \
  --data-urlencode 'end=2026-06-09T00:00:00Z' \
  --data-urlencode 'granularity=86400'
```

No Transfer backfill, golden RPC, feature build, entry/exit matching, return calculation, bootstrap, or backtest was run before freezing these Attachment II corrections. The human instruction following Attachment II authorizes the bounded v1 run under this corrected version only.
