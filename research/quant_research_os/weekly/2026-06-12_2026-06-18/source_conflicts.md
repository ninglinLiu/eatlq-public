# Source Conflicts — Round 5 Weekly

Conflicts are **preserved**, not merged.

---

## CFW-01: 15% feasibility tension

```yaml
conflict_id: CFW-01
object_a: UTW-004 (user: retry filters → 15%+ possible)
object_b: CNW-002 (agent: constant 15%+small DD incompatible in explored space)
conflict_type: GOAL_FEASIBILITY
status: UNRESOLVED
notes: CNW-006 refines — convex high months ≠ constant 15%
```

---

## CFW-02: top1_removed gate

```yaml
conflict_id: CFW-02
object_a: UTW-016 (user challenges top1_removed necessity)
object_b: RPW-014 + sprint097 (negative EV widespread, not single-pool)
conflict_type: METHODOLOGY
status: PARTIALLY_RESOLVED
notes: User concern active for KEYSTONE/convex; sprint097 addresses generic taker only
```

---

## CFW-03: Retry vs permanent reject

```yaml
conflict_id: CFW-03
object_a: UTW-004 (regime retry on failed lines)
object_b: CNW-010 (forced-seller permanent REJECT)
conflict_type: HYPOTHESIS_STATUS
status: UNRESOLVED
notes: Both valid at different scopes — user philosophy vs empirical record
```

---

## CFW-04: 25U authorization vs execution

```yaml
conflict_id: CFW-04
object_a: DCW-005 (user approved paper-forward + 25U live)
object_b: FLW-006 (no confirmed fill in Jun18 window)
conflict_type: PLAN_VS_EXECUTION
status: UNRESOLVED
notes: OUTSIDE_WINDOW_CONTINUATION Jun19+
```

---

## R3/R5 provenance

- keystone-go-live.md mtime vs content date — inherited from R3 CONF-01; not re-litigated.
