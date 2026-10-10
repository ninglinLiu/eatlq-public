#!/usr/bin/env python3
"""V4 State Entry Final Probe — verdict edition.

A. entry_block = signal_block + 1 (no lookahead)
B. Core table: MFE / MAE / trail / adj (not offset-0 roundtrip only)
C. Forward tick-path trail entry → entry+7200, hard_stop/trail/time_exit, realgas
D. Enhanced wave2: signed flow, buyer retention, LP post-entry flags

Usage:
  python3 v4_state_entry_final.py --detect
  bash run_in_cgroup.sh python3 v4_state_entry_final.py --path
  python3 v4_state_entry_final.py --verdict
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from live_gate import executor_healthy, wait_for_live_clear
from v4_alpha_readout import SPEND_ETH, SPEND_WEI, SWAP_INDEX, _load_json, _pool_meta, _strong_labels, pnl_frac
from v4_fork_replay import replay_at_block
from v4_state_entry_final_lib import (
    ENTRY_LAG,
    FORWARD_HORIZON,
    PATH_OFFSETS,
    adj_ret,
    core_table_row,
    detect_wave2_final,
    simulate_trail,
    tick_path_from_timeline,
)
from v4_swap_timeline import (
    DEPLOYER_CACHE,
    TIMELINE_PATH,
    build_timelines,
    ensure_deployers,
    fetch_pool_timeline,
    weth_is_currency0,
)

OUT_DETECT = os.path.join(HERE, "_state_entry_final_detect.json")
OUT_PATH = os.path.join(HERE, "_state_entry_final_path.json")
OUT_VERDICT = os.path.join(HERE, "_state_entry_final_verdict.json")
OUT_REPORT = os.path.join(HERE, "_state_entry_final_report.md")
FINAL_HORIZON = 12000  # timeline must cover entry+7200


def detect_all(*, build: bool = True) -> dict:
    meta = _pool_meta()
    strong = _strong_labels()
    targets = {p for p, r in strong.items() if r.get("label") == "clean"}
    rows = {pid: meta[pid] for pid in targets if pid in meta}

    if build:
        ensure_deployers(rows)
        swap_index = _load_json(SWAP_INDEX).get("pools", {}) if os.path.exists(SWAP_INDEX) else {}
        build_timelines(rows, horizon=FINAL_HORIZON, swap_index=swap_index)

    timeline = _load_json(TIMELINE_PATH)
    deployers = _load_json(DEPLOYER_CACHE).get("pools", {})
    results = []
    reasons = Counter()
    for pid in sorted(targets):
        tl = timeline.get("pools", {}).get(pid)
        if not tl or not tl.get("swaps"):
            results.append({"poolId": pid, "ok": False, "reason": "no_timeline"})
            reasons["no_timeline"] += 1
            continue
        init_from = (deployers.get(pid) or {}).get("init_from")
        det = detect_wave2_final(tl, rows[pid], init_from=init_from)
        det["poolId"] = pid
        det["hook_template"] = strong.get(pid, {}).get("hook_template")
        results.append(det)
        reasons[det.get("reason", "ok")] += 1

    out = {
        "probe": "state_entry_final",
        "entry_rule": f"signal_block + {ENTRY_LAG}",
        "forward_horizon": FORWARD_HORIZON,
        "n_pools": len(results),
        "n_confirmed": sum(1 for r in results if r.get("ok")),
        "reason_counts": dict(reasons),
        "pools": results,
    }
    json.dump(out, open(OUT_DETECT, "w"), indent=2)
    return out


def _extend_timeline_if_needed(pid: str, pool_row: dict, entry_block: int, cache: dict) -> dict:
    need_to = entry_block + FORWARD_HORIZON + 50
    tl = cache.get("pools", {}).get(pid, {})
    if tl.get("to_block", 0) >= need_to:
        return tl
    from_blk = tl.get("from_block", int(pool_row["blockNumber"]))
    from research_rpc import get_research_rpc

    fresh = fetch_pool_timeline(pid, pool_row, from_block=from_blk, to_block=need_to, rpc=get_research_rpc())
    cache.setdefault("pools", {})[pid] = fresh
    return fresh


def refresh_paths(*, incremental: bool = True) -> dict:
    if not executor_healthy() or not wait_for_live_clear(max_wait_s=120):
        raise RuntimeError("live gate blocked")
    detect = _load_json(OUT_DETECT)
    meta = _pool_meta()
    timeline = _load_json(TIMELINE_PATH)
    out = _load_json(OUT_PATH) if incremental and os.path.exists(OUT_PATH) else {"pools": {}, "meta": {}}
    pools_out = out.setdefault("pools", {})
    done = 0
    t0 = time.time()

    for row in detect.get("pools", []):
        if not row.get("ok"):
            continue
        pid = row["poolId"]
        if incremental and pid in pools_out and pools_out[pid].get("mfe") is not None:
            continue
        pool_row = meta[pid]
        entry = int(row["entry_block"])
        tl = _extend_timeline_if_needed(pid, pool_row, entry, timeline)
        weth_c0 = weth_is_currency0(pool_row)

        tick_path = tick_path_from_timeline(tl, entry, horizon=FORWARD_HORIZON, weth_c0=weth_c0)
        trail_sim = simulate_trail(tick_path)
        mfe = trail_sim.get("mfe")
        mae = trail_sim.get("mae")
        trail_ret = trail_sim.get("trail_ret")

        entry_rt = replay_at_block(entry, pool_row, SPEND_WEI)
        entry_fric = pnl_frac(entry_rt, SPEND_WEI)
        exec_offsets = {}
        for off in PATH_OFFSETS:
            blk = entry + off
            if off == 0:
                exec_offsets[str(off)] = {
                    "block": blk,
                    "roundtrip_pnl": round(entry_fric, 5),
                    "buy_ok": entry_rt.get("buy_ok"),
                    "sell_ok": entry_rt.get("sell_ok"),
                }
                continue
            rep = replay_at_block(blk, pool_row, SPEND_WEI)
            exec_offsets[str(off)] = {
                "block": blk,
                "roundtrip_pnl": round(pnl_frac(rep, SPEND_WEI), 5),
                "buy_ok": rep.get("buy_ok"),
                "sell_ok": rep.get("sell_ok"),
            }
            done += 1

        wave1_end = int(row.get("first_swap_block", entry)) + 300
        wave1_rep = replay_at_block(min(wave1_end, entry - 1), pool_row, SPEND_WEI)
        wave1_fric = pnl_frac(wave1_rep, SPEND_WEI) if wave1_end < entry else None
        exec_recovery = (
            round(entry_fric - wave1_fric, 5)
            if wave1_fric is not None and entry_fric > -1 and wave1_fric > -1
            else None
        )

        raw_ret = float(trail_ret) if trail_ret is not None else None
        adj = adj_ret(raw_ret) if raw_ret is not None and raw_ret > -0.999 else None
        if adj is not None:
            adj = round(adj + min(entry_fric, 0), 5)

        exec_pnls = [v["roundtrip_pnl"] for v in exec_offsets.values() if v.get("roundtrip_pnl", -1) > -1]
        exec_mfe = max(exec_pnls) if exec_pnls else None
        exec_mae = min(exec_pnls) if exec_pnls else None

        pools_out[pid] = {
            "poolId": pid,
            "signal_block": row.get("signal_block"),
            "entry_block": entry,
            "entry_fric_roundtrip": round(entry_fric, 5),
            "exec_recovery_vs_wave1_end": exec_recovery,
            "tick_path_len": len(tick_path),
            "mfe": round(float(mfe), 5) if mfe is not None else None,
            "mae": round(float(mae), 5) if mae is not None else None,
            "trail_ret": round(float(trail_ret), 5) if trail_ret is not None else None,
            "adj_ret": adj,
            "exit_block": trail_sim.get("exit_block"),
            "exit_why": trail_sim.get("exit_why"),
            "exec_mfe_roundtrip": round(float(exec_mfe), 5) if exec_mfe is not None else None,
            "exec_mae_roundtrip": round(float(exec_mae), 5) if exec_mae is not None else None,
            "executable_offsets": exec_offsets,
            "detection": row,
        }
        if done % 10 == 0:
            json.dump(out, open(OUT_PATH, "w"), indent=2)
            print(f"  final path {len(pools_out)} pools {int(time.time()-t0)}s", flush=True)

    out["meta"] = {
        "spend_eth": SPEND_ETH,
        "path_offsets": PATH_OFFSETS,
        "forward_horizon": FORWARD_HORIZON,
        "n_with_path": len(pools_out),
    }
    json.dump(out, open(OUT_PATH, "w"), indent=2)
    json.dump(timeline, open(TIMELINE_PATH, "w"), indent=2)
    return out


def verdict() -> dict:
    detect = _load_json(OUT_DETECT)
    paths = _load_json(OUT_PATH).get("pools", {})
    rows = []
    for r in detect.get("pools", []):
        if not r.get("ok"):
            continue
        p = paths.get(r["poolId"])
        if not p or p.get("mfe") is None:
            continue
        if p.get("exec_mfe_roundtrip") is None and p.get("executable_offsets"):
            pnls = [v["roundtrip_pnl"] for v in p["executable_offsets"].values() if v.get("roundtrip_pnl", -1) > -1]
            if pnls:
                p["exec_mfe_roundtrip"] = round(max(pnls), 5)
                p["exec_mae_roundtrip"] = round(min(pnls), 5)
        rows.append(p)

    table = core_table_row([
        {
            "mfe": x["mfe"],
            "mae": x["mae"],
            "trail_ret": x["trail_ret"],
            "adj_ret": x["adj_ret"] if x.get("adj_ret") is not None else -1,
        }
        for x in rows
    ])

    has_exec_mfe = [x for x in rows if (x.get("exec_mfe_roundtrip") or 0) > 0.05]
    tick_only = [x for x in rows if (x.get("mfe") or 0) > 0.05 and (x.get("exec_mfe_roundtrip") or 0) <= 0.05]

    conclusions = []
    if table.get("n", 0) == 0:
        conclusions.append("No confirmed wave2 entries with forward path — cannot verdict.")
    elif not has_exec_mfe and tick_only:
        conclusions.append(
            f"FINAL: {len(tick_only)}/{table['n']} show tick MFE>5% but executable roundtrip MFE≤0 — "
            "sqrtPrice peaks are NOT capturable; not an exit/timing fix."
        )
    elif table.get("p_mfe_gt_5pct", 0) == 0:
        conclusions.append(
            "FINAL: No tick MFE>5% — V4 state entry has no peak; exit tuning cannot help."
        )
    elif not has_exec_mfe:
        conclusions.append(
            "FINAL: Tick path shows peaks but roundtrip never turns positive — hook friction dominates."
        )
    elif table.get("trail_winrate", 0) < 0.1:
        conclusions.append("Executable MFE exists but trail/realgas winrate ~0 — exit/cost problem.")
    else:
        conclusions.append("Partial executable signal — per-pool review only.")

    report = {
        "probe": "state_entry_final",
        "detection": {
            "n_clean": detect.get("n_pools"),
            "n_confirmed": detect.get("n_confirmed"),
            "reason_counts": detect.get("reason_counts"),
            "entry_rule": detect.get("entry_rule"),
        },
        "core_table": table,
        "mfe_gt_5pct_n": sum(1 for x in rows if (x.get("mfe") or 0) > 0.05),
        "exec_mfe_gt_5pct_n": len(has_exec_mfe),
        "tick_only_mfe_n": len(tick_only),
        "conclusions": conclusions,
        "pools": rows,
    }
    json.dump(report, open(OUT_VERDICT, "w"), indent=2)

    md = f"""# V4 State Entry Final Probe

**Entry rule:** signal_block + {ENTRY_LAG} (no lookahead)  
**Forward path:** tick mark-to-market entry → +{FORWARD_HORIZON} blocks  
**Exit:** hard_stop −50%, trail 25%, time {FORWARD_HORIZON}  
**Cost:** gas ${0.15}/$50 stake + 2% haircut + entry roundtrip friction

## Detection

| | n |
|--|--|
| clean scanned | {detect.get('n_pools')} |
| wave2 confirmed | {detect.get('n_confirmed')} |

Reasons: `{detect.get('reason_counts')}`

## Core table (confirmed + path)

| Entry | n | med MFE | P(MFE>5%) | P(MFE>10%) | P(MFE>25%) | med MAE | med trail | trail winrate |
|-------|---|---------|-----------|------------|------------|---------|-----------|---------------|
| wave2+{ENTRY_LAG} | {table.get('n',0)} | {table.get('median_mfe')} | {table.get('p_mfe_gt_5pct')} | {table.get('p_mfe_gt_10pct')} | {table.get('p_mfe_gt_25pct')} | {table.get('median_mae')} | {table.get('median_trail_ret')} | {table.get('trail_winrate')} |

**Executable roundtrip MFE>5%:** {len(has_exec_mfe)}/{table.get('n',0)}  
**Tick-only MFE>5% (roundtrip still negative):** {len(tick_only)}/{table.get('n',0)}

## Verdict

{chr(10).join('- ' + c for c in conclusions)}

## Interpretation

- **MFE=0** → no peak after state entry; exit tuning cannot help.
- **MFE>0 but adj winrate=0** → peak exists but trail/cost/slippage eats it.
- **adj winrate>0** → only then discuss live wiring.
"""
    open(OUT_REPORT, "w").write(md)
    print(json.dumps({"core_table": table, "conclusions": conclusions}, indent=2))
    print(f"Wrote {OUT_VERDICT} {OUT_REPORT}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--path", action="store_true", help="anvil entry fric + tick forward path")
    ap.add_argument("--verdict", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.all or not any([args.detect, args.path, args.verdict]):
        args.detect = args.path = args.verdict = True

    if args.detect:
        d = detect_all(build=not args.no_build)
        print(json.dumps({"n_confirmed": d["n_confirmed"], "reasons": d["reason_counts"]}, indent=2))

    if args.path:
        refresh_paths()

    if args.verdict:
        verdict()

    return 0


if __name__ == "__main__":
    sys.exit(main())
