"""
Run-004 Patch D4: v2-only executable label gate (read-only, same design as Patch D).

Wrapper: sets RUN004_D_V2_ONLY and D4 default paths, then calls validate_run004_executable_labels.main.

Default env (override as needed):
  RUN004_CANDIDATES_CSV=logs/run004_candidates_swaponly_full.csv
  RUN004_D_MAX_SAMPLE_ROWS=400
  RUN004_D_STRATIFY_SEED=42
  RUN004_D_OUT_JSON=mvp/diagnostics/run004_patch_d4_v2only_executable_report.json
  LABEL_RPC_URL or EVM_HTTP_URL, CLICKHOUSE_*, REDIS_URL, etc.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

os.environ["RUN004_D_V2_ONLY"] = "1"
os.environ.setdefault("RUN004_D_MAX_SAMPLE_ROWS", "400")
os.environ.setdefault(
    "RUN004_D_OUT_JSON",
    str(REPO / "mvp" / "diagnostics" / "run004_patch_d4_v2only_executable_report.json"),
)

from validate_run004_executable_labels import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
