# EATLQ / LASZLO — Agent tooling & guardrails

Cross-tooling contract for Cursor, Claude Code, Codex, and other repo agents. **Authoritative runtime contracts** remain `Architecture.md`, `Operations.md`, and `docs/*` guard files (`mvp/audits/failed_no_7d_alpha_guard.md`, etc.). This file defines **when to reach for which local OSS tool** and what is **out of bounds**.

## Alpha / signal / paper-shadow default workflow (**auto-load SOP**)

Whenever the task involves **alpha, signals, wallets, pools, FeatureStore (`feat:` keys), labels, models, pool_score / smart_money routing, dashcam/paper-shadow, dry-run, live-readiness, or execution feasibility docs**:

1. **Read first:** `docs/alpha-sop-decision-tree.md` (maps families F1–F7 and gates; **does not** authorize live, training, or promotion).
2. **Register/update:** append or edit a lightweight row in `mvp/audits/alpha_registry.md` for the sprint or thread (see table + `mvp/diagnostics/sprint068/alpha_registry.json` for machine-shaped multi-alpha rules).
3. **New audits/readouts tied to mining or execution-path design:** prepend YAML frontmatter with `sop_applied`, `alpha_id`, `family`, `next_gate`, nested `decision_flags.training_allowed`, `decision_flags.live_allowed` — print a template via `python3 scripts/check_alpha_sop_compliance.py --print-template` and optionally validate with `python3 scripts/check_alpha_sop_compliance.py <artifact.md>`.

**Cursor IDE:** canonical rule text for this workflow is **`docs/cursor/rules/alpha-sop.mdc`** (`alwaysApply`). Copy/symlink to `.cursor/rules/alpha-sop.mdc` locally (`.cursor/` is gitignored).

## Out of bounds (never “work around”)

- **No live trading**, no writing Redis `signals` / execution hot paths, no RPC/archive replay for “just checking,” **no training / retrain / model promotion**, no `cryo`/router experimentation, unless a human explicitly opens that sprint and guard docs allow it.
- **Never read, print, copy, or commit** real `.env`, private keys, seed phrases, RPC URLs with tokens, Telegram/OpenRouter/Cursor API tokens.
- **Never** `git add .`; stage paths explicitly. **Never** commit raw Gitleaks JSON, `.venv_audit/`, `node_modules/`, full `logs/`, `cache/`, large CSV/Parquet dumps, or `*.duckdb` working files.

## Tool selection (default habits)

### Gitleaks — secret detection

**Use when:** before `git push` or publishing a bundle; after bulk edits or adding dependencies; when touching env/config examples; when landing third-party tooling.

**How:** `gitleaks detect --redact` (optionally `--no-git` for working tree). Emit **path/rule/line counts only**; do **not** paste `Secret`, `Match`, or file bodies. **Do not** commit raw reports.

**Adoption status:** **sandbox_more** — repo has heavy `generic-api-key` noise under diagnostics/JSON; pair scanning with **baseline/path excludes** (`mvp/audits/gitleaks_triage/triage_summary.json`). **P0** (`private-key`, `telegram-bot-api-token`) must be triaged; if a P0 hit lands on a **tracked** file, stop and escalate for security review before push.

**Refs:** `mvp/audits/gitleaks_triage/triage_readout.md`, `docs/dev/tooling-policy.md`.

### DuckDB — read-only analytics

**Use when:** `mvp/audits/`, `mvp/runs/`, `mvp/diagnostics/`, OOS/coverage summaries, sidecar ROI rollups, CSV/Parquet under **size caps** (do not load multi‑GB into the agent context).

**How:** Prefer `SELECT … FROM 'path.csv'` / Parquet in DuckDB (CLI or `duckdb` Python) over ad-hoc pandas soup.

**Does not replace:** ClickHouse production paths, chain RPC, or strategy Feature store.

**Adoption status:** **adopt_now** for offline inventory (see `mvp/audits/oss_three_tool_preflight/`).

### Pandera — schema & leakage gate

**Use when:** validating dashcam / labeled dataset shapes, ROI columns, feature column lists, denylisted “future” or label leakage columns, offline dataset gates.

**How:** `import pandera.pandas as pa`; explicit `datetime64[ns, UTC]` when needed; **offline only** — does not authorize training runs.

**Adoption status:** **adopt_now** for gates (see `toy_schema_gate.py` pattern under `mvp/audits/oss_three_tool_preflight/`).

## Principles

1. If DuckDB can answer it on a bounded file, **don’t** hand-roll giant pandas.
2. If Pandera can encode the schema/leakage rule, **don’t** eyeball column names only.
3. Before push, **Gitleaks with `--redact`** on the change set / tree per team practice.
4. Any tool use **must respect** the same FAILED_NO_7D_ALPHA and live/training guards as the rest of the repo.

## Cursor IDE

The repo root `.cursor/` tree is **gitignored**. The committed Cursor rules live under **`docs/cursor/rules/`** — copy/symlink to `.cursor/rules/` locally: `eatlq-tooling.mdc` (secrets/tooling gates) and `alpha-sop.mdc` (alpha / paper-shadow / dry-run workflow).

## See also

- `Home.md`, `Architecture.md`, `Operations.md`, `Code-Review.md`, `docs/alpha-sop-decision-tree.md`, `mvp/audits/alpha_registry.md`
- `docs/dev/tooling-policy.md` — full policy and rationale
- `mvp/audits/oss_three_tool_preflight/` — smoke/preflight artifacts
- `mvp/audits/gitleaks_triage/` — path/rule/git triage (no secrets)
