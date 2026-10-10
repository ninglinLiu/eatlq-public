# Current State — VPS Global

**Authority: MIXED_AUTHORITY** — see `AUTHORITY_MODEL.md`. Do not treat all objects as equally reliable.

## Window metrics (correct semantics)

| Metric | Value |
|--------|------:|
| TOTAL_REGISTERED_WINDOWS | 39 |
| QA_PASSED_WINDOWS (full pipeline) | 10 |
| EMPTY_CONFIRMED | 20 |
| ARTIFACTS_ONLY (needs curation) | 9 |
| **QA_PASS_RATE_AMONG_PROCESSED** | **100%** (10/10) |
| QA_PASSED_SHARE_OF_ALL_WINDOWS | 26% |

Note: `QA_PASSED share` ≠ `QA pass rate`. EMPTY_CONFIRMED and ARTIFACTS_ONLY did not enter full QA.

## Global object authority mix

- AUTO_CURATED: **527**
- VALIDATED: **139**

## Canonical totals (deduped)

- user_theses: **86**
- research_principles: **20**
- hypotheses: **76**
- experiments: **353**
- conclusions: **59**
- failures: **28**
- methods: **12**
- decisions: **32**

## Known live facts (authority: VALIDATED, R6)

- KEYSTONE live: 2× PARADAENGI fills Jun 23, ABANDON −100%, total **−0.114 ETH**
- **No confirmed profitable round-trip** (`live_proven: false`)
- SYSTEM_PANIC: active after Jun 23 disaster
- deep_edge_no: Jun 25 (AUTO_CURATED promotion pass)

## Example: how to read a conclusion

```yaml
statement: KEYSTONE holdout +59.7%
authority_level: VALIDATED
execution_mode: BACKTEST
live_proven: false
```

## UNKNOWN_AFTER_SEARCH

- Current executor running state (Jul 11+)
- Profitable round-trip completion
- ChatGPT-web / other-VPS research not indexed
