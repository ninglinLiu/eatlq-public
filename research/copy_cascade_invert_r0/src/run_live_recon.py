"""Today's contemporaneous reconstruct + compare. Public index/RPC only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import ensure_no_live, utc_now_iso, write_json
from src.compare_gmgn_live import compare_chain
from src.finalize import finalize
from src.reconstruct_bsc import reconstruct_bsc
from src.reconstruct_live import reconstruct_chain, verify_rpc_sample


def main(chains: tuple[str, ...] = ("base", "bsc")) -> dict:
    ensure_no_live()
    log = {"started_at": utc_now_iso(), "steps": [], "GO": False}
    for chain in chains:
        rec = {"name": f"reconstruct_{chain}", "ok": False}
        try:
            out = reconstruct_bsc() if chain == "bsc" else reconstruct_chain(chain)
            rec["ok"] = True
            rec["n_ok"] = out.get("n_ok")
            rec["n_fail"] = out.get("n_fail")
            rec["head"] = (out.get("rpc") or {}).get("head")
        except Exception as exc:  # keep going
            rec["error"] = f"{type(exc).__name__}: {exc}"
        log["steps"].append(rec)
        write_json(ROOT / "out/LIVE_RUN_LOG.json", log)

        crec = {"name": f"compare_{chain}", "ok": False}
        try:
            cmp = compare_chain(chain)
            crec["ok"] = True
            crec["status"] = cmp.get("status")
            crec["sign_agree_rate_7d"] = cmp.get("sign_agree_rate_7d")
        except Exception as exc:
            crec["error"] = f"{type(exc).__name__}: {exc}"
        log["steps"].append(crec)
        write_json(ROOT / "out/LIVE_RUN_LOG.json", log)

        if rec.get("ok") and chain == "base":
            vrec = {"name": f"rpc_verify_{chain}", "ok": False}
            try:
                ver = verify_rpc_sample(chain)
                vrec["ok"] = True
                vrec["n_agree"] = ver.get("n_agree")
                vrec["n"] = ver.get("n")
            except Exception as exc:
                vrec["error"] = f"{type(exc).__name__}: {exc}"
            log["steps"].append(vrec)

    fin = finalize()
    log["finished_at"] = utc_now_iso()
    log["verdict"] = fin.get("verdict")
    write_json(ROOT / "out/LIVE_RUN_LOG.json", log)
    return log


if __name__ == "__main__":
    chains = tuple(sys.argv[1:]) if len(sys.argv) > 1 else ("base", "bsc")
    result = main(chains)
    print(json.dumps(result, indent=2, default=str))
