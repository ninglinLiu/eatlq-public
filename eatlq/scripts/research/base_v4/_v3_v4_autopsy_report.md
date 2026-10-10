# V3/V4 Differential Autopsy

Question: **Is V3 +72% from demand migration, or from lower friction / executable price paths?**

## Headline

| Cohort | n | med fric | med tick MFE | P(tick MFE>5%) | med executable | P(win) |
|--------|---|----------|--------------|----------------|----------------|--------|
| V3 live gated | 121 | -1.1% | 159.7% | 91.7% | 78.1% | 79.3% |
| V3 winners | 96 | -1.1% | 218.1% | 100.0% | 113.9% | — |
| V4 wave2 final | 4 | -2.6% | 0.4% | 25.0% | -0.5% | 25.0% |
| V4 clean init (baseline) | 112 | -2.6% | — | — | — | 0.0% |

## Dimension table (V3 live gated vs V4 wave2)

| Dimension | V3 | V4 wave2 |
|-----------|----|---------|
| entry executable MFE>5% | None | 0.0% |
| tick MFE median | 1.5972 | 0.4% |
| P(tick MFE>5%) | 91.7% | 25.0% |
| tick peak NOT executable | 12.4% | 25.0% |
| buy/sell entry friction | -1.1% | -2.6% |
| forward executable (final/trail) | 78.1% | -0.5% |
| P(executable win) | 79.3% | 25.0% |
| pre-entry activity (ns_pre) | 206.0 | None |
| pool/hook type | None | None |

## Verdict

- ROOT CAUSE: V3 has both tick peaks AND executable capture; V4 may show tick peaks with ZERO executable capture — not a timing/exit problem.
- V3 +72% raw is NOT genesis friction: entry roundtrip fric median ~−1% vs V4 ~−2.6%.
- V3 live cohort (n=121): tick path MFE median 1.5972 AND executable real_net median 0.7809 — peaks are largely capturable via QuoterV2+trail+realgas.
- V4 wave2 (n=4): executable roundtrip MFE>5% = 0; tick can show peaks that roundtrip cannot (tick-not-exec rate 0.25).
- V3 edge = lower hook friction + demand-driven tick path that STAYS executable on UniV3 router path.
- V4 failure mode = returns_delta / hook tax makes tick movement a mirage for roundtrip/buy-sell strategies.
- HARD RULE: any future chain/protocol — measure executable MFE (buy→hold→sell or roundtrip grid) BEFORE tick MFE.

## Hard rule (portable)

> **Any chain / pool / protocol: measure executable MFE first. Ignore tick-only MFE for go/no-go.**

Artifacts: `/opt/EATLQ/scripts/research/base_v4/_v3_v4_autopsy.json`
