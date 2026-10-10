# F01_OFS_SIGN_RUNLEN — Preregistration (Stage 1A freeze)

```text
STAGE = SIGNAL_LOGIC_FROZEN
parameter_selection = FORBIDDEN
pnl_inspection = FORBIDDEN
backtest = FORBIDDEN
```

## Identity

- **strategy_id**: `F01_OFS_SIGN_RUNLEN`
- **family**: F01 Order-flow sequence & microstructure
- **economic_mechanism**: After an unusually long same-sign swap run with sufficient quote notional, short-horizon continuation is hypothesized versus fade counterparties.
- **counterparty**: LPs and short-horizon fade traders

## Data grain (mandatory)

Signals are computed on **individual swaps**, not bars.

### Sort authority

```text
primary:   block_number ASC
secondary: transaction_index ASC   # if present
tertiary:  log_index ASC
```

On current `wave1_probe` extracts:

- `transaction_index` = **UNKNOWN** (column absent)
- Effective sort: `(block_number, log_index)`
- **Limitation**: same-block swaps may be mis-ordered relative to true tx order; same `log_index` collisions must be treated as data defects (dedupe / skip), not invent order.

### Trade sign

```text
BUY  = +1  # WETH → token (is_buy_weth_to_token True / equivalent)
SELL = -1  # token → WETH
UNKNOWN sign → swap excluded from runs (never coerce to 0)
```

### Quote notional

```text
quote_notional_eth_i = |weth_side_amount_normalized_i|
```

If WETH side amount missing → `quote_notional_eth_i = UNKNOWN` → swap excluded from notional sums and cannot confirm amount gates.

## Feature formulas (as of observation on swap i)

After sorting swaps for one `pool_id` with all swaps having coordinates ≤ observation swap i:

```text
trade_sign_i ∈ {+1,-1}

run_length_i =
  largest k≥1 s.t. trade_sign_{i-k+1..i} all equal

run_notional_eth_i =
  sum(quote_notional_eth_j for j in current run)
  # if any j has UNKNOWN notional → run_notional_eth_i = UNKNOWN

previous_opposite_run_length_i =
  length of the most recent completed run with opposite sign before current run
  # 0 if none (explicit zero only when observed empty history, not for UNKNOWN)

previous_opposite_run_notional_eth_i =
  sum notional of that opposite run, or UNKNOWN/0 per same rules

run_len_mean_prev / run_len_std_prev =
  mean/std of completed run lengths in the trailing L_completed_runs completed runs
  strictly before current run (no leakage of current run)

run_len_z_i =
  (run_length_i - run_len_mean_prev) / (run_len_std_prev + eps)
  # UNKNOWN if fewer than min_completed_runs_for_z completed runs
```

## Universe

- Base Uniswap V3 pools with WETH as one leg (from probe: `token0`/`token1` includes WETH or `weth_leg` present)
- Minimum coverage before any signal: `min_swaps_in_pool` completed swaps with known sign
- Fee-tier unknown pools: allowed in Stage1A sanity; production later may filter

## Timing

| Concept | Definition |
|---|---|
| observation_time | Coordinate of swap i: `(block_number, log_index)` + `timestamp_ms` if present |
| feature_available_at | Same as observation_time of swap i (all features use swaps ≤ i only) |
| entry_time (future BT) | observation_time + execution_delay (candidate: +1 block or +1 swap); **not executed this stage** |

## Entry (signal definition only)

Emit a **BUY** signal (long token) when all hold:

1. `trade_sign_i = +1`
2. Edge trigger: `run_length_i` increased at i and `run_length_i >= min_run_len`
3. Amount confirmation (if enabled): `run_notional_eth_i != UNKNOWN` and `>= min_run_notional_eth`
4. Optional z-gate (if enabled and not UNKNOWN): `run_len_z_i >= z_enter`
5. Cooldown satisfied (see below)

SELL-sign long-only atlas: no short entry this stage (SELL runs recorded in diagnostics only if needed; default signal_fn emits BUY-side only).

## Repeat / cooldown

- **Same pool continuous signals**: forbidden while within cooldown after a signal
- `cooldown_swaps`: number of subsequent swaps (any sign) that must pass before a new signal may fire
- Alternative candidate (not selected): cooldown by seconds — search space listed, not chosen

## Exit / hold candidates (frozen for future BT; unused in Stage1A)

- `hold_swaps` ∈ {10, 30, 60} or time hold ∈ {180s, 600s, 1800s}
- `stop_ret` ∈ {0.08, 0.15}
- `tp_ret` ∈ {0.10, 0.20}
- Stage1A does **not** compute exits or returns

## Parameter search space (not selected)

```json
{
  "min_run_len": [3, 5, 8],
  "min_run_notional_eth": [0.05, 0.3, 1.0],
  "L_completed_runs": [20, 50],
  "z_enter": [null, 1.5, 2.0],
  "cooldown_swaps": [5, 20, 50]
}
```

Notes:

- `z_enter=null` means z-gate off (length+notional only)
- Combo count with null treated as one level: 3×3×2×3×3 = 162 ≤ 512
- Amount confirmation always on when notional known; if UNKNOWN → no signal (fail-closed)

## Embargo (for future nested WF)

Longest lookahead/hold window that purge/embargo must cover:

```text
max_hold_seconds_candidate = 1800
+ max_execution_delay_seconds_candidate = 30
+ feature_lookback_swaps converted via p99 inter-arrival (UNKNOWN until measured)
embargo_seconds >= 1830 as interim floor; refine after inter-arrival stats
```

## Failure handling

| Case | Action |
|---|---|
| UNKNOWN sign | exclude swap; do not break prior run with fake 0 |
| UNKNOWN notional | cannot satisfy amount gate; no signal |
| duplicate (block,log) | keep first; mark duplicate |
| missing log_index/block | drop row |
| shuffled input | re-sort internally before compute |

## Falsification (future)

Shuffle trade signs within pool preserving timestamps; edge must vanish. Top-run dependence check later — not Stage1A.
