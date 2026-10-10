# Round 6 — Blind Weekly Recovery (2026-06-19 to 2026-06-25)

**First true scale blind test** — canonical objects compiled **only from raw sources**, without importing Round 2–5 canonical jsonl as generation input.

## Architecture

```text
weekly_raw_index.jsonl     ← 2,454 events (previews + sha256 + pointers)
        ↓
selected_lineage.jsonl     ← 531 high-value events + 10 relations
selected_timeline.md
        ↓
*.jsonl canonical (44 objects)
```

## Primary sources (blind)

| Transcript | Role |
|------------|------|
| `4cc3c07d` | Jun 19 KEYSTONE pre-live / paper-forward / bridge deploy |
| `3e78f369` | Jun 21–22 capital debate, first honeypot, infra failure |
| `fb885289` | Jun 22–25 audit marathon, go-live, PARADAENGI disaster |
| `abd06259` | Jun 25 base_deepsurvivor pivot |
| `6f29d966` | Jun 25 KEYSTONE origin search (retrospective) |
| `/opt/EATLQ/mvp/audits/*20260619–25*` | Audit artifacts |
| git `90077ef` | keystone live-canary hardening |

## Explicitly NOT used as answer source

- `/root/research_inventory_round2_20260612_0618/`
- `/root/research_inventory_round3_20260618_gold/`
- `/root/research_inventory_round4_canonical_20260618/`
- `/root/research_inventory_round5_weekly_20260612_0618/` *(read only post-freeze for boundary check)*

## Deliverables

| File | Purpose |
|------|---------|
| `weekly_raw_index.jsonl` | Full week index |
| `selected_lineage.jsonl` | High-value events + relations |
| `user_theses.jsonl` … `decisions.jsonl` | 8-class canonical |
| `weekly_canonical_summary.md` | Human index |
| `boundary_continuity_check.md` | Jun 18→19衔接 (post-freeze) |
| `quality_sample_audit.md` | 25-item blind QA |
| `compiler_audit.md` | Process + ratings |

## Build

```bash
python3 _build_round6.py
```
