# Static contract / audit checks — no Docker, no RPC, no live trading.
# See Operations.md §「仓库静态契约检查」.

.PHONY: audit-validate-json static-contracts check-static check-readiness check-local-safety check-compose-config check-secrets-hygiene check-alpha-sop-compliance

audit-validate-json:
	python3 scripts/validate_audit_json.py --root .

static-contracts:
	python3 -m unittest tests.test_whitepaper_v2_static_contracts

# Sprint013 S1-T6: secrets-shape lint on tracked files only (uses git ls-files).
# Fails on QuickNode-like RPC tokens, scheme:// URLs, PRIVATE_KEY=0x<64hex>,
# Bearer tokens, and non-empty secret-named keys in infra/compose/.env.example.
check-secrets-hygiene:
	python3 scripts/check_no_secrets.py --root .

# Alpha SOP: YAML frontmatter self-test only (--self-test fixtures); does not scan mvp/ trees.
check-alpha-sop-compliance:
	python3 scripts/check_alpha_sop_compliance.py --self-test

check-static: audit-validate-json static-contracts check-secrets-hygiene check-alpha-sop-compliance

# Sprint009: paper/dry-run static preflight (files, .env.example parity, guard combos).
check-readiness:
	python3 scripts/paper_dryrun_readiness.py

check-local-safety: check-static check-readiness

# Sprint010: validate compose interpolation only (`config -q`); does not start containers.
check-compose-config:
	python3 scripts/paper_dryrun_readiness.py --compose-config
