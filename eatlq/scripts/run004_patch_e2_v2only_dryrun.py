"""
Run-004 Patch E2: full v2-only label dry-run (all trainable v2 + swap topic).

Wrapper sets RUN004_E2_MODE=1, turns off stratify, and leaves RUN004_E_MAX_ROWS uncapped (0).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

os.environ["RUN004_E2_MODE"] = "1"
os.environ["RUN004_E_STRATIFY"] = "0"
os.environ["RUN004_E_MAX_ROWS"] = "0"

from label_run004_v2only_dryrun import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
