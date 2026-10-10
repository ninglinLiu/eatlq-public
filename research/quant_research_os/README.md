# Quant Research OS

Production pipeline for VPS-wide quantitative research history recovery.

## Architecture

```text
Raw History (jsonl, git, audits)
        ↓
Weekly Recovery  (/weekly/YYYY-MM-DD_YYYY-MM-DD/)
        ↓
Monthly Consolidation  (/monthly/YYYY-MM/)  [after 4× QA_PASSED weeks]
        ↓
Global Knowledge Pack  (/global/)
```

## Validated methods (do not reinvent schema)

| Round | Role | Reference |
|-------|------|-----------|
| R3 | Single-day gold fidelity | `/root/research_inventory_round3_20260618_gold/` |
| R4 | Canonical compiler | `/root/research_inventory_round4_canonical_20260618/` |
| R5 | Weekly merge | `/root/research_inventory_round5_weekly_20260612_0618/` |
| R6 | Blind weekly | `/root/research_inventory_round6_weekly_20260619_0625_blind/` |

## Directory layout

```text
/root/quant_research_os/
├── state/
│   ├── windows.jsonl       # All week windows + status
│   ├── checkpoints.jsonl   # Resume points
│   ├── failures.jsonl      # Failed weeks
│   └── pipeline_state.json
├── weekly/                 # One dir per Mon–Sun window
├── monthly/                # 4-week consolidations
├── global/                 # VPS-wide knowledge pack
└── pipeline/
    ├── bootstrap.py        # Init + register validated weeks
    ├── weekly_compiler.py  # Stages 1–5 per week
    └── run_batch.py        # Batch process pending windows
```

## Window status (v1 taxonomy)

```text
QA_PASSED | EMPTY_CONFIRMED | ARTIFACTS_ONLY | NO_TRANSCRIPT_ACTIVITY
SOURCE_COVERAGE_INSUFFICIENT | NEEDS_REVIEW | FAILED
```

Bare `SKIPPED_EMPTY` is **retired**. See `AUTHORITY_MODEL.md`.

## QA metrics (do not conflate)

```text
QA_PASS_RATE_AMONG_PROCESSED:     100%  (10/10 full-pipeline weeks)
QA_PASSED_SHARE_OF_ALL_WINDOWS:   26%   (10/39 registered — NOT pass rate)
```

## Authority

Global Pack = **MIXED_AUTHORITY**. All objects carry `authority_level`:
GOLD | VALIDATED | AUTO_CURATED | ARTIFACT_ONLY | UNVERIFIED

Pipeline scripts: `artifact_fallback.py`, `authority_promotion.py`

## Commands

```bash
# Initialize OS + register R5/R6 as QA_PASSED
python3 /root/quant_research_os/pipeline/bootstrap.py

# Process next N pending weeks (earliest first, checkpoint resume)
python3 /root/quant_research_os/pipeline/run_batch.py 35

# Single week manual run
python3 /root/quant_research_os/pipeline/weekly_compiler.py \
  --start 2026-03-03 --end 2026-03-09 \
  --out /root/quant_research_os/weekly/2026-03-03_2026-03-09 \
  --window-id W-2026-03-03-2026-03-09
```

## Rules

- Do **not** re-process `QA_PASSED` or `EXISTING_VALIDATED` windows
- Raw index = preview + sha256 + pointer (no full assistant dump)
- Authorization ≠ fill; backtest ≠ live
- Failures → guardrails in `failures.jsonl`
- Monthly consolidation reads 4 weekly canonical packs only (not full raw)
- Global consolidation reads monthly packs only

## Current discovery

- **earliest_research_date:** 2025-10-27
- **latest_research_date:** 2026-07-11
- **total windows:** 37 (see `state/windows.jsonl`)
