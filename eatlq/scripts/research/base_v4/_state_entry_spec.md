# V4 State-Transition Entry (Demand Wave 2)

## Problem with current entry

Initialize / first_swap answers: *"Can I roundtrip at pool birth?"*
That measures **hook friction at genesis**, not **demand migration**.

## Target question

*"After natural selection, did a second cohort pay a higher price?"*

## Observable proxies (archive RPC + anvil)

| Signal | Proxy | Source |
|--------|-------|--------|
| Not pure friction | roundtrip PnL improves vs early entry | anvil replay |
| Survived wave1 sell pressure | swaps continue after wave1 window | Swap logs |
| Second-wave demand | ≥2 distinct WETH-in senders post-wave1 | Swap logs |
| Price recovery | sqrtPrice ≥ wave1_low + 2% | Swap logs |
| LP held | no ModifyLiquidity remove in wave1 | ModifyLiquidity logs |
| Deployer not dumping | init tx.from WETH-out ≤ 2 in wave1 | tx.from + Swap |
| Routing / bot quality | exclude Universal Router from buyer count | sender filter |

## Entry block

First block where all wave2 gates pass (see `v4_state_entry.py`).

## Not yet modeled (future)

- Aggregator route quality score
- Non-bot buyer classifier (nonce / funding graph)
- Tick-path trail exit (reuse V3 path once entry timing fixed)

## Repro

```bash
cd /opt/EATLQ/scripts/research/base_v4
python3 live_gate.py --check-open-pools
bash run_in_cgroup.sh python3 v4_state_entry.py --detect --pnl
python3 v4_state_entry.py --summarize
```

## v1 results (112 clean, 2026-06-25)

| Stage | n | Note |
|-------|---|------|
| wave2 confirmed | **7 / 112** (6%) | entry ~500–5000 blocks after init |
| too_few_swaps | 63 | swap index only sees recent activity window |
| no_wave2 / dead / lp_pull | 42 | filtered by design |

**PnL @ state entry:** median **−2.4%**, positive **0%** (n=7).  
vs first_swap: median delta **0** — later entry does not create upside on this sample.
