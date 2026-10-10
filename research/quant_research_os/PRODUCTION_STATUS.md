# Quant Research OS — Production Status (v1)

**Updated:** 2026-07-11 after Authority Promotion Pass

---

## Correct QA metrics

| Metric | Value | Notes |
|--------|------:|-------|
| **TOTAL_REGISTERED_WINDOWS** | 39 | |
| **NON_EMPTY_PROCESSED_WINDOWS** | 10 | Completed full weekly pipeline |
| **QA_PASSED_WINDOWS** | 10 | Includes R5 + R6 validated imports |
| **QA_PASS_RATE_AMONG_PROCESSED** | **100%** | 10/10 — not 26% |
| **QA_PASSED_SHARE_OF_ALL_WINDOWS** | 26% | 10/39 — share only, NOT pass rate |
| EMPTY_CONFIRMED | 20 | No transcript, no artifacts |
| ARTIFACTS_ONLY | 9 | Artifacts without user transcript |

---

## Window status taxonomy (SKIPPED_EMPTY retired)

Bare `SKIPPED_EMPTY` replaced by artifact fallback classification. See `AUTHORITY_MODEL.md`.

---

## Authority tiers (Global Pack = MIXED_AUTHORITY)

| Tier | Weeks | Trust |
|------|-------|-------|
| **VALIDATED** | R5 Jun 12–18, R6 Jun 19–25 | HIGH |
| **AUTO_CURATED** | 8 May–Jul dense weeks (promotion pass) | MEDIUM |
| **ARTIFACT_ONLY** | 9 windows (Mar–May mostly) | LOW — not yet curated |
| **EMPTY_CONFIRMED** | 20 windows | No research signal |

All global objects now carry `authority_level`. Conclusions carry `execution_mode` / `live_proven` where inferrable.

---

## Authority Promotion Pass results

### A. May–Jul auto week enrichment

Hypotheses + experiments promoted from selected lineage (AUTO_CURATED). Conclusions upgraded with `can_prove` / `cannot_prove` / `execution_mode`.

### B. Artifact fallback (29 former SKIPPED_EMPTY)

| Result | Count |
|--------|------:|
| EMPTY_CONFIRMED | 20 |
| ARTIFACTS_ONLY | 9 |
| NO_TRANSCRIPT_ACTIVITY | 0 |

Detail: `state/artifact_fallback_report.jsonl`

---

## Index scale (unchanged)

| Metric | Value |
|--------|------:|
| Raw events | 12,927 |
| Selected events | 3,069 |
| Global canonical (post-promotion) | see global/*.jsonl |

---

## Final ratings (revised)

```text
HISTORICAL RECOVERY QUALITY:        HIGH (validated + dense weeks)
CANONICAL KNOWLEDGE QUALITY:        MEDIUM-HIGH (MIXED_AUTHORITY — see tier table)
SOURCE TRACEABILITY:                HIGH
QA_PASS_RATE_AMONG_PROCESSED:       100%
QA_PASSED_SHARE_OF_ALL_WINDOWS:     26%
COST_EFFICIENCY:                    HIGH
READY_FOR_CROSS_VPS_MERGE:          PARTIALLY — after ARTIFACTS_ONLY curation
```

---

## VPS graduation checklist

- [x] Quant Research OS v1 structure
- [x] Correct QA metric semantics
- [x] SKIPPED_EMPTY → fine-grained taxonomy
- [x] authority_level on objects
- [x] May–Jul promotion pass
- [x] Artifact fallback classification
- [ ] Manual dedup of auto-promoted experiments (some over-count from regex)
- [ ] Curate 9 ARTIFACTS_ONLY windows
- [ ] Round 3 GOLD objects linked with authority_level: GOLD

**Recommendation:** Complete ARTIFACTS_ONLY curation on this VPS before cloning to Robinhood / Hyperliquid / CEX VPSs.

---

## Commands

```bash
python3 pipeline/artifact_fallback.py      # re-classify empty windows
python3 pipeline/authority_promotion.py    # stamp authority + enrich
python3 pipeline/global_consolidator.py    # rebuild global pack
```
