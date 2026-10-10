#!/usr/bin/env python3
"""V4 trail + friction/deep-pool + realgas-style PnL (V3 live config mirror).

Uses first-swap entry PnL paths from _alpha_alt_entry_cache.json (0 research counter
for trail sim). Optional --probe-depth runs anvil large-size probe (0 counter RPC).

V3 reference gates (adapted):
  - friction >= -5% at entry (offset 0 roundtrip)
  - depth >= 3 ETH (large-size probe: 0.01 ETH roundtrip slippage > -15%)
  - TRAIL=0.25 on mark-to-market PnL path (offsets 0,30,300,1800,7200)
  - realgas adj: ret - GAS/STAKE - HAIRCUT ($0.15 / $50 stake - 2%)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from live_gate import executor_healthy, wait_for_live_clear
from v4_fork_replay import replay_at_block
from v4_alpha_readout import (
    ALT_ENTRY_CACHE,
    PNL_CACHE,
    _load_json,
    _pool_meta,
    _strong_labels,
    _summarize_pnls,
    pnl_frac,
)

OUT = os.path.join(HERE, "_v4_trail_backtest.json")
DEPTH_CACHE = os.path.join(HERE, "_v4_pool_depth_probe.json")

TRAIL = 0.25
FRIC_THR = -0.05
MIN_DEPTH_ETH = 3.0
PROBE_SPEND_ETH = 0.01
PROBE_SLIP_THR = -0.15
STAKE_USD = 50.0
GAS_USD_RT = 0.15
HAIRCUT = 0.02
TRAIL_OFFSETS = [0, 30, 300, 1800, 7200]


def adj_ret(raw: float) -> float:
    return float(raw) - GAS_USD_RT / STAKE_USD - HAIRCUT


def _pnl_path(rec: dict, offsets: list[int]) -> list[tuple[int, float]]:
    by = rec.get("first_swap_pnl_by_offset") or rec.get("pnl_by_offset") or {}
    path = []
    for off in offsets:
        row = by.get(str(off))
        if row and row.get("pnl_frac") is not None:
            path.append((off, float(row["pnl_frac"])))
    return path


def simulate_trail(path: list[tuple[int, float]], trail: float = TRAIL) -> dict:
    """Discrete trail on log(1+ret) — mirrors V3 exit_block_param."""
    if not path:
        return {"exit_offset": None, "trail_ret": None, "why": "empty_path"}
    peak_log = -1e9
    entry_log = math.log(max(1e-9, 1 + path[0][1]))
    thr = math.log(1 - trail)
    hard_stop = math.log(0.5)
    exit_off, exit_ret = path[0]
    for off, ret in path:
        v = math.log(max(1e-9, 1 + ret))
        peak_log = max(peak_log, v)
        if (v - entry_log) <= hard_stop:
            return {"exit_offset": off, "trail_ret": ret, "why": "hard_stop"}
        if (v - peak_log) <= thr:
            return {"exit_offset": off, "trail_ret": ret, "why": "trail"}
        exit_off, exit_ret = off, ret
    return {"exit_offset": exit_off, "trail_ret": exit_ret, "why": "time_exit"}


def probe_depth(pool_row: dict, entry_block: int, spend_wei: int | None = None) -> dict:
    spend_wei = spend_wei or int(PROBE_SPEND_ETH * 1e18)
    rep = replay_at_block(entry_block, pool_row, spend_wei)
    slip = pnl_frac(rep, spend_wei)
    return {
        "probe_spend_eth": PROBE_SPEND_ETH,
        "slippage": round(slip, 4) if slip > -1 else slip,
        "buy_ok": rep.get("buy_ok"),
        "sell_ok": rep.get("sell_ok"),
        "depth_ok": bool(rep.get("buy_ok") and rep.get("sell_ok") and slip >= PROBE_SLIP_THR),
    }


def fetch_depth_probes(pool_ids: set[str], alt_cache: dict, meta: dict) -> dict:
    if not executor_healthy() or not wait_for_live_clear(max_wait_s=120):
        raise RuntimeError("live gate blocked")
    out = _load_json(DEPTH_CACHE) if os.path.exists(DEPTH_CACHE) else {"pools": {}}
    pools = out.setdefault("pools", {})
    done = 0
    for pid in pool_ids:
        if pid in pools and pools[pid].get("depth_ok") is not None:
            continue
        alt = alt_cache.get("pools", {}).get(pid, {})
        row = meta.get(pid)
        if not row:
            continue
        entry = int(alt.get("first_swap_block") or row["blockNumber"])
        pools[pid] = {"poolId": pid, **probe_depth(row, entry)}
        done += 1
        if done % 5 == 0:
            json.dump(out, open(DEPTH_CACHE, "w"), indent=2)
            print(f"  depth probe {done} elapsed…", flush=True)
    out["meta"] = {"probe_spend_eth": PROBE_SPEND_ETH, "slip_thr": PROBE_SLIP_THR}
    json.dump(out, open(DEPTH_CACHE, "w"), indent=2)
    return out


def run_backtest(
    *,
    use_first_swap: bool = True,
    depth_cache: Optional[dict] = None,
    require_depth: bool = False,
) -> dict:
    strong = _strong_labels()
    alt = _load_json(ALT_ENTRY_CACHE)
    init = _load_json(PNL_CACHE)
    depth_cache = depth_cache or {"pools": {}}
    rows = []
    for pid, s in strong.items():
        if s.get("label") != "clean":
            continue
        alt_rec = alt.get("pools", {}).get(pid, {})
        init_rec = init.get("pools", {}).get(pid, {})
        rec = alt_rec if use_first_swap and alt_rec else init_rec
        path = _pnl_path(rec, TRAIL_OFFSETS)
        if len(path) < 2:
            continue
        entry_pnl = path[0][1]
        fric_ok = entry_pnl >= FRIC_THR
        sim = simulate_trail(path)
        trail_ret = sim.get("trail_ret")
        if trail_ret is None:
            continue
        depth_row = depth_cache.get("pools", {}).get(pid, {})
        depth_ok = depth_row.get("depth_ok")
        if require_depth and not depth_ok:
            continue
        tmpl = s.get("hook_template") or "none"
        rows.append({
            "poolId": pid,
            "hook_template": tmpl,
            "entry_mode": "first_swap" if use_first_swap else "initialize",
            "entry_pnl": round(entry_pnl, 5),
            "fric_ok": fric_ok,
            "depth_ok": depth_ok,
            "trail_ret": round(trail_ret, 5),
            "adj_ret": round(adj_ret(trail_ret), 5) if trail_ret > -0.999 else -1.0,
            "exit_offset": sim.get("exit_offset"),
            "exit_why": sim.get("why"),
            "fatal": trail_ret <= -0.5,
        })

    def _gate(name: str, filt) -> dict:
        sub = [r for r in rows if filt(r)]
        rets = [r["adj_ret"] for r in sub if r["adj_ret"] is not None and r["adj_ret"] > -0.999]
        raw = [r["trail_ret"] for r in sub]
        return {
            "gate": name,
            "n": len(sub),
            "raw_trail": _summarize_pnls(raw),
            "adj_realgas": _summarize_pnls(rets),
        }

    gates = [
        _gate("all_clean", lambda r: True),
        _gate("fric_ge_-5pct", lambda r: r["fric_ok"]),
        _gate("fric_and_depth_probe", lambda r: r["fric_ok"] and r.get("depth_ok")),
        _gate("RD-B_fric", lambda r: r["hook_template"] == "returns_delta_top_b" and r["fric_ok"]),
        _gate("RD-B_fric_depth", lambda r: r["hook_template"] == "returns_delta_top_b"
              and r["fric_ok"] and r.get("depth_ok")),
    ]
    by_tmpl = defaultdict(list)
    for r in rows:
        if r["fric_ok"]:
            by_tmpl[r["hook_template"] or "none"].append(r["adj_ret"])
    tmpl_summary = {k: _summarize_pnls(v) for k, v in sorted(by_tmpl.items())}

    report = {
        "config": {
            "trail": TRAIL,
            "fric_thr": FRIC_THR,
            "min_depth_eth_target": MIN_DEPTH_ETH,
            "depth_probe_eth": PROBE_SPEND_ETH,
            "depth_probe_slip_thr": PROBE_SLIP_THR,
            "gas_usd": GAS_USD_RT,
            "stake_usd": STAKE_USD,
            "haircut": HAIRCUT,
            "entry_mode": "first_swap" if use_first_swap else "initialize",
            "offsets": TRAIL_OFFSETS,
        },
        "n_clean_pools": len(rows),
        "gates": gates,
        "by_template_fric_pass": tmpl_summary,
        "v3_reference": {
            "source": "644 aggressive live realgas + gates",
            "fric_ge_-5_depth_ge3_positive_rate": 0.75,
        },
        "conclusion": [],
    }
    best = next((g for g in gates if g["gate"] == "RD-B_fric_depth"), gates[0])
    adj = best["adj_realgas"]
    if adj.get("positive_rate", 0) >= 0.1 or adj.get("tail_ge_100pct_rate", 0) >= 0.05:
        report["conclusion"].append(
            f"RD-B trail+realgas shows signal: pos={adj.get('positive_rate')} "
            f"tail100={adj.get('tail_ge_100pct_rate')} (n={adj.get('n')})."
        )
    else:
        report["conclusion"].append(
            f"No V3-class alpha: best gate {best['gate']} pos={adj.get('positive_rate')} "
            f"med={adj.get('median_pnl_frac')} tail100={adj.get('tail_ge_100pct_rate')} (n={adj.get('n')})."
        )
    report["conclusion"].append(
        "V4 trail uses discrete roundtrip PnL path, not tick path + QuoterV2 — compare directionally to V3 only."
    )
    json.dump({"rows": rows, "report": report}, open(OUT, "w"), indent=2)
    print(json.dumps({"gates": gates, "conclusion": report["conclusion"]}, indent=2))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-depth", action="store_true", help="anvil 0.01 ETH depth probe (0 counter RPC)")
    ap.add_argument("--require-depth", action="store_true", help="only include depth_ok pools in gated rows")
    ap.add_argument("--init-entry", action="store_true", help="use Initialize entry instead of first_swap")
    args = ap.parse_args()

    strong = _strong_labels()
    clean_ids = {p for p, r in strong.items() if r.get("label") == "clean"}
    alt = _load_json(ALT_ENTRY_CACHE)
    meta = _pool_meta()

    depth_cache = {"pools": {}}
    if args.probe_depth or os.path.exists(DEPTH_CACHE):
        if args.probe_depth:
            depth_cache = fetch_depth_probes(clean_ids, alt, meta)
        elif os.path.exists(DEPTH_CACHE):
            depth_cache = _load_json(DEPTH_CACHE)

    run_backtest(
        use_first_swap=not args.init_entry,
        depth_cache=depth_cache,
        require_depth=args.require_depth,
    )
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
