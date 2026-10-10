# Phase 5 Addendum — V3-style Trail + Depth + Realgas on V4

**Date:** 2026-06-25  
**RPC counter:** 10,685 / 20,000 (0 new counter RPC; 112 anvil depth probes)

## Question

Can V4 recover V3-class alpha if we mirror live gates?

| Gate | V3 live reference |
|------|-------------------|
| Entry friction | roundtrip ≥ −5% |
| Pool depth | ≥ 3 ETH WETH |
| Exit | TRAIL = 25% |
| PnL lens | realgas: `ret − $0.15/$50 − 2%` |

## Method

- Script: `v4_trail_backtest.py`
- **Trail:** log(1+ret) peak, TRAIL=0.25, hard stop −50% (V3 `exit_block_param` analogue)
- **Path:** discrete roundtrip PnL at offsets from cached replays (not tick/QuoterV2)
- **Depth proxy:** 0.01 ETH roundtrip at first_swap block; pass if slippage ≥ −15% (112 pools, anvil)
- **Realgas adj:** `adj = trail_ret − 0.003 − 0.02`

## Depth probe

| | n |
|--|--|
| depth_ok | 102 |
| depth_fail (buy/sell fail or slip < −15%) | 10 |

Most clean V4 pools absorb 0.01 ETH roundtrip; depth gate does **not** filter the sample materially.

## Results (adj realgas, best gate = RD-B + friction + depth)

| Entry | Gate | n | Positive | Median | Tail ≥+100% | Fatal ≤−50% |
|-------|------|---|----------|--------|-------------|-------------|
| first_swap | RD-B_fric_depth | 50 | **0%** | **−4.9%** | 0% | 0% |
| first_swap | fric_ge_-5pct (all clean) | 98 | 0% | −4.9% | 0% | 0% |
| Initialize | RD-B_fric_depth | 50 | 0% | −4.9% | 0% | 0% |
| Initialize | all_clean (raw trail) | 112 | 0% | −2.6% | 0% | **9.8%** |

**V3 benchmark (644 clean, same gates, realgas walk-forward):** ~75% positive, median ~+72% raw on Initialize-style study.

## Trail behavior

- **112/112 first_swap paths:** `time_exit` at offset 300 — PnL flat ~−2.6% at every checkpoint; trail never fires.
- **Initialize paths (offsets 0→7200):** still flat ~−2.6% on survivors; ~10 pools hit hard_stop / sell-fail on late offsets (trail does not create upside).

## Verdict

**`v4_alpha_candidate_no` — unchanged.**

V3-style trail + friction + depth + realgas **does not** surface alpha on V4 clean/RD-B pools. Underlying issue is **flat negative roundtrip friction (~−2.6%) with no positive excursions** in the PnL path — exit logic cannot manufacture edge from a path with no peak.

RD-B remains a **safety / label filter**, not an alpha source.

## Artifacts

| File | Purpose |
|------|---------|
| `v4_trail_backtest.py` | Trail + gates + realgas |
| `_v4_trail_backtest.json` | first_swap rows + report |
| `_v4_trail_backtest_init_entry.json` | Initialize entry report |
| `_v4_pool_depth_probe.json` | 0.01 ETH depth cache |

## Repro

```bash
cd /opt/EATLQ/scripts/research/base_v4
python3 live_gate.py --check-open-pools
bash run_in_cgroup.sh python3 v4_trail_backtest.py --probe-depth
python3 v4_trail_backtest.py --init-entry   # optional Initialize entry
```

## For 委托人 (plain language)

- We tried V3’s full playbook on V4: only enter if slippage is OK, only deep pools, trail-stop exit, subtract gas/haircut like live.
- **Result:** still **0% winners**, typical loss ~**5% after gas**, no big upside tail.
- **V4 still cannot go live** for alpha. Next research would need a **different entry mechanism** (not roundtrip at Initialize/first-swap), not more exit tuning.
