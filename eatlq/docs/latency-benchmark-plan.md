# Latency Benchmark Plan (Static Contract)

Generated at: 2026-05-02  
Scope: documentation-only; planning only. No benchmark run, no RPC, no Docker, no implementation authorization.  
MM reference: MM-06 (Latency Benchmark Plan)  
Active guard: `mvp/audits/failed_no_7d_alpha_guard.md` (unchanged).

---

## Purpose

The whitepaper narrative may describe sub-millisecond or `<20ms` end-to-end timelines (ingest → signal → executor readiness). This document pins the engineering rule: **those numbers are not currently benchmark-proven** in this repository. Code paths exist (sequential ingestor → Redis → strategy → Redis → executor); measured wall-clock or stream-lag evidence is **absent** until a future benchmark artifact is produced under explicit authorization.

## Stable contract phrases

- **T+ms timeline is not currently benchmark-proven.**
- Claims such as `<20ms` tick-to-signal or similar **must not** be stated as measured facts without a cited benchmark artifact path under `mvp/` or `mvp/diagnostics/`.
- Static structure (services, streams) is **not** proof of latency.

---

## Future measurement points (no run authorized here)

When authorized, a benchmark should record timestamps at:

| Stage | Suggested probe |
| --- | --- |
| Ingestor publish | Redis `XADD` to `market_ticks` (or ingestor wall clock before write) |
| Strategy consume | First `XREADGROUP` delivery time for the same message ID |
| Signal publish | `XADD` to `signals` |
| Executor consume | First `XREADGROUP` on executor for that signal |

Lag can also be approximated from Redis stream consumer-group **pending** / **lag** fields (`XINFO GROUPS`, `XPENDING`) for a controlled replay fixture.

---

## Pass / fail (future)

Thresholds (e.g. P95 &lt; X ms) must be defined in the same artifact that authorizes the benchmark run. Until then, **no threshold is binding**.

---

## Forbidden

- Do not run production load tests from this document alone.
- Do not claim Phase 6 or live readiness from latency structure.
- Do not use this plan to lift `FAILED_NO_7D_ALPHA`.

---

**Decision flags:** IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
