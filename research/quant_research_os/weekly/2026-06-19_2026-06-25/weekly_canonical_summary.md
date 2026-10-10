# Weekly Canonical Summary — Blind (2026-06-19 to 2026-06-25)

## Epistemic arc

```text
Jun 19  paper-forward / pre-live audit HOLD
Jun 21  first live honeypot + infra gaps (no TG/monitor)
Jun 22  disaster left-tail audit → config hardening
Jun 23  go-live → 2× PARADAENGI −100% (−0.114 ETH) → panic
Jun 24  Stage 1.0 price PASS; exit lookahead; go-live session docs
Jun 25  aggressive rebacktest; deep_edge_no pivot; KEYSTONE origin search
```

## Main research threads (discovered, not preset)

| Thread | Period | Status |
|--------|--------|--------|
| KEYSTONE hardening / paper-forward | 6/19 | ✅ DC6-001 |
| Signal bridge + executor infra | 6/19–21 | ⚠️ FL6-003 |
| Capital / sizing / MEV debate | 6/21–22 | ✅ UT6-001/002 |
| Live audit series (MEV, left-tail, alerts) | 6/22–23 | ✅ MT6-001 |
| Controlled go-live + honeypot disaster | 6/23 | ✅ CN6-001 |
| Honeypot defense v1/v2 + survivorship | 6/23 | ✅ FL6-001/005 |
| Exit logic lookahead | 6/24 | ✅ EX6-004 |
| Aggressive live rebacktest | 6/25 | ✅ HY6-006 |
| Base deep-survivor pivot | 6/25 | ✅ CN6-005 |
| KEYSTONE origin retrospective | 6/25 | HISTORICAL_ONLY |

---

## Top 20 long-reusable knowledge

1. **RP6-001** — Authorization ≠ execution confirmed
2. **RP6-002** — QuoterV2 / state-override ≠ real sellability
3. **RP6-003** — Audit conclusions require n; n=1 cannot move params
4. **RP6-004** — SYSTEM_PANIC fail-closed on Redis errors
5. **RP6-005** — Time-split TRAIN→TEST; no random holdout
6. **CN6-002** — Live left tail > backtest left tail (shared blind spot)
7. **CN6-001** — No normal buy→sell loop before Jun 23 disaster
8. **FL6-001** — state-override sell_sim bypass guardrail
9. **FL6-002** — honeypot.is schema parser guardrail
10. **FL6-004** — GoPlus reference-only for newborn tokens
11. **FL6-005** — Report population truth vs 131 survivor sample separately
12. **UT6-006** — No post-hoc hard gates (inherit KEYSTONE first principles)
13. **UT6-001** — Validate infra loop before scaling capital
14. **MT6-003** — Smoke under panic with manual $5 buy
15. **MT6-002** — Fork-replay population ground truth
16. **CN6-003** — ETH/USD sizing price cross-check methodology
17. **CN6-007** — Left-tail P0 config epoch (post Jun 22)
18. **HY6-004 INVALIDATED** — Entry sell_sim useless for delayed rug
19. **CN6-005** — deep_edge_no honest negative (exploratory n=16)
20. **DC6-005** — Post-disaster panic is default until user releases

---

## Top 10 failure / guardrail

| ID | Guardrail |
|----|-----------|
| FL6-001 | Never treat state-override/eth_call as sellability ground truth |
| FL6-002 | Parse honeypot.is flags/holderAnalysis, not missing boolean fields |
| FL6-003 | Live fill → TG + position monitor before "infra ready" |
| FL6-004 | GoPlus alone cannot pass newborn memecoins |
| FL6-005 | Survivor-biased 131 sample ≠ population honeypot rate |
| FL6-006 | Panic key fail-closed + alert on Redis errors |
| RP6-001 | execution_confirmed for live PnL claims |
| RP6-002 | Label backtest with same lens as live pretrade |
| MT6-003 | Strategy smoke ≠ infra smoke; don't unpanic for $5 test |
| CN6-004 | Destruction rate is methodology-dependent (3.8% vs 31%) |

---

## Supersession / invalidation / pivot

**Invalidated:**
- HY6-002 external gate alone
- HY6-003 backtest 3.8% destruction as live proxy
- HY6-004 entry sell_sim for delayed rug

**Superseded:**
- CN6-006 (pre-fix left-tail config) → CN6-007

**Pivot (not correction):**
- UT6-005 / EX6-005 → deep-survivor new battlefield (KEYSTONE early-pool not abandoned, parallel research)

---

## Live status (strict epistemic labels)

| Event | Status |
|-------|--------|
| Jun 19 paper-forward authorized | AUTHORIZED |
| Jun 21 first buy (~$1.9 honeypot) | LIVE_FILL_CONFIRMED |
| Jun 23 2× PARADAENGI | LIVE_FILL_CONFIRMED, ABANDON −100% |
| Jun 23 normal sell loop | **NOT CONFIRMED** |
| Jun 24 $50 go-live session | PARTIAL — see go_live_session audit |
| Normal profitable round-trip | **UNKNOWN / NOT CONFIRMED** in window |

---

## Unresolved

See `source_conflicts.md` — CF6-01 through CF6-03.
