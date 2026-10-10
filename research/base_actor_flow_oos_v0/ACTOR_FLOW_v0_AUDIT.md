# ACTOR_FLOW v0 — Estimand-Repair Audit (diagnostic)

Generated: `2026-07-20T16:53:53Z`

> Diagnostic only. Does **not** mutate `configs/actor_flow_freeze.json`, scored arms, or `out/oos/scorecard.csv`.

## Corrected verdict table

| Item | Verdict |
| --- | --- |
| Actor resolution | `PASS_PENDING_STRATIFIED_CI_AUDIT` |
| WHALE_EVENT historical absolute return | `BACKTEST_POSITIVE` |
| WHALE vs POOL_ONLY incremental | `INCREMENTAL_EDGE_CONFIRMED_PAIRED` |
| Copy execution constraints | `COPYABILITY_BACKTEST_PASS` |
| Copyable edge | `PROSPECTIVE_CONFIRMATION_REQUIRED` |
| Live deployment | `NO_GO` |
| Observer-only shadow | `ALLOWED_IF_SPEC_FROZEN` |
| Monthly earn $1000 target | `TARGET_NO_GO` |
| Whale label integrity | `KEEP_WHALE_EVENT_LABEL_PENDING` |
| Loss handling | `LOSS_HANDLING_GAPS_FOUND` |
| Overall WHALE_EVENT | `HISTORICAL_COPY_SIM_PASS__INCREMENTAL_EDGE_PAIRED_POSITIVE` |
| Spec decision | `SPEC_SURVIVES_STATISTICS_ONLY__SHADOW_ALLOWED_AFTER_FREEZE` |

## Overall

**`WHALE_EVENT = HISTORICAL_COPY_SIM_PASS__INCREMENTAL_EDGE_PAIRED_POSITIVE`**

**Spec decision:** `SPEC_SURVIVES_STATISTICS_ONLY__SHADOW_ALLOWED_AFTER_FREEZE`

Statistics were mis-stated but the selection definition may still be freezable. Recommended next step: freeze v0 spec + code hash, then start **observer-only** prospective shadow at a fresh T0. **No live trading.**

### Decision-gate caveats (do not ignore)

1. **Paired Δ is positive, but match quality is imperfect.** Covariate-matched treat mean size ≈ 0.34 WETH vs control ≈ 0.17 WETH; `depth_proxy` was 0 for both arms (bins collapsed). Overlay evidence (within POOL_ONLY spine) is the cleaner incremental check.
2. **WHALE is still not identity.** No actor fields enter `whale_*`; `whale_absorbed` (non-reversal) is the main non-size filter (≈35% drop). Jaccard vs `LARGE_BUY_IMPACT` ≈ 0.62; 97.8% of whale selections are also LBI — label stays pending, not “smart money.”
3. **Capital reality:** unconstrained 38.25 WETH vs fixed-$1k wallet ≈ $1,595 withdrawn; **skip_rate ≈ 99%** under `max_concurrent=3`. `TARGET_NO_GO` stands.
4. **Actor-resolution CI:** existing n=25 traces → Wilson 95% upper ≈ 13.3%, not &lt;2%. RPC expansion was skipped (`AUDIT_RUN_RPC=1` to run).
5. **Loss-handling gaps:** recovery floor never applied; 2,836 infra/tier-D SUCCESS rows exist in the panel (selection may still exclude many via arm filters).

## Key corrected numbers

- Legacy invalid arithmetic diff: `12.854891724712182` WETH
- Overlay paired Δ p05 (token cluster): `30.686485768009092`
- Matched paired Δ p05 (token cluster): `35.22129256557226`
- Whale share of POOL_ONLY selected: `0.6198533945331233`
- WHALE unique tokens / trades: `505` / `19684`
- Unconstrained opportunity PnL: `38.250217890793536` WETH
- Fixed-$1000 scheduled cum withdrawn: `$1594.9429649629276`
- pct_months_hit_1000_usd: `0.0` → `TARGET_NO_GO`
- Gate Wilson 95% upper (existing n): `0.13319649395317873`
- Jaccard(WHALE, LARGE_BUY_IMPACT): `0.6187071214610107`
- Size explains effect: `False`

## What was wrong in the scorecard

1. `incremental_vs_pool_only_weth` subtracted totals of two different selection sets.
2. `COPY_EDGE_GO` overstated copyable edge; renamed to `COPYABILITY_BACKTEST_PASS`.
3. `bootstrap_p05` was absolute, not paired Δ.
4. `sampled_error_rate=0` on ~25 traces cannot claim <2% (rule-of-three needs ~150).
5. `total_wallet_pnl_weth` is unconstrained opportunity sum; economic target uses scheduled wallet.

## Artifact index

- `out/audit/WHALE_EVENT_v0_DIAGNOSTIC.json`
- `out/audit/01_incremental.json`
- `out/audit/02_dedup_capital.json`
- `out/audit/03_coverage_ci.json`
- `out/audit/04_resampling.json`
- `out/audit/05_proxy_ablation.json`
- `out/audit/06_loss_handling.json`

## Hard bans still in force

- No live deploy
- No silent upgrade of PROSPECTIVE → INFORMATION_EDGE_GO
- No editing freeze after seeing audit results without declaring v1

