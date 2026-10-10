# Source Conflicts — Round 6 Blind

Preserved, not merged.

---

## CF6-01: Destruction rate vs aggressive backtest

```yaml
conflict_id: CF6-01
object_a: CN6-004 (destruction ~31% under aggressive relabel)
object_b: HY6-006 (aggressive live config still positive on cache)
conflict_type: METHODOLOGY
status: UNRESOLVED
notes: Different lenses — relabeled left tail vs QV2/fork cache EV; live-only gaps remain
```

---

## CF6-02: Deploy urgency vs validation patience

```yaml
conflict_id: CF6-02
object_a: UT6-002 (crypto alpha window too short to wait)
object_b: UT6-001 (must validate live loop before scaling)
conflict_type: GOAL_FEASIBILITY
status: UNRESOLVED
notes: User held both; Jun 21-23 sequence shows tension resolved temporarily by small-size live
```

---

## CF6-03: deep_edge_no vs exploratory sample

```yaml
conflict_id: CF6-03
object_a: CN6-005 (deep_edge_no)
object_b: UT6-005 (new battlefield worth exploring)
conflict_type: HYPOTHESIS_STATUS
status: PARTIALLY_RESOLVED
notes: TEST n=16<30 — negative result exploratory not definitive; pivot continues as research not live
```
