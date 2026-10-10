# Phase E1: T+15 Ex-ante Discriminant

> Universe n=**4154** | labeled replay n=**426** | tradable_tail=**51** | RPC supplement=**0**

## 1. 三组对比（feature median）

| group | n | n_trades_15 | unique_buyers_15 | vSol_delta_5_15 | progress_15 | premium_0_15 |
|-------|---|-------------|------------------|-----------------|-------------|--------------|
| tradable_tail | 51 | 21.00 | 1.00 | 13.12 | 0.49 | 0.32 |
| hit100_nontradable | 123 | 13.00 | 6.00 | 8.21 | 0.71 | 0.25 |
| random_control | 51 | 5.00 | NA | 0.00 | 0.35 | 0.00 |

## 2. Univariate lift（tradable_tail vs base）

| feature | base | p(hi) | lift | dir |
|---------|------|-------|------|-----|
| n_trades_15 | 0.134 | 0.214 | 1.60 | high |
| time_to_5th_s | 0.134 | 0.156 | 1.16 | high |
| reached_birth | 0.134 | 0.149 | 1.12 | high |
| vSol_delta_5_15 | 0.134 | 0.149 | 1.12 | high |
| last_trade_before_15 | 0.134 | 0.149 | 1.11 | high |
| first_trade_delay | 0.134 | 0.137 | 1.02 | high |
| has_snap_15 | 0.134 | 0.134 | 1.00 | high |
| prior_n | 0.134 | 0.134 | 1.00 | high |
| prior_grad_rate | 0.134 | 0.134 | 1.00 | high |
| prior_log_max_ath | 0.134 | 0.134 | 1.00 | high |
| prior_med_t2ath | 0.134 | 0.134 | 1.00 | high |
| premium_0_15 | 0.134 | 0.127 | 0.95 | low |
| vSol_15 | 0.134 | 0.112 | 0.84 | low |
| progress_15 | 0.134 | 0.112 | 0.84 | low |
| vSol_5 | 0.134 | 0.104 | 0.78 | low |

## 3. Simple rank score — full universe top-k lift

| bucket | n | labeled | p(tradable) | p(+300%) | p(bad) | median ret | lift |
|--------|---|---------|-------------|----------|--------|------------|------|
| ALL | 4154 | 426 | 12.0% | 4.9% | 43.2% | -12.8% | - |
| top_1pct | 41 | 38 | 26.3% | 10.5% | 57.9% | -46.8% | 2.20 |
| top_5pct | 207 | 175 | 24.0% | 10.9% | 52.0% | -44.5% | 2.00 |
| top_10pct | 415 | 241 | 18.7% | 8.3% | 56.0% | -46.3% | 1.56 |

## 4. OOS（train 06-02~06-11, test 06-12~06-15）

- train labeled n=269 | test labeled n=157
- test top5% p(tradable)=21.3% vs ALL test=9.6%
- test top5% median ret=-18.2% vs ALL=-9.7%

## 5. Daily top5% p(tradable)

| day | top5 n | labeled | p(tradable) | median ret |
|-----|--------|---------|-------------|------------|
| 2026-06-02 | 14 | 13 | 46.2% | +74.4% |
| 2026-06-03 | 14 | 13 | 46.2% | +26.7% |
| 2026-06-04 | 14 | 7 | 0.0% | -33.3% |
| 2026-06-05 | 14 | 13 | 23.1% | -56.6% |
| 2026-06-06 | 14 | 14 | 35.7% | -67.2% |
| 2026-06-07 | 14 | 12 | 16.7% | -52.2% |
| 2026-06-08 | 14 | 14 | 28.6% | -68.1% |
| 2026-06-09 | 14 | 13 | 7.7% | -22.3% |
| 2026-06-10 | 16 | 14 | 14.3% | -58.1% |
| 2026-06-11 | 14 | 11 | 27.3% | -12.5% |
| 2026-06-12 | 14 | 10 | 10.0% | -2.5% |
| 2026-06-13 | 14 | 7 | 14.3% | +1.4% |
| 2026-06-14 | 14 | 13 | 23.1% | -2.7% |
| 2026-06-15 | 14 | 11 | 36.4% | -45.5% |

## 6. 结论

**PARTIAL ex-ante discriminant.** Top5% p(tradable)=24.0% vs ALL=12.0% (lift=2.00x, labeled n=175). Top5% median ret=-44.5%, p(bad)=52.0%. OOS test top5 p(tradable)=21.3%. Rate lift 存在但 median 仍为负、fatal25 偏高 → 仅适合作筛选信号，非可交易策略。

