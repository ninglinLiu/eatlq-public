#!/usr/bin/env python3
"""Stage 4a — cheap (1-3 RPC/coin) EARLY-PATH features from the bonding-curve's
own signature stream. Tests the stage-3 hypothesis that the real ex-ante edge
lives in the first 1-3 minutes of trading activity, not in static metadata.

For each sampled coin we getSignaturesForAddress(bonding_curve) newest-first and
page back until we reach the creation time (most pump coins die within hours, so
1-2 pages cover birth). From the trade TIMESTAMPS alone (no getTransaction) we
derive activity features that a live poller could observe by T+180s:

  n_trades_60s / 180s / 600s   : trades within W seconds of creation
  time_to_5th_trade_s          : how fast 5 trades accrue (demand velocity)
  lifespan_s                   : last_trade - created (did it keep trading?)
  alive_at_180s                : any trade in [120s,180s]
  total_trades_seen            : within paged window
  reached_birth                : did pagination reach creation (else ultra-hot)

These are genuinely ex-ante for an entry at T+180s. Output:
  mvp/diagnostics/sol/creates_earlypath.jsonl  (merged with labels)
"""
from __future__ import annotations
import json
import os
import time

import importlib.util

_spec = importlib.util.spec_from_file_location("samp", "/opt/EATLQ/scripts/research/sol/sol_sample_creates.py")
_s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s)
RPC = _s.RPC
rpc_url = _s.rpc_url

LAB = "/opt/EATLQ/mvp/diagnostics/sol/creates_labeled.jsonl"
OUT = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"
MAX_PAGES = 3


def trade_times(rpc, bc, created_s):
    """Return (sorted blockTimes incl create-adjacent, reached_birth, err_rate)."""
    times = []
    errs = 0
    before = None
    reached = False
    for _ in range(MAX_PAGES):
        params = [bc, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        if not sigs:
            reached = True  # exhausted = we have everything
            break
        before = sigs[-1]["signature"]
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None:
                continue
            times.append(bt)
            if s.get("err"):
                errs += 1
        if (sigs[-1].get("blockTime") or 1e18) <= created_s + 5:
            reached = True
            break
        if len(sigs) < 1000:
            reached = True
            break
    times.sort()
    return times, reached, (errs / len(times) if times else 0.0)


def features(times, reached, created_s):
    if not times:
        return None
    rel = [t - created_s for t in times]
    rel = [x for x in rel if x >= -2]  # drop pre-create noise
    n60 = sum(1 for x in rel if x <= 60)
    n180 = sum(1 for x in rel if x <= 180)
    n600 = sum(1 for x in rel if x <= 600)
    t5 = rel[4] if len(rel) >= 5 else None
    alive180 = any(120 <= x <= 180 for x in rel)
    lifespan = max(rel) if rel else 0
    return {
        "n_trades_60s": n60,
        "n_trades_180s": n180,
        "n_trades_600s": n600,
        "time_to_5th_s": round(t5, 1) if t5 is not None else None,
        "alive_at_180s": alive180,
        "lifespan_s": round(lifespan, 1),
        "total_trades_seen": len(rel),
        "reached_birth": reached,
    }


def main():
    rows = [json.loads(l) for l in open(LAB)]
    rows = [r for r in rows if r.get("bonding_curve") and r.get("created_ms")]
    print(f"[earlypath] {len(rows)} coins with bonding_curve")
    done = set()
    mode = "w"
    if os.path.exists(OUT):
        for l in open(OUT):
            try:
                done.add(json.loads(l)["mint"])
            except Exception:
                pass
        mode = "a"
    rpc = RPC(rpc_url(), min_interval=0.18)
    n = 0
    with open(OUT, mode) as f:
        for i, r in enumerate(rows):
            if r["mint"] in done:
                continue
            created_s = r["created_ms"] / 1000.0
            try:
                times, reached, errr = trade_times(rpc, r["bonding_curve"], created_s)
            except Exception:
                times, reached, errr = [], False, 0.0
            feat = features(times, reached, created_s)
            if feat is None:
                feat = {"n_trades_60s": 0, "n_trades_180s": 0, "n_trades_600s": 0,
                        "time_to_5th_s": None, "alive_at_180s": False, "lifespan_s": 0,
                        "total_trades_seen": 0, "reached_birth": True}
            out = {**r, **feat, "trade_err_rate": round(errr, 3)}
            f.write(json.dumps(out) + "\n")
            n += 1
            if (i + 1) % 100 == 0:
                f.flush()
                print(f"  [earlypath] {i+1}/{len(rows)} rpc={rpc.calls}", flush=True)
    print(f"[done] wrote {n} -> {OUT}  total_rpc={rpc.calls}")


if __name__ == "__main__":
    main()
