#!/usr/bin/env python3
"""Write the deterministic top-level GL2 artifact hash ledger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "mvp/diagnostics/base_gl2_artifact_hashes_20260715.json"
ARTIFACTS = [
    "reports/GL2_task_spec_three_historical_opens_20260715.md",
    "reports/GL2_A_prereg_five_evidence_20260715.md",
    "reports/GL2_A_prereg_five_evidence_20260715.md.sha256",
    "scripts/research/base_gl2/gl2_a_five_evidence_rpc.py",
    "scripts/research/base_gl2/gl2_a_trigger_five_evidence.py",
    "scripts/research/base_gl2/gl2_a_full_tail_review.py",
    "scripts/research/base_gl2/gl2_a_authoritative_result.py",
    "scripts/research/base_gl2/gl2_a_finalize_rpc_ledger.py",
    "scripts/research/base_gl2/gl2_a_finalize_hashes.py",
    "mvp/diagnostics/base_gl2_a_five_evidence_rpc_20260715.json",
    "mvp/diagnostics/base_gl2_a_trigger_five_evidence_rpc_20260715.json",
    "mvp/diagnostics/base_gl2_a_trigger_evidence_reviewed_20260715.json",
    "mvp/diagnostics/base_gl2_a_universe_ev_20260715.json",
    "mvp/diagnostics/base_gl2_a_rpc_full_ledger_20260715.jsonl",
    "mvp/diagnostics/base_gl2_a_rpc_ledger_20260715.json",
    "mvp/diagnostics/base_gl2_a_hash_ledger_20260715.json",
    "mvp/diagnostics/base_gl2_a_test_evidence_20260715.json",
    "reports/GL2_A_universe_ev_result_20260715.md",
    "tests/test_base_gl2_a.py",
    "reports/GL2_B_prereg_attribution_fifo_20260715.md",
    "reports/GL2_B_prereg_attribution_fifo_20260715.md.sha256",
    "mvp/diagnostics/base_gl2_b_stage1_distribution_20260715.json",
    "mvp/diagnostics/base_gl2_b_stage1_distribution_20260715.sha256",
    "reports/GL2_B_prereg_copy_forward_20260715.md",
    "reports/GL2_B_prereg_copy_forward_20260715.md.sha256",
    "reports/GL2_B_prereg_copy_forward_20260715.erratum.md",
    "reports/GL2_B_prereg_copy_forward_20260715.erratum.md.sha256",
    "scripts/research/gl2_b_smartmoney.py",
    "mvp/diagnostics/base_gl2_b_fifo_cashleg_golden_20260715.json",
    "mvp/diagnostics/base_gl2_b_copy_forward_20260715.json",
    "mvp/diagnostics/base_gl2_b_final_20260715.json",
    "mvp/diagnostics/base_gl2_b_hash_ledger_20260715.json",
    "mvp/diagnostics/base_gl2_b_test_evidence_20260715.json",
    "reports/GL2_B_smartmoney_copyability_result_20260715.md",
    "tests/test_base_gl2_b.py",
    "reports/GL2_C_prereg_exogenous_weather_20260715.md",
    "mvp/diagnostics/base_gl2_c_preregistration_20260715.sha256",
    "scripts/research/base_gl2/gl2_c_weather_oos.py",
    "mvp/diagnostics/base_gl2_c_weather_oos_20260715.json",
    "mvp/diagnostics/base_gl2_c_hash_ledger_20260715.json",
    "mvp/diagnostics/base_gl2_c_test_evidence_20260715.json",
    "reports/GL2_C_exogenous_weather_oos_result_20260715.md",
    "tests/test_base_gl2_c.py",
    "reports/GL2_independent_line_review_20260715.md",
    "mvp/diagnostics/base_gl2_independent_review_20260715.json",
    "mvp/diagnostics/base_gl1_golden_truth_ledger_20260715.json",
    "scripts/research/base_gl2/gl2_integrate_gl1.py",
    "mvp/diagnostics/base_gl1_golden_truth_ledger_gl2_20260715.json",
    "reports/GL1_golden_truth_ledger_GL2_20260715.md",
    "mvp/diagnostics/base_gl2_three_opens_20260715.json",
    "reports/GL2_incremental_plain_language_summary_20260715.md",
    "mvp/audits/alpha_registry.md",
    "tests/test_base_gl2_integration.py",
    "mvp/diagnostics/base_gl2_test_evidence_20260715.json",
    "scripts/research/base_gl2/gl2_finalize_artifacts.py"
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    missing = [name for name in ARTIFACTS if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(missing)
    payload = {
        "alpha_id": "AR-GL2-THREE-OPENS",
        "artifacts": {name: sha(ROOT / name) for name in sorted(ARTIFACTS)},
        "decision_flags": {"live_allowed": False, "self_go_allowed": False, "training_allowed": False},
        "go_decision": "NOT_MADE",
        "independent_grades": {"GL1-12": "OPEN", "GL1-18": "OPEN", "GL1-19": "OPEN"},
        "research_only": True,
        "schema_version": 1,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} with {len(ARTIFACTS)} artifacts")


if __name__ == "__main__":
    main()
