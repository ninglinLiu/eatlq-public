# F03 PIT and Formula Audit

UTC: 2026-07-18  
Strategy: `F03_VOL_JUMP_CONTINUE`  
Status label: **`FIXTURE_VALIDATED_SIGNAL_LOGIC`** (fixture/engine validation only — not real-market alpha)

## Verdict

Runner aligned to frozen preregistration after Stage1A audit:

- Jump uses **close-to-close log return** vs **prior-window realized vol** (not high-low).
- Features visible only at **closed bar** `feature_available_at`.
- Missing/invalid closes → **NaN**, never coerced to 0.
- Repeat jumps suppressed by `jump_cooldown_bars`.

## Frozen definitions

| Item | Freeze |
|---|---|
| Return | `r_t = log(close_t) - log(close_{t-1})` |
| Baseline RV | `rv_l_{t-1} = sqrt(sum r_j^2)` over finite returns in `[t-l, t)` |
| Jump ratio | `jump_ratio_t = \|r_t\| / rv_l_{t-1}` |
| Jump flag | `jump_ratio_t >= k` |
| High-low / Parkinson | **not used** |
| Min history | `len(pool_bars) >= l + 2` and ≥ `max(2, l//4)` finite returns in RV window |
| Bar close | panel 60s bucket end; `event_time = bar_close_time` |
| Earliest visibility | `feature_available_at` of bar t (= close); incomplete bars forbidden |
| Incomplete current bar | rejected if `feature_available_at < event_time` |
| Missing bar / close | no imputation; invalid close → NaN return; skip signal at that t |
| Anti-repeat | after signal at bar index t, suppress until `t - last_sig_t > jump_cooldown_bars` |

## Arms

- `arm=0`: BUY on positive jump (continuation)
- `arm=1`: BUY on negative jump (fade)

## Parameter space (not selected)

`k ∈ {3,4,5}`, `l ∈ {60,120}`, `arm ∈ {0,1}`, `jump_cooldown_bars ∈ {3,5,10}`, `hold ∈ {3,6,12}`

Stage1A sanity used `l=30` only because tiny panel depth is short; search space remains `{60,120}`.

## PIT checks performed

1. Fixture: RV window end_exclusive = t (excludes r_t from baseline).
2. Fixture: future bars do not alter past signal timestamps.
3. Fixture: cooldown reduces re-triggers.
4. Regression: missing `close` does not zero-fill returns.
5. Unit label in tests: `FIXTURE_VALIDATED_SIGNAL_LOGIC` only.

## Known limitations

- Gaps between consecutive provided bars can inflate `|r_t|` (no calendar gap fill).
- Tiny-panel sanity is engineering coverage, not alpha evidence.
- No PnL / outcome inspected this stage.
