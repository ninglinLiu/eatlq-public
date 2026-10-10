#!/usr/bin/env python3
"""P1-10: Report scripts still using protocol fee encoding in discovered_pools SQL."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

# SQL filters against discovered_pools.fee_bps with wrong (protocol) units
BAD_PATTERNS = [
    re.compile(r"fee_bps\s+IN\s*\(\s*100\s*,\s*500\s*,\s*3000"),
    re.compile(r"fee_bps\s+IN\s*\(\s*500\s*,\s*3000\s*,\s*10000"),
    re.compile(r"fee_bps\s*!=\s*10000"),
]

FIXED_HINT = {
    "sprint088_historical_backtest.py",
    "sprint090_hypothesis_runner.py",
    "sprint095_pre_dentry_retest.py",
    "sprint086_weathervane_0x6ff5.py",
    "sprint092_universe_expander.py",
    "sprint092_pool_meta_backfill.py",
    "fee_bps_encoding.py",
    "p1_fee_bps_audit.py",
}


def main() -> int:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.name in FIXED_HINT:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            for pat in BAD_PATTERNS:
                if pat.search(line) and "discovered_pools" in text:
                    hits.append((str(path.relative_to(REPO)), i, line.strip()))
                    break
    print(f"Potential fee_bps encoding issues: {len(hits)} lines")
    for fp, ln, content in hits[:40]:
        print(f"  {fp}:{ln}: {content[:100]}")
    if len(hits) > 40:
        print(f"  ... and {len(hits) - 40} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
