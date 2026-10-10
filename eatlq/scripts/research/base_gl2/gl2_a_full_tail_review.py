#!/usr/bin/env python3
"""Full-index E3 tail export and conservative E4 review for GL2-A."""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import defaultdict
from pathlib import Path


ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics"
SOURCE = DIAG / "base_gl2_a_trigger_five_evidence_rpc_20260715.json"
OUT = DIAG / "base_gl2_a_trigger_evidence_reviewed_20260715.json"
CUTOFF = 46865124


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def side(row: dict, orient: int) -> str:
    a0, a1 = int(row["a0"]), int(row["a1"])
    weth, token = (a0, a1) if orient == -1 else (a1, a0)
    return "sell" if weth < 0 and token > 0 else "buy" if weth > 0 and token < 0 else "other"


def main() -> None:
    data = json.loads(SOURCE.read_text())
    scoped = [r for r in data["rows"] if r["unresolved_windows_before_five_evidence"]]
    pools = sorted({r["pool"] for r in scoped})
    lower = min(min(v["target_block"] for v in r["windows"].values()) for r in scoped)
    quoted = ",".join("'" + p + "'" for p in pools)
    sql = f"""SELECT lower(pool_address) pool,block_number block,lower(tx_hash) tx,log_index,toString(amount0) a0,toString(amount1) a1
FROM eatlq.uniswap_v3_swaps_decoded FINAL
WHERE chain_id=8453 AND removed=0 AND block_number BETWEEN {lower} AND {CUTOFF}
AND lower(pool_address) IN ({quoted}) ORDER BY pool,block,log_index FORMAT JSONEachRow"""
    proc = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query", sql], text=True, capture_output=True, timeout=900, check=True)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line]
    by_pool = defaultdict(list)
    for event in events:
        by_pool[event["pool"]].append(event)

    reviewed = json.loads(json.dumps(data))
    e3_sell_counts = {"1": 0, "5": 0, "15": 0}
    prior_rugs = {"1": 0, "5": 0, "15": 0}
    for row in reviewed["rows"]:
        for window, evidence in row["windows"].items():
            prior_rugs[window] += bool(evidence["confirmed_nonrecovering_rug"])
            sells = [e for e in by_pool.get(row["pool"], []) if int(e["block"]) >= int(evidence["target_block"]) and side(e, int(row["orient"])) == "sell"]
            e3_sell_counts[window] += bool(sells)
            evidence["E3"]["full_index_query"] = {
                "database": "eatlq",
                "table": "uniswap_v3_swaps_decoded FINAL",
                "chain_id": 8453,
                "removed": 0,
                "pool_scope_n": len(pools),
                "from_block": lower,
                "through_block": CUTOFF,
                "query_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                "events_for_pool_after_target": len([e for e in by_pool.get(row["pool"], []) if int(e["block"]) >= int(evidence["target_block"])]),
                "verified_sell_events_after_target": len(sells),
            }
            evidence["E3"]["no_later_real_sell_through_cutoff"] = not bool(sells)
            # A cutoff-only atomic call plus an event index cannot exclude an
            # intermediate interval of restored atomic sellability.  Freeze E4
            # as unknown unless a recovery is positively observed.
            if sells:
                evidence["E4"]["recovered"] = True
                evidence["E4"]["no_recovery_through_cutoff"] = False
                evidence["adjudication"] = "RECOVERED_OR_TEMPORARY"
            else:
                evidence["E4"]["recovered"] = False
                evidence["E4"]["no_recovery_through_cutoff"] = None
                evidence["E4"]["review_reason"] = "cutoff single-point atomic evidence cannot exclude intermediate atomic recovery; no exhaustive state-change ladder"
                evidence["adjudication"] = "UNRESOLVED_E4_INTERMEDIATE_RECOVERY_NOT_EXCLUDED"
            evidence["confirmed_nonrecovering_rug"] = False

    reviewed["schema_version"] = 2
    reviewed["review"] = {
        "status": "PRIOR_RUG_LABELS_DOWNGRADED",
        "reason": "E3 was re-exported from the full decoded-swap index; E4 continuous non-recovery was not proven by a cutoff-only call",
        "full_index_events": len(events),
        "pool_scope_n": len(pools),
        "lower_block": lower,
        "cutoff_block": CUTOFF,
        "prior_confirmed_rugs": prior_rugs,
        "confirmed_rugs_after_review": {"1": 0, "5": 0, "15": 0},
        "windows_with_later_sell": e3_sell_counts,
    }
    reviewed["source_hashes"]["base_gl2_a_trigger_five_evidence_rpc_20260715.json"] = sha(SOURCE)
    reviewed["summary"]["confirmed_nonrecovering_rug"] = {"1": 0, "5": 0, "15": 0}
    reviewed["summary"]["final_unresolved_after_review"] = reviewed["summary"]["unresolved_before_five_evidence"]
    OUT.write_text(json.dumps(reviewed, indent=2, sort_keys=True) + "\n")
    print(json.dumps(reviewed["review"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
