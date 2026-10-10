#!/usr/bin/env python3
"""Orchestrate deep-survivor research pipeline (offline, read-only)."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CGROUP = os.path.join(HERE, "..", "base_v4", "run_in_cgroup.sh")

# Live gate: yield to V3 open positions
sys.path.insert(0, os.path.join(HERE, "..", "base_v4"))
try:
    from live_gate import wait_for_live_clear  # noqa: E402
    wait_for_live_clear(poll_s=60, max_wait_s=300)
except Exception as e:
    print(f"[live_gate] skip: {e}", flush=True)

TASKS = [
    "task0_build_universe.py",
    "task_a_baseline.py",
    "task_b_smartmoney.py",
    "task_c_flow_signals.py",
    "task_d_lefttail.py",
    "task_e_evaluate.py",
    "generate_report.py",
]


def run_task(script: str) -> int:
    path = os.path.join(HERE, script)
    print(f"\n{'='*60}\n>>> {script}\n{'='*60}", flush=True)
    if script.startswith("task_a") and os.path.exists(CGROUP):
        cmd = ["bash", CGROUP, "python3", path]
    else:
        cmd = [sys.executable, path]
    return subprocess.call(cmd, cwd=HERE)


def main():
    print("#### Base Deep-Survivor Research Pipeline ####", flush=True)
    for t in TASKS:
        rc = run_task(t)
        if rc != 0:
            print(f"WARN: {t} exited {rc}", flush=True)
    print("\n#### Pipeline complete ####", flush=True)


if __name__ == "__main__":
    main()
