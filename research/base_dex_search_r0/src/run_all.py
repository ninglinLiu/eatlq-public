#!/usr/bin/env python3
"""Run the search pipeline in order. Safe to re-run; tables are replaced."""
import subprocess
import sys
from pathlib import Path

ROOT = Path("/root/base_dex_search_r0")
(ROOT / "out").mkdir(parents=True, exist_ok=True)

STEPS = [
    "census_venues.py",
    "v2_panel.py",
    "recipes.py",
    "tier1.py",
    "tier2.py",
    "tier3.py",
    "dollar_gate.py",
    "stamp.py",
]


def main():
    for i, name in enumerate(STEPS, 1):
        print(f"\n======== [{i}/{len(STEPS)}] {name} ========", flush=True)
        p = subprocess.run([sys.executable, str(ROOT / "src" / name)])
        if p.returncode != 0:
            raise SystemExit(f"{name} failed with {p.returncode}")
    print("\nALL STEPS OK")


if __name__ == "__main__":
    main()
