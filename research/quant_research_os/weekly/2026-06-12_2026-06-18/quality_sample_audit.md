# Quality Sample Audit — 20 high-value lineage items

Manual-style review of selected events. Source: `selected_lineage.jsonl` + Round 2 chains.

---

## 5× User challenges

| # | ID / source | Check | Pass? |
|---|-------------|-------|-------|
| 1 | UTW-013 jsonl ~558 (体检≠送钱) | User original, not pasted | ✅ |
| 2 | UTW-014 forced-seller 傻钱 (6/13) | CHALLENGE type correct | ✅ |
| 3 | UTW-015 zero onset (6/13) | Linked to CC-01 not wrong causal | ✅ |
| 4 | UTW-016 top1 PF (6/13) | User objection preserved | ✅ |
| 5 | deployer≠operator jsonl 22 (6/18) | CORRECTS agent misID | ✅ |

**Risk noted:** UTW-013 user message contains long pasted articles (6/12 P-004) — classified MIXED_CONTENT in raw index; thesis extracted from user framing only. ✅

---

## 5× Agent conclusions

| # | Source | Check | Pass? |
|---|--------|-------|-------|
| 6 | sprint097 verdict pointer | EMPIRICAL_RESULT not live | ✅ |
| 7 | jsonl 33 operator −38/−50% | Result not overstated as live | ✅ |
| 8 | jsonl 198 KEYSTONE +59.7% | Marked backtest; MEV caveat | ✅ |
| 9 | jsonl 71 six-method table | TRAIN/holdout flip preserved | ✅ |
| 10 | jsonl 6 opening 15% recap | AGENT_EXPLICIT authorship | ✅ |

---

## 3× Errors / corrections

| # | Chain | Check | Pass? |
|---|-------|-------|-------|
| 11 | CC-01 pipeline vs market | correction not permanentized wrongly | ✅ |
| 12 | CC-05 deployer misID | FLW-001 guardrail linked | ✅ |
| 13 | CC-06 ath_mc label | RPW-005; not called user error | ✅ |

---

## 3× Pivots

| # | Pivot | Check | Pass? |
|---|-------|-------|-------|
| 14 | CC-07 audit reframe | pivot not labeled "correction" only | ✅ |
| 15 | CC-08 KEYSTONE | PIVOTS_FROM not SUPERSEDES of all alpha | ✅ |
| 16 | HYW-006 status SUPPORTED | not conflated with live READY | ✅ |

---

## 2× Execution authorization

| # | Decision | Check | Pass? |
|---|----------|-------|-------|
| 17 | DCW-007 P0-P3 audit | execution_confirmed true | ✅ |
| 18 | DCW-005 25U live | execution_confirmed **false** | ✅ |

---

## 2× Unresolved

| # | Item | Check | Pass? |
|---|------|-------|-------|
| 19 | CFW-01 15% tension | Both sides kept | ✅ |
| 20 | CFW-04 25U fill | Not backfilled from Jun19 | ✅ |

---

## Aggregate QA

| Failure mode | Found? |
|--------------|--------|
| Wrong context linking | 0 critical |
| Pasted content as user view | 1 mitigated (UTW-013 MIXED) |
| Same-day ≠ causation overclaim | 0 (relations use confidence) |
| Plan as execution | Prevented via FLW-006 / DCW-005 |
| Old conclusion permanentized | Prevented via valid_from/until |

**Sample audit result: PASS (20/20 with 1 documented mitigation)**
