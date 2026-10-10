#!/usr/bin/env python3
"""R3 Shadow recorder — append-only pre-outcome ledger for every KEYSTONE pass.

Zero capital. Zero decision impact. Does not call executor buy/sell.
Does not retune threshold. Outcomes are joined later by a separate job.
"""
from __future__ import annotations
import hashlib, json, os, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/root/keystone_r3_shadow")
LEDGER = ROOT / "ledger" / "preoutcome_signals.jsonl"
SPEC = json.loads((ROOT / "R3_PREREGISTRATION.json").read_text())
PRIMARY_THR = float(SPEC["operational_scorer"]["primary_threshold"])


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_preoutcome(rec: dict):
    required = SPEC["must_write_before_outcome"]
    missing = [k for k in required if k not in rec]
    if missing:
        raise ValueError(f"missing pre-outcome fields: {missing}")
    if rec.get("outcome") is not None:
        raise ValueError("outcome must not be present in pre-outcome write")
    rec = dict(rec)
    rec["ledger_write_utc"] = utcnow()
    rec["shadow_mode"] = True
    rec["decision_impact"] = False
    rec["f10_pass"] = bool(float(rec["f10_score"]) >= PRIMARY_THR)
    rec["f10_fail"] = not rec["f10_pass"]
    line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(line + "\n")
    return hashlib.sha256(line.encode()).hexdigest()


if __name__ == "__main__":
    print("R3 recorder library. Import append_preoutcome from bridge/shadow hook.")
    print("ledger=", LEDGER)
    print("primary_thr=", PRIMARY_THR)
