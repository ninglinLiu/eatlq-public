"""Fail closed if NO_LIVE.lock is missing."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "NO_LIVE.lock"


def require_no_live() -> None:
    if not LOCK.is_file():
        print(
            "FATAL: NO_LIVE.lock missing under "
            f"{ROOT}. Refusing to run Mimic collectors.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    text = LOCK.read_text(encoding="utf-8")
    if "LIVE_TRADING = FORBIDDEN" not in text:
        print("FATAL: NO_LIVE.lock content invalid.", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    require_no_live()
    print("NO_LIVE.lock OK")
