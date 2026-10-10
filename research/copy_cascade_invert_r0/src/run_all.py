"""One-shot research pipeline. Writes only under this project."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import ensure_no_live, utc_now_iso, write_json
from src.collect_debot import collect as collect_debot
from src.collect_fomo import collect as collect_fomo
from src.collect_gmgn import collect as collect_gmgn
from src.copy_offline_base import copy_all
from src.finalize import finalize
from src.latency_tax import run_tax
from src.normalize_boards import normalize
from src.reconcile_vendor_pnl import reconcile
from src.arm_a_crowd_fade import run_arm_a
from src.arm_b_new_cohort import run_arm_b


def main() -> dict:
    ensure_no_live()
    log = {"started_at": utc_now_iso(), "steps": []}

    def step(name, fn):
        rec = {"name": name, "ok": False}
        try:
            out = fn()
            rec["ok"] = True
            if isinstance(out, dict):
                rec["status"] = out.get("status") or out.get("lineage") or out.get("verdict") or out.get("same_direction_copy_arm")
                rec["n"] = out.get("n_rows") or out.get("n_signals") or out.get("n_copied") or out.get("n_ok_with_rows")
        except Exception as exc:  # keep going; finalize will see missing artifacts
            rec["error"] = f"{type(exc).__name__}: {exc}"
        log["steps"].append(rec)
        write_json(ROOT / "out/RUN_LOG.json", log)
        return rec

    step("copy_offline", copy_all)
    step("collect_gmgn", collect_gmgn)
    step("collect_debot", collect_debot)
    step("collect_fomo", collect_fomo)
    boards = None
    rec = step("normalize_boards", normalize)
    # If all vendors empty, still run tape-side tax/arms; merit stays INCONCLUSIVE.
    step("reconcile", reconcile)
    step("latency_tax", run_tax)
    step("arm_a", run_arm_a)
    step("arm_b", run_arm_b)
    fin = finalize()
    log["finished_at"] = utc_now_iso()
    log["verdict"] = fin.get("verdict")
    log["GO"] = False
    write_json(ROOT / "out/RUN_LOG.json", log)
    return log


if __name__ == "__main__":
    result = main()
    print(json.dumps({"GO": False, "verdict": result.get("verdict"), "steps": result.get("steps")}, indent=2))
