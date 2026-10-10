#!/usr/bin/env python3
"""Finalize RM2 evidence after independent line review.

This script never constructs ChainIO and never runs execute/emergency-flatten.
It reruns only the disabled proof, static checks, and offline tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
ADAPTER = ROOT / "scripts/research/base_rm2/rm2_guarded_adapter.py"
PROOF = D / "rm2_guarded_adapter_disabled_proof_20260715.json"
REVIEW = D / "rm2_independent_review_20260715.json"
RESULT = D / "rm2_capstone_and_guarded_adapter_20260715.json"
TEST_EVIDENCE = D / "rm2_test_evidence_20260715.json"
HASH_LEDGER = D / "rm2_hash_ledger_20260715.json"
HASH_SIDECAR = D / "rm2_hash_ledger_20260715.json.sha256"

ARTIFACTS = [
    "reports/RM2_capstone_and_guarded_adapter_20260715.md",
    "reports/RM2_KEYSTONE_capstone_professional_20260715.md",
    "reports/RM2_KEYSTONE_capstone_plain_20260715.md",
    "reports/RM2_guarded_adapter_disabled_result_20260715.md",
    "reports/RM2_human_ARM_and_emergency_flatten_manual_20260715.md",
    "reports/RM2_independent_line_review_20260715.md",
    "scripts/research/base_rm2/rm2_guarded_adapter.py",
    "scripts/research/base_rm2/rm2_finalize_artifacts.py",
    "tests/test_rm2_guarded_adapter.py",
    "configs/research/base_rm1_v2_preregistration_20260715.json",
    "mvp/diagnostics/rm2_guarded_adapter_disabled_proof_20260715.json",
    "mvp/diagnostics/rm2_independent_review_20260715.json",
    "mvp/diagnostics/rm2_capstone_and_guarded_adapter_20260715.json",
    "mvp/diagnostics/rm2_test_evidence_20260715.json",
    "mvp/diagnostics/base_gl1_golden_truth_ledger_gl3_20260715.json",
    "mvp/audits/alpha_registry.md",
]

NEW_SECRET_SCAN_ARTIFACTS = [
    name for name in ARTIFACTS
    if not name.startswith("configs/research/base_rm1")
    and "base_gl1_golden_truth_ledger" not in name
    and name != "mvp/audits/alpha_registry.md"
    and name != "reports/RM2_capstone_and_guarded_adapter_20260715.md"
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, expect: str | None = None, env: dict[str, str] | None = None,
        stdin: bytes | None = None) -> dict[str, object]:
    proc = subprocess.run(command, cwd=ROOT, input=stdin, capture_output=True, env=env)
    combined = proc.stdout + proc.stderr
    text = combined.decode("utf-8", errors="replace")
    if proc.returncode != 0 or (expect is not None and expect not in text):
        raise RuntimeError(f"failed: {' '.join(command)}\n{text[-4000:]}")
    return {
        "command": " ".join(command), "exit_code": proc.returncode, "result": "PASS",
        "output_sha256": hashlib.sha256(combined).hexdigest(),
    }


def main() -> None:
    authority_names = ("ARM_TRADE_ID", "RM2_PRIVATE_KEY_FILE", "RM2_PRIVATE_KEY_ENV_NAME", "RM2_RPC_URL")
    unexpectedly_present = [name for name in authority_names if os.environ.get(name)]
    if unexpectedly_present:
        raise RuntimeError(f"RM2 finalization requires absent human authority inputs: {unexpectedly_present}")
    clean_env = dict(os.environ)
    for name in authority_names:
        clean_env.pop(name, None)

    commands: list[dict[str, object]] = []
    commands.append(run(
        ["python3", "-m", "unittest", "tests.test_base_rm1", "tests.test_rm2_guarded_adapter", "-v"],
        expect="Ran 31 tests",
    ))
    commands.append(run([
        "python3", "scripts/check_alpha_sop_compliance.py",
        "reports/RM2_KEYSTONE_capstone_professional_20260715.md",
        "reports/RM2_KEYSTONE_capstone_plain_20260715.md",
        "reports/RM2_guarded_adapter_disabled_result_20260715.md",
        "reports/RM2_human_ARM_and_emergency_flatten_manual_20260715.md",
        "reports/RM2_independent_line_review_20260715.md",
    ], expect="PASS"))
    commands.append(run([
        "python3", "-m", "py_compile",
        "scripts/research/base_rm2/rm2_guarded_adapter.py",
        "scripts/research/base_rm2/rm2_finalize_artifacts.py",
        "tests/test_rm2_guarded_adapter.py",
    ]))
    commands.append(run([
        "python3", "-B", "scripts/research/base_rm2/rm2_guarded_adapter.py", "disabled-proof",
        "--output", "mvp/diagnostics/rm2_guarded_adapter_disabled_proof_20260715.json",
    ], expect='"send_calls": 0', env=clean_env))
    commands.append(run(["git", "diff", "--check", "--", "mvp/audits/alpha_registry.md"]))

    secret_scan_files = [ROOT / name for name in NEW_SECRET_SCAN_ARTIFACTS if (ROOT / name).is_file()]
    scan_bytes = b"\n".join(path.read_bytes() for path in secret_scan_files)
    commands.append(run(
        ["gitleaks", "detect", "--pipe", "--no-banner", "--redact"], stdin=scan_bytes,
    ))

    containers = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], cwd=ROOT, text=True,
        capture_output=True, check=True,
    ).stdout.splitlines()
    forbidden = [name for name in containers if "executor" in name.lower() or "strategy" in name.lower()]
    if forbidden:
        raise RuntimeError(f"forbidden runtime container active: {forbidden}")

    proof = json.loads(PROOF.read_text())
    review = json.loads(REVIEW.read_text())
    if not (
        proof["all_rejected"] is True
        and proof["guard_n"] == 28
        and proof["posture"]["sign_calls"] == 0
        and proof["posture"]["send_calls"] == 0
        and proof["posture"]["real_tx_hashes"] == []
        and proof["posture"]["live_allowed"] is False
        and proof["posture"]["self_go_allowed"] is False
    ):
        raise RuntimeError("disabled proof is not fail-closed")
    if review["adapter_sha256"] != sha(ADAPTER) or review["disabled_proof_sha256"] != sha(PROOF):
        raise RuntimeError("independent review does not bind current code/proof hashes")
    if review["verdict"] != "ADAPTER_REVIEW_PASS_DISABLED_ONLY" or review.get("blockers"):
        raise RuntimeError("independent review did not pass disabled-only adapter")

    result = {
        "schema_version": 1,
        "alpha_id": "AR-RM2-CAPSTONE-ADAPTER",
        "research_only": True,
        "part_1": {
            "operation": "SEALED_FACT_AGGREGATION_ONLY_NO_RECOMPUTATION",
            "authoritative_gl1_ledger_sha256": sha(D / "base_gl1_golden_truth_ledger_gl3_20260715.json"),
            "professional_report_sha256": sha(ROOT / "reports/RM2_KEYSTONE_capstone_professional_20260715.md"),
            "plain_report_sha256": sha(ROOT / "reports/RM2_KEYSTONE_capstone_plain_20260715.md"),
            "gl1_12_grade": "IRON",
            "gl1_01_grade": "PINNED",
            "gl1_02_status": "OPEN_REAL_MONEY_FILL_EVIDENCE_MISSING",
        },
        "part_2": {
            "adapter_sha256": sha(ADAPTER),
            "disabled_proof_sha256": sha(PROOF),
            "guard_rejections": proof["guard_n"],
            "all_rejected": proof["all_rejected"],
            "sign_calls": 0,
            "send_calls": 0,
            "real_tx_hashes": [],
            "rpc_calls_in_disabled_proof": 0,
            "independent_verdict": review["verdict"],
        },
        "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "next_gate": "HUMAN_ONLY_PHASE1_DEEP_POOL_ARM_AFTER_REVIEW",
    }
    RESULT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")

    evidence = {
        "schema_version": 1,
        "alpha_id": "AR-RM2-CAPSTONE-ADAPTER",
        "research_only": True,
        "commands": commands,
        "tests": {"passed": 31, "total": 31, "rm1": 12, "rm2": 19},
        "secret_scan": {
            "tool": "gitleaks", "redacted": True, "findings": 0,
            "scope": "RM2_NEW_ARTIFACTS_ONLY_SEALED_UPSTREAM_EXCLUDED",
        },
        "safety": {
            "executor_active": False,
            "strategy_active": False,
            "arm_trade_id_present": False,
            "private_key_source_present": False,
            "disabled_proof_rpc_calls": 0,
            "sign_calls": 0,
            "send_calls": 0,
            "real_tx_hashes": [],
        },
        "independent_verdict": review["verdict"],
        "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
    }
    TEST_EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    missing = [name for name in ARTIFACTS if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(missing)
    ledger = {
        "schema_version": 1,
        "alpha_id": "AR-RM2-CAPSTONE-ADAPTER",
        "research_only": True,
        "independent_verdict": review["verdict"],
        "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "artifacts": {name: sha(ROOT / name) for name in sorted(ARTIFACTS)},
    }
    HASH_LEDGER.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    HASH_SIDECAR.write_text(f"{sha(HASH_LEDGER)}  {HASH_LEDGER.relative_to(ROOT)}\n")
    print(json.dumps({
        "tests": evidence["tests"], "guard_rejections": proof["guard_n"],
        "send_calls": 0, "real_tx_hashes": [], "independent_verdict": review["verdict"],
        "artifact_n": len(ARTIFACTS), "hash_ledger_sha256": sha(HASH_LEDGER),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
