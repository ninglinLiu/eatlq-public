# Development tooling policy (OSS agents)

This document is the **human-readable** expansion of `AGENTS.md`. It describes how EATLQ agents should use **Gitleaks**, **DuckDB**, and **Pandera** safely and consistently.

## Goals

- **Security:** no secret values in chat, logs, or commits; redacted scans only.
- **Inventory:** fast, reproducible read-only SQL over bounded CSV/Parquet for audits.
- **Correctness:** schema and leakage gates on offline datasets without triggering training.

## Gitleaks

| When | Action |
|------|--------|
| Pre-push / release artifact | Run `gitleaks detect --redact`; summarize paths, rule IDs, counts |
| Bulk file writes / new deps / config churn | Same; consider `--no-git` for working tree |
| CI (future) | Baseline + scoped paths; **do not** fail the repo on raw `generic-api-key` noise without tuning |

**Never:** commit raw JSON/SARIF with uncleared fields; paste `Secret`/`Match` into issues or PRs.

**Current posture:** **sandbox_more** — see `mvp/audits/gitleaks_triage/triage_summary.json` — expect many generic hits in diagnostics JSON; **P0** rules require human triage. **Tracked P0 must be zero** before declaring a push “clean.”

## DuckDB

| When | Action |
|------|--------|
| Audits, runs, diagnostics CSV/Parquet | SQL with explicit file paths; cap file sizes (e.g. avoid scanning `logs/` multi‑GB without a plan) |
| OOS / coverage / ROI rollups | Prefer DuckDB over one-off pandas scripts |

**Never:** use DuckDB as a substitute for ClickHouse in production; **no RPC** inside DuckDB workflows for this policy.

## Pandera

| When | Action |
|------|--------|
| Dataset contract / leakage | `DataFrameSchema` or `DataFrameModel` with strict columns + time-order + denylist checks |
| dashcam / labeled exports | Validate before any **human-approved** training workflow (agent does **not** start training) |

**Never:** treat passing Pandera as authorization to run `retrain_model.py` or Phase 6.

## Git & ignore hygiene

Keep real secrets and bulky artifacts out of Git:

- Env files, tokens, keys, raw Gitleaks dumps, `.venv_audit/`, `node_modules/`, `cache/`, `*.duckdb`, and large generated CSV/Parquet under the patterns in **`.gitignore`** / **`.dockerignore`**.

## References

- `AGENTS.md` — short agent contract
- `docs/cursor/rules/eatlq-tooling.mdc` — Cursor rule mirror
- `mvp/audits/oss_three_tool_preflight/readout.md`
- `mvp/audits/gitleaks_triage/triage_readout.md`
