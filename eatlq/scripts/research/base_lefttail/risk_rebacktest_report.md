# Keystone Risk Rebacktest

Methodology caveat: GoPlus/Honeypot.is overlay is current-state scanner data, not historical block-state.

## Scenario Summary

| scenario | trades | sizes | mean | median | fatal | $1000 cap monthly | $1000 cap MDD |
|---|---:|---|---:|---:|---:|---:|---:|
| none_haircut_0% | 131 | {'1000': 36, '300': 81, '100': 14} | +56.2% | +29.2% | 3.8% | +162.5% | -18.2% |
| none_haircut_2% | 131 | {'1000': 36, '300': 81, '100': 14} | +54.2% | +27.2% | 3.8% | +158.5% | -19.2% |
| none_haircut_5% | 131 | {'1000': 36, '300': 81, '100': 14} | +51.2% | +24.2% | 3.8% | +151.9% | -19.9% |
| none_haircut_10% | 131 | {'1000': 36, '300': 81, '100': 14} | +46.2% | +19.2% | 5.3% | +141.2% | -20.8% |
| scanner_only_haircut_0% | 6 | {'1000': 3, '300': 3, '100': 0} | +26.1% | +40.5% | 16.7% | +7.7% | -6.6% |
| scanner_only_haircut_2% | 6 | {'1000': 3, '300': 3, '100': 0} | +24.1% | +38.5% | 16.7% | +7.1% | -6.8% |
| scanner_only_haircut_5% | 6 | {'1000': 3, '300': 3, '100': 0} | +21.1% | +35.5% | 16.7% | +6.1% | -7.1% |
| scanner_only_haircut_10% | 6 | {'1000': 3, '300': 3, '100': 0} | +16.1% | +30.5% | 16.7% | +4.5% | -7.6% |
| strict_haircut_0% | 6 | {'1000': 3, '300': 3, '100': 0} | +26.1% | +40.5% | 16.7% | +7.7% | -6.6% |
| strict_haircut_2% | 6 | {'1000': 3, '300': 3, '100': 0} | +24.1% | +38.5% | 16.7% | +7.1% | -6.8% |
| strict_haircut_5% | 6 | {'1000': 3, '300': 3, '100': 0} | +21.1% | +35.5% | 16.7% | +6.1% | -7.1% |
| strict_haircut_10% | 6 | {'1000': 3, '300': 3, '100': 0} | +16.1% | +30.5% | 16.7% | +4.5% | -7.6% |

## Fatal Multiplier Stress

| multiplier | assumed fatal | median end | p10 end | bankruptcy | median MDD |
|---:|---:|---:|---:|---:|---:|
| 1x | 3.8% | $22,931 | $16,136 | 0.0% | -11.4% |
| 2x | 7.6% | $19,163 | $13,280 | 0.0% | -15.2% |
| 5x | 19.1% | $9,787 | $4,953 | 0.0% | -30.5% |
| 10x | 38.2% | $442 | $98 | 0.0% | -80.1% |
| 25x | 95.4% | $0 | $0 | 0.0% | -100.0% |

## Active Exposure (Butterfly) Model

- Pools: 743 total, 542 with >=3 real buys.
- True rug/unsellable (real-gas revert): 181 total; among live pools rug is rare.
- Fraction of pools where $1000 exceeds 5% of WETH depth: 0.4040590405904059.
- Observed exposure penalty by buyer share: rug {'100': 0.0, '1000': 0.0}, death {'100': 2.183453787200594e-05, '1000': 0.0015611694578484246} (confounded lower bound).
- max_buy_share is observed largest buyer; high-share pools self-select for live pools. Observational penalty is a lower bound.

### Exposure-adjusted stress (data base rate + confound margin)

| confound margin | base rug | big-fish frac | median end | p10 end | bankruptcy | median MDD |
|---:|---:|---:|---:|---:|---:|---:|
| 0% | 0.7% | 8% | $26,489 | $19,271 | 0.0% | -6.4% |
| 2% | 0.7% | 8% | $26,361 | $19,045 | 0.0% | -7.2% |
| 5% | 0.7% | 8% | $26,037 | $18,656 | 0.0% | -9.3% |
| 10% | 0.7% | 8% | $25,197 | $18,154 | 0.0% | -10.0% |

## Known Risks

- Modeled: size-dependent slippage, QuoterV2 friction, real-gas sell reverts, API scanner overlay, gas cost, MEV/latency haircuts.
- Partially modeled: larger size attracting adversarial behavior via fatal multipliers; current-state scanner data as a conservative proxy.
- Not fully modeled: future owner toggle, proxy upgrade after our buy, address-specific blacklist after first few buyers, private mempool failure, and adaptive attackers reacting to our wallet.
