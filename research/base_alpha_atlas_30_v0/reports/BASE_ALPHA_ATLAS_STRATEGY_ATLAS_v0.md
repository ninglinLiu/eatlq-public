# BASE_ALPHA_ATLAS_STRATEGY_ATLAS_v0

Phase 0.5 design atlas. No parameters selected. No historical PnL inspected.

Total strategies: 30

## F01 — Order-flow sequence & microstructure

### F01_OFS_SIGN_RUNLEN — Signed-trade run-length persistence

- **economic_mechanism**: Continuation after unusually long same-sign trade runs; counterparty is short-horizon mean-reverters fading runs.
- **counterparty**: liquidity_providers_and_fade_traders
- **universe**: Base UniswapV3 WETH pairs with >= N swaps in lookback; exclude fee-tier unknowns.
- **observation_time**: swap event time t (block_time); decision on bar close after run completes
- **feature_available_at**: feature_available_at = max(swap.block_time of last trade in run, bar_close_time); no future swaps
- **entry**: BUY token if run_sign=+1 and run_len_z>=z_enter and run_notional_eth>=min; SELL/short-token proxy = flatten-only (long-only atlas default: enter long only on +1 runs)
- **execution_delay**: 1 bar (or +1 block if event-time mode)
- **position_size**: fixed_usd in {50,100} search later; design default notional_usd grid {50,100}
- **exit**: time exit at hold_bars OR stop OR take_profit
- **stop**: close if ret_from_entry <= -stop_ret
- **take_profit**: close if ret_from_entry >= tp_ret where tp_ret search {0.10,0.20}
- **maximum_hold**: hold_bars
- **repeat_entry**: no re-entry while position open; after exit wait cooldown_bars
- **cooldown**: cooldown_bars in {6,12}
- **param_combo_count**: 108
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: label shuffle of forward returns conditional on run_len_z; destroy edge if only from top 1% runs
- **diff_vs_prior_failures**: Not net-flow chase: requires run-length z-score vs own history, not unsigned volume spike.
- **live_conflict_note**: Different from KEYSTONE p_fatal ML age-window; still same venues → deploy conflict if shared wallet.

Feature formulas:
  - `sign_i` = sign(amount_in_weth_of_swap_i) with +1 buy token with WETH, -1 sell token for WETH
  - `run_len_t` = largest k s.t. sign_{t-k+1..t} all equal
  - `run_len_z` = (run_len_t - mean(run_len_{t-L..t-1})) / (std(run_len_{t-L..t-1})+eps)
  - `run_notional_eth` = sum(|amount_weth|) over the current run

Parameter search space:
```json
{
  "L_bars": [
    60,
    120,
    240
  ],
  "z_enter": [
    1.5,
    2.0,
    2.5
  ],
  "min_run_notional_eth": [
    0.3,
    1.0
  ],
  "hold_bars": [
    3,
    6,
    12
  ],
  "stop_ret": [
    0.08,
    0.15
  ]
}
```

### F01_OFS_SIZE_SEQUENCE — Large-then-small size sequence toxicity

- **economic_mechanism**: After a large signed trade, a burst of tiny same-sign trades indicates informed follow-through vs spoof-like fade.
- **counterparty**: uninformed_retail_and_LP
- **universe**: V3 WETH pools with trade count>=M in 15m
- **observation_time**: t = time of first small trade after large seed trade
- **feature_available_at**: all trades in windows ending at t only
- **entry**: Enter long token if seq_score=1 and opp_fade<=max_opp_fade and large sign = +1
- **execution_delay**: delay_sec in {2,5} mapped to next bar
- **position_size**: notional_usd grid {50,100}
- **exit**: exit at hold_sec or stop/tp
- **stop**: ret<=-stop_ret stop_ret in {0.08,0.12}
- **take_profit**: ret>=tp_ret tp_ret in {0.12,0.20}
- **maximum_hold**: hold_sec
- **repeat_entry**: one entry per large-seed event
- **cooldown**: cooldown_sec in {300,600}
- **param_combo_count**: 72
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: permute trade sizes within window keeping signs; edge should vanish
- **diff_vs_prior_failures**: Uses ordered size transition, not aggregate buy_pressure_ratio used in older LASZLO features.
- **live_conflict_note**: Mechanism ≠ KEYSTONE features; execution conflict if co-deployed.

Feature formulas:
  - `size_eth_i` = |weth_amount_i|
  - `large_flag_i` = size_eth_i >= Q_p(size_eth, lookback) with p in search
  - `seq_score` = 1 if exists large_+ then >=k small_+ within dt_sec else 0
  - `opp_fade` = count opposite-sign within dt_sec after large

Parameter search space:
```json
{
  "p_large": [
    0.9,
    0.95
  ],
  "k_small": [
    3,
    5,
    8
  ],
  "dt_sec": [
    30,
    60,
    120
  ],
  "max_opp_fade": [
    0,
    1
  ],
  "hold_sec": [
    120,
    300
  ]
}
```

### F01_OFS_INTERARRIVAL — Inter-arrival compression continuation

- **economic_mechanism**: Sudden compression of trade inter-arrival times predicts short-horizon continuation of the dominant sign in the compressed window.
- **counterparty**: slower_market_makers
- **universe**: pools with >=20 swaps in 1h
- **observation_time**: bar close each 1m
- **feature_available_at**: inter-arrival stats from swaps with time<=bar_close
- **entry**: long if compress_ratio>=compress_min and dom_sign=+1
- **execution_delay**: 1 bar
- **position_size**: notional_usd {50,100}
- **exit**: time/stop/tp
- **stop**: ret<=-stop_ret
- **take_profit**: tp_ret in {0.08,0.15}
- **maximum_hold**: hold_bars
- **repeat_entry**: no overlap positions per pool
- **cooldown**: cooldown_bars {5,10}
- **param_combo_count**: 72
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: time-warp inter-arrivals (shuffle gaps); edge must die
- **diff_vs_prior_failures**: Not activity_heat count; uses gap compression ratio.
- **live_conflict_note**: Orthogonal to KEYSTONE fatal classifier.

Feature formulas:
  - `ia_i` = t_i - t_{i-1}
  - `ia_med_L` = median(ia) over last L swaps
  - `ia_med_S` = median(ia) over last S swaps, S<L
  - `compress_ratio` = ia_med_L / (ia_med_S+eps)
  - `dom_sign` = sign of sum(sign_i) over last S swaps

Parameter search space:
```json
{
  "L": [
    50,
    100
  ],
  "S": [
    5,
    10
  ],
  "compress_min": [
    2.0,
    3.0,
    4.0
  ],
  "hold_bars": [
    2,
    5,
    10
  ],
  "stop_ret": [
    0.07,
    0.12
  ]
}
```

## F02 — LP & liquidity events

### F02_LP_POST_MINT_DRIFT — Post-mint short-horizon drift

- **economic_mechanism**: Large liquidity mints temporarily improve executable depth; subsequent informed flow may drift price before inventory adjusts.
- **counterparty**: LPs_providing_inventory
- **universe**: V3 pools with decoded mints
- **observation_time**: mint event time
- **feature_available_at**: mint row known_at; price path only after mint
- **entry**: long if mint_z>=mint_z_min and mint_eth_proxy>=min and subsequent 1st swap sign not forced (no lookahead)
- **execution_delay**: delay 1 bar after mint confirmation
- **position_size**: notional_usd {50,100}
- **exit**: exit h_bars/stop/tp
- **stop**: ret<=-stop_ret
- **take_profit**: ret>=tp_ret
- **maximum_hold**: h_bars
- **repeat_entry**: max 1 open per pool
- **cooldown**: cooldown 30 bars
- **param_combo_count**: 72
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: randomize mint timestamps within day; edge gone
- **diff_vs_prior_failures**: Event-study on mint, not unsigned 'liquidity improved' narrative.
- **live_conflict_note**: Not KEYSTONE entry window logic.

Feature formulas:
  - `mint_eth_proxy` = abs(amount0/1) mapped to WETH side when possible else UNKNOWN
  - `mint_z` = zscore(mint_eth_proxy vs prior 24h mints)
  - `fwd_ret_h` = px(t+h)/px(t)-1 using mid from ticks/swaps

Parameter search space:
```json
{
  "mint_z_min": [
    1.0,
    1.5,
    2.0
  ],
  "h_bars": [
    5,
    15,
    30
  ],
  "stop_ret": [
    0.1,
    0.2
  ],
  "tp_ret": [
    0.08,
    0.15
  ],
  "min_mint_eth": [
    0.5,
    2.0
  ]
}
```

### F02_LP_POST_BURN_PRESSURE — Post-burn sell pressure

- **economic_mechanism**: Large burns reduce depth; residual inventory seekers sell into thinner book → negative drift.
- **counterparty**: remaining_LPs_and_holders
- **universe**: V3 pools with burns
- **observation_time**: burn event time
- **feature_available_at**: burn known_at
- **entry**: long-only atlas: enter SHORT-TOKEN via avoiding; design records SELL-token / flat-bias: open short-token synthetic = do not long; optional future short. For long-only: enter opposite pool? → specify: signal is BEARISH; strategy takes no long; paper metric tracks short mid. Implementation: side=SELL_SIGNAL_ONLY (no buy).
- **execution_delay**: 1 bar
- **position_size**: 0 USD long; tracks bearish mark-to-market for falsification only OR future short sleeve
- **exit**: signal horizon hold_bars
- **stop**: n/a long
- **take_profit**: n/a long
- **maximum_hold**: hold_bars
- **repeat_entry**: n/a
- **cooldown**: cooldown 30 bars
- **param_combo_count**: 48
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: burn size shuffle
- **diff_vs_prior_failures**: Opposite of mint-drift; burn-specific.
- **live_conflict_note**: Bearish signal sleeve; low live buy conflict but still research-only.

Feature formulas:
  - `burn_eth_proxy` = WETH-normalized burned liquidity proxy
  - `burn_z` = zscore vs 24h burns
  - `depth_proxy_drop` = 1 - depth_after/depth_before if available else UNKNOWN

Parameter search space:
```json
{
  "burn_z_min": [
    1.0,
    2.0
  ],
  "hold_bars": [
    5,
    15,
    30
  ],
  "stop_ret": [
    0.1,
    0.2
  ],
  "tp_ret": [
    0.08,
    0.15
  ],
  "min_burn_eth": [
    0.5,
    2.0
  ]
}
```

### F02_LP_DEPTH_LEAD — Depth thinning leads returns

- **economic_mechanism**: Measurable depth proxy declines before adverse selection; trade mean-reversion or momentum per sign of thinning.
- **counterparty**: toxic_takers
- **universe**: pools with mint/burn/swap to proxy depth
- **observation_time**: 1m bars
- **feature_available_at**: depth_proxy from events <= bar_close
- **entry**: if mode=0 fade thinning (long after thin if ret_1m<0); if mode=1 momentum (long if thin and ret_1m>0). mode is structural arm, not free continuous param spam.
- **execution_delay**: 1 bar
- **position_size**: notional {50,100}
- **exit**: hold/stop/tp
- **stop**: ret<=-stop_ret
- **take_profit**: tp in {0.08,0.15}
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: 10 bars
- **param_combo_count**: 72
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: placebo depth from shuffled mint/burn
- **diff_vs_prior_failures**: Lead-lag depth→price, not 'liquidity improved' slogan.
- **live_conflict_note**: Heavy data; parked from Wave1.

Feature formulas:
  - `depth_proxy_t` = cum_mints_eth - cum_burns_eth + baseline (UNKNOWN if incomplete)
  - `thin_rate` = (depth_{t-w}-depth_t)/(abs(depth_{t-w})+eps)
  - `ret_1m` = px_t/px_{t-1}-1

Parameter search space:
```json
{
  "w": [
    5,
    15,
    30
  ],
  "thin_min": [
    0.1,
    0.2,
    0.35
  ],
  "hold": [
    5,
    15
  ],
  "stop_ret": [
    0.1,
    0.15
  ],
  "mode": [
    0,
    1
  ]
}
```

## F03 — Volatility regime transitions

### F03_VOL_COMPRESS_EXPAND — Volatility compression then expansion

- **economic_mechanism**: Low realized vol regimes store energy; breakout expansion continues briefly.
- **counterparty**: option-like_speculators
- **universe**: pools with continuous 1m prices >= H hours/day
- **observation_time**: 1m close
- **feature_available_at**: vol from past returns only
- **entry**: enter long in breakout direction sign(sum r over last s) when compress_max met then expand_min met
- **execution_delay**: 1 bar after expand trigger
- **position_size**: notional {50,100}
- **exit**: hold/stop/tp
- **stop**: stop_ret {0.1,0.2}
- **take_profit**: tp {0.1,0.2}
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: 20 bars
- **param_combo_count**: 32
- **data_class**: `A_OFFLINE_DATA_LIKELY_AVAILABLE`
- **main_falsification**: vol block bootstrap
- **diff_vs_prior_failures**: Explicit compress→expand state machine vs raw momentum.
- **live_conflict_note**: Classic vol; not KEYSTONE meme fatal model.

Feature formulas:
  - `r_t` = log px_t - log px_{t-1}
  - `rv_s` = sqrt(sum r^2 over s bars)
  - `rv_l` = sqrt(sum r^2 over l bars)
  - `compress` = rv_s/rv_l
  - `expand_flag` = rv_s_next_window / rv_s >= e_min computed without peek: use causal trigger rv_s_t / rv_s_{t-s} 

Parameter search space:
```json
{
  "s": [
    15,
    30
  ],
  "l": [
    120,
    240
  ],
  "compress_max": [
    0.35,
    0.5
  ],
  "expand_min": [
    1.5,
    2.0
  ],
  "hold": [
    10,
    20
  ]
}
```

### F03_VOL_JUMP_CONTINUE — Jump continuation

- **economic_mechanism**: Returns exceeding k*rv are jumps; test continuation vs reversal as separate arms (structural).
- **counterparty**: inventory_liquidators
- **universe**: same as F03 compress universe
- **observation_time**: 1m
- **feature_available_at**: causal rv
- **entry**: arm=0 continuation long if jump_sign=+1; arm=1 fade long if jump_sign=-1 (buy dip after down jump)
- **execution_delay**: 1 bar
- **position_size**: notional {50,100}
- **exit**: hold/stop/tp
- **stop**: stop_ret
- **take_profit**: tp {0.08,0.15}
- **maximum_hold**: hold
- **repeat_entry**: one per jump
- **cooldown**: 10 bars
- **param_combo_count**: 72
- **data_class**: `A_OFFLINE_DATA_LIKELY_AVAILABLE`
- **main_falsification**: sign flip of jumps
- **diff_vs_prior_failures**: Jump definition mathematical; arms are explicit hypotheses.
- **live_conflict_note**: Distinct from KEYSTONE.

Feature formulas:
  - `jump_flag` = |r_t| >= k * rv_l_{t-1}
  - `jump_sign` = sign(r_t)

Parameter search space:
```json
{
  "k": [
    3.0,
    4.0,
    5.0
  ],
  "l": [
    60,
    120
  ],
  "hold": [
    3,
    6,
    12
  ],
  "arm": [
    0,
    1
  ],
  "stop_ret": [
    0.1,
    0.2
  ]
}
```

### F03_VOL_FAKE_BREAK_FADE — Failed breakout fade

- **economic_mechanism**: Price closes beyond Donchian high then reverses within m bars back inside → fade.
- **counterparty**: breakout_chasers
- **universe**: OHLCV pools
- **observation_time**: 1m
- **feature_available_at**: Donchian from past only
- **entry**: At confirmation time t+m when fail=True, enter short-token bearish mark / no long; long-only atlas records BEARISH_FADE signal quality. Optional: long if failed break_down symmetric.
- **execution_delay**: confirm at t+m, delay 1
- **position_size**: 0 long or {50,100} on upside failed breakdown arm only
- **exit**: hold/stop/tp
- **stop**: stop_ret
- **take_profit**: tp_ret
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: n bars
- **param_combo_count**: 72
- **data_class**: `A_OFFLINE_DATA_LIKELY_AVAILABLE`
- **main_falsification**: path scramble inside window
- **diff_vs_prior_failures**: Requires failure confirmation; not plain breakout.
- **live_conflict_note**: Mostly bearish fade; low buy conflict with KEYSTONE.

Feature formulas:
  - `don_hi` = max(high)_{t-n..t-1}
  - `don_lo` = min(low)_{t-n..t-1}
  - `break_up` = close_t > don_hi
  - `fail` = break_up and min(close)_{t+1..t+m} < don_hi (evaluated at t+m without using >t+m)

Parameter search space:
```json
{
  "n": [
    30,
    60,
    120
  ],
  "m": [
    3,
    5,
    10
  ],
  "hold": [
    5,
    15
  ],
  "stop_ret": [
    0.08,
    0.12
  ],
  "tp_ret": [
    0.06,
    0.12
  ]
}
```

## F04 — Cross-sectional capital rotation

### F04_XS_LEADER_TO_SECOND — Leader to second-tier rotation

- **economic_mechanism**: Within a 1h cohort, capital rotates from top return leader to 2nd-4th rank names with rising relative volume.
- **counterparty**: late_chasers_on_leader
- **universe**: cross-section of WETH V3 pools active in hour
- **observation_time**: hour close
- **feature_available_at**: ranks from past hour only
- **entry**: long second_ok names when leader ret_1h > leader_min (leader_min in {0.05,0.10})
- **execution_delay**: next hour open proxy = +1 bar
- **position_size**: equal weight among signals capped notional_total {100,200}
- **exit**: hold_h
- **stop**: stop_ret
- **take_profit**: tp_ret
- **maximum_hold**: hold_h
- **repeat_entry**: rebalance hourly
- **cooldown**: 0
- **param_combo_count**: 48
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: rank shuffle within hour
- **diff_vs_prior_failures**: Cross-sectional relative, not single-pool momentum.
- **live_conflict_note**: XS ≠ KEYSTONE single-pool age entry.

Feature formulas:
  - `ret_1h` = px_h/px_{h-1}-1
  - `rank_ret` = rank(ret_1h)
  - `rel_vol` = vol_1h / mean(vol_1h of cohort)
  - `leader` = rank_ret==1
  - `second_ok` = rank_ret in {2,3,4} and rel_vol rising vs prior hour

Parameter search space:
```json
{
  "cohort_min_pools": [
    10,
    20
  ],
  "hold_h": [
    1,
    2,
    4
  ],
  "rel_vol_min": [
    1.0,
    1.5
  ],
  "stop_ret": [
    0.15,
    0.25
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```

### F04_XS_SAME_AGE_RS — Same-age cohort relative strength

- **economic_mechanism**: Among pools with age in [a,b] minutes, long top RS, avoid bottom (long-only: long top).
- **counterparty**: weak_same_age_pools
- **universe**: new pools age 5-120m
- **observation_time**: minute bars since pool first swap
- **feature_available_at**: age from first_swap_time known
- **entry**: long if age in [age_lo,age_hi] and rs>=rs_min
- **execution_delay**: 1 bar
- **position_size**: notional {50,100}
- **exit**: hold/stop/tp
- **stop**: 0.15/0.25
- **take_profit**: 0.1/0.2
- **maximum_hold**: hold
- **repeat_entry**: one per pool lifetime window
- **cooldown**: until age_hi
- **param_combo_count**: 48
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: cohort label shuffle
- **diff_vs_prior_failures**: Age-bucket RS; KEYSTONE uses ML p_fatal not RS zscore.
- **live_conflict_note**: HIGH live overlap risk: same young-pool universe as KEYSTONE → Wave parking bias.

Feature formulas:
  - `age_m` = (t-first_swap)/60
  - `rs` = ret_{w} zscore within age cohort bucket

Parameter search space:
```json
{
  "age_lo": [
    5,
    10
  ],
  "age_hi": [
    30,
    60,
    120
  ],
  "w": [
    5,
    10
  ],
  "rs_min": [
    0.5,
    1.0
  ],
  "hold": [
    5,
    15
  ]
}
```

### F04_XS_OLD_TO_NEW_MIGRATE — Old-to-new capital migration

- **economic_mechanism**: When aggregate notional in pools age>24h falls while age<2h rises, long a basket of newest qualifying pools.
- **counterparty**: migrating_speculators
- **universe**: Base V3 WETH universe hourly
- **observation_time**: hour close
- **feature_available_at**: age from first_swap
- **entry**: if migrate_ratio>=ratio_min long top-k newest pools by flow_new with vol filter
- **execution_delay**: +1h
- **position_size**: total notional {100,200} split k ways
- **exit**: hold_h
- **stop**: stop_ret
- **take_profit**: tp {0.15,0.25}
- **maximum_hold**: hold_h
- **repeat_entry**: hourly
- **cooldown**: 0
- **param_combo_count**: 48
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: permute pool ages
- **diff_vs_prior_failures**: Macro rotation aggregate, not single-pool micro.
- **live_conflict_note**: Heavy; also universe overlap.

Feature formulas:
  - `flow_old` = sum buy_weth in age>24h
  - `flow_new` = sum buy_weth in age<2h
  - `migrate_ratio` = flow_new/(flow_old+eps)

Parameter search space:
```json
{
  "ratio_min": [
    1.0,
    1.5,
    2.0
  ],
  "basket_k": [
    3,
    5
  ],
  "hold_h": [
    1,
    2
  ],
  "stop_ret": [
    0.2,
    0.3
  ],
  "min_pool_vol_eth": [
    1.0,
    3.0
  ]
}
```

## F05 — Base-wide regime

### F05_REG_BREADTH_EXPAND — Active breadth expansion gate

- **economic_mechanism**: Trade any subordinate alpha only when unique active pools/hour rises above quantile; this strategy itself goes long a breadth ETF-like top-volume basket.
- **counterparty**: narrow_market_makers
- **universe**: Base-wide hourly
- **observation_time**: hour
- **feature_available_at**: breadth from past hour counts
- **entry**: if breadth_z>=z_min long top-k pools by volume that hour
- **execution_delay**: +1h
- **position_size**: total {100,200}
- **exit**: hold_h
- **stop**: 0.2
- **take_profit**: 0.15
- **maximum_hold**: hold_h
- **repeat_entry**: hourly
- **cooldown**: 0
- **param_combo_count**: 48
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: time permute breadth series
- **diff_vs_prior_failures**: Timing gate family: when to trade, not toxic-flow ML.
- **live_conflict_note**: Regime sleeve; can gate others later.

Feature formulas:
  - `breadth_h` = count distinct pools with >=q swaps in hour h
  - `breadth_z` = zscore vs trailing D days

Parameter search space:
```json
{
  "q": [
    5,
    10
  ],
  "D": [
    7,
    14
  ],
  "z_min": [
    0.5,
    1.0,
    1.5
  ],
  "basket_k": [
    5,
    10
  ],
  "hold_h": [
    1,
    2
  ]
}
```

### F05_REG_NEWPOOL_DILUTION — New-pool dilution avoidance

- **economic_mechanism**: When new pools created/hour exceed threshold, expected edge per pool dilutes; strategy flats risk-on basket (signal=FLAT).
- **counterparty**: attenion_fragmented_traders
- **universe**: Base-wide
- **observation_time**: hour
- **feature_available_at**: new pool counts from first_swap or pool_created events <=hour
- **entry**: Trade long top-k ONLY if dilution<=dilution_max; else no entry (edge from avoidance).
- **execution_delay**: +1h
- **position_size**: {100,200} when allowed
- **exit**: hold_h
- **stop**: 0.2
- **take_profit**: 0.15
- **maximum_hold**: hold_h
- **repeat_entry**: hourly
- **cooldown**: 0
- **param_combo_count**: 48
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: shuffle new_pools_h series
- **diff_vs_prior_failures**: Avoidance regime ≠ always-in KEYSTONE.
- **live_conflict_note**: Could later risk-reduce live; research only now.

Feature formulas:
  - `new_pools_h` = count first_swap in hour
  - `dilution` = new_pools_h / median_7d

Parameter search space:
```json
{
  "dilution_max": [
    1.5,
    2.0,
    3.0
  ],
  "lookback_d": [
    7,
    14
  ],
  "basket_k": [
    5,
    10
  ],
  "hold_h": [
    1,
    2
  ],
  "z_volume": [
    0,
    1
  ]
}
```

### F05_REG_GAS_CONGESTION — Gas congestion entry filter

- **economic_mechanism**: High base-fee regimes worsen net EV; long micro-alpha basket only when base_fee_z below threshold.
- **counterparty**: congested_priority_fees_eat_edge
- **universe**: needs base fee series + basket
- **observation_time**: hour/ L2 block
- **feature_available_at**: fee from blocks <=t
- **entry**: enter basket if base_fee_z<=z_max
- **execution_delay**: +1h
- **position_size**: {100,200}
- **exit**: hold_h
- **stop**: stop_ret
- **take_profit**: tp_ret
- **maximum_hold**: hold_h
- **repeat_entry**: hourly
- **cooldown**: 0
- **param_combo_count**: 48
- **data_class**: `D_DATA_UNAVAILABLE`
- **main_falsification**: permute fees vs returns
- **diff_vs_prior_failures**: Cost-regime timing; not flow toxicity.
- **live_conflict_note**: Low conflict; data missing.

Feature formulas:
  - `base_fee_z` = zscore(base_fee_per_gas)

Parameter search space:
```json
{
  "z_max": [
    -0.5,
    0.0,
    0.5
  ],
  "basket_k": [
    5,
    10
  ],
  "hold_h": [
    1,
    2
  ],
  "stop_ret": [
    0.2,
    0.3
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```

## F06 — Silence & revival

### F06_REV_SILENCE_FIRST_TRADE — First trade after silence

- **economic_mechanism**: After silence_gap_sec with zero swaps, the first swap's sign and size predict short continuation.
- **counterparty**: sleeping_LP_inventory
- **universe**: pools with at least one silence >=G historically
- **observation_time**: event time of revival swap
- **feature_available_at**: gap measured with prior swap time known
- **entry**: long if gap_sec>=G_min and revive_sign=+1 and revive_eth>=min_eth
- **execution_delay**: delay_sec {2,5}
- **position_size**: {50,100}
- **exit**: hold_sec
- **stop**: stop_ret
- **take_profit**: tp_ret
- **maximum_hold**: hold_sec
- **repeat_entry**: one per silence episode
- **cooldown**: G_min
- **param_combo_count**: 72
- **data_class**: `A_OFFLINE_DATA_LIKELY_AVAILABLE`
- **main_falsification**: shuffle gaps
- **diff_vs_prior_failures**: Silence definition hard; ≠ continuous momentum.
- **live_conflict_note**: Different horizon than 10-15m KEYSTONE window.

Feature formulas:
  - `gap_sec` = t_i - t_{i-1}
  - `revive_sign` = sign_i
  - `revive_eth` = size_eth_i

Parameter search space:
```json
{
  "G_min": [
    1800,
    3600,
    7200
  ],
  "min_eth": [
    0.2,
    0.5,
    1.0
  ],
  "hold_sec": [
    300,
    900
  ],
  "stop_ret": [
    0.1,
    0.2
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```

### F06_REV_VOL_BEFORE_PRICE — Volume revival before price

- **economic_mechanism**: Swap count recovers to >c*baseline while |ret| still <r_max → long anticipating price catch-up.
- **counterparty**: latent_demand
- **universe**: previously active pools silent then reviving
- **observation_time**: 1m
- **feature_available_at**: vol and ret causal
- **entry**: long if vol_now>=c*vol_base and ret_abs<=r_max and prior silence>=G (G in {1800,3600})
- **execution_delay**: 1 bar
- **position_size**: {50,100}
- **exit**: hold
- **stop**: 0.12
- **take_profit**: 0.12
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: 30 bars
- **param_combo_count**: 32
- **data_class**: `A_OFFLINE_DATA_LIKELY_AVAILABLE`
- **main_falsification**: shuffle volume path vs price
- **diff_vs_prior_failures**: Requires volume-price divergence, not raw revival chase.
- **live_conflict_note**: Orthogonal mechanism to KEYSTONE.

Feature formulas:
  - `vol_base` = mean swap_count over baseline window B prior to silence
  - `vol_now` = swap_count over last s bars
  - `ret_abs` = abs(px_t/px_{t-s}-1)

Parameter search space:
```json
{
  "B": [
    60,
    120
  ],
  "s": [
    5,
    10
  ],
  "c": [
    0.5,
    1.0
  ],
  "r_max": [
    0.02,
    0.05
  ],
  "hold": [
    10,
    20
  ]
}
```

### F06_REV_LIQ_REINJECT — Liquidity re-injection revival

- **economic_mechanism**: After silence, a mint above threshold then swaps resume → long.
- **counterparty**: reactivated_LP
- **universe**: pools with mint after silence
- **observation_time**: mint time
- **feature_available_at**: mint+prior gap known
- **entry**: long when gap_before_mint>=G_min and mint_eth>=min and swaps_after>=min (confirm at mint+dt)
- **execution_delay**: 1 bar after confirm
- **position_size**: {50,100}
- **exit**: hold
- **stop**: 0.15
- **take_profit**: 0.15
- **maximum_hold**: hold
- **repeat_entry**: one per episode
- **cooldown**: G_min
- **param_combo_count**: 32
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: mint/swap order scramble
- **diff_vs_prior_failures**: Joint mint+silence+resume; not mint-only F02.
- **live_conflict_note**: Event combo ≠ KEYSTONE.

Feature formulas:
  - `gap_before_mint` = mint_time - last_swap_before
  - `mint_eth` = WETH proxy
  - `swaps_after` = count swaps in (mint, mint+dt]

Parameter search space:
```json
{
  "G_min": [
    3600,
    7200
  ],
  "mint_min_eth": [
    0.5,
    2.0
  ],
  "dt_sec": [
    300,
    900
  ],
  "min_swaps_after": [
    2,
    5
  ],
  "hold": [
    10,
    30
  ]
}
```

## F07 — Wallet cohort & inventory migration

### F07_WAL_SYNC_NEW_WALLETS — Synchronized new-wallet entry

- **economic_mechanism**: k distinct tx_from with first-seen label NEW enter same pool within dt, excluding routers.
- **counterparty**: dispersed_coordinated_demand
- **universe**: pools with tx_from on swaps
- **observation_time**: event
- **feature_available_at**: wallet age labels known_at<=t
- **entry**: long if sync_count>=k_min
- **execution_delay**: 1 bar
- **position_size**: {50,100}
- **exit**: hold
- **stop**: stop_ret
- **take_profit**: tp {0.1,0.2}
- **maximum_hold**: hold
- **repeat_entry**: one per cluster
- **cooldown**: dt
- **param_combo_count**: 48
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: wallet id permute
- **diff_vs_prior_failures**: Cohort sync ≠ single smart-money follow.
- **live_conflict_note**: Heavy; not Wave1.

Feature formulas:
  - `new_wallet` = first_seen_tx_time >= t - W_new
  - `sync_count` = count distinct new_wallet buyers in dt
  - `router_exclude` = tx_from not in static router set

Parameter search space:
```json
{
  "W_new": [
    3600,
    86400
  ],
  "dt": [
    60,
    300
  ],
  "k_min": [
    3,
    5,
    8
  ],
  "hold": [
    10,
    30
  ],
  "stop_ret": [
    0.15,
    0.25
  ]
}
```

### F07_WAL_EARLY_HOLDER_RELEASE — Early-holder inventory release

- **economic_mechanism**: Wallets that bought in first A minutes net sell above threshold → bearish / avoid long.
- **counterparty**: early_insiders_distributing
- **universe**: young pools
- **observation_time**: event/1m
- **feature_available_at**: cohort defined by buys with time<=first_swap+A
- **entry**: mode0: BEARISH no-long; mode1: long if net_sell below low threshold (holders retaining)
- **execution_delay**: 1 bar after W
- **position_size**: {0 or 50,100}
- **exit**: hold
- **stop**: 0.2
- **take_profit**: 0.15
- **maximum_hold**: hold
- **repeat_entry**: once
- **cooldown**: until age>A+W
- **param_combo_count**: 48
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: cohort membership scramble
- **diff_vs_prior_failures**: Inventory release ≠ copy-trade.
- **live_conflict_note**: Young-pool overlap with live.

Feature formulas:
  - `early_set` = tx_from with buy in [0,A]
  - `net_sell_eth` = sell_weth - buy_weth of early_set in window W after A

Parameter search space:
```json
{
  "A_min": [
    5,
    10,
    15
  ],
  "W_min": [
    10,
    30
  ],
  "sell_eth_min": [
    1.0,
    3.0
  ],
  "hold": [
    10,
    30
  ],
  "mode": [
    0,
    1
  ]
}
```

### F07_WAL_CONC_DECLINE — Holder concentration decline

- **economic_mechanism**: HHI of token balances among top wallets declines at rate d → healthier distribution long.
- **counterparty**: concentrated_whales_distributing_to_broader_set
- **universe**: tokens with transfer data
- **observation_time**: 5m/1h snapshots
- **feature_available_at**: balances known_at from transfers <=t
- **entry**: long if dHHI<=dHHI_max and liquidity filter
- **execution_delay**: 1 bar
- **position_size**: {50,100}
- **exit**: hold
- **stop**: stop_ret
- **take_profit**: 0.15
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: w
- **param_combo_count**: 32
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: holder permute
- **diff_vs_prior_failures**: Concentration path ≠ flow run-length.
- **live_conflict_note**: Heavy.

Feature formulas:
  - `HHI_t` = sum share_i^2 top K holders
  - `dHHI` = HHI_t - HHI_{t-w}

Parameter search space:
```json
{
  "K": [
    10,
    20
  ],
  "w": [
    12,
    24
  ],
  "dHHI_max": [
    -0.02,
    -0.05
  ],
  "hold": [
    12,
    24
  ],
  "stop_ret": [
    0.2,
    0.3
  ]
}
```

## F08 — Protocol & event driven

### F08_EVT_POOL_AGE_RULE — Deterministic pool-age liquidity rule

- **economic_mechanism**: Pure age-window + min pre-volume + max p_impact_proxy rule without ML fatal model — structural contrast to KEYSTONE.
- **counterparty**: early_LP_and_snipers
- **universe**: new V3 WETH pools
- **observation_time**: age minutes since first_swap
- **feature_available_at**: first_swap known; features from [0,age_entry]
- **entry**: long if age in [lo,hi] and pre_vol>=min and impact_proxy<=max
- **execution_delay**: 1 bar
- **position_size**: {50,100}
- **exit**: hold_min
- **stop**: 0.5 hard stop like live optional stress
- **take_profit**: tp {0.2,0.5}
- **maximum_hold**: hold_min
- **repeat_entry**: once per pool
- **cooldown**: until age>hi
- **param_combo_count**: 108
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: compare to KEYSTONE signals overlap; must show incremental EV after excluding overlapping entries
- **diff_vs_prior_failures**: Explicitly non-ML and different feature set than p_fatal; still HIGH live universe overlap → research contrast only.
- **live_conflict_note**: REJECT_LIVE_CONFLICT risk if deployed; kept as contrast sleeve for orthogonality/falsification of KEYSTONE.

Feature formulas:
  - `age_m` = (t-first_swap)/60
  - `pre_vol_eth` = sum |weth| in [0, age_m]
  - `impact_proxy` = abs(ret) / (pre_vol_eth+eps)

Parameter search space:
```json
{
  "age_enter_lo": [
    8,
    10
  ],
  "age_enter_hi": [
    12,
    15,
    20
  ],
  "min_pre_vol": [
    0.5,
    1.0,
    2.0
  ],
  "max_impact_proxy": [
    0.05,
    0.1
  ],
  "hold_min": [
    20,
    60,
    120
  ]
}
```

### F08_EVT_MINT_BURST — Clustered mint burst event

- **economic_mechanism**: >=k mints from distinct owners within dt after pool birth → structural attention event.
- **counterparty**: multi_LP_bootstrappers
- **universe**: new pools
- **observation_time**: event
- **feature_available_at**: mints <=t
- **entry**: long if mint_burst>=k_min and age_m<=age_max
- **execution_delay**: 1 bar
- **position_size**: {50,100}
- **exit**: hold
- **stop**: stop_ret
- **take_profit**: 0.15
- **maximum_hold**: hold
- **repeat_entry**: once
- **cooldown**: age_max
- **param_combo_count**: 48
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: owner ids permute
- **diff_vs_prior_failures**: Multi-LP burst ≠ single mint drift F02.
- **live_conflict_note**: Young pool overlap medium-high.

Feature formulas:
  - `mint_burst` = count distinct owner mints in dt after first_mint

Parameter search space:
```json
{
  "dt_sec": [
    300,
    900,
    1800
  ],
  "k_min": [
    3,
    5
  ],
  "age_max_m": [
    30,
    60
  ],
  "hold": [
    15,
    45
  ],
  "stop_ret": [
    0.2,
    0.3
  ]
}
```

### F08_EVT_FEE_TIER_SHIFT — Fee-tier / pool migration proxy

- **economic_mechanism**: When same token pair appears with new fee tier pool and volume migrates, long migrating pool / avoid dying pool.
- **counterparty**: migrating_flow_takers
- **universe**: token pairs with >=2 fee tiers
- **observation_time**: 1h
- **feature_available_at**: pair mapping known
- **entry**: long new-tier pool if share_delta>=min and vol filters
- **execution_delay**: +1h
- **position_size**: {50,100}
- **exit**: hold_h
- **stop**: stop_ret
- **take_profit**: tp_ret
- **maximum_hold**: hold_h
- **repeat_entry**: one per migration episode
- **cooldown**: 24h
- **param_combo_count**: 48
- **data_class**: `D_DATA_UNAVAILABLE`
- **main_falsification**: tier labels shuffle
- **diff_vs_prior_failures**: Protocol structure change; not micro runs.
- **live_conflict_note**: Low live conflict; data missing.

Feature formulas:
  - `share_new` = vol_new_tier / (vol_all_tiers+eps)
  - `share_delta` = share_new_t - share_new_{t-1}

Parameter search space:
```json
{
  "share_delta_min": [
    0.1,
    0.2
  ],
  "hold_h": [
    2,
    6,
    12
  ],
  "min_vol_eth": [
    5,
    20
  ],
  "stop_ret": [
    0.15,
    0.25
  ],
  "tp_ret": [
    0.1,
    0.2
  ]
}
```

## F09 — Quote, routing & execution anomalies

### F09_EXE_IMPACT_ASYMM — Buy/sell impact asymmetry

- **economic_mechanism**: Estimate impact_buy and impact_sell on matched notionals; trade direction with lower adverse impact persistence.
- **counterparty**: asymmetric_LP_curves
- **universe**: pools with dense swaps
- **observation_time**: 5m estimation windows
- **feature_available_at**: impacts from past windows only
- **entry**: long if asym<=-asym_min (buys cheaper impact than sells) else flat/bearish mark
- **execution_delay**: 1 bar
- **position_size**: {50,100}
- **exit**: hold
- **stop**: stop_ret
- **take_profit**: 0.1
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: 15 bars
- **param_combo_count**: 32
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: sign flip of sides
- **diff_vs_prior_failures**: Microstructure impact geometry ≠ signal ML.
- **live_conflict_note**: Distinct; medium compute.

Feature formulas:
  - `impact(side,n)` = median |ret| over dt after swaps of side with size~n
  - `asym` = impact_buy - impact_sell

Parameter search space:
```json
{
  "n_eth": [
    0.3,
    1.0
  ],
  "dt_sec": [
    30,
    60
  ],
  "asym_min": [
    0.002,
    0.005
  ],
  "hold": [
    5,
    15
  ],
  "stop_ret": [
    0.1,
    0.15
  ]
}
```

### F09_EXE_NOTIONAL_CURVE — Nonlinear notional slippage curve

- **economic_mechanism**: Fit impact ≈ a*n^b on historical swaps; enter only notionals where modeled net EV>0 under stress.
- **counterparty**: size_takers_ignoring_convexity
- **universe**: dense pools
- **observation_time**: offline fit windows
- **feature_available_at**: fit on past only
- **entry**: enter long only if edge_hat(n_usd)>=min_edge_hat and separate alpha direction from F01 sign run (uses dom_sign)
- **execution_delay**: 1 bar
- **position_size**: n_usd
- **exit**: hold
- **stop**: 0.1
- **take_profit**: 0.1
- **maximum_hold**: hold
- **repeat_entry**: no overlap
- **cooldown**: 10
- **param_combo_count**: 32
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: scramble sizes vs returns in fit
- **diff_vs_prior_failures**: Position sizing economics; depends on external direction signal defined as dom_sign from last S swaps.
- **live_conflict_note**: Sizing overlay risk if combined with live.

Feature formulas:
  - `model` = impact_hat(n)=a*n^b
  - `edge_hat` = expected_move_hat - impact_hat(n) - costs

Parameter search space:
```json
{
  "fit_lookback_h": [
    6,
    24
  ],
  "n_usd": [
    50,
    100
  ],
  "hold": [
    5,
    15
  ],
  "min_edge_hat": [
    0.0,
    0.01
  ],
  "dom_S": [
    5,
    10
  ]
}
```

### F09_EXE_QUOTE_STALE — Stale quote recovery

- **economic_mechanism**: If mid moves >=x without swaps for g seconds (stale), trade mean-reversion toward last trade price when swaps resume.
- **counterparty**: stale_arbitrageurs_slow
- **universe**: pools with external mid OR tick-derived mid
- **observation_time**: event
- **feature_available_at**: stale detection causal
- **entry**: on resume, long if mid>last_trade (expect down? → fade: side = -sign(mid/last_trade-1))
- **execution_delay**: delay_sec {1,2}
- **position_size**: {50,100}
- **exit**: hold_sec
- **stop**: stop_ret
- **take_profit**: tp_ret
- **maximum_hold**: hold_sec
- **repeat_entry**: one per stale episode
- **cooldown**: g_sec
- **param_combo_count**: 72
- **data_class**: `D_DATA_UNAVAILABLE`
- **main_falsification**: inject fake stale flags
- **diff_vs_prior_failures**: Latency/staleness microstructure; not trend.
- **live_conflict_note**: No arb executor; research only; data missing.

Feature formulas:
  - `stale` = no swaps for g sec AND abs(mid_now/last_trade_px-1)>=x
  - `resume` = first swap after stale

Parameter search space:
```json
{
  "g_sec": [
    15,
    30,
    60
  ],
  "x": [
    0.01,
    0.02,
    0.05
  ],
  "hold_sec": [
    30,
    120
  ],
  "stop_ret": [
    0.05,
    0.1
  ],
  "tp_ret": [
    0.005,
    0.02
  ]
}
```

## F10 — Passive LP & fee capture

### F10_PLP_FEE_VS_IL — Short-horizon fee vs IL

- **economic_mechanism**: Provide liquidity for H minutes; net = fee_income - IL - gas - rebalance - withdraw; enter only when predicted net>0.
- **counterparty**: takers_paying_fees
- **universe**: V3 pools with fee + price path
- **observation_time**: LP open time
- **feature_available_at**: prediction features <= open
- **entry**: open LP if pred_net>=min_pred_net using vol_forecast from past w
- **execution_delay**: 1 bar to mint
- **position_size**: liq_usd
- **exit**: close after H_min
- **stop**: emergency withdraw if IL mid-path exceeds stop_il {0.02,0.05}
- **take_profit**: n/a take-profit beyond H; optional early exit if fee_captured>=tp_fee
- **maximum_hold**: H_min
- **repeat_entry**: no overlapping LP on same pool
- **cooldown**: H_min
- **param_combo_count**: 72
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: fee path scramble vs price path
- **diff_vs_prior_failures**: Full net components mandatory; APR-only forbidden.
- **live_conflict_note**: Different product (LP not swap); still not Wave1.

Feature formulas:
  - `fee_income` = fee_tier * expected_volume * liq_share
  - `IL` = IL(price_ratio) standard V3 formula for range
  - `net` = fee_income - IL - gas - rebalance_cost - withdrawal_cost

Parameter search space:
```json
{
  "H_min": [
    15,
    60,
    240
  ],
  "range_width_ticks": [
    10,
    50,
    200
  ],
  "liq_usd": [
    50,
    100
  ],
  "min_pred_net": [
    0.0,
    0.01
  ],
  "vol_forecast_w": [
    30,
    120
  ]
}
```

### F10_PLP_HIGHVOL_LOWTREND — High volume low trend LP

- **economic_mechanism**: Select pools with high vol_volume_z and low |trend|; earn fees with limited IL.
- **counterparty**: noisy_takers
- **universe**: V3 pools
- **observation_time**: 1h decision
- **feature_available_at**: features past only
- **entry**: open LP if vol_z>=min and trend_abs<=max
- **execution_delay**: +1h mint
- **position_size**: liq_usd {50,100}
- **exit**: H_h hours
- **stop**: stop_il {0.02,0.05}
- **take_profit**: early exit if fee_captured>=tp
- **maximum_hold**: H_h
- **repeat_entry**: no overlap
- **cooldown**: H_h
- **param_combo_count**: 32
- **data_class**: `B_SMALL_BOUNDED_EXPORT_REQUIRED`
- **main_falsification**: shuffle volume vs trend labels
- **diff_vs_prior_failures**: Screen on volume/trend geometry for LP, not directional buy.
- **live_conflict_note**: LP sleeve; low swap-executor conflict.

Feature formulas:
  - `vol_z` = zscore(volume_eth_1h)
  - `trend_abs` = abs(px_h/px_{h-L}-1)

Parameter search space:
```json
{
  "vol_z_min": [
    1.0,
    1.5
  ],
  "trend_max": [
    0.02,
    0.05
  ],
  "L": [
    6,
    12
  ],
  "H_h": [
    2,
    6
  ],
  "range_width_ticks": [
    50,
    200
  ]
}
```

### F10_PLP_EVENT_WINDOW — Event-window LP

- **economic_mechanism**: Mint LP for short window around scheduled/detected volume events (e.g., post-mint burst) then withdraw.
- **counterparty**: event_takers
- **universe**: pools with detectable events from F02/F08 definitions
- **observation_time**: event time
- **feature_available_at**: event features <=t
- **entry**: on event_flag, mint at t+pre_min, burn at t+post_min
- **execution_delay**: 1 bar
- **position_size**: liq_usd
- **exit**: fixed schedule
- **stop**: stop_il
- **take_profit**: n/a
- **maximum_hold**: post_min
- **repeat_entry**: one per event
- **cooldown**: 60m
- **param_combo_count**: 48
- **data_class**: `C_PRODUCTION_DB_HEAVY`
- **main_falsification**: placebo events
- **diff_vs_prior_failures**: Scheduled LP around events; distinct from continuous highvol-lowtrend screen.
- **live_conflict_note**: Heavy LP sim.

Feature formulas:
  - `event_flag` = reuse F08 mint_burst or volume z event without future
  - `net` = fee_income - IL - gas - withdraw

Parameter search space:
```json
{
  "pre_min": [
    0,
    5
  ],
  "post_min": [
    10,
    30,
    60
  ],
  "range_width_ticks": [
    20,
    100
  ],
  "liq_usd": [
    50,
    100
  ],
  "min_event_z": [
    1.0,
    2.0
  ]
}
```

