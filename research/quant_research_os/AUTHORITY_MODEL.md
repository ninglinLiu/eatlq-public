# Authority Model — Quant Research OS v1

Every canonical object SHOULD carry:

```yaml
authority_level:
  - GOLD           # Round 3 single-day full fidelity
  - VALIDATED      # Round 5/6 manual QA weeks
  - AUTO_CURATED   # Pipeline + promotion pass; medium trust
  - ARTIFACT_ONLY  # Git/audit/mtime only; no user transcript
  - UNVERIFIED     # Default for unpromoted objects
```

For conclusions involving trading, ALSO carry:

```yaml
execution_mode: BACKTEST | PAPER | LIVE | SHADOW | UNKNOWN
live_proven: true | false
```

## Global pack trust tiers

```text
MIXED_AUTHORITY
├── VALIDATED weeks (R5 Jun 12–18, R6 Jun 19–25)     → HIGH
├── AUTO_CURATED weeks (May–Jul promotion pass)        → MEDIUM
├── ARTIFACT_ONLY windows (9 classified)             → LOW / NOT_YET_CURATED
└── EMPTY_CONFIRMED (20 windows)                       → NO RESEARCH SIGNAL
```

## Window status taxonomy (replaces bare SKIPPED_EMPTY)

| Status | Meaning |
|--------|---------|
| QA_PASSED | Full pipeline + QA (includes R5/R6 validated) |
| EMPTY_CONFIRMED | No transcript AND no artifacts |
| ARTIFACTS_ONLY | Artifacts exist; no user transcript → NEEDS_REVIEW before canonical |
| NO_TRANSCRIPT_ACTIVITY | Assistant-only transcript lines |
| SOURCE_COVERAGE_INSUFFICIENT | User prompts but no artifact corroboration |
| NEEDS_REVIEW | Requires human curation |

## QA metrics (correct semantics)

```text
QA_PASS_RATE_AMONG_PROCESSED = QA_PASSED / NON_EMPTY_PROCESSED
  where NON_EMPTY_PROCESSED = windows that completed full weekly pipeline

QA_PASSED_SHARE_OF_ALL_WINDOWS = QA_PASSED / TOTAL_REGISTERED
  (includes EMPTY_CONFIRMED + ARTIFACTS_ONLY that never entered QA)
```

**Never conflate these two.**

## Agent loading policy

| authority_level | reuse_policy guidance |
|-----------------|----------------------|
| VALIDATED + ALWAYS_LOAD | Safe for live decisions |
| AUTO_CURATED | Topic-match only; verify execution_mode |
| ARTIFACT_ONLY | Audit reference only |
| UNVERIFIED | Never auto-load |
