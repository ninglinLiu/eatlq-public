#!/usr/bin/env python3
"""Run final GL3 checks and write deterministic evidence/hash ledgers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path("/opt/EATLQ")
D = ROOT / "mvp/diagnostics"
TEST_EVIDENCE = D / "base_gl3_test_evidence_20260715.json"
HASH_LEDGER = D / "base_gl3_hash_ledger_20260715.json"
HASH_SIDECAR = D / "base_gl3_hash_ledger_20260715.json.sha256"
ARTIFACTS = [
    "reports/GL3_task_spec_close_gl12_alpha_beta_20260715.md",
    "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md",
    "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md.sha256",
    "scripts/research/base_gl3/DualBalanceCreate.sol",
    "scripts/research/base_gl3/gl3_full_interval_recovery.py",
    "scripts/research/base_gl3/gl3_integrate_gl1.py",
    "scripts/research/base_gl3/gl3_finalize_artifacts.py",
    "mvp/diagnostics/base_gl3_event_inventory_20260715.json",
    "mvp/diagnostics/base_gl3_pool_interval_evidence_20260715.json",
    "mvp/diagnostics/base_gl3_gl12_alpha_beta_20260715.json",
    "mvp/diagnostics/base_gl3_rpc_full_ledger_20260715.jsonl",
    "mvp/diagnostics/base_gl3_rpc_ledger_20260715.json",
    "reports/GL3_gl12_alpha_beta_result_20260715.md",
    "reports/GL3_independent_line_review_20260715.md",
    "mvp/diagnostics/base_gl3_independent_review_20260715.json",
    "mvp/diagnostics/base_gl1_golden_truth_ledger_gl3_20260715.json",
    "reports/GL1_golden_truth_ledger_GL3_20260715.md",
    "reports/GL3_incremental_plain_language_summary_20260715.md",
    "mvp/audits/alpha_registry.md",
    "tests/test_base_gl3_gl12.py",
    "tests/test_base_gl3_integration.py",
    "mvp/diagnostics/base_gl3_test_evidence_20260715.json",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, expect: str | None = None) -> dict:
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0 or (expect is not None and expect not in combined):
        raise RuntimeError(f"failed: {' '.join(command)}\n{combined[-4000:]}")
    return {"command": " ".join(command), "exit_code": proc.returncode, "result": "PASS", "output_sha256": hashlib.sha256(combined.encode()).hexdigest()}


def main() -> None:
    commands = []
    commands.append(run(["python3", "-m", "unittest", "tests.test_base_gl3_gl12", "tests.test_base_gl3_integration", "-v"], expect="Ran 12 tests"))
    commands.append(run(["python3", "scripts/check_alpha_sop_compliance.py",
        "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md",
        "reports/GL3_gl12_alpha_beta_result_20260715.md",
        "reports/GL1_golden_truth_ledger_GL3_20260715.md",
        "reports/GL3_incremental_plain_language_summary_20260715.md"], expect="PASS"))
    commands.append(run(["python3", "-m", "py_compile",
        "scripts/research/base_gl3/gl3_full_interval_recovery.py",
        "scripts/research/base_gl3/gl3_integrate_gl1.py",
        "scripts/research/base_gl3/gl3_finalize_artifacts.py",
        "tests/test_base_gl3_gl12.py", "tests/test_base_gl3_integration.py"]))
    commands.append(run(["sha256sum", "-c", "reports/GL3_prereg_close_gl12_full_interval_recovery_20260715.md.sha256"], expect="OK"))
    commands.append(run(["git", "diff", "--check", "--", "mvp/audits/alpha_registry.md"]))
    containers = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.splitlines()
    strategy = [name for name in containers if "executor" in name.lower()]
    if strategy:
        raise RuntimeError(f"executor container unexpectedly active: {strategy}")
    result = json.loads((D / "base_gl3_gl12_alpha_beta_20260715.json").read_text())
    review = json.loads((D / "base_gl3_independent_review_20260715.json").read_text())
    rpc = json.loads((D / "base_gl3_rpc_ledger_20260715.json").read_text())
    payload = {
        "schema_version": 1, "alpha_id": "AR-GL3-CLOSE-GL12", "research_only": True,
        "commands": commands, "tests": {"passed": 12, "total": 12},
        "independent_grade": {"GL1-12": review["final_grade"], "substantive_blocker": review["substantive_blocker"]},
        "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "safety": {"executor_active": False, "rpc_endpoint_alias": rpc["rpc"]["endpoint_alias"], "rpc_methods": rpc["rpc"]["methods_attempted"], "send_sign_broadcast_calls": 0},
        "anchor_counts": {"confirmed_continuously_unsellable_cells": result["adjudication_counts"]["CONFIRMED_CONTINUOUSLY_UNSELLABLE"], "remaining_unresolved_cells": result["adjudication_counts"]["UNRESOLVED"], "reserve_integer_equal": result["event_reserve_summary"]["reserve_integer_equal_n"], "reserve_checks": result["event_reserve_summary"]["reserve_checks_n"]},
    }
    TEST_EVIDENCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    missing = [name for name in ARTIFACTS if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(missing)
    ledger = {
        "schema_version": 1, "alpha_id": "AR-GL3-CLOSE-GL12", "research_only": True,
        "independent_final_grade": review["final_grade"], "go_decision": "NOT_MADE",
        "decision_flags": {"training_allowed": False, "live_allowed": False, "self_go_allowed": False},
        "artifacts": {name: sha(ROOT / name) for name in sorted(ARTIFACTS)},
    }
    HASH_LEDGER.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    HASH_SIDECAR.write_text(f"{sha(HASH_LEDGER)}  {HASH_LEDGER.relative_to(ROOT)}\n")
    print(json.dumps({"tests": payload["tests"], "final_grade": review["final_grade"], "artifact_n": len(ARTIFACTS), "hash_ledger_sha256": sha(HASH_LEDGER)}, indent=2))


if __name__ == "__main__":
    main()
