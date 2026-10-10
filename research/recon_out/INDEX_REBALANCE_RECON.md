# ON-CHAIN INDEX / BASKET REBALANCING ON BASE — READ-ONLY RECON

**Date**: 2026-08-19
**Scope**: Is there a tractable research line in calendar-determined forced flow from tokenized index rebalancing on Base?
**Panel**: `census.cl_swaps` 424,433,536 swaps / 366 days (2025-08-17 → 2026-08-17); `edge.bars_1h_year_50`; `edge.bars_universe_50` (411 pools).
**Constraints honored**: zero DDL, zero writes outside `/root/recon_out/`, nothing touched in `base_aero_emissions_r0` / `base_fee_tier_r0`. All measurements are pure `SELECT`. Web research used for product identification only.

---

## 0. VERDICT

### `INFEASIBLE_ECONOMICS`

**The single number driving it: 4.**

Four executed rebalance events in the 366-day panel for **LCAP**, which is the only index product on Base with non-trivial size — it holds **86% ($4.31M of $5.03M)** of the *entire* Base index-DTF market cap. LCAP rebalances **quarterly**.

The number that makes it unrescuable is the second one: **$5,034,510** — the summed market cap of *all thirteen* Base index DTFs, taken from Reserve's own API. The next largest product after LCAP is **$203,062**.

Two further independent kills, each sufficient on its own:

| Kill | Evidence |
|---|---|
| **Event count** | LCAP = 4/yr. The only monthly-rebalancing Base product (BGCI) has **$184,320** AUM. **Size and frequency are anti-correlated across the whole universe — there is no cell with both.** |
| **Economics** | LCAP's *entire fund* ($4.31M) equals **0.44 days** of normal volume in its own largest constituent pool (cbBTC/WETH, median 4,217.8 WETH/day ≈ $9.82M at panel-median ETH $2,329). A full liquidation of the fund would be half a day of ordinary flow. |
| **Observability** | **0 of 424,433,536 swaps** have any of the 12 Base DTF Folio addresses as `sender` or `recipient`. Mean volume z-score on rebalance days = **−0.088** vs **+0.002** on all other days — rebalance days are marginally *quieter* than average. |

`INFEASIBLE_DATA` also holds independently (see §4): the rebalance is a Dutch auction on the Folio contract, settled via `bid()` and CoW Swap solvers. It never appears as an AMM swap attributable to the fund. I could not identify a single rebalance event from the panel alone.

**Required panel length to power this line: 20 to 250 years.** Arithmetic in §6.

---

## 1. What CMC20 actually is

**Address on Base**: `0xa0a8481fc246cd12f75227abb96220ff5360fad3` (18 decimals).

```sql
SELECT token, symbol, decimals FROM census.tokens WHERE symbol ILIKE '%CMC%';
-- 0xa0a8481fc246cd12f75227abb96220ff5360fad3  CMC20  18
-- 0xd70fa56cdc46d14d588d4a18eef97c92a677fb1a  CMC    18
```

**It is the CoinMarketCap 20 Index DTF** — a Reserve Protocol "Decentralized Token Folio" tracking the CoinMarketCap 20 Index, launched **November 2025** in partnership with CoinMarketCap.

| Attribute | Value | Source |
|---|---|---|
| Product | CoinMarketCap 20 Index DTF, powered by Reserve Protocol | [CoinMarketCap](https://coinmarketcap.com/currencies/coinmarketcap-20-index/), [Reserve](https://dtf.reserve.org/cmc20lp) |
| Index provider | CoinMarketCap | same |
| Methodology | Top 20 by market cap, **market-cap weighted**; excludes stablecoins, pegged/wrapped tokens, and assets with limited investability | [CMC20 methodology PDF](https://s3.coinmarketcap.com/uploads/CoinMarketCap_20_Index_Methodology.pdf) |
| **Rebalance frequency** | **Monthly, at 00:00 UTC on the 1st of each month** | methodology PDF §3, [CMC20 chart page](https://coinmarketcap.com/charts/cmc20/) |
| Constituent set published in advance? | Rules are public and deterministic (top-20 by CMC rank). The *realized* list is only fixed at 00:00 UTC on the 1st. | methodology PDF |
| Rebalance mechanism | On-chain Dutch auction on the Folio contract (`bid()`), plus CoW Swap "Trusted Filler" routing since release 4.0.0 | [Reserve docs](https://docs.reserve.org/core-components/index-dtfs/rebalancing/technical-details) |
| **Canonical chain** | **BSC (chain 56)** | [Reserve DTF reference](https://app.reserve.org/skills/dtf.md): `\| CMC20 \| CoinMarketCap 20 Index DTF \| BSC \|`; and "BSC has an index subgraph (CMC20 lives there)" |

### The decisive fact about CMC20: the Base token is not the Folio

```
$ curl -s "https://api.reserve.org/discover/dtf?chainId=8453"   → 13 DTFs
['ABX','AI','BDTF','BGCI','CLUB','CLX','LCAP','MVDA25','MVTT10F','SBR','VLONE','VTF','ZINDEX']
CMC20 present on Base DTF list?  False
```

Reserve's own registry of Base index DTFs does **not** contain CMC20. Combined with the docs entry placing the CMC20 Folio on BSC, and BaseScan's max total supply of **3,408.48 CMC20** on Base against a **47,439 CMC20** circulating supply overall (~7%), the Base `0xa0a8481f…` token is a **bridged representation** of the BSC Folio.

**Consequence**: CMC20's monthly rebalance auctions do not trade Base assets, do not touch Base pools, and produce **no Base-side forced flow at all**. The one product on Base with an attractive rebalance frequency is the one whose rebalance does not happen on Base.

*(Caveat, labeled: I inferred "not a Base Folio" from Reserve's registry + docs + supply split. I did **not** run an `eth_call` to confirm the Base contract holds no basket. That would cost RPC credits and would only confirm what three independent sources already say.)*

---

## 2. CMC20 tradeability in the panel — verified, and it is genuinely good

CMC20 has exactly **one** pool on Base:

```sql
SELECT p.pool, p.venue, p.fee, p.tick_spacing, p.token0, p.token1
FROM census.pools p
WHERE p.token0='0xa0a8481fc246cd12f75227abb96220ff5360fad3'
   OR p.token1='0xa0a8481fc246cd12f75227abb96220ff5360fad3';
-- 0x102680f80efa3caad7e6f9661a34f48eb646e16f | univ3 | 500 | 10
--   token0 = 0x4200…0006 (WETH), token1 = CMC20
```

Panel membership (`edge.bars_universe_50`, the tradeable-at-$50 universe):

| pool | venue | fee | tradeable_days | cost_p50 |
|---|---|---:|---:|---:|
| `0x102680f8…e16f` | univ3 | 500 (5 bps) | **265 / 366** | **5.23 bps** |

`cost_p50` of 5.23 bps against a 5 bps fee tier means slippage at $50 is **0.23 bps** — this is the cheapest non-major, non-pegged asset in the panel by a wide margin. 265 tradeable days is consistent with a launch on 2025-11-24 (366 − 265 = 101 days; first swap observed `2025-11-24 11:50:45`), which independently corroborates the November 2025 launch date.

Monthly activity:

```sql
SELECT toStartOfMonth(block_time) AS mo, count() AS n_swaps,
       round(sum(abs(toFloat64(amount0)))/1e18,3) AS weth_vol,
       round(quantile(0.5)(abs(toFloat64(amount0)))/1e18,6) AS p50_weth,
       uniqExact(sender) AS u_senders
FROM census.cl_swaps WHERE pool='0x102680f80efa3caad7e6f9661a34f48eb646e16f'
GROUP BY mo ORDER BY mo;
```

| month | swaps | WETH volume | median clip (WETH) | uniq senders |
|---|---:|---:|---:|---:|
| 2025-11 (from 11-24) | 494 | 237.6 | 0.0362 | 15 |
| 2025-12 | 2,367 | 1,392.5 | 0.1690 | 30 |
| 2026-01 | 4,442 | 930.8 | 0.0322 | 29 |
| 2026-02 | 19,424 | 2,284.0 | 0.0291 | 38 |
| 2026-03 | 21,868 | 2,883.3 | 0.0230 | 46 |
| 2026-04 | 15,708 | 1,655.7 | 0.0238 | 37 |
| 2026-05 | 18,044 | 916.1 | 0.0215 | 38 |
| 2026-06 | 36,880 | 1,974.1 | 0.0225 | 37 |
| 2026-07 | 24,875 | 1,093.3 | 0.0208 | 38 |
| 2026-08 (to 08-17) | 9,594 | 309.3 | 0.0204 | 21 |
| **total** | **153,696** | **199,813.9*** | — | — |

\* the total-row WETH figure is the raw `census.cl_swaps` sum and is inflated by double-counted legs; the bars table gives the clean number: **13,676.7 WETH** across 5,980 hourly bars. Median daily volume 12.3 WETH ≈ **$28.6K/day** at ETH $2,329.

**Answer: yes, liquid enough for a $50 trade — comfortably.** Median clip size is ~0.02 WETH (~$47), i.e. the pool's own median trade is exactly our size. This is a real, cheap, persistently tradeable instrument. It just has no rebalance flow behind it on this chain.

One structural note on why the pool is so tight: the dominant counterparty is a market maker, not directional flow.

```sql
SELECT sender, count() AS n, countIf(amount0>0) AS buys, countIf(amount0<0) AS sells,
       round(sum(abs(toFloat64(amount0)))/1e18,1) AS weth_vol, round(sum(toFloat64(amount0))/1e18,3) AS net_weth
FROM census.cl_swaps WHERE pool='0x102680f80efa3caad7e6f9661a34f48eb646e16f' GROUP BY sender ORDER BY n DESC;
-- 0x278d858f05b94576c1e6f73285886876ff6ef8d2 | 128,922 | 63,724 | 65,198 | 3,945.5 | -5.394
```

128,922 swaps, 63,724 buys vs 65,198 sells, net position **−5.4 WETH on 3,945.5 WETH of volume (0.14%)**. That is a textbook two-sided MM. 83.9% of all swaps in the pool come from this one address. A forced-flow imbalance would have to get past it. *(I could not determine the identity of this address — labeled as undetermined.)*

---

## 3. All index / basket products found on Base

### 3a. Reserve Index DTFs — the complete Base universe (Reserve API, `chainId=8453`)

Sorted by market cap. "In panel" = present in `edge.bars_universe_50`, matched **by token address** (not symbol — there are four distinct tokens with symbol `AI` on Base, and the one in the panel is *not* the AIndex DTF).

| Symbol | Name | Market cap | Fee | Basket | In $50 panel? | cost_p50 | tradeable_days |
|---|---|---:|---:|---|---|---:|---:|
| **LCAP** | CF Large Cap Index | **$4,307,749** | 1.5% | cbBTC, WETH, uXRP, uSOL, HYPE, uDOGE, uADA, uXLM | **YES** aero_cl 3000 | 30.20 | 327 |
| ABX | Alpha Base Index | $203,062 | 2% | ALTT, LFI, TIBBIR, ODAI, BLOCKTRONICS | YES univ3 3000 | 35.27 | 365 |
| VLONE | Venionaire L1 Select | $192,530 | 0.5% | 20 assets (TONCOIN, uAPT, stTAO, uSEI, uSUI, cbBTC, uSOL, WETH, uXRP, …) | no | — | — |
| BGCI | Bloomberg Galaxy Crypto Index | $184,320 | 2% | cbBTC, bsdETH, uSOL, uLINK, uADA, uXLM, uBCH, uXRP | **YES** univ3 3000 | 30.10 | 365 |
| CLX | Clanker Index | $70,705 | 2% | CLANKER, BNKR, FAIR, CLAWD, MOLT, noice, … | YES univ3 3000 | 33.86 | 365 |
| MVTT10F | MarketVector Token Terminal Fundamental | $43,440 | 2% | TRX, uSOL, bsdETH, uNEAR, UNI, WBNB, cbBTC, AAVE, HYPE, uPOL | YES univ3 3000 | 41.59 | 355 |
| VTF | Virtuals Index | $12,661 | 1% | AIXBT, GAME, VADER, LUNA, AXR, WIRE, VPAY, TIBBIR, FACY, ETHY | YES univ3 3000 | 39.80 | 206 |
| AI | AIndex | $9,729 | 2% | VIRTUAL, FAI, TOSHI, AIXBT, VVV, TONY, COOKIE, REKT, REI, TIBBIR | **no** | — | — |
| CLUB | Club Night DTF | $3,874 | 2% | 20 assets | no | — | — |
| BDTF | Base MemeIndexer DTF | $3,850 | 2% | DTF, SPX, uDOGE, Mog, TYBG, PEPE | no | — | — |
| MVDA25 | MarketVector Digital Assets 25 | $1,199 | 2% | 21 assets | no | — | — |
| SBR | Strategic Base Reserve | $998 | 1% | 21 assets | no | — | — |
| ZINDEX | Zora Index | $393 | 1% | jesse, balajis, propaganda, docker, … | no | — | — |
| — | **TOTAL** | **$5,034,510** | | | 7 of 13 tradeable | | |

**LCAP is 85.6% of the total.** The bottom seven products together are worth **$32,704** — roughly one median day of volume in the LCAP token's own pool ($28.6K).

### 3b. CMC20 — bridged, not a Base Folio

| Symbol | Market cap (all chains) | Canonical chain | In $50 panel? | cost_p50 | tradeable_days |
|---|---:|---|---|---:|---:|
| CMC20 | $6,185,740 (~$444K of it on Base) | **BSC** | **YES** univ3 500 | **5.23** | 265 |

The best-executing index token on Base is the one whose rebalance happens on another chain.

### 3c. Non-Reserve index products

| Family | Result |
|---|---|
| **Index Coop** (DPI, MVI, icETH, dsETH, hyETH, ETH2x-FLI, BTC2x-FLI, GMI, BED, cdETH, INDEX) | **Not present.** `SELECT symbol FROM census.tokens WHERE symbol IN (…)` returns **only** `AMKT` out of 19 probed index symbols. Index Coop has no CL-pool presence on Base in a 2,462-token, 38,767-pool census. |
| **Alongside AMKT** | Present at `0x13f4196cc779275888440b3000ae533bbbbc3166`, 2 pools (AMKT/WETH univ3 3000, AMKT/USDC univ3 100). **1,831 + 1,464 = 3,295 swaps in 366 days** (≈9/day), 8.24 WETH total volume. **Not in the $50 panel.** Dead on arrival. |
| **Reserve Yield DTFs (RTokens)** | `bsdETH` is on Base and is one of the 7 depth-confirmed non-major assets (3 pools, cheapest **1.00 bps**, 365 days). `USD+` also present (4.74 bps). **But yield DTFs are not calendar-rebalanced** — their baskets change on governance/collateral-default events, which are exactly *not* publicly scheduled. Out of scope for this thesis. |
| **Balancer / Aerodrome weighted-pool indexes** | **Structurally inapplicable.** A weighted AMM pool holds constant *weights*, so it rebalances continuously via arbitrage. There is no discrete, scheduled, forced trade to front-run — the "rebalance" is the arbitrage, and that family was already closed in Round 37 (`ARB_CLOSED_BY_BOTS`, 86/86 atomic samples negative). |
| **TokenSets** | No matching symbols in `census.tokens`. |
| Other index-named tokens found | `Bindex` (559 swaps, dead since 2026-01-20), `BLOCK` (3 pools, 2,922/2,188/70 swaps), `DATA` (no pool). None in the panel; none verified as real index products. |

---

## 4. THE KEY FEASIBILITY QUESTION: observable? measurable?

### 4a. Direct detection — Folio addresses have zero AMM footprint

The Reserve rebalance mechanism is a Dutch auction **on the Folio contract**: `REBALANCE_MANAGER` starts a rebalance, `AUCTION_LAUNCHER` opens auctions, and bidders call `bid()` on the Folio. Since release 4.0.0 the DTF also routes to CoW Swap solvers via the Trusted Filler integration. **None of this is an AMM swap.** I tested this directly:

```sql
SELECT
  countIf(sender    IN (<12 Base DTF Folio addresses>)) AS dtf_as_sender,
  countIf(recipient IN (<same 12>))                     AS dtf_as_recipient,
  countIf(sender    ='0x2626664c2603336e57b271c5c0b26f421741e481') AS control_universal_router,
  countIf(recipient ='0x9008d19f58aabd9ed0d60971565aa8510560ab41') AS control_cowswap_settlement,
  count() AS total_rows
FROM census.cl_swaps;
```

| field | value |
|---|---:|
| `dtf_as_sender` | **0** |
| `dtf_as_recipient` | **0** |
| `control_universal_router` (sender) | 50,901,113 |
| `control_cowswap_settlement` (recipient) | 390,769 |
| `total_rows` | **424,433,536** |

The two controls prove the scan works. **Zero of 424.4M swaps in 366 days involve any Base index DTF as a swap counterparty.** Attribution is impossible in principle from this table: CoW's `GPv2Settlement` does appear 390,769 times as recipient, but there is no field in `census.cl_swaps` that distinguishes a DTF rebalance order from any of the other hundreds of thousands of CoW orders.

**This alone is an `INFEASIBLE_DATA` result for detection-based event identification.** Rebalance events must come from published dates, not from the panel.

### 4b. Published dates — LCAP, the only product that matters

Reserve's governance page for LCAP (`app.reserve.org/base/index-dtf/0x4da9a0f397db1397902070f93a4d6ddbc0e0e6e8/governance/`) lists the actual proposal history. Rebalance proposals only:

| Proposal | Outcome | Date |
|---|---|---|
| September Rebalance | **Executed** | 2025-09-02 09:06 |
| September rebalance (1st attempt) | *Defeated* (33.3% / 66.7%) | — |
| December 2025 Rebalance | **Executed** | 2025-11-28 04:00 |
| January 2026 Rebalance | *Defeated* (10.96% / 89.03%) | — |
| March 2026 Rebalance | **Executed** | 2026-03-02 16:02 |
| June 2026 Rebalance | **Executed** | 2026-06-01 16:36 |

**Executed rebalance events in the 366-day panel: 4.** Cadence Sep → Dec → Mar → Jun is exactly quarterly, matching CF Benchmarks' published rule: *"Rebalanced and reconstituted quarterly, the index is calculated daily at 4:00 pm U.S. EST."*

Two notes that make it *worse*, not better:
1. **Two of six rebalance proposals were voted down.** The schedule is not actually deterministic — it is subject to a `vlRSR-LCAP` governance vote with 34 voting addresses. The "January 2026 Rebalance" was defeated 89–11. So the trade is not calendar-determined; it is calendar-*proposed* and governance-*determined*. **That breaks the core premise of the thesis**: the counterparty does not in fact *have* to trade.
2. Rebalance frequency for the other products: **BGCI is monthly** ("Cryptocurrencies are considered for addition/removal to/from the Index on a monthly basis" — Bloomberg rules, confirmed on the BGCI DTF page) but carries **$184,320**. MarketVector products (MVTT10F, MVDA25) are quarterly by index rule. I could not retrieve governance histories for ABX, VLONE, CLX, VTF, AI, CLUB, BDTF, SBR, ZINDEX (see §7).

**Maximum annual event count anywhere in the Base index-DTF universe: 12/yr, at $184K AUM. Maximum AUM: $4.31M, at 4/yr.**

### 4c. Are the four LCAP rebalances measurable? No.

LCAP's basket is *ideally* suited to this test — every constituent is tradeable in the panel:

```sql
SELECT symbol, pool, venue, fee, tradeable_days, round(cost_p50,2) AS cost_bps
FROM edge.bars_universe_50 WHERE symbol IN ('uSOL','uXRP','uDOGE','uADA','uXLM','cbBTC','HYPE');
```

| constituent | weight | best pool | venue | cost_p50 (bps) | tradeable_days | median daily volume |
|---|---:|---|---|---:|---:|---:|
| cbBTC | 42.94% | `0x22aee369…` | aero_cl 85 | **0.87** | 347 | 4,217.8 WETH ($9.82M) |
| WETH | 22.98% | — | — | — | — | — |
| uXRP | 12.33% | `0xc8da14d7…` | univ3 3000 | 30.21 | 365 | 28.3 WETH ($65.9K) |
| uSOL | 9.71% | `0x0225ba89…` | aero_cl 1500 | 15.08 | 365 | 146.4 WETH ($341K) |
| HYPE | 7.43% | `0x9e48d016…` | aero_cl 500 | 6.09 | 288 | 95.2 WETH ($222K) |
| uDOGE | 2.19% | `0x84a8fedc…` | univ3 3000 | 30.43 | 365 | 17.0 WETH ($39.6K) |
| uADA | 1.48% | `0xd52ad625…` | univ3 3000 | 30.53 | 365 | 12.6 WETH ($29.3K) |
| uXLM | 0.94% | `0x9140d1bc…` | univ3 3000 | 36.30 | 365 | 1.4 WETH ($3.3K) |

I measured daily WETH volume z-scores in all 8 constituent pools (7 above + LCAP's own pool) on the 4 rebalance dates and the following day:

```sql
WITH daily AS (SELECT sym, toDate(bar) AS d, sum(vol_weth) AS v FROM edge.bars_1h_year_50 … GROUP BY sym,d),
     z AS (SELECT sym, d, (v-avg(v) OVER (PARTITION BY sym))/stddevPop(v) OVER (PARTITION BY sym) AS zz FROM daily)
SELECT if(d IN (<4 rebalance dates and their +1 days>),'REBALANCE_WINDOW','all_other_days') AS grp,
       count(), avg(zz), quantile(0.5)(zz), max(zz), countIf(zz>2) FROM z GROUP BY grp;
```

| group | n | mean z | median z | max z | count(z > 2) | rate |
|---|---:|---:|---:|---:|---:|---:|
| **REBALANCE_WINDOW** | 62 | **−0.088** | −0.155 | 4.50 | 1 | **1.6%** |
| all_other_days | 2,776 | **+0.002** | −0.262 | 8.63 | 126 | **4.5%** |

**Rebalance days are marginally quieter than random days, and produce z > 2 at one third the base rate.** There is no forced-flow signature.

The one z = 4.50 cell (uXLM, 2026-06-01) is not a rebalance spike: uXLM's four *largest* volume days of the entire year are 2026-05-28, 05-29, 05-30, and 05-31 — all **before** the rebalance and all **larger** than the rebalance day, which ranks 5th. It is the tail of a multi-day burst that began four days early.

Top-8 volume days per pool, tested against the 8 rebalance-window dates: **1 hit in 32 slots** (the uXLM rank-5 above). Chance expectation for 8 flagged days out of ~365 is 0.7 hits in 32.

The most promising a-priori candidate — HYPE, whose 7.43% weight suggests a recent constituent *addition* worth ~$320K, and which showed 203/230 WETH on 06-01/06-02 against a 95.2 WETH median — collapses on inspection: HYPE's top-15 volume days range from **585.2 to 1,252.2 WETH**, all in July and August 2026. 2026-06-01 does not make the top 15. z = +0.15.

LCAP's own pool shows nothing either (median daily volume 12.28 WETH; p90 52.77; p99 157.17):

| date | WETH volume | ×median |
|---|---:|---:|
| 2025-11-28 **REBAL** | 14.09 | 1.15× |
| 2026-03-02 **REBAL** | 14.30 | 1.16× |
| 2026-06-01 **REBAL** | 16.94 | 1.38× |
| 2026-06-04 (no event) | 52.77 | 4.30× |
| 2026-06-05 (no event) | 75.38 | 6.14× |

*(The 2025-09-02 rebalance is unmeasurable in LCAP's own pool: the pool had 1–6 bars/day and ~0 volume until mid-September 2025. The constituent pools do have data on that date and show nothing — uSOL z=0.57, uXRP z=0.29/0.35, uDOGE z=−0.81, uADA z=−0.40, uXLM z=−0.14.)*

### 4d. Why the flow is invisible — the arithmetic

LCAP's dollar holdings, and a 5% quarterly turnover assumption (typical for cap-weighted large-cap reconstitution):

| constituent | $ held | 5% turnover = forced flow | as % of one median day's pool volume |
|---|---:|---:|---:|
| cbBTC | $1,849,747 | $92,487 | **0.9%** |
| uXRP | $531,145 | $26,557 | **40%** |
| uSOL | $418,282 | $20,914 | 6.1% |
| HYPE | $320,066 | $16,003 | 7.2% |
| uDOGE | $94,340 | $4,717 | 12% |
| uADA | $63,755 | $3,188 | 11% |
| uXLM | $40,493 | $2,025 | **62%** |

Note the shape, because it is the same shape this program has now hit four times: **the flow is largest relative to pool depth exactly where the pool is thinnest and the toll is highest.** uXLM has the best flow/volume ratio (62%) and the worst cost (36.3 bps one-way → 72.6 bps round trip) and the highest volatility (332 bps daily σ). cbBTC has the cheapest execution (0.87 bps) and a flow that is 0.9% of a day. This is Round 38's "*edge lives entirely on the coins you cannot afford the toll on*" and Round 39's "*the tier that earns the most fees is the tier that is hardest to trade in*", now appearing a third time in a completely different family.

And in absolute terms the best cell is **$2,025 of forced flow**, against which we would pre-position **$50**. Even a perfect prediction of a $2,025 order is not a business.

---

## 5. Price path around candidate events (descriptive only — NOT a test)

Per instruction, this is a post-hoc look and is not a hypothesis test. Since LCAP gives only n=4, the only cell with enough events to say anything is CMC20's 9 monthly index rebalance boundaries (2025-12-01 … 2026-08-01). Note again that CMC20's Folio is on BSC, so this measures the *index* rebalance, not Base-side flow.

Hourly path in `edge.bars_1h_year_50`, indexed to h=0 = 00:00 UTC on the 1st:

| h | n | mean return vs h=0 (bps) | se (bps) | mean WETH vol |
|---:|---:|---:|---:|---:|
| −24 | 9 | −10.1 | 40.5 | 1.44 |
| −6 | 9 | −8.2 | 18.3 | 1.69 |
| −3 | 9 | −7.5 | 9.3 | 2.11 |
| −1 | 9 | −1.8 | 8.3 | 1.50 |
| **0** | 9 | 0 | — | **2.72** |
| +1 | 8 | −19.6 | 16.3 | 2.41 |
| +3 | 8 | −23.6 | 21.1 | 2.28 |
| +6 | 8 | −17.5 | 19.7 | 3.80 |
| +12 | 7 | −33.0 | 32.0 | 2.12 |
| +24 | 9 | +20.7 | 59.2 | 1.87 |

Paired long-only round trips (buy at −k, sell at +m), against a **10.46 bps** round-trip cost floor (2 × 5.23):

| leg | n | gross (bps) | se (bps) | net of cost | 95% CI |
|---|---:|---:|---:|---:|---|
| buy h−24 / sell h+24 | 9 | **+30.8** | 97.4 | +20.3 | [−171, +212] |
| buy h−1 / sell h+24 | 9 | +22.5 | 62.9 | +12.0 | [−111, +135] |
| buy h−6 / sell h+6 | 8 | −6.4 | 17.5 | −16.9 | [−51, +18] |
| buy h−24 / sell h+48 | 9 | −25.2 | 93.7 | −35.7 | [−219, +148] |

Volume by day-of-month bucket — no calendar signature at all:

| bucket | bars | mean WETH/hr | mean swaps/hr |
|---|---:|---:|---:|
| 1st of month | 194 | 2.458 | 26.0 |
| 2nd | 201 | 2.950 | 25.1 |
| 28th+ | 667 | 1.778 | 22.5 |
| mid-month | 4,918 | 2.322 | 26.1 |

**Reading**: the largest point estimate is +30.8 bps with se 97.4 — a 95% interval **12× wider than the effect**, and the effect flips sign depending on the exit hour chosen (+30.8 at +24h, −25.2 at +48h, −6.4 at ±6h). This is noise with 9 draws, not a lead. The one nominally interesting column (h=+12, −33.0 ± 32.0) has the **wrong sign for a long-only account** — it would require shorting, which this program has ruled out as unimplementable.

**No effect here is large enough to justify a preregistered round.** The 30–143 bps cost floor is not even the binding constraint; the standard error is.

---

## 6. The cheapest possible KILL for this line

**Two HTTP GETs and one SQL query. Roughly two minutes, zero RPC credits, zero backfill.**

**Step 1 — kill on economics (one GET, ~5 s):**

```bash
curl -s "https://api.reserve.org/discover/dtf?chainId=8453" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(x['marketCap'] for x in d), len(d))"
# 5034510.4  13
```

If total addressable AUM across all on-chain index products on the chain is **$5.0M**, and 86% of it is in one fund, then no rebalance turnover assumption produces flow worth pre-positioning against. Stop here. *This is the whole kill.*

**Step 2 — kill on event count (one GET, confirms the schedule):**

Read the governance page of the largest product. `app.reserve.org/base/index-dtf/0x4da9a0f397db1397902070f93a4d6ddbc0e0e6e8/governance/` → 4 executed rebalances in 12 months, 2 defeated. **Quarterly, and governance-conditional.**

**Step 3 — kill on observability (one SQL, 51 s, no credits):**

```sql
SELECT countIf(sender IN (<Folio addrs>)) AS s, countIf(recipient IN (<Folio addrs>)) AS r,
       countIf(sender='0x2626664c2603336e57b271c5c0b26f421741e481') AS control, count()
FROM census.cl_swaps;   -- 0, 0, 50901113, 424433536
```

Zero counterparty footprint in 424M swaps. Events cannot be identified from the panel; they must be taken from published dates, and there are 4 of them.

### The power arithmetic that makes "wait for more data" futile

This is the number to quote when this idea is proposed again under a different name.

| Cell | events/yr | measured se | cost floor | events needed for CI half-width < cost | **years of waiting** |
|---|---:|---:|---:|---:|---:|
| CMC20 monthly boundary, h−24→h+24 | 12 | 97.4 bps (n=9) | 10.5 bps | 9 × (97.4/5.34)² = **2,994** | **250** |
| CMC20, merely to exclude zero at +30.8 | 12 | 97.4 bps | — | 9 × (97.4/15.7)² = **346** | **29** |
| LCAP quarterly → uSOL, 1-day hold | 4 | 80.5 bps (σ/√4) | 30.2 bps | 4 × (80.5/15.4)² = **109** | **27** |
| LCAP quarterly → uXLM, 1-day hold | 4 | 166 bps (σ/√4) | 72.6 bps | 4 × (166/37.0)² = **80** | **20** |

Underlying daily log-return volatility (measured, `edge.bars_1h_year_50`, 266–365 days each): CMC20 120 bps, LCAP 125, cbBTC 156, uSOL 161, uXRP 191, uADA 215, uDOGE 218, uXLM 332, HYPE 424.

**Required panel: 20 to 250 years.** Compare with Round 40's constraint on A2 multi-day mean reversion, which needed 4× the data and was declared blocked at "the panel can be mined at most 2 more years." This family is one to two orders of magnitude worse off, because the binding resource is not panel *length* but the *number of scheduled events the world actually produces*, and that is fixed at 4 per year by a published index rule.

**Corollary — the structural reason this family cannot work here, which generalizes:** calendar-determined forced flow trades event count against event size. A quarterly index gives you 4 large events; a weekly mechanism gives you 52 small ones. Base's index-DTF universe sits at the worst corner of that trade-off: its only large fund is quarterly, and its monthly funds are 23× smaller. **Do not reopen this under a new name unless a Base index product crosses roughly $50M AUM *and* rebalances at least monthly.** Neither condition is close: the largest is $4.31M and quarterly.

**One further condition that must be checked before any future attempt, because it is fatal on its own:** LCAP rebalances are approved by a `vlRSR-LCAP` vote among **34 voting addresses**, and **2 of 6** rebalance proposals in the panel window were **defeated**. A trade whose counterparty may simply vote not to trade is not forced flow. Any revival of this line must first show that the rebalance is contractually mandatory, not governance-discretionary.

---

## 7. What I could not determine — honest statement

1. **Per-rebalance turnover for LCAP.** I never observed an actual rebalance auction's trade sizes. `census.cl_swaps` contains swaps only — no `bid()` events, no `RebalanceStarted`/`AuctionOpened` logs, no Folio balances. The 5% turnover figure in §4d is my assumption, flagged as such. What I *do* have is a hard upper bound: total AUM $4.31M, so no rebalance can move more than that, and the *entire fund* is 0.44 days of cbBTC-pool volume. The conclusion is insensitive to the turnover assumption over any plausible range (1%–30%).

2. **Rebalance frequency for 9 of the 13 Base DTFs** — ABX, VLONE, CLX, VTF, AI, CLUB, BDTF, SBR, ZINDEX. `app.reserve.org/.../governance/` timed out repeatedly on WebFetch for these (the BGCI and LCAP pages did return). Their combined market cap is **$497,802**, so no frequency could rescue them; but the counts are genuinely unmeasured. Getting them would need the Reserve index subgraph or the `dtf-cli`, neither of which I used.

3. **Whether any DTF rebalance flow reaches Base CL pools via CoW Swap.** `GPv2Settlement` (`0x9008d19f…`) appears as `recipient` in **390,769** swaps in the panel. Reserve's Trusted Filler routes rebalance auctions to CoW solvers, so some of that flow is *probably* DTF rebalancing — but `census.cl_swaps` has no field that attributes a settlement swap to an originating order. Resolving this needs CoW order-book data (off-chain API), which is out of scope here. **This is the one genuine blind spot**, and note its direction: even if all 390,769 CoW swaps were DTF rebalances, that would only strengthen the observability kill, because it would mean the flow is deliberately routed through a solver network specifically designed to minimize its own price impact.

4. **Whether the Base CMC20 contract holds a basket.** Inferred as "no" from three independent sources (Reserve's Base DTF registry omits CMC20; Reserve docs place the Folio on BSC; Base supply is ~7% of circulating). Not confirmed by `eth_call`. A single `assets()` or `totalAssets()` call on `0xa0a8481f…` at any recent block would settle it for ~1 credit — worth doing only if someone wants to revive the CMC20 angle specifically.

5. **The exact UTC offset of the governance "Executed on" timestamps.** The page renders local time. I absorbed this with ±1-day event windows, which is why §4c tests 8 days for 4 events. A ±3-day window would only dilute further — the mean z on the tested windows is already negative.

6. **The identity of `0x278d858f05b94576c1e6f73285886876ff6ef8d2`**, which is the counterparty on 83.9% of CMC20 pool swaps with a perfectly balanced MM signature. Not identified. Does not affect the verdict, but it is the reason CMC20's pool is the cheapest non-major in the panel, and any future work on that pool should know who it is.

7. **Non-Reserve, non-Index-Coop basket products that do not use an index-like ticker.** My cross-reference was symbol-based against `census.tokens` (2,462 symbols). A product named without "index"/"20"/"FLI"/"basket" and not on my known-issuer list would have been missed. Given that the Reserve API enumeration is authoritative for the dominant issuer on Base and totals $5.03M, I judge the residual risk low, but it is not zero.

---

## Appendix — provenance

| Fact | Source | Verified how |
|---|---|---|
| CMC20 = CoinMarketCap 20 Index DTF, monthly rebalance | CMC methodology PDF, CMC chart page, Reserve `dtf.md` | web, 3 independent sources |
| CMC20 Folio on BSC, not Base | Reserve `dtf.md` table; `api.reserve.org/discover/dtf?chainId=8453` omits CMC20 | 2 sources + API |
| LCAP quarterly, 4 executed / 2 defeated | app.reserve.org LCAP governance page; CF Benchmarks blog | web, 2 sources |
| BGCI monthly | app.reserve.org BGCI DTF page (Bloomberg index rules) | web |
| $5,034,510 total Base index DTF market cap | `api.reserve.org/discover/dtf?chainId=8453`, saved to `/root/recon_out/dtf_base.json` | live API, 200 OK, 54,581 bytes |
| 0 / 424,433,536 Folio swaps | `census.cl_swaps` | own SQL, 2 working controls |
| mean z −0.088 vs +0.002 | `edge.bars_1h_year_50` | own SQL |
| all cost_p50 / tradeable_days | `edge.bars_universe_50` | own SQL, matched by token address |
| panel-median ETH $2,329.1 | `edge.eth_usd_1h` | own SQL |

Raw API response retained at `/root/recon_out/dtf_base.json`.
