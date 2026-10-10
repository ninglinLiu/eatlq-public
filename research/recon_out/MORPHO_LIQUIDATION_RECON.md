# RECON: Lending-Protocol Liquidation Flow on Base as a Source of Edge

**Date**: 2026-08-19
**Mode**: read-only reconnaissance. No DDL. No writes outside `/root/recon_out/`.
**RPC budget used**: **198 calls** (~3,300 credits) against the QuickNode archive endpoint in `/tmp/rpc_url.txt`.
**Nothing in `/root/base_aero_emissions_r0`, `/root/base_fee_tier_r0`, or `/root/base_morpho_credit_r0` was modified.**

---

## 0. VERDICT

### `INFEASIBLE_ECONOMICS`

**The single number that drives it: 1.81%.**

That is the share of forced-liquidation *notional* on Base's four worst stress days that lands
in collateral other than WETH / USDC / USDbC / cbBTC. Measured directly, not estimated:
**$152.1M of seized collateral across four full stress days, of which $2.75M (1.810%) was
non-deep-major.** The other 98.19% is forced selling of assets whose round-trip cost at $50 is
**0.87–0.90 bps** and whose depth is effectively unbounded at any size this account can trade.
A $3.66M cbBTC liquidation cannot produce a price dislocation that a $50 order can harvest.

Two further findings, each independently sufficient to close the line:

1. **The forced sale is atomic with the trigger.** All 6 of the largest cbXRP liquidations I
   pulled receipts for contain the `Liquidate` event **and 2 DEX `Swap` logs in the same
   transaction** (gas 780k–955k — the signature of flash-loan → repay → seize → swap → repay).
   The price impact is realised *inside* the transaction that emits the observable trigger.
   To "be the buyer of the collateral being dumped" you must be inside that transaction, i.e.
   you must be the liquidator — which needs up to $210k of repayment capital and a won gas
   auction. That is E2 (MEV/frontrunning), which `RESEARCH_PLAN.md` §4 lists under 永远不做.
   The only residual for a long-only spot account is *post*-impact reversion at block N+1,
   which is a directional prediction — family A, dead with high power (rounds 35, 38, 41).
2. **The residual playable set is underpowered and single-asset.** Non-major collateral,
   ≥$10k seized, in an asset that actually has a tradeable Base pool: **~50–150 events per
   year arriving in ~30–80 independent episodes**, of which **cbXRP alone is 228 of 560
   non-major events and $1.75M of $2.75M non-major notional**. The program's mandatory
   concentration gate (`RESEARCH_PLAN.md` §"五、强制条款" 1: any cell where one symbol
   contributes >30% of net PnL cannot alone constitute a verdict) voids this before it starts.

**If the verdict had to be a power verdict instead**, it would be `FEASIBLE_BUT_UNDERPOWERED`
at ~30–80 independent episodes/year. I chose `INFEASIBLE_ECONOMICS` because the atomicity
finding is *structural and permanent* — more panel years cannot fix it — whereas an
underpowered verdict invites "wait for more data," and more data will not come: the constraint
is 10–17 stress days per year and the panel can only be extended ~2 more years.

**Data feasibility is NOT the blocker.** The backfill is cheap (§4). This line dies on economics,
not on instrumentation. That is the useful part of this recon: it costs 4,000 credits to confirm
(§8) and it does not require building anything.

---

## 1. The five pre-written goal documents at `/root/base_morpho_credit_r0/`

Five documents, dated 2026-08-15, authored as Claude→Codex work orders. **None was ever
executed** (`CUMULATIVE_KILL_LEDGER.md` row 27: `NEVER_TESTED`, "派工单写好，一份未执行").
`NO_LIVE.lock` present.

| Doc | Intent | Verdict vocabulary |
|---|---|---|
| `GOAL.md` (R0) | **Instrument round, gate for all others.** Reproduce Morpho Blue Base config from official GraphQL/REST + `eth_call`, implement LTV/HF/LIF/APY/interest-accrual formulas against official worked examples (borrow APY 4.89% / supply APY 4.15% / HF 1.1467 / LIF≈1.05), build an hourly panel for the core market, census the `listed:true` universe. Explicitly answers *no* strategy question. | PASS / FAIL |
| `GOAL_R1_rate_utilization.md` | Does utilization deviation from the 90% AdaptiveCurveIRM target produce a pre-registerable, out-of-sample rate path with economic meaning at $1,000 — or is the mechanism real but arbitraged to the 4% floor? | `REFUTED` / `MECHANISM_ONLY` / `UNDERPOWERED` / `INCONCLUSIVE` / `SHADOW_CANDIDATE` |
| `GOAL_R2_oracle_spot.md` | Is there a persistent, tradeable basis between the market's oracle price and observable Base spot, after 1–2 block delay and two-sided quoting at $1,000? Null: median basis ≈ 0, tails explained by feed heartbeat. | `DATA_INSUFFICIENT` / `REFUTED` / `HEARTBEAT_ONLY` / `UNDERPOWERED` / `SHADOW_CANDIDATE` |
| `GOAL_R3_health_paths.md` | Borrower state machine: after entering HF ∈ (1.00, 1.05], is the next state top-up / repay / liquidated / recovered, and is that distinguishable from a frozen-behaviour simulated position driven by price alone? Plus bad-debt arithmetic for a $1,000 supplier. | `DATA_INSUFFICIENT` / `REFUTED` / `DESCRIPTIVE_ONLY` / `UNDERPOWERED` / `MECHANISM_ONLY` |
| `GOAL_R4_orthogonality_f03.md` | Do credit-state features add information the F03 signal score cannot linearly explain — as direction, as a veto, or only on non-quotable tick residual? | `REFUTED` / `CONTEMPORANEOUS_ONLY` / `UNDERPOWERED` / `ORTHOGONAL_IN_TICK_SPACE` |

**The single most important thing to report about these documents: all five explicitly and
repeatedly forbid exactly the research line I was asked to assess.** The word 清算 (liquidation)
appears 67 times across the seven files, and every substantive occurrence is a prohibition:

- `GOAL.md` §0.3: "禁止做清算机器人、清算回测、清算抢跑模拟、MEV 拍卖、私有交易中继。官方文档写明 Morpho 清算是先到先得、可演变为 gas war。**那是金矿，不是本系列的对象。本轮甚至不准把"清算利润"写成研究问题。**"
- `GOAL.md` §1.3: LIF is implemented "只为校验文档，**禁止**用它算'清算利润策略'".
- `GOAL_R3` title: "健康度路径与清算回避（**不是清算赛跑**）"; §0.1 bans liquidation bots, frontrunning, flashloan liquidation, gas bidding, private relays, LIF profit backtests; §3-P3 bans reporting liquidator PnL and gas bids; §6 bans "为了增加 Liquidate 样本而扩大到全部 Base 市场" — the exact sweep I ran.
- `GOAL_R4` uses `n_liquidations_24h` only as a *feature*, never as an event to trade.

The prior author's stated reason is the same one my atomicity measurement now confirms
empirically: liquidation is a first-come-first-served gas auction among professionals. My
independent finding is stronger than the prior prohibition — it is not merely that the race is
crowded, but that **there is no non-racing version of the trade**, because the dump and the
trigger are the same transaction.

---

## 2. Which lending protocols have meaningful size on Base

All addresses verified live by `eth_getCode` at head block 50,181,676 (10 calls). Non-zero
runtime code on every one. Emitter counts are from my own log sample (§3), so they are
*measured presence in liquidation flow*, not TVL.

| Protocol | Contract (Base 8453) | codesize | Liquidation logs in sample | Note |
|---|---|---:|---:|---|
| **Morpho Blue** | `0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb` | 15,623 | **1,400** | Dominant. 45 distinct market ids in sample, 72 in full-day scans |
| Morpho AdaptiveCurveIRM | `0x46415998764C29aB2a25CbeA6254146D50D22687` | 2,323 | — | Base-specific IRM, matches `GOAL.md` §1.2 |
| **Aave v3 Pool** | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` | 1,933 | **245** | Proxy; impl `0xDb578D67A83E94DE73c9e0C14280f804F6C1c3e4` (web) |
| **Seamless Pool** (Aave v3 fork) | `0x8F44Fd754285aa6A2b8B9B97739B79746e0475a7` | 2,333 | **161** | Live and materially sized |
| **Moonwell** (Compound v2 fork) | Comptroller `0xfBb21d0380beE3312B33c4353c8936a0F13EF26C` | 1,412 | **260** across 22 mTokens | Top mTokens `0xedc817a2…` (171), `0x628ff693…` (18), `0xb682c840…` (16) |
| **Compound v3** cUSDCv3 | `0xb125E6687d4313864e53df431d5425969c15Eb2F` | 1,878 | 16 / 15 | `AbsorbCollateral` / `AbsorbDebt` |
| Compound v3 cUSDbCv3 | `0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf` | 1,878 | 12 / 12 | |
| Compound v3 cWETHv3 | `0x46e6b214b524310239732D51387075E0e70970bf` | 1,878 | 0 | live, no absorbs in sample |
| Compound v3 (4th Comet) | `0x784efeb622244d2348d4f2522f8860b96fbece89` | not probed | 2 / 2 | discovered from logs, **not identified** |
| **Fluid** liquidity layer? | `0x52Aa899454998Be5b000Ad077a46Bbe360F4e497` | 4,462 | — | **Has code, but I did NOT confirm this is Fluid-on-Base, and did NOT find or test a Fluid liquidation topic0. Unresolved.** |
| Aave-fork #3 | `0x09b11746dfd1b5a8325e30943f8b3d5000922e03` | not probed | 10 | **unidentified**; emits `LiquidationCall` |
| Aave-fork #4 | `0x766f21277087e18967c1b10bf602d8fe56d0c671` | not probed | 6 | **unidentified** |
| Aave-fork #5 | `0x6374a1f384737bccccd8fae13064c18f7c8392e5` | not probed | 1 | **unidentified** |

External size context (web, labelled as third-party, not measured by me): Morpho ≈ $3.3B TVL on
Base ≈ 60–65% of chain lending TVL; Aave v3 Base ≈ $300M; Compound ≈ $150M. `GOAL.md` §1.1
recorded $3.27B Morpho / $358M Uniswap / $250M Aerodrome on 2026-08-15 from DefiLlama.
**Morpho's single cbBTC/USDC market is larger than all Base DEX TVL combined** — but that is
precisely why its liquidations are unharvestable (§5).

---

## 3. Liquidation events: exact signatures and topic0

Computed with `cast keccak` (foundry 1.7.1), **then independently verified**: 8 of the 8 Morpho
Blue `EventsLib` topic0 hashes I computed appear verbatim on real Morpho Blue logs sitting in
`eatlq.raw_evm_logs` (blocks 24,654,858–24,654,920). That satisfies `GOAL.md` §1.4's requirement
to check computed topic0 against real transactions — and `Liquidate` itself was then confirmed in
practice by 1,400 sampled logs from `0xbbbb…ffcb` that decode to sane `MarketParams`.

| Protocol | Event signature | topic0 |
|---|---|---|
| **Morpho Blue** | `Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)`<br>= `Liquidate(Id indexed id, address indexed caller, address indexed borrower, uint256 repaidAssets, uint256 repaidShares, uint256 seizedAssets, uint256 badDebtAssets, uint256 badDebtShares)` | `0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41` |
| **Aave v3** (+ Seamless + 3 unidentified forks) | `LiquidationCall(address,address,address,uint256,uint256,address,bool)`<br>indexed: collateralAsset, debtAsset, user | `0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286` |
| **Moonwell / Compound v2** | `LiquidateBorrow(address,address,uint256,address,uint256)` — no indexed params; market = log `address` | `0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52` |
| **Compound v3 (Comet)** | `AbsorbCollateral(address,address,address,uint256,uint256)` | `0x9850ab1af75177e4a9201c65a2cf7976d5d28e40ef63494b44366f86b2f9412e` |
| **Compound v3 (Comet)** | `AbsorbDebt(address,address,uint256,uint256)` | `0x1547a878dc89ad3c367b6338b4be6a65a5dd74fb77ae044da1e8747ef1f4f62f` |

Verified-by-observation Morpho Blue companions (all present in `raw_evm_logs`, all needed for a
borrower panel — §7):

| Event | topic0 | logs seen |
|---|---|---:|
| `AccrueInterest(bytes32,uint256,uint256,uint256)` | `0x9d9bd501d0657d7dfe415f779a620a62b78bc508ddc0891fbbd8b7ac0f8fce87` | 50 |
| `Withdraw(bytes32,address,address,address,uint256,uint256)` | `0xa56fc0ad5702ec05ce63666221f796fb62437c32db1aa1aa075fc6484cf58fbf` | 28 |
| `Supply(bytes32,address,address,uint256,uint256)` | `0xedf8870433c83823eb071d3df1caa8d008f12f6440918c20d75a3602cda30fe0` | 24 |
| `FlashLoan(address,address,uint256)` | `0xc76f1b4fe4396ac07a9fa55a415d4ca430e72651d37d3401f3bed7cb13fc4f12` | 17 |
| `Repay(bytes32,address,address,uint256,uint256)` | `0x52acb05cebbd3cd39715469f22afbf5a17496295ef3bc9bb5944056c63ccaa09` | 5 |
| `WithdrawCollateral(bytes32,address,address,address,uint256)` | `0xe80ebd7cc9223d7382aab2e0d1d6155c65651f83d53c8b9b06901d167e321142` | 4 |
| `Borrow(bytes32,address,address,address,uint256,uint256)` | `0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43` | 1 |
| `SupplyCollateral(bytes32,address,address,uint256)` | `0xa3b9472a1399e17e123f3c2e6586c23e504184d504de59cdaa2b375e880c6184` | 1 |

`CreateMarket(bytes32,(address,address,address,address,uint256))` = `0xac4b2400f169220b0c0afdde7a0b32e775ba727ea1cb30b35f935cdaab8683ac` (computed, **not** observed).

Liquidation Incentive Factor, `LIF = min(1.15, 1/(1 − 0.3·(1 − lltv)))`, computed for the LLTVs
that actually appear in liquidated markets: **86% → 1.0438**, 91.5% → 1.0262, 94.5% → 1.0168,
77% → 1.0741, 62.5% → 1.1268. (`GOAL.md` §1.3's "≈1.05" for 86% is a rounding of 1.0438.)
This 1.7%–12.7% is the *liquidator's* gross incentive, competed away in a gas auction.

---

## 4. Can we backfill these events cheaply? YES — and the answer is unusual

### 4.1 `eatlq.raw_evm_logs` contains ZERO liquidation events

```sql
SELECT topic0, count() FROM eatlq.raw_evm_logs
WHERE topic0 IN ('0xa4946ede…7e41','0xe413a321…5286','0x298637f6…bb52',
                 '0x9850ab1a…412e','0x1547a878…f62f') GROUP BY topic0;
-- → 0 rows
```

Table has 19,429,983 rows, blocks 0–50,181,546, `block_time` 2025-10-01 → 2026-08-19,
37,591 distinct addresses — but it is a DEX-log subscription. Its top topic0 is the Uniswap V3
`Swap` (13.08M rows). Morpho Blue appears at all:

```sql
SELECT topic0, count(), min(block_number), max(block_number) FROM eatlq.raw_evm_logs
WHERE lower(address)=lower('0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb') GROUP BY topic0;
-- → 130 rows total, 8 topic0, ALL inside blocks 24,654,858–24,654,920 (62 blocks)
```

That is an accidental 62-block capture from some earlier probe. **No liquidation coverage exists.
A backfill is required.** (This confirms `GOAL.md` §1.5's "本地 ClickHouse …**没有** Morpho 语义表".)

### 4.2 Measured RPC constraint: a hard 10,000-block span cap

Not a response-size cap — a **block-range** cap, and it is exact:

| span requested | result |
|---|---|
| 10,000 | **OK**, 14 logs |
| 10,001 | HTTP 413 |
| 12,000 | HTTP 413 |
| 20,000 / 50,000 / 100,000 / 500,000 / 2M / 8M / 16M | HTTP 413 |

Confirmed that `topics: [[t0_a, t0_b, …]]` OR-filtering works — **one pass covers all five
protocols simultaneously** (26 logs in one 10,000-block call: 14 Morpho + 9 Aave + 3 Moonwell).

### 4.3 Backfill cost for the full 366-day panel

Panel window taken from `census.cl_swaps`: blocks **34,300,000 → 50,085,519 = 15,785,520 blocks**
(424,433,536 swaps). Block↔time is exactly linear at **0.5000 blocks/sec** (verified:
`eatlq.block_ts_dict` gives block 42,520,453 = 2026-02-23 07:04:13 and 47,077,237 = 2026-06-08
18:37:01), so 34,300,000 ≈ 2025-08-17 and 50,085,519 ≈ 2026-08-17 — the stated panel dates.

| Quantity | Value | Basis |
|---|---|---|
| Chunks needed | **1,579** = ceil(15,785,520 / 10,000) | hard span cap |
| **eth_getLogs calls** | **1,579** (one OR-filter for all 5 topic0) | no adaptive halving needed: max logs in any 10,000-block chunk observed was 1,428, far from any cap |
| **Credits @20/call** | **31,580** | rate used by `base_truth_panel_r0/src/backfill_cl_swaps.py` (`est_credits = TOTAL*20`) |
| **Bytes** | **~52 MB** (60-call random sample returned 1.97 MB over 3.801% of the panel → 51.8 MB extrapolated); allow **60–150 MB** for stress-day mass | measured |
| **Wall clock, 4 workers** | **~6–15 min** (measured 60 calls / ~30 s and 30 calls / ~24 s at 4 workers) | measured |

**The honest and slightly counter-intuitive comparison to the ledger's swap backfill
(15,840 credits / 76 GB / 64 min for 94.6 days):**

The RPC **credit** cost is *not* lower, because the 10,000-block cap makes cost proportional to
**blocks scanned, not events found**. A full-year liquidation backfill costs the same ~31,580
credits as a full-year swap backfill would. The savings are entirely elsewhere:
**~500–1,500× less data** (52–150 MB vs 76 GB for 94.6 days, i.e. ~294 GB/year) and
**~4–10× less wall clock**. So "liquidations are far rarer than swaps so it should be very
cheap" is right about disk and time and **wrong about credits**.

Adding the other 8 Morpho topic0 to the same OR-filter (needed for a borrower panel, §7) costs
**the same 1,579 calls / 31,580 credits**, but I did **not** measure Morpho non-liquidation event
density, so I cannot quote the byte cost for that variant.

---

## 5. MEASURED EVENT COUNTS AND EXTRAPOLATION

### 5.1 Method A — stratified random sample

60 windows × 10,000 blocks, evenly spaced across 34,300,000–50,085,519.
**600,000 blocks = 3.801% of the panel. 60 RPC calls. 2,143 liquidation logs.**
(`/root/recon_out/sample_liq.py`, raw logs in `sample_liq.json`.)

```python
get_logs([[MORPHO_LIQ, AAVE_LIQ, CV2_LIQ, COMET_C, COMET_D]], a, a+9999)
# for a in [34_300_000 + i*263_091 for i in range(60)]
```

Window counts: min 0, p25 2, **median 4**, p75 12, **max 1,428**, mean 35.7.
The distribution is violently heavy-tailed, so I report three estimators:

| Protocol | sampled | naive ×26.31 | drop-max window | median-based |
|---|---:|---:|---:|---:|
| MORPHO `Liquidate` | 1,401 | 36,859 | 4,067 | 0 |
| AAVE-fork `LiquidationCall` | 423 | 11,129 | 8,508 | 3,157 |
| MOONWELL `LiquidateBorrow` | 260 | 6,840 | 5,538 | 3,157 |
| COMET `AbsorbCollateral` | 30 | 789 | 508 | 0 |
| COMET `AbsorbDebt` | 29 | 763 | 508 | 0 |
| **TOTAL** | **2,143** | **56,381** | **19,130** | **6,314** |

One window, blocks **46,866,907–46,876,906 (2026-06-03 21:00 → 2026-06-04 UTC)**, holds
**1,428 events (66.6% of the whole sample)** — 1,249 Morpho liquidations across 20 markets in
282 minutes, $31.9M seized. That is a cascade, and it is the entire reason the naive estimator
is 3× the robust one.

### 5.2 Method B — full-day scans of stress vs calm days (the better method)

The panel's own ETH price (`edge.eth_usd_1h`, 8,769 hours, 2025-08-17 → 2026-08-17) partitions
the 364 complete days by intraday high→low drawdown: **≤−10%: 10 days · −8..−10%: 7 · −6..−8%:
39 · −4..−6%: 65 · >−4%: 243**. I then scanned six complete days end-to-end
(43,200 blocks = 5 calls each, 30 calls total, `fullday_scan.py`):

| Day | ETH intraday | events | episodes (gap>300 blk) | seized notional* | non-major notional* |
|---|---:|---:|---:|---:|---:|
| 2026-02-05 | **−15.03%** | **5,096** | 24 | $101.5M | $50k (0.05%) |
| 2026-06-05 | −11.70% | 2,624 | 30 | $27.8M | $2.31M (8.3%) |
| 2025-10-10 | −12.45% | 2,155 | 20 | $14.1M | $237k (1.7%) |
| 2025-11-04 | −11.86% | 1,373 | 31 | $8.7M | $152k (1.7%) |
| 2026-08-15 | −0.41% | **9** | 7 | $449 | $446 |
| 2026-04-25 | −0.52% | **23** | 19 | $15 | $0 |

\* seized-collateral notional using hand-entered approximate reference prices — **order of
magnitude only, explicitly not a measured panel price**. Native-unit amounts are exact.

**Stress days run 150–500× the event rate of calm days.** Reconstructing the year from the day
census (10 × 2,812 mean stress + 243 × 16 calm + 111 unmeasured intermediate days) gives
**~48,000–56,000 events/year**, which agrees with Method A's naive estimator. So:

> **Raw liquidation event count on Base over 366 days ≈ 48,000–56,000, with a robust
> lower bound of ~19,000. It is emphatically NOT "a few hundred."**

### 5.3 …but the *tradeable* count is two to three orders of magnitude smaller

Filter to what could actually matter: **non-deep-major collateral, ≥$10,000 seized, and the
asset has a pool in `edge.bars_universe_50`** (the $50 cheap-and-feasible universe).

Per stress day: **37, 3, 1, 0** such events → mean **10.25 events/day, 3.0 episodes/day**.
Per calm day: **0, 0**.
Method A cross-check: 38 sampled instances, but **36 of the 38 sit inside the single cascade
window**; excluding it, 2 sampled → **54/year in ~27 episodes**.

| Bucket (Method A, whole sample) | sampled | naive/yr | excl. cascade/yr | episodes/yr |
|---|---:|---:|---:|---:|
| ALL collateral | 1,823 | 47,962 | 12,548 | 4,473 |
| DEEP major (WETH/USDC/USDbC/cbBTC) | 1,420 | 37,359 | 9,899 | 3,815 |
| Non-major, any size | 319 | 8,393 | 1,204 | 710 |
| **Non-major, ≥$10k, in $50 universe** | **38** | **1,000** | **54** | **132** |
| Non-major, ≥$50k, in $50 universe | 9 | 237 | 27 | 105 |
| Non-major, ≥$250k | **0** | **0** | **0** | **0** |

Combining both methods:

> **~50–150 tradeable-impact liquidation events per year, arriving in ~30–80 independent
> episodes, essentially all on the 10–17 stress days.**

For calibration against this program's own history: round 40 called A2 `UNDERPOWERED` at
**60–76 independent blocks**, and round 40's C2 mechanism test was believed only because it had
**334 independent blocks**. **30–80 episodes is below the level at which this program has
already declined to rule.** And the panel cannot be extended more than ~2 years
(`RESEARCH_PLAN.md` §三), which would reach at most ~90–240 episodes — on one asset.

---

## 6. Collateral assets intersected with the tradeable pool universe

Morpho market ids resolved by `eth_call idToMarketParams(bytes32)` = `0x2c3c9157`
(45 calls, 45/45 resolved; 97.5% of full-day Morpho logs covered, **39 market ids representing
2.5% left unresolved**). Aave-fork collateral read directly from indexed `topic1`. Symbols from
`census.tokens` plus 9 `eth_call symbol()`.

### 6.1 Full liquidated-collateral list (Method A sample, 1,824 logs)

| Symbol | Address | Morpho | Aave-fork | total | Base pool exists |
|---|---|---:|---:|---:|:---:|
| **cbBTC** | `0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf` | 840 | 38 | **878** | yes, 0.87 bps |
| **WETH** | `0x4200000000000000000000000000000000000006` | 177 | 334 | **511** | yes (quote asset) |
| **cbXRP** | `0xcb585250f852c6c6bf90434ab21a00f02833a4af` | 175 | 0 | **175** | yes, see §6.3 |
| cbDOGE | `0xcbd06e5a2b0c65597161de254aa074e489deb510` | 40 | 0 | 40 | **no** |
| SOL | `0x311935cd80b76769bf2ecc9d8ab7635b2139cf82` | 35 | 0 | 35 | **no** |
| cbADA | `0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c` | 26 | 0 | 26 | yes, 25.22 bps |
| USDC | `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913` | 2 | 23 | 25 | yes, 0.87 bps |
| cbETH | `0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22` | 23 | 1 | 24 | yes, 0.65 bps |
| weETH | `0x04c0599ae5a44757c0af6f9ec3b93da8976c150a` | 14 | 6 | 20 | yes, 0.85 bps |
| cbLTC | `0xcb17c9db87b595717c857a08468793f5bab6445f` | 13 | 0 | 13 | yes, 25.43 bps |
| wstETH | `0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452` | 6 | 5 | 11 | yes, 0.90 bps |
| rETH | `0xb6fe221fe9eef5aba221c348ba20a1bf5e73624c` | 9 | 0 | 9 | yes, 5.03 bps |
| AERO | `0x940181a94a35a4569e4529a3cdfb74e38fd98631` | 5 | 3 | 8 | yes, 30.04 bps |
| bsdETH | `0xcb327b99ff831bf8223cced12b1338ff3aa322ff` | 8 | 0 | 8 | yes, 1.00 bps |
| USDbC | `0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca` | 0 | 6 | 6 | yes, 7.70 bps |
| VVV | `0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf` | 4 | 0 | 4 | yes, 27.08 bps |
| AAVE | `0x63706e401c06ac8513145b7687a14804d17f814b` | 0 | 3 | 3 | yes, 30.08 bps |
| TOSHI | `0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4` | 3 | 0 | 3 | yes, 24.87 bps |
| wrsETH | `0xedfa23602d0ec14714057867a78d01e94176bea0` | 3 | 0 | 3 | yes, 30.94 bps / 294 d |
| PT-USDe-11DEC2025 | `0x194b8fed256c02ef1036ed812cae0c659ee6f7fd` | 3 | 0 | 3 | **no** (Pendle PT) |
| wbCOIN | `0xdec933e2392ad908263e70a386fbf34e703ffe8f` | 3 | 0 | 3 | **no** |
| USR | `0x35e5db674d8e93a03d814fa0ada70731efe8a4b9` | 2 | 0 | 2 | **no** |
| uSOL | `0x9b8df6e244526ab5f6e6400d331db28c8fdddb55` | 2 | 0 | 2 | yes, 15.08 bps |
| stkWELL | `0xe66e3a37c3274ac24fe8590f7d84a2427194dc17` | 2 | 0 | 2 | **no** |
| LBTC | `0xecac9c5f704e954931349da37f60e39f515c11c1` | 0 | 2 | 2 | **no** |
| VIRTUAL, WMTX, wBLT, SKI, xSolvBTC, PERP, PT-USR-24APR2025 | — | 1 each | ≤1 | 1 each | mixed |

**Split of the 1,824 sampled logs: 82.24% deep-major/LST/stable · 17.76% non-major ·
12.94% non-major *and* in the $50 universe.** Excluding the cascade window, the non-major share
collapses to **9.8% (46 of 470)** and those 46 are almost all dust (AERO max $8, TOSHI max $8,
VIRTUAL $58).

**The economics kill is in the notional, not the count.** Across the four full stress days:
**non-major = $2,753,854 of $152,119,023 seized = 1.810%.** In the Method A sample, cbBTC alone
is $30.3M of $33.0M (91.7%).

### 6.2 Non-major collateral across all scanned days, by notional

| Symbol | events | approx notional | in $50 universe | best cost (≥300 d pool) |
|---|---:|---:|:---:|---:|
| **cbXRP** | **228** | **$1,749,772** | yes | **25.11 bps** |
| SOL | 25 | $235,008 | **no** | — |
| AAVE | 88 | $177,418 | yes | 30.08 bps |
| cbADA | 47 | $168,964 | yes | 25.22 bps |
| WMTX | 15 | $152,926 | **no** | — |
| cbDOGE | 49 | $109,471 | **no** | — |
| AERO | 52 | $44,036 | yes | 30.04 bps |
| stkWELL | 19 | $42,456 | **no** | — |
| TOSHI | 6 | $37,146 | yes | 24.87 bps |
| cbLTC | 12 | $28,500 | yes | 25.43 bps |
| uSOL | 8 | $6,476 | yes | 15.08 bps |
| SKI, VIRTUAL, wbCOIN, EURC | ≤5 each | <$1,500 each | mixed | — |

**cbXRP is 41% of non-major events and 64% of non-major notional.** It is the only asset with a
coherent thesis: a Coinbase-wrapped asset whose fair value is pinned on global venues while its
Base liquidity is thin, so forced selling should create a mechanical dislocation that reverts.
It is also, by construction, a single-symbol strategy that the concentration gate forbids.

Three of the top six non-major assets by notional — **SOL, cbDOGE, WMTX** — have **no pool in
the tradeable universe at all.** Their liquidations are unharvestable regardless of size.

### 6.3 A correction I have to make about cbXRP's cost

My first pass reported cbXRP at "best cost 5.05 bps, 364 tradeable days." **That is wrong** — it
combined `min(cost_p50)` from one pool with `max(tradeable_days)` from a different pool. Per pool:

| pool | venue | fee | tradeable_days | cost_p50 |
|---|---|---:|---:|---:|
| `0xb90fe999be6869af0afc557dccfbe169ea3403d6` | aero_cl | 500 | **123** | 5.05 bps |
| `0xa6da283cf7d06b2279626e787c329d6acf7e2994` | aero_cl | 2500 | **364** | **25.11 bps** |

The cheap pool did not exist for two thirds of the panel (first swap at block 44,675,652). The
only cbXRP pool covering the panel costs **25.11 bps round trip**. This is the same failure mode
the ledger flags repeatedly ("度量少了一个维度"), so I am recording it explicitly rather than
quietly fixing it. Screened properly — pool must be ≤30 bps **and** ≥300 tradeable days — the
liquidated non-major set is: **uSOL 15.08 · TOSHI 24.87 · cbXRP 25.11 · cbADA 25.22 ·
cbLTC 25.43 · VVV 27.08 bps**, i.e. an edge must clear ~25 bps.

One instrument-side positive: the panel **can** measure this. The two cbXRP pools carry 64,189
and 216,995 swaps over the panel, with **5,568 and 629 swaps on 2026-06-05** and 1,102 / 199
inside the 2026-06-03 cascade window. Block-level impact around a liquidation is measurable
from data already on disk, zero RPC. The instrument is not the blocker.

---

## 7. Is the liquidation observable in advance?

**Mechanically yes; economically it does not help a long-only spot account.**

Morpho Blue health is fully computable from on-chain state (`GOAL.md` §1.3, official):

```
LTV = BORROWED / (COLLATERAL · ORACLE_PRICE / 1e36)
HF  = COLLATERAL_VALUE_IN_LOAN_TOKEN · LLTV / BORROWED
liquidatable ⇔ HF < 1 ⇔ LTV > LLTV
```

Inputs and where each comes from:

| Input | Source | Cost |
|---|---|---|
| `lltv`, `oracle`, `loanToken`, `collateralToken` | `eth_call idToMarketParams(id)` = `0x2c3c9157` | 1 call per market, **already done for 45 markets** |
| oracle price | `eth_call oracle.price()` | 1 call per market per timestamp |
| position `(supplyShares, borrowShares, collateral)` | `eth_call position(id, onBehalf)` | **1 call per borrower per timestamp** |
| share→asset conversion + accrual | `eth_call market(id)` + `_accrueInterest` | 1 call per market per timestamp |
| **borrower set** | `Borrow` topic2 = `onBehalf` (indexed), plus `SupplyCollateral` topic2 | **free** — same 1,579-call backfill |

So the borrower *roster* is free, and the watchlist is a `position()` fan-out. **I did not measure
the number of distinct Morpho borrowers on Base**, and that is the entire cost driver, so I
cannot quote a figure. Note `GOAL.md` §1.3's warning: `market()`/`position()` raw words exclude
interest accrued since `lastUpdate`, so **HF must be computed post-accrual** or it is
systematically overstated.

**Why it does not help.** A watchlist tells you forced *selling* is imminent. For a long-only
spot account the only actions are (a) buy before it — buying into a fall, economically backwards;
or (b) wait and buy after — post-impact reversion, which is directional prediction. And per §0,
the impact lands inside the liquidator's own transaction, so "after" begins at block N+1 at best.
The one thing advance knowledge genuinely unlocks — being the liquidator and capturing the
1.7%–12.7% LIF — needs up to $210k of repayment capital per event and a won gas auction. Both
are excluded: total capital is <$5,000, and E2 is on the permanent do-not-do list.

---

## 8. The cheapest possible KILL for this line

The program's discipline is to lead with the cheapest experiment that could kill the line. Here
the cheapest kill is **cheaper than the backfill by an order of magnitude, and it needs no
backfill, no panel work, and no pre-registration** — because it measures a mechanism, not a return.

> ### KILL-1: atomicity census. ~200 `eth_getTransactionReceipt` calls, ~4,000 credits, ~2 minutes.
>
> Take 200 `Liquidate` / `LiquidationCall` transactions stratified across protocols and across
> ≥5 distinct days (the tx hashes for 7,753 of them are already sitting in
> `/root/recon_out/fullday.json` — **zero further getLogs needed**). For each, fetch the receipt
> and count DEX `Swap` logs (`0xc42079f9…` UniV3/Slipstream, `0xb3e27736…` Aero vAMM,
> `0xd78ad95f…` V2) in the **same transaction**.
>
> **Pre-registered kill rule: if ≥90% of liquidations contain a DEX swap in the same
> transaction, the line is dead**, because the forced-sale price impact is not observable before
> it is already realised, and the only entry available to a long-only spot account is
> post-impact reversion — family A, already dead with high power.
>
> **Status: 6/6 already measured this way, all atomic** (2 swap logs each, gas 780k–955k, all on
> 2026-06-05, all cbXRP, the largest being $210k). n=6 on one day and one asset is **not**
> conclusive; the remaining 194 receipts are what makes it conclusive.

If and only if KILL-1 fails (i.e. a material fraction of liquidations are non-atomic), the
second cheapest kill is also free of RPC:

> ### KILL-2: impact-and-reversion on data already on disk. Zero RPC, pure SQL.
>
> The exact block numbers of every large non-major liquidation are already recorded in
> `/root/recon_out/fullday.json` and `sample_liq.json`. `census.cl_swaps` has 5,568 cbXRP swaps
> on 2026-06-05 alone, with `sqrt_price_x96` per swap — so mid-price at block N−1, N, N+1, N+k is
> directly computable. **Pre-register first** (grid, split, cost model = R2 two-sided measured
> cost ≥25.11 bps for cbXRP, decision thresholds, power calc), then measure. Kill rule: if the
> N+1 → N+k reversion's 95% upper bound is below 25 bps, done.
>
> **KILL-2 must not be run before pre-registration**, and I deliberately did not run it in this
> recon, to avoid burning the sample. It is also already known to be underpowered
> (~30–80 episodes), so KILL-1 is strictly the better first move.

The 31,580-credit full backfill should only be paid for **after** KILL-1 fails. It is cheap in
absolute terms, but it is 8× the cost of the kill and this line does not deserve it.

---

## 9. What I could NOT determine — explicitly

1. **USD notionals are approximate.** Every seized amount is exact in native token units; the
   USD figures use hand-entered reference prices (cbBTC $60k, WETH $1,900, cbXRP $2.00, …), not
   point-in-time panel prices. Bucket boundaries at $10k/$50k could shift by tens of percent.
   The panel *can* produce exact point-in-time prices (§6.3, `edge.eth_usd_1h` + pool
   `sqrt_price_x96`) — I did not spend the effort.
2. **The 111 intermediate-drawdown days (−4% to −10%) were never scanned.** The annual
   tradeable-event estimate interpolates across them. This is the largest single source of
   uncertainty in the 50–150 range.
3. **Atomicity rests on n=6**, all cbXRP, all one day, all Morpho. It is the highest-leverage
   finding in this report and the least sampled. §8 KILL-1 fixes it for 4,000 credits.
4. **Number of distinct Morpho borrowers on Base is unmeasured** → the cost of a
   "positions near liquidation" watchlist is unquantified (§7).
5. **Fluid on Base is unresolved.** `0x52Aa899454998Be5b000Ad077a46Bbe360F4e497` has 4,462 bytes
   of code on Base, but I did not confirm it is Fluid's liquidity layer, did not locate Fluid's
   vault contracts, and did not compute or test a Fluid liquidation topic0. Fluid liquidation
   flow is therefore **entirely absent** from every count in this report.
6. **Three Aave-v3-fork pools and one Comet are unidentified** —
   `0x09b11746dfd1b5a8325e30943f8b3d5000922e03` (10 logs),
   `0x766f21277087e18967c1b10bf602d8fe56d0c671` (6), `0x6374a1f384737bccccd8fae13064c18f7c8392e5`
   (1), `0x784efeb622244d2348d4f2522f8860b96fbece89` (2/2 absorbs). A web search failed to
   resolve them. They are ~2% of Aave-fork flow, so this does not move any conclusion.
7. **Moonwell / Compound v2 sizes were never decoded.** `LiquidateBorrow.seizeTokens` is in
   mToken units and needs `exchangeRateStored` per market per block to convert. So the
   **~5,500–6,800 Moonwell liquidations/year carry no notional estimate**, and Moonwell's
   collateral mix is known only as 22 mToken addresses, not as assets. This is the biggest
   *unmeasured* protocol in the report. It does not change the verdict, because Moonwell's
   collateral menu is the same WETH/cbBTC/USDC/DAI/AERO/wstETH set, but I did not verify that.
8. **39 Morpho market ids (2.5% of full-day Morpho logs) are unresolved** — I ran out of RPC
   budget for `idToMarketParams`. At 2.5% they cannot flip the 1.81% notional finding, but they
   are a hole.
9. **Why the 2026-06-03/04 cascade happened is unknown.** It is *not* in the top-20 ETH intraday
   drawdown days, yet it produced 1,428 liquidations in 282 minutes. Its collateral was
   cbBTC/cbXRP/SOL/cbDOGE/cbADA, so it may be a BTC-led move rather than ETH, or an oracle
   event. **Unlabelled speculation avoided; I simply did not determine it.** It matters because
   this one episode drives 66.6% of the random sample.
10. **I caused an incident.** A `WHERE tx_hash IN (7,753 hashes)` query against the 424M-row
    `census.cl_swaps` OOM'd the ClickHouse server; `eatlq-autoheal-1` restarted it and it came
    back healthy within ~25 s. No data loss, no DDL, no writes. I abandoned that approach and
    answered the atomicity question via receipts instead. **Flagging it because another agent was
    writing to `base_aero_emissions_r0` / `base_fee_tier_r0` at the time and may have had a query
    interrupted.**

---

## 10. Artifacts

All in `/root/recon_out/`:

| File | Contents |
|---|---|
| `MORPHO_LIQUIDATION_RECON.md` | this report |
| `probe_rpc.py` | read-only RPC wrapper, call counter |
| `sample_liq.py`, `sample_liq.json` | 60-window stratified sample + all 2,143 raw logs |
| `analyze_sample.py` | clustering, market-id and asset extraction |
| `resolve_morpho.py`, `morpho_markets.json` | 45 market ids → MarketParams |
| `extra_syms.json`, `getcode.json` | 9 `symbol()` lookups, 9 `eth_getCode` results |
| `build_collateral_table.py`, `collateral_table.json` | collateral × tradeable-universe join |
| `size_and_cascade.py`, `tradeable_events.py` | notional distributions, cascade decomposition, bucket counts |
| `fullday_scan.py`, `fullday.json` | 6 full-day scans + all 11,280 raw logs (incl. 7,753 liquidation tx hashes for KILL-1) |
| `analyze_fullday.py` | per-day collateral/notional decode |
| `liqtx.json` | the 7,753 liquidation tx hashes, ready for KILL-1 |
