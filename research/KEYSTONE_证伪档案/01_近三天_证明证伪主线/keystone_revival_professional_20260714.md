# KEYSTONE regime-gate revival falsification — professional report

## Executive result

B6 tested whether a causal exogenous “weather gate” could revive the B5-failed KEYSTONE long strategy. The rule was frozen before gate-result inspection and never referenced W1/W2/W3 labels. It required all three trailing inputs to pass: seven-day pool issuance ratio ≤1.25, seven-day honeypot proxy ≤20% with n≥20, and lagged seven-day ETH return ≥−5%.

Among 102 continuous calendar days, only 40 had complete inputs. The gate opened on **0/40 eligible days**, selected **0 opportunities**, and therefore produced no estimable return. A same-open-share random gate necessarily also opened zero days; its return distribution was undefined rather than favorable. All eight operationalized preregistered gates were false. B6 evidence status is **FAIL**; `go_decision=NOT_MADE`.

## Safety, scope and chronology

- Workspace: `/opt/EATLQ`; executor and strategy were confirmed stopped before and after analysis.
- B6 core analysis used local frozen B3/B4/B5 evidence and local ClickHouse SELECTs: network RPC=0. One archive `eth_blockNumber` preflight returned `0x2e5fc50`.
- No send, signing, broadcasting, service restart or live-config mutation occurred.
- Signal weather is evaluated at UTC day start and uses only observations strictly before that timestamp. W1/W2/W3 are post-hoc acceptance strata only (`used_by_gate=false`).
- Thresholds were frozen in `configs/research/base_b6_preregistration_20260714.json`, SHA-256 `c0c913de56ca47c911c412d487e34edd75501458246099cfd8e7817c005217a0`.
- Calibration segment ends 2026-04-15 00:00 UTC; the later segment is held out. Thresholds are fixed constants, not fitted to returns.
- Rolling validation uses fixed 14-day windows stepped every seven days from 2026-02-27 05:41:15 UTC.
- KEYSTONE source ends 2026-06-01 22:23:01 UTC, so W3 is partial relative to the requested end 2026-06-08 18:29:40 UTC.

## Evidence lineage: B3 → B4 → B5

### B3 exogenous environment

These labels are not gate inputs; the underlying continuous quantities motivate and audit the causal trailing calculations.

| Historical stratum | Mapped pools/day | Pool second/first rate | Young-pool WETH/day | Young-volume second/first | ETH return |
|---|---:|---:|---:|---:|---:|
| W1 | 12.423 | 0.0049 | 520.699 | 0.0008 | +4.7509% |
| W2 | 56.800 | 4.9373 | 11.769 | 1.1465 | +8.7582% |
| W3 | 46.891 | 1.8949 | 24.550 | 2.9487 | −30.1872% |

### B4 sellability facts

The p-fatal-gated 644-pool universe contained 262 delayed rugs: 260 LP pulls and two transfer toggles. Of 244 with known exit timing, four flipped before/at exit and 240 after exit.

The unbiased fresh-V3 census covered 1,265 pools. Confirmed same-block instant honeypots were 306/1,265=24.190% (Wilson 95% [21.910%, 26.625%]), concentrated in W2 at 304/408=74.510%. Atomic-unknown adverse rows were 224. Confirmed QuoterV2 reasonable-price false positives were 0/306, but the adverse H0a identification interval remained [0%, 42.264%], so H0a stayed inconclusive.

### B5 execution baseline reproduced by B6

All returns use decoded real swap integer amounts, 0.001 WETH notional, $0.075 per attempted leg, frozen limit-fill logic, and adverse treatment of missing entry/exit evidence.

| Strategy | W1 mean | W2 mean | W3 mean | Pooled mean | Top-1%-removed pooled |
|---|---:|---:|---:|---:|---:|
| Unconditional | −73.65% | −39.28% | −78.46% | −68.02% | −73.02% |
| Friction-only baseline | −69.83% | −22.46% | −14.85% | −50.06% | −55.72% |
| KEYSTONE always-on | +16.06% | −18.48% | −1.02% | **−4.6085%** | **−13.9451%** |
| D5-on adverse | +3.38% | −24.45% | −28.62% | −15.94% | −25.48% |
| General-feature migration upper bound | −3.14% | −9.83% | −17.54% | −9.20% | −16.59% |

B6 exactly reproduces always-on `n=116`, mean `−0.04608470016564725`.

## Frozen causal weather rule

| Input | Causal construction at day D 00:00 UTC | Frozen gate condition | Missing policy |
|---|---|---:|---|
| Issuance ratio | Pools/day in [D−7,D) divided by pools/day in [D−35,D−7); history begins 2026-01-23 | ≤1.25 | closed |
| Honeypot proxy | Among observable B4 fresh-V3 probes in [D−7,D), Quoter sell recovery <10%; unknown counts adverse for the gate | rate ≤20%, n≥20 | closed/adverse |
| ETH trend | Close(D−1)/Close(D−8)−1; current-day close forbidden | ≥−5% | closed |

The gate is open only when all three conditions pass. This is an AND rule; no result-dependent override is permitted.

### Proxy validation against measured Atomic truth

Unknown rows are excluded from association statistics and counted adverse only by the live gate.

| Measured n | TP | FP | FN | TN | Precision | Recall | Phi correlation | Unknown excluded |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,041 | 302 | 419 | 4 | 316 | 41.8863% | 98.6928% | 0.411578 | 224 |

The proxy catches nearly all measured severe cases but produces many false alarms. It is useful as a conservative alarm, not a precise disease classifier.

## Weather availability and gate decisions

| Calendar status | Days |
|---|---:|
| Requested continuous calendar days | 102 |
| Complete/eligible input days | 40 |
| True gate open days | 0 |
| Closed by issuance threshold among eligible days | 27 |
| Closed by honeypot proxy among eligible days | 40 |
| Closed by ETH trend among eligible days | 9 |

Failure counts overlap. Across eligible days, issuance ratio ranged 0.2064–972.0, proxy rate 60.6897%–98.4894%, proxy n=24–336, and lagged ETH return −22.3170%–+7.7247%. The proxy alone failed every eligible day.

## Overall, calibration and held-out results

| Segment | Universe n / mean | Always-on n / mean | True gate n / mean | True gate top1-removed |
|---|---:|---:|---:|---:|
| Calibration (<2026-04-15) | 263 / −66.9195% | 88 / −11.3060% | 0 / not estimable | not estimable |
| Held-out OOS | 168 / −69.7428% | 28 / +16.4410% | 0 / not estimable | not estimable |
| Pooled | 431 / −68.0200% | 116 / −4.6085% | 0 / not estimable | not estimable |

Zero exposure is not treated as positive expected return. OOS positivity therefore fails.

## Fixed rolling subwindows

Random-gate columns are empty because the frozen true open share is exactly zero.

| ID | Start | End exclusive | Unconditional n/mean | Always-on n/mean | True gate n/mean | Random p95 |
|---|---|---|---:|---:|---:|---:|
| R00 | 02-27 | 03-13 | 194 / −73.65% | 38 / +16.06% | 0 / — | — |
| R01 | 03-06 | 03-20 | 0 / — | 0 / — | 0 / — | — |
| R02 | 03-13 | 03-27 | 0 / — | 0 / — | 0 / — | — |
| R03 | 03-20 | 04-03 | 36 / −48.80% | 27 / −34.56% | 0 / — | — |
| R04 | 03-27 | 04-10 | 68 / −48.65% | 50 / −32.10% | 0 / — | — |
| R05 | 04-03 | 04-17 | 37 / −46.65% | 24 / −26.08% | 0 / — | — |
| R06 | 04-10 | 04-24 | 16 / −25.26% | 6 / +39.34% | 0 / — | — |
| R07 | 04-17 | 05-01 | 18 / −5.07% | 10 / +43.16% | 0 / — | — |
| R08 | 04-24 | 05-08 | 23 / −10.92% | 12 / +32.72% | 0 / — | — |
| R09 | 05-01 | 05-15 | 27 / −30.23% | 12 / +13.55% | 0 / — | — |
| R10 | 05-08 | 05-22 | 43 / −69.06% | 8 / −7.07% | 0 / — | — |
| R11 | 05-15 | 05-29 | 92 / −89.52% | 5 / −36.00% | 0 / — | — |
| R12 | 05-22 | 06-05 | 87 / −93.06% | 2 / −55.87% | 0 / — | — |
| R13 | 05-29 | 06-08 | 27 / −88.99% | 0 / — | 0 / — | — |
| R14 | 06-05 | 06-08 | 0 / — | 0 / — | 0 / — | — |

The source pool timestamps are batchy; empty and underpowered windows remain visible and are not used as favorable evidence.

## Same-open-share random gate

- Eligible calendar days: 40; true open days: 0.
- Draws: 10,000, each preserving exactly zero open days.
- Finite-return draws: 0; p95 undefined.
- True-gate mean is also undefined. Therefore `true_mean > random_p95` is false.

The SVG deliberately shows the degenerate zero-open case rather than inventing a distribution: `mvp/diagnostics/base_b6_random_gate_distribution_20260714.svg`.

## Capacity

| Selected opportunities | Open days | Open days with opportunities | Opportunities/open day | Total WETH notional | Max one-day WETH |
|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 0 | 0 | 0 | 0 |

The rule did not revive a small-capacity strategy; it shut the strategy off completely.

## Preregistered verdict

| Gate | Result |
|---|---|
| Pooled mean >0 | False |
| Top-1%-removed mean >0 | False |
| Every powered rolling window ≥−10% | False — no powered gate windows |
| At least two powered acceptance regimes have positive mean and alpha | False — zero powered regimes |
| True mean > same-share random p95 | False — both are non-estimable at zero share |
| MC terminal p5 >100R | False — MC not run, n=0 |
| MC MDD p95 ≤20R | False — MC not run, n=0 |
| Held-out OOS mean >0 | False — n=0 |

Evidence status: **FAIL**. This is not a GO/no-GO operational decision.

## Golden, tests, hashes and reproduction

- Five deterministic opportunity/weather records are frozen in `tests/fixtures/base_b6_golden_20260714.json` and asserted with exact equality.
- Eight regression tests cover exact B5 reproduction, no regime-label gate input, preregistration hash, measured-only proxy association, zero-RPC research posture, rolling/W3 coverage and zero-open failure semantics.
- RPC ledger: `mvp/diagnostics/base_b6_rpc_ledger_20260714.json` (one preflight read, zero analysis RPC).
- Complete input/output hashes are embedded in `mvp/diagnostics/base_b6_keystone_regime_gate_20260714.json`.

```bash
cd /opt/EATLQ
sha256sum configs/research/base_b6_preregistration_20260714.json
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python3 scripts/research/base_b6/b6_regime_gate.py
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python3 -m unittest tests.test_base_b6_regime_gate -v
```

## Limitations

1. W3 is partial after 2026-06-01.
2. Historical third-party swaps are counterfactual fill evidence, not proof that our own order would fill.
3. The proxy’s 41.89% precision causes severe over-closing. Lowering 20% after seeing zero openings would be post-selection and is not permitted in B6.
4. A future revival attempt requires a newly preregistered proxy/threshold and genuinely new OOS time; B6 cannot be repaired by editing this rule after the result.
