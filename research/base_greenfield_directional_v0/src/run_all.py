#!/usr/bin/env python3
"""Orchestrate greenfield pipeline end-to-end."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

STEPS = [
    "src/build_panel.py",
    "src/build_labels.py",
    "src/features.py",
    "src/models_routes.py",
    "src/validate_extra.py",
    "src/finalize.py",
]


def main():
    for step in STEPS:
        print(f"\n===== RUN {step} =====", flush=True)
        r = subprocess.run([str(PY), str(ROOT / step)], cwd=str(ROOT))
        if r.returncode != 0:
            raise SystemExit(f"FAILED {step} rc={r.returncode}")
    print("\n===== GREENFIELD PIPELINE COMPLETE =====", flush=True)


if __name__ == "__main__":
    main()
