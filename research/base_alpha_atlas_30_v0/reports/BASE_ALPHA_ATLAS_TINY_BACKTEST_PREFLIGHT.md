# BASE_ALPHA_ATLAS — Tiny Backtest Preflight (Frozen Spec Only)

UTC: 2026-07-18  
**Status: PREFLIGHT ONLY — not executed.**

```text
ready_for_tiny_backtest = false
tiny_panel ≠ Paper authorization
tiny_panel ≠ Live authorization
tiny_panel ≠ final Holdout
```

## Purpose of tiny_panel

Engineering + early Discovery plumbing only (signal shape, join keys, cost hooks).  
Passing a tiny backtest later still **cannot** authorize Paper or Live.

## Frozen plan (when health gates later allow)

### Data universe

- Bars: `shared_panel/tiny_wave1/` (30 pools, 8419 bars) for bar strategies (F03).
- Swaps: `normalized/wave1_probe/` v3r6 head for swap strategies (F01).
- No production DB pull in tiny BT unless a future gate explicitly flips `ready_for_production_database_access`.

### Entry

- Signal at `feature_available_at`.
- Intent at observation; earliest fill after delay below.
- Long-only BUY intents for atlas Stage1 arms under test.

### Execution delay

- F03: +1 closed bar (candidate); seconds-scale placeholder in runner for later calibration.
- F01: +1 block or +1 swap (candidates in preregistration; not selected).

### Exit

- Max hold from candidate sets (`hold` bars / `hold_swaps` / time holds).
- Optional stop/tp candidates from strategy cards — selection forbidden until param layer authorized.

### Cost / slippage / failure

Use `cost_fn` placeholders until calibrated:

- gas_usd, protocol/lp fee, slippage, impact, mev
- `revert_prob`, `failed_sell_prob`
- Unknown fields remain UNKNOWN (no silent zero)

### Walk-forward / purge / embargo

- Nested walk-forward on tiny calendar only as smoke.
- Purge: drop training labels overlapping test event windows.
- Embargo ≥ longest hold + delay (F01 interim floor 1830s; F03 ≥ max hold bars + 1).

### Duplicate signals

- Apply strategy cooldown first.
- Engine: at most one open position per (strategy, pool) unless card says otherwise; queue/ignore extras.

### Parameter selection layer

- **Forbidden** while `ready_for_parameter_search=false`.
- Tiny BT may run **single frozen default vectors** only for plumbing, not grid search.

### Untouched fragment

- Reserve final calendar/pool slice as untouched; tiny panel is **not** that holdout.

### Baselines (structure, when BT runs)

- Random same-frequency baseline (match signal count / pool-hour intensity).
- Buy-all / always-long on same universe & hold (cost-aware).

### Stop conditions (tiny BT engineering)

- Peak RSS > 256 MiB or timeout > 120s → abort run.
- Production RestartCount change / signals pending > 0 / swap growth spike → abort research job (do not touch prod).
- Any PnL used to choose parameters → invalidate run.

## Explicit non-claims

| Claim | Allowed? |
|---|---|
| Tiny panel = discovery sandbox | yes (engineering) |
| Tiny panel authorizes Paper | **no** |
| Tiny panel authorizes Live | **no** |
| Tiny panel = final Holdout | **no** |
| Strict Gate bypass via prior exception | **no** (see exception note) |

## Next gate required before execution

`ready_for_tiny_backtest = true` **and** live-safe posture confirmed. This Stage1A file does not flip that flag.
