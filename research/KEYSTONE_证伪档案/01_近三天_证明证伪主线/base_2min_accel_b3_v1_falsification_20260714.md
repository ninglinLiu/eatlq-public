---
sop_applied: true
alpha_id: AR-B3-BASE-2MIN-ACCEL
family: F1/F6/F7
next_gate: HUMAN_RESULT_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
---

# B3 Base 2-minute acceleration v1 falsification report

**Research-only, no GO. Verdict: `NEEDS_REVIEW/INCONCLUSIVE`.** This run placed no order, made no signature or broadcast, started/restarted no live/executor/strategy service, and changed no live configuration. The verdict is a frozen-rule research classification, not a GO recommendation.

## 1. Schema copy-back and data reality

The implementation was written only after re-reading the schema authority. The real source columns are:

- `erc20_transfers`: `token_address, sender, recipient, amount_raw, block_number, tx_hash, log_index`.
- `uniswap_v3_swaps`: `pool_address, amount0_raw, amount1_raw, sqrt_price_x96_raw, liquidity_raw, tick_raw`; signed values are exposed by `eatlq_hex_to_int256` in the decoded view.
- `uniswap_v2_swaps`: `pair_address, amount0_in_raw, amount1_in_raw, amount0_out_raw, amount1_out_raw`.
- `uniswap_v3_mints/burns`: `pool_address, amount0_raw, amount1_raw, liquidity_raw, tick_lower_raw, tick_upper_raw`.
- `uniswap_v2_mints/burns`: `pair_address, amount0_raw, amount1_raw`.
- `013_decoded_views.sql`: `erc20_transfers_decoded.amount`, `uniswap_v2_swaps_decoded`, `uniswap_v3_swaps_decoded`, `eatlq_hex_to_uint256`, `eatlq_hex_to_int256`.
- `discovered_pools`: `pool_address, pool_type, token0_address, token1_address, token0_decimals, token1_decimals, fee_bps, has_weth_leg, created_block, created_tx_hash`.
- `block_ts_map`: `(chain_id, block_number) -> block_time`.
- Holder exclusions come only from `configs/research/base_attribution_exclusions.json` plus the pool, target token, dead address, WETH/stables.

Local `erc20_transfers` remains unsuitable for this history: 28,947 raw / 28,939 decoded rows, block range 42,525,716–46,243,704, with no adequate target-token coverage for the locked cohort. B3 therefore used the approved bounded archive replay. B2's beneficial-account attribution failure does not enter B3: this is pool/address balance mathematics, not trader identity resolution.

## 2. Frozen universe, time, and RPC audit

- Locked universe reproduced exactly: 3,331 pools / 3,330 target tokens, 2026-02-27 05:41:15 through 2026-06-08 18:29:39 UTC.
- W1/W2/W3: 407 / 1,547 / 1,377 pools. All 3,331 have a nonzero 120-second snapshot block; look-ahead violations: 0.
- Existing map only: no `block_ts_map` expansion. Across 1,003,850 locked map rows, `block_time_ms - 2,000*block_number` has exactly one distinct value. This proves the Base block-time affine relation over the entire locked interval; unmapped swap rows were therefore ordered by their real two-second block cadence while snapshot inputs remained bounded by the frozen mapped snapshot blocks.
- Fresh classification: 3,302 fresh; 29 old-token/new-pool; 0 freshness-unknown after retry. Old pools are excluded from every main return group.
- Frozen logical requests: 9,992 = 3,330 target-token ranges + 2×3,331 WETH pool-direction ranges.
- The archive provider rejected 43,200-block requests with HTTP 413 and accepted 10,000 blocks. Provider-mandated fixed subranges produced 28,596 calls in the complete main run; 21 incomplete logical ranges were then replayed at low concurrency in 25 calls. Final status: 9,992/9,992 complete; 329,619 raw rows = 329,619 `FINAL` exact request/event keys.
- Discarded operational attempts are not hidden: the initial 413 run consumed 900 failed calls, the capability probes consumed 2, and an aborted low-concurrency tuning run reached 1,304 calls. These do not contribute rows to the final research tables.

No token-genesis scan, full-inventory scan, or unapproved universe expansion occurred.

## 3. External regimes and frozen corrections

The preregistered external metrics remain:

| Window | Pools | ETH return | mapped pools/day | pool 2nd/1st | young WETH/day | young volume 2nd/1st | Label |
|---|---:|---:|---:|---:|---:|---:|---|
| W1 | 407 | +4.75% | 12.423 | 0.0049 | 520.699 | 0.0008 | sideways / launch contraction |
| W2 | 1,547 | +8.76% | 56.800 | 4.9373 | 11.769 | 1.1465 | sideways / supply expansion, weak demand |
| W3 | 1,377 | −30.19% | 46.891 | 1.8949 | 24.550 | 2.9487 | bear / launch-turnover expansion |

These are two macro regimes (sideways and bear), with W1/W2 separated by launch microstructure. They are not presented as three independent macro regimes.

The Attachment II cost correction was frozen before replay: 0.001 WETH notional; actual swap amount price, which embeds venue fee and that swap's pool impact; USD 0.075 per attempted leg; minimum USD 0.15 for a filled buy plus attempted terminal sell. `ROUND_TRIP_COST=0.006` is absent. The sample-priority correction was also frozen: `n<20` is untested and can only yield INCONCLUSIVE/NEEDS_REVIEW.

## 4. Feature funnel and proxy bias

| Window | Fresh pools | Volume gate + acceleration | + true reserve acceleration | + true holder acceleration | Real all-three | Proxy |
|---|---:|---:|---:|---:|---:|---:|
| W1 | 394 | 18 | 18 | 17 | **17** | **17** |
| W2 | 1,544 | **0** | 0 | 0 | **0** | **0** |
| W3 | 1,364 | 162 | **0** | 49 | **0** | **47** |

This confirms the requested W2 zero-trigger result in both real and proxy versions. The W3 single-window probe is also unambiguous: 162 fresh pools clear real volume acceleration, but none doubles its true WETH stock reserve; the real signal has `n=0`. The RH-style proxy instead creates 47 W3 hits.

Real/proxy sets: real 17, proxy 64, intersection 17, union 64, Jaccard 0.265625, proxy-only 47, real-only 0. The proxy hit count is 276% above the real count. In W1 the sets are identical; the entire bias is W3's `volume/2` liquidity proxy.

EOA sensitivity was limited to the only 18 pools capable of clearing volume+reserve gates. Archive `eth_getCode` checked 3,532 newly-positive addresses; all were EOA at the snapshot. Real hit counts remain 17/0/0, so the EOA sensitivity changes zero decisions.

## 5. Real-swap returns and costs

All numbers are opportunity-level ITT returns. Unfilled entries contribute zero. A filled position with no terminal sell realizes the unsold fraction at −100%; no row is deleted.

| Group/window | n | Ladder net mean | Top-1%-removed mean | Top-1% positive contribution |
|---|---:|---:|---:|---:|
| Real W1 | 17 | **+27.62%** | +19.96% | 22.11% |
| Real W2 | 0 | NO_OPPORTUNITY | — | — |
| Real W3 | 0 | NO_OPPORTUNITY | — | — |
| Proxy W1 | 17 | +27.62% | +19.96% | 22.11% |
| Proxy W3 | 47 | **−97.94%** | −100.07% | 0% |
| Real pooled | 17 | +27.62% | +19.96% | 22.11% |
| Proxy pooled | 64 | **−64.59%** | −68.00% | 22.11% |
| Volume-only pooled | 564 | −54.98% | −58.14% | 49.09% |
| Unconditional pooled | 3,302 | −2.54% | −12.07% | 99.26% |

W1's real mean is +30.29 percentage points above its same-window unconditional mean (−2.67%), but `n=17`; it is descriptive and cannot establish merit. W3 proxy has 45/47 filled entries and 39 opportunities at net return `<=−100%`. This is evidence that the proxy admits economically bad pools, not a FAIL verdict for the true signal.

Cost decomposition:

| Group | Gross pooled mean | Net pooled mean | Mean gas USD/opportunity | Gross→net drag |
|---|---:|---:|---:|---:|
| Real | +41.23% | +27.62% | $0.2647 | 13.61 pp |
| Proxy | −55.02% | −64.59% | $0.1828 | 9.56 pp |
| Volume-only | −48.14% | −54.98% | $0.1293 | 6.84 pp |
| Unconditional | −1.18% | −2.54% | $0.0258 | 1.36 pp |

The low unconditional mean gas reflects many unfilled ITT rows at zero gas. Filled ladder rows can require more than two legs, so the real-signal mean gas exceeds USD 0.15.

Observed selected-swap average-price deviation versus the frozen audit reference (V3 same-row post-swap marginal price; V2 nearest prior execution): real mean/median/p95 = 81.2/29.9/414.6 bps over 56 swaps; proxy = 183.6/84.1/689.8 bps over 107 swaps. It is not subtracted twice: actual amount-derived entry/exit prices already contain pool fee and realized swap impact.

Uncapped control: real W1 `n=17`, net mean +103.29%, top-1%-removed +72.17%; proxy W3 `n=47`, net mean −103.34%. The ladder materially truncates W1's right tail, as preregistered.

The 5-minute control cannot replace the primary: real W1 `n=2`, mean −23.80%; W2/W3 real `n=0`. Proxy W1/W3 each have `n=5`; pooled proxy mean +223.80% but top-1%-removed mean −28.30% and positive contribution 93.29%. Every 5-minute scope is underpowered.

No Monte Carlo gate was run because no real window has `n>=20`. Running a powered-scope MC on zero powered scopes would manufacture precision.

## 6. Golden and data coverage audit

Archive golden contains four cases:

- Three locked standard pools (one V2, two V3): all six pool reserve checks and all 70 holder balance checks are exact raw-integer `transfer_net == balanceOf` equalities. The reconstructed holder count also exactly equals the number of queried positive balances.
- One separate fee-on-sell sentinel, RXSby (`0xfd86…6b9`), has cached Honeypot simulation sell tax 100%. It is explicitly outside the locked universe and contributes to no signal or return. Its WETH/target transfer-net also equals `balanceOf`; a sell tax does not necessarily break ERC-20 Transfer accounting.
- Golden archive calls: 88 per reproducible run. Fixture hash is recorded below.

The required mint/burn/swap event-side equality does **not** pass. In the locked 2-minute ranges the local mint and burn decoded surfaces contain zero rows, while swaps and archive transfers are present. For example, the golden event reconstruction can be negative while archive transfer-net and `balanceOf` are identical and positive. This is a local historical event-coverage gap, not averaged away. It independently forces NEEDS_REVIEW under the frozen golden rule; the primary reserve remains transfer-net, never the incomplete event proxy.

Relevant 2-minute surface coverage:

| Surface | W1 rows / block range | W2 | W3 rows / block range |
|---|---|---|---|
| RPC target transfers (lookback→5m) | 110,303 / 42,648,166–44,092,525 | 26,373 / 44,122,124–45,401,574 | 133,089 / 45,379,630–47,077,160 |
| RPC WETH pool transfers (0→2m) | 8,559 / 42,690,764–44,092,478 | 7,396 / 44,631,349–45,401,497 | 7,544 / 45,402,569–47,077,061 |
| V3 swaps (0→2m) | 8,436 / 42,690,764–44,092,478 | 0 | 1,264 / 46,505,805–47,076,032 |
| V2 swaps (0→2m) | 0 | 0 | 2,532 / 46,243,846–47,077,061 |
| V2/V3 mints and burns (0→2m) | 0 | 0 | 0 |

Two Basescan transaction anchors for selected real entries:

- [block 42,700,731, 0.45 WETH buy](https://basescan.org/tx/0x2fbb1bd549080679f542e5ae724fbbaa1febad7e0c50dfb9b18dc49523b5c999)
- [block 42,701,855, 0.3465 WETH buy](https://basescan.org/tx/0x622e9b03f1f7dc135cc4d9c00cd7dad0cdc027bca1ba1f4f11d8d6df729523f7)

Both selected swaps are far larger than the fixed 0.001 WETH research notional.

## 7. Frozen verdict application

1. W1 real `n=17`: underpowered, so its positive observed return is descriptive only.
2. W2 real `n=0`: `NO_OPPORTUNITY`, not zero-return FAIL.
3. W3 real `n=0`: the single-window probe is untested on merit, not a W3 collapse with `n>=20`.
4. Fewer than two distinct-regime windows are powered; event-side golden equality also fails due missing local mint/burn history.

Therefore the only allowed v1 verdict is **`NEEDS_REVIEW/INCONCLUSIVE`**. It is not PASS and not FAIL. The true-value signal's per-trade merit and cross-regime stability remain untested. What is reproducibly established is narrower: the RH proxy creates 47 W3 false-positive hits with very poor realized returns, and W2 has zero trigger density.

No GO, training, promotion, or live action follows from this result.

## 8. Reproduction

```bash
cd /opt/EATLQ
python3 scripts/research/base_2min_accel_b3.py universe
python3 scripts/research/base_2min_accel_b3.py fetch --workers 48
python3 scripts/research/base_2min_accel_b3.py retry --workers 6
python3 scripts/research/base_2min_accel_b3.py features
python3 scripts/research/base_2min_accel_b3.py eoa --workers 4
python3 scripts/research/base_2min_accel_b3.py backtest
python3 scripts/research/base_2min_accel_b3_golden.py
python3 -m unittest tests.test_base_2min_accel_b3 -v
python3 scripts/check_alpha_sop_compliance.py reports/base_2min_accel_b3_v1_falsification_20260714.md
```

Primary hashes after the final run:

- result JSON: `21bebd7ebdd323640f28958d34475ceb8692adeba2798c6f8936cae1bc03fcb2`
- golden fixture: `356832b7c83aa717e1e1375ffd77bbbd4a1fd7f4fd355140a3411172dacd3b81`
- ETH daily fixture: `728fc565687dddf9792553fd5ffc31072cd260b00dbe451bea54c9cad2f8e129`
- frozen preregistration: `f8335c45f73fe513fc991d5143c3a085f47fa3dfad5b7ad76f1e42b9fc041ef9`
