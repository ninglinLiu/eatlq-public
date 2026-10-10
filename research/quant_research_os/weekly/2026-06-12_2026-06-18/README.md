# Round 5 — Weekly Scale Test (2026-06-12 to 2026-06-18)

Validates **low-cost weekly compilation** of high-value, traceable Canonical Knowledge.

## Architecture (three layers)

```text
weekly_raw_index.jsonl     ← full week index (previews + sha256 + pointers)
        ↓
selected_lineage.jsonl     ← high-value user/agent events only
selected_timeline.md
        ↓
*.jsonl canonical objects   ← Round 4 schema + persistence/reuse/epistemic fields
```

## Does NOT contain

- Full assistant reply text for all 7 days
- Duplicate tool stdout
- Forced prompt→artifact mapping for every event

## Reference inputs

- Round 3: `/root/research_inventory_round3_20260618_gold/` (Jun 18 fidelity)
- Round 4: `/root/research_inventory_round4_canonical_20260618/` (schema)
- Round 2: `/root/research_inventory_round2_20260612_0618/` (week correction chains)

## All deliverables (open via Cursor file tree or Ctrl+P)

Base path: `/root/research_inventory_round5_weekly_20260612_0618/`

| File | Purpose |
|------|---------|
| `README.md` | This index |
| `weekly_raw_index.jsonl` | All indexed events (preview only) |
| `selected_lineage.jsonl` | High-value events + relations |
| `selected_timeline.md` | Human-readable selected timeline |
| `user_theses.jsonl` | Canonical USER_THESIS |
| `research_principles.jsonl` | Canonical RESEARCH_PRINCIPLE |
| `hypotheses.jsonl` | Canonical HYPOTHESIS |
| `experiments.jsonl` | Canonical EXPERIMENT |
| `conclusions.jsonl` | Canonical CONCLUSION |
| `failures.jsonl` | Canonical FAILURE |
| `methods.jsonl` | Canonical METHOD |
| `decisions.jsonl` | Canonical DECISION |
| `knowledge_graph.jsonl` | Cross-object relations |
| `weekly_canonical_summary.md` | Week arc + top knowledge |
| `open_questions.md` | Unresolved research questions |
| `source_conflicts.md` | Preserved conflicts (CFW-01–04) |
| `coverage_report.md` | Source coverage + counts |
| `compiler_audit.md` | Pipeline, cost, quality ratings |
| `quality_sample_audit.md` | 20-item lineage QA sample |
| `round4_vs_round5_diff.md` | Jun 18 consistency vs Round 4 |
| `_build_stats.json` | Machine-readable build counts |

## New fields (vs Round 4)

Every canonical object includes:

- `persistence_scope`: PERMANENT | LONG_TERM | REGIME_DEPENDENT | …
- `reuse_policy`: ALWAYS_LOAD | TOPIC_MATCH_ONLY | …
- `epistemic_role`: USER_BELIEF | METHODOLOGY_RULE | EMPIRICAL_RESULT | …
