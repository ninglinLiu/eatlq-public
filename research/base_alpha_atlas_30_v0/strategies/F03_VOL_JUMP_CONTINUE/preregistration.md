# F03_VOL_JUMP_CONTINUE — Preregistration (Stage 1A freeze)

```text
STAGE = SIGNAL_LOGIC_FROZEN
status_label = FIXTURE_VALIDATED_SIGNAL_LOGIC
NOT_REAL_MARKET_ALPHA
parameter_selection = FORBIDDEN
pnl_inspection = FORBIDDEN
```

## Identity

Jump continuation / fade arms on **closed 1m bars**.

## Price / return

```text
px_t = bar.close at bar_close_time
r_t = log(px_t) - log(px_{t-1})   # close-to-close log return
```

High-low range is **not** used for the jump statistic in this freeze.

## Realized volatility baseline

```text
rv_l_{t-1} = sqrt( sum_{j=t-l}^{t-1} r_j^2 )
```

- Uses only returns with index &lt; t (end_exclusive = t)
- `l` ∈ {60, 120} search space (not selected)
- Require at least `max(2, l//4)` finite returns in window else UNKNOWN (skip)

## Jump definition

```text
jump_ratio_t = |r_t| / rv_l_{t-1}
jump_flag_t = jump_ratio_t >= k
jump_sign_t = sign(r_t)
```

`k` ∈ {3.0, 4.0, 5.0} search space (not selected).

## Bar close / visibility

| Item | Rule |
|---|---|
| Bar interval | 60s buckets from panel builder |
| bar_close_time | end of bucket (minute+60) |
| close known at | `feature_available_at = bar_close_time` |
| Signal earliest visible | at `feature_available_at` of bar t — **never** before bar close |
| Incomplete bar | forbidden as input; runner assumes provided bars are closed |

## Minimum history

```text
min_bars_total >= l + 2
```

before any signal may fire.

## Arms

- `arm=0`: BUY if jump_sign=+1 (continuation)
- `arm=1`: BUY if jump_sign=-1 (fade / buy dip)

## Anti-repeat

After emitting a signal at bar t, suppress new signals until
`jump_cooldown_bars` subsequent bars elapse (candidate {3,5,10}; Stage1A default 5).

## Missing bars

- No imputation
- If `close` missing or ≤0 → skip that bar (break return chain locally; do not fill 0 price)
- Gaps: returns computed only between consecutive provided closed bars (gap may inflate |r|; documented limitation)

## Parameter search space (not selected)

```json
{
  "k": [3.0, 4.0, 5.0],
  "l": [60, 120],
  "arm": [0, 1],
  "jump_cooldown_bars": [3, 5, 10],
  "hold": [3, 6, 12]
}
```

## Stage1A status

Fixture tests validate formula mechanics only → `FIXTURE_VALIDATED_SIGNAL_LOGIC`.
