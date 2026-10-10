# Aerodrome vAMM Backfill Recon

**Date**: 2026-08-19
**Nature**: Read-only feasibility recon. No DDL, no writes outside `/root/recon_out/`, no live trading.
**RPC budget**: 273 of 300 calls used. All against the archive endpoint in `/opt/EATLQ/infra/compose/.env`.
**Scripts**: `/root/recon_out/s01..s12*.py`, raw results in the matching `.json` files.

LIVE_TRADING = FORBIDDEN. This round produces no trading authorisation and promotes no candidate.

---

## 0. VERDICT

### `NOT_WORTH_BACKFILLING` for statistical power. Conditional `GO` only if D1/D2 (emissions) is actually going to be run.

The two decisive numbers:

| Question | Answer |
|---|---|
| **How many vAMM swaps in the 366-day panel?** | **38.9 M** (95% CI 30.7–47.1 M). Three independent estimates: 38.46 M, 38.91 M, 37.98 M. That is **9.2 % of the 424.4 M CL swaps already backfilled** and **71 % of Slipstream's 54.75 M**. This is a real venue, not dust. |
| **How many genuinely NEW independent tradeable assets would it add?** | **14.** Assets absent from `census.pools` entirely, present on **12 of 12** sampled full days spanning the year, and at **≤100 bps** one-way at $50. At the program's own ≤30 bps cheap tier the answer is **0**. |

**Why the large swap count does not translate into research value — three findings, in order of force:**

1. **The fee is a flat 30 bps and there is no tier choice.** 1,236 of 1,378 active volatile pools (89.7 %) sit at exactly 30 bps, and the factory permits **exactly one pool per `(token0, token1, stable)` triple** — verified: 28,591 pools map to 28,591 distinct triples, zero collisions. So a pair has at most a vAMM and an sAMM pool, never two fee tiers. This does two things: it puts the entire ≤30 bps cheap tier out of reach (2 pools of 897 clear it, both ETH-derivative crosses already in the panel), and it makes vAMM **structurally incapable of hosting C2**, the one mechanism the program has confirmed (334 independent blocks, ΔIL ≈ 0). C2 *is* within-pair tier selection; vAMM has no within-pair tiers.

2. **Half the volume is a correlated duplicate.** 49.2 % of measured vAMM volume sits on assets already in `edge.bars_universe_50`. AERO alone is **22.1 %** of vAMM volume across 68 pools. Only 19.5 % of volume is on assets absent from `census.pools`, spread thinly over 380 addresses.

3. **The arithmetic does not close.** The binding constraint is independent sample count; halving a confidence interval needs 4×. Adding 14 assets to the 138 cheap-and-feasible pools at $50 narrows a cross-sectional CI by **4.7 %** (38 assets → 11.5 %). Reaching 4× would need 552 pools. Round 40 already recorded that the universe path was infeasible; this measures exactly how infeasible. And for the case that actually binds — h=168 with 60–76 independent *blocks* — extra assets add **zero** independent blocks, because that count is a time quantity.

**The honest counter-argument, stated fairly**: the backfill is cheap (31,580 credits ≈ 0.32 % of a 10 M monthly quota, 4.6 GB disk against 73 GB free, ~30 min) and it would make **265 live-gauge pools carrying 67.9 % of vAMM volume** visible to the emissions line, which currently sees zero vAMM pools. If D1/D2 is genuinely next, pay for it. If the next round is another attempt at directional or market-making edge, do not.

---

## 1. The event

### Signature and topic0 — confirmed four independent ways

```
Swap(address indexed sender, address indexed to,
     uint256 amount0In, uint256 amount1In, uint256 amount0Out, uint256 amount1Out)

topic0 = 0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b
```

| # | Confirmation | Result |
|---|---|---|
| 1 | `keccak256("Swap(address,address,uint256,uint256,uint256,uint256)")` | `0xb3e27736…` — matches the ledger §5 prefix |
| 2 | Independent corroboration from the project's own `eatlq.aerodrome_vamm_swaps_mv` SELECT | filters `topic0 = '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'` |
| 3 | Real log pulled from chain and decoded (below) | 3 topics + 128 data bytes, exactly 4 words |
| 4 | `factory()` on all 1,485 pools that emitted it in the probes | 1,485 resolve to the Aerodrome PoolFactory; `isPool()` returns true 24/24 |

Not to be confused with Uniswap V2's `0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822`, which is `Swap(address,uint256,uint256,uint256,uint256,address)` — the same six parameters with `to` moved to last.

### The parameter layout, proven from reserve deltas rather than trusted from an ABI

Given this project's history with layout traps, I did not take the word order on faith. For three swaps that were the only swap in their pool in their block, I read `getReserves()` at `block-1` and at `block`:

| pool | data words `[w0,w1,w2,w3]` | reserve0 Δ | reserve1 Δ |
|---|---|---|---|
| `0xa6c7fbd1…` | `[0, 908993097450000000000, 37551981, 0]` | `−37,551,981` | `+906,266,118,157,650,000,000` |
| `0x85900924…` | `[22991825699492864000, 0, 0, 266331916777259166052]` | `+22,761,907,442,497,935,360` | `−266,331,916,777,259,166,052` |
| `0x2c490935…` | `[0, 6487172939337010000000, 2982779337586015, 0]` | `−2,982,779,337,586,015` | `+6,467,711,420,518,998,970,000` |

The **out** side matches the reserve delta to the unit in all three cases (`w2` ↔ reserve0, `w3` ↔ reserve1), which fixes the order as `(amount0In, amount1In, amount0Out, amount1Out)`.

**The in side does not match, and the gap is exactly the fee.** Case 1: `w1 − Δreserve1 = 2.727e18`, and `2.727e18 / 9.09e20 = 0.0030` = 30 bps. Case 3 gives 30 bps. Case 2 gives `2.299e17 / 2.299e19 = 0.0100` = 100 bps, and `getFee()` independently reports 100 bps for that pool.

> **Instrument note for whoever builds this.** Aerodrome skims the fee off the **input** and transfers it out of the pool, so the event reports the **gross** `amountIn` while the reserve only rises by `amountIn × (1 − f)`. Fee revenue is therefore `amountIn_gross × f` (convenient — this is the `Collect` substitute that CL never provided), but the **execution price is `amountOut / amountIn_gross`**. Using the net input would understate cost by exactly one fee.

### The materialized-view trap is real, and worse than documented

Per instruction I ran `SHOW CREATE TABLE` before touching anything ending in `_mv`:

```sql
CREATE MATERIALIZED VIEW eatlq.aerodrome_vamm_swaps_mv TO eatlq.uniswap_v2_swaps ...
WHERE topic0 = '0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b'
```

`eatlq.uniswap_v2_swaps_mv` also targets `eatlq.uniswap_v2_swaps`. So both venues write into one table.

**The reason this was invisible is that the two events are bit-identical on the wire**: both carry 3 topics (`topic1 = sender`, `topic2 = to`) and 4 uint256 data words in the order `amount0In, amount1In, amount0Out, amount1Out`. They differ *only* in topic0, which the target table does not store. Once mixed, the rows are **not separable by any column** — you must re-ask the factory which addresses are Aerodrome pools. The `source` column partially rescues it by accident: `source=''` isolates the 2 subscribed vAMM pairs (`0x6cdcb1c4…` = vAMM-USDC/AERO, 38,145 rows; `0x7f670f78…` = vAMM-WETH/AERO, 7,431 rows), while `discovery_poller` holds 25,784 UniV2 pairs. That matches the ledger's account of how 25,523 UniV2 pairs got misreported as Aerodrome.

### Factories

The authoritative list comes from the protocol itself, not from a deployments file:

```
Voter 0x16613524e02ad97eDfeF371bC883F2F5d6C480A5
  .factoryRegistry()          -> 0x5c3f18f06cc09ca1910767a34a20f771039e37c0
FactoryRegistry.poolFactories() -> 4 entries:
  0x420dd381b31aef6683db6b902084cb0ffece40da   <- PoolFactory  (vAMM + sAMM)  ** the debt **
  0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a   <- CL, already backfilled
  0xade65c38cd4849adba595a4323a8c7ddfe89716a   <- CL, already backfilled
  0xf8f2eb4940cfe7d13603dddd87f123820fc061ef   <- CL, already backfilled
```

**There is exactly one vAMM/sAMM factory.** The three CL factories are precisely the `CL_FACTORIES_8453` triple the ledger already documented, so this closes the "did we miss a legacy factory" question from on-chain protocol state rather than from a web source. Router (for any future execution test): `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`.

**topic0 `0xb3e27736…` is effectively Aerodrome-exclusive on Base**: 99.84 % of sampled logs (97,302 of 97,459) came from the PoolFactory. Two stray forks contributed 7 logs total (`0x0913205c…`, `0xf60cacf0…`) and 2 pools (150 logs) failed to resolve. Unlike UniV2's topic0, a bare topic0 backfill here needs no address filter.

---

## 2. Universe size

`PoolFactory.allPoolsLength()` = **28,591**. Fully enumerated via `allPools(i)`, then `metadata()` on every one (0 failures) through Multicall3 — 40 eth_calls for 57,182 sub-calls.

| | total | volatile (vAMM) | stable (sAMM) |
|---|---:|---:|---:|
| all pools | **28,591** | **27,894** | **697** |
| with a WETH leg | 21,414 | 21,309 | 105 |
| both reserves > 0 now | 28,170 | 27,495 | 675 |
| active in the 40 probe windows | 1,485 | 1,378 | 107 |
| distinct pools over 12 full days | 2,539 | — | — |

Distinct tokens across all Aerodrome vAMM/sAMM pools: **24,553**. Distinct token pairs: 28,236, of which only **355** offer both a vAMM and an sAMM pool.

Same shape as UniV3's 35,483 pools: a large nominal count, a small active core. 1,485 of 28,591 (5.2 %) traded at all in the sampled windows.

**Fee schedule** (`getFee(pool, stable)`, active pools):

| fee | volatile pools | | fee | stable pools |
|---:|---:|---|---:|---:|
| 1–28 bps | 9 (0.7 %) | | 1–5 bps | 92 |
| **30 bps** | **1,236 (89.7 %)** | | 15–30 bps | 12 |
| 60–100 bps | 123 (8.9 %) | | 100–200 bps | 3 |
| 200–300 bps | 10 (0.7 %) | | | |

`stableFee()` = 5, `volatileFee()` = 30. Aerodrome applies `amountIn × fee / 10000`, so the raw value is bps.

---

## 3. Measured swap volume

### Design (fixed before any result was seen)

- Panel = blocks **34,300,000 → 50,085,519** (`census.bf_progress` min/max), 15,785,520 blocks, 366 days.
- **Probe A**: 40 anchors evenly spaced, 1,000 blocks each = 40,000 blocks (0.253 %).
- **Control**: at every anchor, also probe the **CL topic0**, whose true count is known exactly (424,433,536). This converts the extrapolation from an assumption into a measurement — the CL error *is* the method's bias.
- **Probe B** (independent, for continuity): 12 anchors, **43,200 blocks each** (one full day) = 518,400 blocks (3.28 %), 60 calls, 0 failures.

### Result

| segment | vAMM | CL | vAMM/CL |
|---|---:|---:|---:|
| Q1 2025-08…11 | 42,483 | 373,867 | **0.114** |
| Q2 2025-11…02 | 24,887 | 299,750 | 0.083 |
| Q3 2026-02…05 | 16,215 | 186,303 | 0.087 |
| Q4 2026-05…08 | 13,874 | 203,133 | **0.068** |
| **total sampled** | **97,459** | **1,063,053** | **0.0917** |

```
Probe A, scale 394.64x:
  CL   extrapolated 419,521,110   vs true 424,433,536   -> bias -1.16%   <-- method validated
  vAMM extrapolated  38,461,025   bias-corrected 38,911,388   [95% CI 30,747,609 .. 47,075,168]
Probe B, 1,247,211 swaps / 518,400 blocks:
  vAMM extrapolated  37,976,600   <-- independent, agrees to 2.4%
```

**Best estimate: 38.9 M vAMM swaps over 366 days.** = 9.2 % of the backfilled CL total, 71 % of Slipstream's 54.75 M, and 8.4 % of a would-be Base all-AMM total.

### The venue is shrinking, and the recent end is the thinnest

First probe (2025-08-17) 7,235 swaps/1,000 blocks → last probe (2026-08) 1,301. **A 5.6× decline**, reproduced by Probe B (300,893 → 53,034 per day, 5.7×). vAMM's share of CL fell from 0.114 to 0.068 — Slipstream is eating it. So the rows land disproportionately in the *oldest* part of the panel, which is the least useful part for any forward-looking test.

### Exact calls run

```
s01  eth_blockNumber; multicall3 x1 (factory probes); eth_getLogs[vAMM] 50180197-50181697
     multicall3 x2 (factory()/token0/token1/stable/symbol on 24 pools; isPool x24)      =  5 calls
s02  eth_getLogs x40 [topic0=vAMM]  +  eth_getLogs x40 [topic0=CL], 1,000 blocks each   = 80 calls
s03  multicall3 x15  (factory/token0/token1/stable/symbol/getReserves on 1,489 pools)   = 15 calls
s04  factoryRegistry/poolFactories/allPoolsLength; allPools x12; metadata x24           = 40 calls
s06  stableFee/volatileFee; eth_getLogs x40 (per-window presence)                       = 42 calls
s07  multicall3 x3 (getFee x1,485); multicall3 x1 (getAmountOut x30)                    =  4 calls
s08  eth_blockNumber; multicall3 x5 (getReserves+getFee @ pinned); x1 (getAmountOut)    =  7 calls
s09  eth_getLogs x6 [vAMM, 10,000-block production chunks]                              =  6 calls
--   eth_getLogs x2 (direct bytes/log measurement, vAMM + CL control)                   =  2 calls
s11  multicall3 x4 (Voter.gauges x1,485; Voter.isAlive x567)                            =  4 calls
s12  eth_getLogs x60 [vAMM, 12 x 43,200-block full days]                                = 60 calls
--   eth_getLogs x1; eth_call x6 (getReserves at block-1/block, word-order proof)       =  8 calls
                                                                          TOTAL         = 273 calls
```

---

## 4. Backfill cost

### Reference point (measured, not estimated)

From `base_truth_panel_r0/out/backfill_year.log` + `census.bf_progress` + `system.parts`:

| | 94.6-day run | year extension | **total CL** |
|---|---:|---:|---:|
| chunks (10,000 blocks) | 409 | 1,170 | **1,579** |
| rows | 85,260,199 | 339,173,337 | **424,433,536** |
| download | 75.8 GB | 302.48 GB | **378.3 GB** |
| wall clock | 56.6 min | 243.6 min | **300.2 min** |
| credits | 8,160 | 23,400 | **31,560** |

`census.bf_progress` has **0** intervals whose `from_block % 10000 != 0`, so the adaptive-halving path never fired: **exactly one `eth_getLogs` per chunk**. Download 892 bytes/CL-log (matches my direct measurement of 892 exactly). Disk: 46.45 GiB compressed / 424,433,536 rows = **117.5 bytes/row**.

### vAMM over the same 366 days

| item | estimate | basis |
|---|---:|---|
| `eth_getLogs` calls | **1,579** | same window, same 10,000-block chunk; vAMM is 11× sparser than CL so halving is even less likely |
| **credits** | **31,580** | QuickNode Base = 20 credits/call, flat, for every method |
| rows | **38.9 M** [30.7–47.1 M] | CL-validated extrapolation, corroborated by a 12-full-day probe |
| download | **32.2 GB** | **828 bytes/log measured directly** (26,802 logs in one 10,000-block call; CL control in the same script returned 892, reproducing the logged value) |
| ClickHouse disk | **4.6 GB** | at cl_swaps' 117.5 B/row; vAMM stores 4 numeric columns vs CL's 5, so this is a mild over-estimate |
| wall clock | **~26–40 min** | 32.2 GB at the CL run's measured 20.7 MB/s aggregate (4 workers) = 26 min; per-chunk latency bound 1,579 × 4.5 s / 4 = 30 min; add insert overhead |

**Relative to the CL year backfill: 100 % of the credits, 8.5 % of the download, 9.2 % of the rows.**

Credits do not scale down with sparsity — the call count is set by the block range, not the log count. That is the whole cost story: **you pay the same 31,580 credits as the CL backfill to get 9 % as many rows.** In absolute terms it is still cheap (0.32 % of a 10 M monthly quota, and 73 GB is free on `/`).

---

## 5. Overlap analysis: new assets vs assets already in the panel

Independence is counted per **alt asset** (token address), not per pool — a second pool on an asset already in the panel is a correlated observation. Panel references: `edge.bars_universe_50` = 411 pools / **305 distinct alts**; `census.pools` = 38,767 pools / **34,773 distinct tokens**.

### Volume-weighted overlap (Probe A, 1,485 active Aerodrome pools, 97,302 swaps)

| bucket | assets | share of alt-bearing vAMM volume | share of all vAMM volume |
|---|---:|---:|---:|
| already in `edge.bars_universe_50` | **136** | **53.6 %** | **49.2 %** |
| in `census.pools` but not the panel | 358 | 25.3 % | 23.2 % |
| **absent from `census.pools` entirely** | **380** | **21.2 %** | **19.5 %** |
| quote/quote pools (WETH-USDC etc., add no alt) | — | — | 8.1 % |

Concentration is extreme: **AERO alone is 22.1 %** of Aerodrome volume (21,535 sampled swaps, 68 pools), followed by VIRTUAL 6.3 %, VVV 3.3 %, wstETH 2.0 %, KTA 1.7 %, cbBTC 1.7 %. The top of the volume distribution is almost entirely assets the panel already trades.

### After the cost gate (validated closed form, one-way, $50)

Cost uses the program's own definition (`cost_model.py` L19: `1e4·fee_rate + 1e4·slip`, one-way). For constant product, `slip = dx/x` against the quote-side reserve. **Validated against `Pool.getAmountOut` at a single pinned block, 30 pools: measured − model = p10 −0.071, p50 −0.027, p90 −0.000 bps.** Stable pools are quoted, never modelled (sAMM uses `x³y + y³x = k`).

| gate | pools | assets | swap share | of which NEW |
|---|---:|---:|---:|---:|
| ≤30 bps | **2** | **2** | 0.0 % | **0** |
| ≤50 bps | 68 | 62 | 35.1 % | 18 |
| ≤100 bps | 129 | 120 | 44.3 % | 45 |
| ≤300 bps | 221 | 206 | 63.5 % | 88 |

The only two pools under 30 bps are **`0x6b87b866…` vAMM-WETH/bsdETH (fee 1 bps, cost 2.01)** and **`0x91f0f349…` vAMM-weETH.base/WETH (fee 5 bps, cost 5.74)** — both custom-fee ETH-derivative crosses, both already in the panel. bsdETH is the same asset Round 38 flagged as *cheap in the wrong place* (2.0 bps toll against 0.4 bps hourly volatility, ratio 2.82, among the worst in the universe) and the same pool Round 40 caught printing `1.0001^-887272`.

### After the continuity gate (Probe B: 12 full days across the year)

| gate | all | in panel | census-only | **new** |
|---|---:|---:|---:|---:|
| present on any of 12 days | 693 | 133 | 334 | 226 |
| present on **all 12** days | 185 | 60 | 93 | 32 |
| all 12 days **and** ≤100 bps | **64** | 26 | 24 | **14** |
| all 12 days and ≤100 bps and ≥24 swaps/day | 61 | 26 | 23 | 12 |
| all 12 days **and ≤30 bps** | **2** | **2** | 0 | **0** |

### The 14 genuinely new tradeable candidates

| asset | symbol | swaps/day | one-way $50 |
|---|---|---:|---:|
| `0xe014d2a4…` | SOGNI | 1,312.2 | 34.2 |
| `0x93dc5cb3…` | MET | 796.0 | 31.6 |
| `0x739f9350…` | LMY | 584.8 | 34.9 |
| `0xe67f39fb…` | ANYONE | 136.2 | 47.3 |
| `0x391359ab…` | SUPER | 126.0 | 65.5 |
| `0xca4c2e10…` | SOVRN | 122.1 | 43.5 |
| `0x9cd44eca…` | SPARKLET | 115.8 | 67.7 |
| `0x084382d1…` | MODE | 57.8 | 83.8 |
| `0x844b40db…` | $YEEHAW | 50.7 | 90.1 |
| `0xf757c980…` | REZ | 49.4 | 90.5 |
| `0xe061aa40…` | XVGBASE | 45.8 | 84.1 |
| `0xedc68c4c…` | JAV | 43.9 | 72.3 |
| `0xb1e1f3cc…` | CXT | 16.9 | 60.8 |
| `0xef73611f…` | Kendu | 13.2 | 60.4 |

Plus **24 census-only** assets (CL price history exists, but they never made `bars_universe_50`) and **26 already-in-panel** assets that would gain a second venue.

### The power arithmetic

| base universe | +14 new | +38 (new + census-only) | 4× target |
|---|---:|---:|---:|
| 138 cheap+feasible ≥90 d pools @ $50 | CI −4.7 % | CI −11.5 % | 552 pools |
| 64 cheap+feasible ≥300 d @ $50 | CI −9.4 % | CI −20.8 % | 256 pools |
| 411 `bars_universe_50` pools | CI −1.7 % | CI −4.3 % | 1,644 pools |

And for the constraint that actually binds — h=168 with **60–76 independent blocks** — the gain is **zero**, because that count is set by panel length, not by cross-sectional width.

---

## 6. Secondary value, assessed

**(a) Emissions (D1/D2) — this is the strongest pro-backfill argument.**
`census.aero_gauge_map` holds 157 pools, **157/157 of which are CL pools and 0 of which are vAMM**. So the concurrent emissions line is entirely blind to vAMM. Asking `Voter.gauges(pool)` directly: **567 of 1,485** active vAMM/sAMM pools have a gauge, **265 are `isAlive`** (currently receiving emissions), and those carry **67.9 % of vAMM swap volume**. Top live-gauge pools include vAMM-USDC/AERO (8,210 swaps, 40/40 windows), vAMM-WETH/AERO, vAMM-AERO/TAROT, vAMM-WETH/VVV. A backfill would roughly triple the gauge-bearing pool count available to that line. Caveat: only 57 of the 265 are ≤100 bps, and only 7 of those have an alt that is genuinely new — so the emissions value is in *breadth of gauge observations*, not in new tradeable assets.

**(b) sAMM stable pools (C3) — essentially nothing.** This one I expected to be positive and it is not. 697 sAMM pools exist but only **107 were active**, carrying **5.42 %** of Aerodrome volume, and only **5** appear in ≥30 of 40 probe windows:

| pool | symbol | fee | windows | swaps | class |
|---|---|---:|---:|---:|---|
| `0x35480296…` | sAMM-WETH/USDC | 5 | 40 | 3,133 | quote-only |
| `0x44d7dd23…` | sAMM-WETH/USDbC | 5 | 32 | 489 | quote-only |
| `0xffa80952…` | sAMM-BRETT/AERO | 5 | 38 | 302 | already in panel |
| `0xde4fb30c…` | sAMM-WETH/msETH | 5 | 36 | 121 | already in panel |
| `0xcefc8b79…` | sAMM-msUSD/USDC | 5 | 33 | 107 | census-only |

Of 63 distinct sAMM alts, 28 are already in the panel, 28 are census-only, **7 are new** — and none of the 7 is continuously active. **C3 gains no new structurally-quiet asset from vAMM.**

I checked the pre-registered C3 candidate list directly against the full enumeration rather than assuming. The pools **do exist** — 19 candidates (cbBTC, cbXRP, uXRP, cbADA, uADA, cbLTC, uSOL, uDOGE, uSUI, tBTC, LINK, AAVE, MORPHO, wstETH, cbETH, weETH, bsdETH, msETH, rETH) hold **29 sAMM pools**, 8 of them with a WETH leg (bsdETH, cbETH, cbLTC, msETH, rETH, tBTC, wstETH), plus a large vAMM count (cbBTC alone has **156** vAMM pools, cbETH 39, wstETH 31, cbXRP 28, tBTC 28). But **only one of those sAMM pools registered any activity** in either probe — `sAMM-WETH/msETH`, at 121 swaps and 36/40 windows. So the correction to make is precise: Aerodrome *has* wrapped-major pools outside Slipstream, they are simply **not traded**. A backfill would add pool rows, not usable observations. And because there is one pool per `(t0,t1,stable)` triple, C3 could not do tier selection on them either.

**(c) Cross-venue second price series.** 136 assets already in `bars_universe_50` also trade on vAMM; 31 of them at ≤100 bps at $50. Arbitrage is closed (`ARB_CLOSED_BY_BOTS`, 86/86 atomic samples negative), so the value is only an independent price series for cross-checking — e.g. a second opinion on the CLMM tick-limit bad-pool class Round 40 found. Real but small, and a constant-product pool cannot hit `MIN_TICK`, which is mildly useful as a control.

---

## 7. The cheapest KILL or GO decision experiment

**The KILL criterion is already met, at 273 RPC calls (~5,460 credits) — this recon is that experiment.** Two facts, both cheap and both decisive:

1. **0 of 693** vAMM alt assets reach the program's ≤30 bps cheap tier, because 89.7 % of active volatile pools are locked at exactly 30 bps and the factory permits **one pool per `(t0,t1,stable)` triple** (28,591 pools = 28,591 triples, verified). Round 41 established that at $50 the cost *is* the fee tier and that the decisive ratio (toll / σ) has a ~0.5 floor. A venue with a flat 30 bps floor and **no within-pair tier alternative** cannot improve that ratio and cannot host C2's confirmed mechanism.
2. **14 new continuously-traded ≤100 bps assets** against a requirement of 4× the sample count. That is a 4.7 % CI narrowing where 50 % is needed.

**If someone wants to overturn this, the cheapest confirming experiment is:**

- **Test**: two-sided atomic round-trip via the Aerodrome Router `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43` with state-override (the R2 / `base_f03_atomic_r0` engine, already built and validated), on the 14 new + 26 in-panel candidates, at 10 historical blocks spread across the panel, both directions.
- **Cost**: 40 assets × 2 legs × 10 blocks = **800 `eth_call` ≈ 16,000 credits, ~15 min**. A 20-asset version is 8,000 credits. For comparison the arb kill spent 346 calls / 6,900 credits.
- **Pre-registered GO rule**: GO only if **≥30 new assets** hold two-sided one-way cost ≤100 bps on ≥90 % of the 10 blocks. 30 is the number that would produce a **17.5 %** CI narrowing on the 64-pool ≥300 d base — the smallest gain worth 31,580 credits. My measurement says the true value is 14, so this should fail.
- **Why even a perfect result cannot justify it on power grounds**: 14 → 138+14 is arithmetically fixed. A GO can only come from the emissions line.

**Cheaper still, if the only question is D1/D2**: skip the swap backfill entirely and backfill *gauge and vote events* for the 265 live-gauge vAMM pools. Emissions research needs `NotifyReward` / `Voted` / `Deposit` logs, not every swap. Those are far sparser than 38.9 M swaps and can be pulled by topic0 with an address filter over the same 1,579 chunks — **same 31,580 credits but ~0.2 GB instead of 32 GB**, and it delivers the actual input D1/D2 needs. **This is the recommendation.**

---

## 8. What I could not determine — stated honestly

1. **No sell-leg test. This is the largest gap.** Ledger error #3 (BGPT buy 1.2 bps / sell 10,000 bps) is exactly this failure. Constant product is curve-symmetric, so a CL-style one-sided pool cannot exist here — but a **transfer-taxed token** would still trap, and I tested **zero** of the 14 new assets for transfer tax. `Pool.getAmountOut` is a pure reserve calculation and would not reveal a tax; only a real transfer simulation would.
2. **Depth is single-block.** All reserves come from one pinned block near the panel end. The ledger measured that single-block depth can be off **3–15×** (HOME: 60 bps single-block vs 225.7 median over 30 days). R2 needed 60 historical time points to settle this; I used one. My cost table should be read as a screen, not a measurement.
3. **No atomic execution test.** I never confirmed the Aerodrome Router can actually route these pools. The ledger notes SwapRouter02 cannot, and that Aerodrome needs its own router — three f03 shadows died on exactly this.
4. **Continuity is 12 sampled days, not 366.** An asset could be dead for a 30-day stretch between my samples and still score 12/12. The "≥90 tradeable days" figure the panel uses is not something I measured.
5. **Row count CI is wide**: [30.7 M, 47.1 M] from between-window variance, though three independent methods land within 2.4 % of each other (38.46 / 38.91 / 37.98 M), which suggests the true uncertainty is much tighter than the formal CI.
6. **sAMM cost is uncharacterised.** I refused to apply the constant-product form to stable pools and did not implement the `x³y + y³x = k` solution, so there is no sAMM cost distribution here. Given sAMM is 5.4 % of volume with 7 new assets, none continuous, I judged this not worth the calls.
7. **The 380 "genuinely new" assets are new *to `census.pools`*.** They could still be correlated with panel assets through a common factor (most are Base micro-caps that co-move with AERO/VIRTUAL). I measured address-level novelty, **not statistical independence**. The true independent-sample gain is therefore **≤14**, possibly well below it. This cuts against the backfill, so the verdict is conservative in the right direction.
8. **One measurement I discarded**: a concurrent 6-chunk throughput run reported 2,660 bytes/log, 3.2× the true value, from a race on the shared byte counter. The 828 B/log figure used here is a single-threaded direct measurement whose CL control reproduced the logged 892 B/log exactly.
9. **I did not verify the 2 unresolved probe pools** (150 logs, 0.15 %) or the two stray forks. Too small to move anything.

---

## 9. Reproduction

```bash
cd /root/recon_out
python3 s01_identify.py       # topic0 + factory + decode a real log        (5 calls)
python3 s02_probe.py          # 40x1,000-block probes, vAMM + CL control    (80 calls)
python3 s03_attribute.py      # factory attribution of every probed pool    (15 calls)
python3 s04_universe.py       # full 28,591-pool enumeration + metadata      (40 calls)
python3 s06_tradeability.py   # per-window presence, first-pass gates        (42 calls)
python3 s08_cost_pinned.py    # pinned-block cost + closed-form validation    (7 calls)
python3 s09_backfill_cost.py  # production-chunk throughput                   (6 calls)
python3 s11_gauges_samm.py    # Voter.gauges / isAlive, sAMM alt set          (4 calls)
python3 s12_continuity.py     # 12 full days + triple-uniqueness proof       (60 calls)
```

Needs the archive RPC from `POOL_DISCOVERY_RPC_URL` in `/opt/EATLQ/infra/compose/.env`. Every script is read-only: `SELECT` against ClickHouse, `eth_call` / `eth_getLogs` / `eth_blockNumber` against the RPC. No DDL, no INSERT, no writes outside `/root/recon_out/`.

---

**LIVE_TRADING = FORBIDDEN. This report produces no trading authorisation and promotes no candidate.
The 14 new assets are a screen output, not a tradeable set — they have not passed a sell leg,
a tax check, a cross-block depth test, or an execution-path test.**
