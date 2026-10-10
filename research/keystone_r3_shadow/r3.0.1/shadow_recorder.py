#!/usr/bin/env python3
"""r3.0.1-shadow append-only preoutcome recorder."""
from __future__ import annotations
import hashlib, json, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/keystone_r3_shadow/r3.0.1")
SPEC = json.loads((ROOT / "R3_PREREGISTRATION.json").read_text())
LEDGER = Path("/root/keystone_r3_shadow") / "ledger" / "r3_preoutcome_events.jsonl"
STATE = Path("/root/keystone_r3_shadow") / "ledger" / "r3_preoutcome_chain_tip.txt"
PRIMARY_THR = float(SPEC["operational_scorer"]["primary_threshold"])
ART_SHA = SPEC["operational_scorer"]["artifact_sha256_expected"]
PIPE_SHA = SPEC["operational_scorer"]["feature_pipeline_sha256"]
REZ_SHA = SPEC["operational_scorer"]["residualizer_sha256"]
PRICE_SHA = SPEC["price_only_control"]["artifact_sha256"]
RECORDER_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_preoutcome(rec: dict) -> str:
    if SPEC.get("clock_started") is not True:
        # allow writes only after human arm sets clock_started true in an arm file
        arm = Path("/root/keystone_r3_shadow") / "HUMAN_ARM.json"
        if not arm.exists() or not json.loads(arm.read_text()).get("clock_started"):
            raise RuntimeError("R3 confirmatory clock not armed; refusing write")
    required = [
        "signal_id","t0_block","decision_block","feature_cutoff_block","preoutcome_write_block",
        "target_entry_block","decision_block_hash","pool","keystone_pass","p_fatal","friction",
        "f10_score","impact_residual_value","ineffective_buying_value","liq_fragility_score_value",
        "part_exhaust_score_value","accel_value","price_only_control_score","feature_snapshot_hash",
    ]
    missing = [k for k in required if k not in rec]
    if missing:
        raise ValueError(f"missing: {missing}")
    if "outcome" in rec:
        raise ValueError("outcome forbidden in preoutcome")
    rec = dict(rec)
    rec["f10_pass"] = bool(float(rec["f10_score"]) >= PRIMARY_THR)
    rec["manifest_version"] = "r3.0.1-shadow"
    rec["feature_pipeline_sha256"] = PIPE_SHA
    rec["residualizer_sha256"] = REZ_SHA
    rec["price_only_control_sha256"] = PRICE_SHA
    rec["operational_artifact_sha256"] = ART_SHA
    rec["recorder_code_sha256"] = RECORDER_SHA
    rec["timestamp_written_utc"] = utcnow()
    rec["decision_impact"] = False
    rec["shadow_mode"] = True
    prev = STATE.read_text().strip() if STATE.exists() else "GENESIS"
    rec["prev_record_sha256"] = prev
    line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    rh = hashlib.sha256(line.encode()).hexdigest()
    rec2 = json.loads(line)
    rec2["record_sha256"] = rh
    line2 = json.dumps(rec2, sort_keys=True, separators=(",", ":"))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(line2 + "\n")
    STATE.write_text(rh + "\n")
    return rh


if __name__ == "__main__":
    print("recorder", Path(__file__))
    print("status", SPEC.get("confirmatory_start_status"))
    print("artifact", ART_SHA)
