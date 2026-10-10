# Coverage Report — Round 6 Blind

**Window:** 2026-06-19 00:00:00 – 2026-06-25 23:59:59

## Sources indexed (no prior canonical import)

| Source | Method | Notes |
|--------|--------|-------|
| Cursor jsonl (7 transcripts) | Parse + embedded `<timestamp>` | fb885289, 3e78f369, 4cc3c07d, abd06259, 6f29d966, 86d41fa2, ea44e6b6 |
| `/opt/EATLQ/mvp/audits/*` | mtime + filename date filter | 30 audit reports in window |
| git `/opt/EATLQ` | log since 2026-06-19 | 1 commit (90077ef) |
| Subagent jsonl | Not line-indexed | File-level via parent transcripts |
| `/root/全部对话_复制用.md` | Not full-rescan | Overlap via jsonl |
| `.env` / secrets | **Not read** | |

## Counts

| # | Metric | Value |
|---|--------|------:|
| 1 | Raw weekly index events | **2,454** |
| 2 | High-value selected events | **531** (+ 10 relations) |
| 3 | User Thesis | **6** |
| 4 | Research Principle | **5** |
| 5 | Hypothesis | **6** |
| 6 | Experiment | **5** |
| 7 | Conclusion | **7** |
| 8 | Failure | **6** |
| 9 | Method | **3** |
| 10 | Decision | **6** |

**Canonical total: 44 objects** | **KG edges: 10** | **Conflicts: 3**

## Selection ratio

531 / 2,454 = **21.6%** — no full assistant text copied to canonical.

## Source pointer coverage

44/44 canonical objects have ≥1 `source_pointers` entry = **100%**.

## Thread discovery (search hints → evidence)

| Search hint | Found? |
|-------------|--------|
| KEYSTONE hardening | ✅ git 90077ef, Jun 19 audit |
| paper-forward | ✅ DC6-001 |
| 25U live | ⚠️ Jun 18 carry; week shows $100/$50 sizing |
| Actual fills | ✅ Jun 21 + Jun 23 PARADAENGI |
| executor / signal bridge | ✅ extensive |
| live PnL | ✅ −0.114 ETH confirmed |
| exit logic / lookahead | ✅ EX6-004 |
| aggressive go-live audit | ✅ Jun 25 rebacktest |
| Base V4 | ⚠️ infra files touched; not main thread |
| deep survivor | ✅ EX6-005 / CN6-005 |
| Sol Keystone follow-up | ❌ not in window |
| live gate | ✅ left-tail + external gate audits |

## Scale success criteria

| Criterion | Met? |
|-----------|------|
| No prior canonical import | ✅ |
| ≥90% objects with source pointer | ✅ 100% |
| 25-sample QA no critical error | ✅ (see quality_sample_audit.md) |
| live/paper/backtest not confused | ✅ explicit status labels |
| Main threads recoverable | ✅ |
| Cost < Round 3 full fidelity | ✅ (~21% selection, no full-text dump) |
