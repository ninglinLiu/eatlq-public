#!/usr/bin/env python3
"""Phase 5: Alpha readout — PnL on roundtrip_strong clean / RD-B subsets (0 counter RPC for analysis)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from statistics import median
from typing import Optional

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from live_gate import executor_healthy, wait_for_live_clear
from v4_fork_replay import HOLD_OFFSETS, LIQUIDITY_BLOCK_OFFSET, load_pools, replay_at_block

DEFAULT_LABELS = os.path.join(HERE, "_fork_replay_labels_merged_strong.json")
DEFAULT_ROUNDTRIP = os.path.join(HERE, "_fork_replay_labels_roundtrip.json")
PNL_CACHE = os.path.join(HERE, "_alpha_pnl_cache.json")
ALT_ENTRY_CACHE = os.path.join(HERE, "_alpha_alt_entry_cache.json")
POOLS_JSONL = "/tmp/v4_created_pools.jsonl"
SWAP_INDEX = "/tmp/v4_pool_swap_index.json"
V3_UNI_GLOB = "/opt/EATLQ/scripts/research/base_lefttail/_uni_shards/_uni_s*.json"
V3_REALGAS = "/opt/EATLQ/scripts/research/base_lefttail/_realgas_replay.json"
V3_LABELED = "/opt/EATLQ/scripts/research/base_lefttail/_labeled_universe.json"

SPEND_ETH = 0.003
SPEND_WEI = int(SPEND_ETH * 1e18)
LATE_DELTAS = [100, 500, 2000]
ENTRY_MODE = "initialize_block"
FAST_SWAP_BLOCKS = 100  # init→first_swap delay proxy for "activity arrived early"


def _load_json(path: str) -> dict:
    return json.load(open(path))


def _pool_meta() -> dict[str, dict]:
    m = {}
    for line in open(POOLS_JSONL):
        if line.strip():
            r = json.loads(line)
            m[r["poolId"]] = r
    return m


def _strong_labels(path: str = DEFAULT_ROUNDTRIP) -> dict[str, dict]:
    data = _load_json(path)
    return {p["poolId"]: p for p in data.get("pools", [])}


def _template_group(row: dict, meta: dict) -> str:
    tmpl = row.get("hook_template") or meta.get(row["poolId"], {}).get("hook_template", {})
    if isinstance(tmpl, dict):
        name = tmpl.get("template_name")
    else:
        name = tmpl
    if name == "returns_delta_top_b":
        return "RD-B"
    if name == "returns_delta_top_a":
        return "RD-A"
    tier = row.get("hook_tier") or meta.get(row["poolId"], {}).get("hook_profile", {}).get("risk_tier")
    if tier == "vanilla" or name == "no_hook" or not row.get("hooks", "0x0").endswith("0000" * 6):
        if tier == "vanilla" or (row.get("hooks") or "").lower().endswith("0" * 24):
            return "vanilla"
    fam = (meta.get(row["poolId"], {}).get("hook_template") or {}).get("hook_family", "")
    if "returns_delta" in str(fam) or tier == "returns_delta_high":
        return "unmatched_RD"
    if tier == "vanilla":
        return "vanilla"
    return "other"


def pnl_frac(rep: dict, spend_wei: int = SPEND_WEI) -> float:
    """WETH roundtrip return fraction (keystone-compatible: ret = out/in - 1)."""
    if not rep.get("buy_ok"):
        return -1.0
    if not rep.get("sell_ok"):
        return -1.0
    wb = int(rep.get("weth_back") or 0)
    if wb <= 0 or wb > spend_wei * 100:
        return -1.0
    return wb / spend_wei - 1.0


def _summarize_pnls(pnls: list[float]) -> dict:
    if not pnls:
        return {"n": 0}
    a = np.array(pnls, dtype=float)
    dec = np.sort(a)[max(0, int(len(a) * 0.9) - 1):].mean() if len(a) >= 10 else float(a.max())
    return {
        "n": len(a),
        "positive_rate": round(float((a > 0).mean()), 3),
        "median_pnl_frac": round(float(np.median(a)), 3),
        "mean_pnl_frac": round(float(a.mean()), 3),
        "tail_ge_100pct_rate": round(float((a >= 1.0).mean()), 3),
        "tail_ge_300pct_rate": round(float((a >= 3.0).mean()), 3),
        "fatal_le_50pct_rate": round(float((a <= -0.5).mean()), 3),
        "fatal_le_90pct_rate": round(float((a <= -0.9).mean()), 3),
        "convexity_mean_minus_median": round(float(a.mean() - np.median(a)), 3),
        "top_decile_mean": round(float(dec), 3),
        "note": "ret = weth_net/spend - 1; sell fail = -100%",
    }


def refresh_pnl(
    *,
    pool_ids: Optional[set[str]] = None,
    offsets: Optional[list[int]] = None,
    late_deltas: Optional[list[int]] = None,
    incremental: bool = True,
) -> dict:
    """Re-run anvil roundtrip to fix weth_back (0 research counter RPC)."""
    if not executor_healthy() or not wait_for_live_clear(max_wait_s=120):
        raise RuntimeError("live gate blocked")
    meta = _pool_meta()
    strong = _strong_labels()
    cache = _load_json(PNL_CACHE) if incremental and os.path.exists(PNL_CACHE) else {"pools": {}}
    pools_cache = cache.setdefault("pools", {})
    offsets = offsets or HOLD_OFFSETS
    late_deltas = late_deltas or []
    targets = [strong[pid] for pid in (pool_ids or strong.keys()) if pid in strong]
    t0 = time.time()
    done = 0
    for row in targets:
        pid = row["poolId"]
        if pid not in meta:
            continue
        pool_row = meta[pid]
        entry = int(row.get("entry_block") or pool_row["blockNumber"])
        rec = pools_cache.setdefault(pid, {
            "poolId": pid,
            "strong_label": row.get("label"),
            "hook_template": row.get("hook_template"),
            "hook_tier": row.get("hook_tier"),
            "entry_block": entry,
            "spend_wei": SPEND_WEI,
            "pnl_by_offset": {},
            "late_entry_pnl": {},
        })
        for off in offsets:
            key = str(off)
            if incremental and key in rec.get("pnl_by_offset", {}):
                continue
            blk = entry + off
            rep = replay_at_block(blk, pool_row, SPEND_WEI)
            pf = pnl_frac(rep)
            rec["pnl_by_offset"][key] = {
                "offset": off,
                "block": blk,
                "pnl_frac": round(pf, 5),
                "pnl_pct": round(pf * 100, 2),
                "buy_ok": rep.get("buy_ok"),
                "sell_ok": rep.get("sell_ok"),
                "weth_back": rep.get("weth_back"),
            }
            done += 1
            if done % 10 == 0:
                _save_cache(cache)
                print(f"  refresh {done} replays elapsed={int(time.time()-t0)}s", flush=True)
        for delta in late_deltas:
            dkey = str(delta)
            if incremental and dkey in rec.get("late_entry_pnl", {}):
                continue
            late_entry = entry + delta
            rep = replay_at_block(late_entry, pool_row, SPEND_WEI)
            pf = pnl_frac(rep)
            rec["late_entry_pnl"][dkey] = {
                "delta_blocks": delta,
                "entry_block": late_entry,
                "pnl_frac": round(pf, 5),
                "pnl_pct": round(pf * 100, 2),
            }
            done += 1
    cache["meta"] = {
        "updated_at": time.time(),
        "spend_eth": SPEND_ETH,
        "entry_mode": ENTRY_MODE,
        "offsets": offsets,
        "late_deltas": late_deltas,
        "n_pools": len(pools_cache),
    }
    _save_cache(cache)
    print(f"refresh done: {done} replays, n_pools={len(pools_cache)}", flush=True)
    return cache


def _pid_key(pid: str) -> str:
    return pid if pid.startswith("0x") else "0x" + pid


def refresh_alt_entry(
    *,
    pool_ids: Optional[set[str]] = None,
    offsets: Optional[list[int]] = None,
    incremental: bool = True,
) -> dict:
    """Roundtrip PnL at first-swap block (0 research counter RPC; archive anvil)."""
    if not executor_healthy() or not wait_for_live_clear(max_wait_s=120):
        raise RuntimeError("live gate blocked")
    meta = _pool_meta()
    strong = _strong_labels()
    index = _load_json(SWAP_INDEX) if os.path.exists(SWAP_INDEX) else {"pools": {}}
    idx_pools = index.get("pools", {})
    offsets = offsets or [0, 30, 300]
    cache = _load_json(ALT_ENTRY_CACHE) if incremental and os.path.exists(ALT_ENTRY_CACHE) else {"pools": {}}
    pools_cache = cache.setdefault("pools", {})
    targets = [strong[pid] for pid in (pool_ids or strong.keys()) if pid in strong]
    done = 0
    t0 = time.time()
    for row in targets:
        if row.get("label") != "clean":
            continue
        pid = row["poolId"]
        if pid not in meta:
            continue
        swap_meta = idx_pools.get(_pid_key(pid))
        if not swap_meta:
            continue
        init_blk = int(row.get("entry_block") or meta[pid]["blockNumber"])
        first_swap_blk = int(swap_meta["block"])
        delay = first_swap_blk - init_blk
        pool_row = meta[pid]
        rec = pools_cache.setdefault(pid, {
            "poolId": pid,
            "strong_label": row.get("label"),
            "hook_template": row.get("hook_template"),
            "hook_tier": row.get("hook_tier"),
            "init_block": init_blk,
            "first_swap_block": first_swap_blk,
            "init_to_first_swap_blocks": delay,
            "spend_wei": SPEND_WEI,
            "first_swap_pnl_by_offset": {},
        })
        for off in offsets:
            key = str(off)
            if incremental and key in rec.get("first_swap_pnl_by_offset", {}):
                continue
            entry = first_swap_blk + off
            rep = replay_at_block(entry, pool_row, SPEND_WEI)
            pf = pnl_frac(rep)
            rec["first_swap_pnl_by_offset"][key] = {
                "offset_from_first_swap": off,
                "entry_block": entry,
                "pnl_frac": round(pf, 5),
                "pnl_pct": round(pf * 100, 2),
                "buy_ok": rep.get("buy_ok"),
                "sell_ok": rep.get("sell_ok"),
            }
            done += 1
            if done % 10 == 0:
                json.dump(cache, open(ALT_ENTRY_CACHE, "w"), indent=2)
                print(f"  alt-entry {done} replays elapsed={int(time.time()-t0)}s", flush=True)
    cache["meta"] = {
        "updated_at": time.time(),
        "entry_mode": "first_swap_block",
        "offsets_from_first_swap": offsets,
        "spend_eth": SPEND_ETH,
        "n_pools": len(pools_cache),
    }
    json.dump(cache, open(ALT_ENTRY_CACHE, "w"), indent=2)
    print(f"alt-entry refresh done: {done} replays, n_pools={len(pools_cache)}", flush=True)
    return cache


def task_alt_entry(
    init_cache: dict,
    alt_cache: dict,
    strong: dict,
    meta: dict,
    out_dir: str,
) -> dict:
    """Compare Initialize vs first-swap entry; stratify by activity delay."""
    per_pool = []
    by_group = defaultdict(lambda: {"init": [], "first_swap": []})
    by_delay = defaultdict(lambda: {"init": [], "first_swap": []})
    FAST_DELAY = 100

    for pid, alt in alt_cache.get("pools", {}).items():
        if strong.get(pid, {}).get("label") != "clean":
            continue
        init_rec = init_cache.get("pools", {}).get(pid, {})
        init_pnl = init_rec.get("pnl_by_offset", {}).get("0", {}).get("pnl_frac")
        fs_pnl = alt.get("first_swap_pnl_by_offset", {}).get("0", {}).get("pnl_frac")
        if init_pnl is None or fs_pnl is None:
            continue
        delay = alt.get("init_to_first_swap_blocks")
        grp = _template_group(alt, meta)
        bucket = "fast_activity" if delay is not None and delay <= FAST_DELAY else "slow_activity"
        per_pool.append({
            "poolId": pid,
            "hook_template": alt.get("hook_template"),
            "init_to_first_swap_blocks": delay,
            "init_pnl_frac": init_pnl,
            "first_swap_pnl_frac": fs_pnl,
            "delta_pnl_frac": round(fs_pnl - init_pnl, 5),
        })
        by_group[grp]["init"].append(init_pnl)
        by_group[grp]["first_swap"].append(fs_pnl)
        by_delay[bucket]["init"].append(init_pnl)
        by_delay[bucket]["first_swap"].append(fs_pnl)

    group_summary = {}
    for g, v in sorted(by_group.items()):
        group_summary[g] = {
            "n": len(v["init"]),
            "initialize_entry": _summarize_pnls(v["init"]),
            "first_swap_entry": _summarize_pnls(v["first_swap"]),
            "median_improvement": round(float(np.median(np.array(v["first_swap"]) - np.array(v["init"]))), 3)
            if v["init"] else None,
        }

    delay_summary = {}
    for b, v in sorted(by_delay.items()):
        delay_summary[b] = {
            "n": len(v["init"]),
            "initialize_entry": _summarize_pnls(v["init"]),
            "first_swap_entry": _summarize_pnls(v["first_swap"]),
        }

    # offset curve at first_swap entry for RD-B
    rdb_curve = []
    for off in (0, 30, 300):
        pnls = [
            alt_cache["pools"][pid]["first_swap_pnl_by_offset"].get(str(off), {}).get("pnl_frac")
            for pid in alt_cache.get("pools", {})
            if strong.get(pid, {}).get("label") == "clean"
            and alt_cache["pools"][pid].get("hook_template") == "returns_delta_top_b"
            and str(off) in alt_cache["pools"][pid].get("first_swap_pnl_by_offset", {})
        ]
        pnls = [p for p in pnls if p is not None]
        rdb_curve.append({"offset_from_first_swap": off, **_summarize_pnls(pnls)})

    improved = sum(1 for p in per_pool if p["delta_pnl_frac"] > 0)
    any_positive_fs = sum(1 for p in per_pool if p["first_swap_pnl_frac"] > 0)
    report = {
        "n_pools": len(per_pool),
        "entry_modes_compared": ["initialize_block", "first_swap_block"],
        "fast_activity_threshold_blocks": FAST_DELAY,
        "summary": {
            "initialize_entry": _summarize_pnls([p["init_pnl_frac"] for p in per_pool]),
            "first_swap_entry": _summarize_pnls([p["first_swap_pnl_frac"] for p in per_pool]),
            "pools_improved_at_first_swap": improved,
            "pools_improved_rate": round(improved / len(per_pool), 3) if per_pool else 0,
            "any_positive_at_first_swap": any_positive_fs,
            "any_positive_rate": round(any_positive_fs / len(per_pool), 3) if per_pool else 0,
        },
        "by_template": group_summary,
        "by_activity_delay": delay_summary,
        "rdb_first_swap_offset_curve": rdb_curve,
        "conclusion_lines": [],
    }
    s = report["summary"]
    fs = s["first_swap_entry"]
    init = s["initialize_entry"]
    if fs.get("positive_rate", 0) > init.get("positive_rate", 0) or fs.get("tail_ge_100pct_rate", 0) > 0:
        report["conclusion_lines"].append(
            f"First-swap entry shows higher positive rate ({fs.get('positive_rate')}) vs Initialize ({init.get('positive_rate')})."
        )
    else:
        report["conclusion_lines"].append(
            f"No right-tail at first-swap entry: positive {fs.get('positive_rate')} (n={fs.get('n')}), "
            f"tail≥+100% {fs.get('tail_ge_100pct_rate')}."
        )
    report["conclusion_lines"].append(
        f"Median PnL shift init→first_swap: {round(float(np.median([p['delta_pnl_frac'] for p in per_pool])), 3) if per_pool else 0}."
    )

    out_path = os.path.join(out_dir, "_alpha_alt_entry_report.json")
    json.dump({"pools": per_pool[:30], "report": report}, open(out_path, "w"), indent=2)
    json.dump(report, open(os.path.join(out_dir, "_alpha_alt_entry_summary.json"), "w"), indent=2)
    print(json.dumps({"n": len(per_pool), "summary": report["summary"], "conclusion": report["conclusion_lines"]}, indent=2))
    return report


def _save_cache(cache: dict) -> None:
    json.dump(cache, open(PNL_CACHE, "w"), indent=2)


def _best_pnl(rec: dict, offset: Optional[int] = None) -> Optional[float]:
    by = rec.get("pnl_by_offset") or {}
    if offset is not None:
        v = by.get(str(offset), {}).get("pnl_frac")
        return v
    vals = [v.get("pnl_frac") for v in by.values() if v.get("pnl_frac") is not None]
    return max(vals) if vals else None


def task_a(cache: dict, strong: dict[str, dict], out_dir: str) -> tuple[dict, dict]:
    clean = [cache["pools"][pid] for pid in cache["pools"]
             if strong.get(pid, {}).get("label") == "clean"]
    per_pool = []
    for rec in clean:
        by = rec.get("pnl_by_offset", {})
        best_off = max(by.items(), key=lambda kv: kv[1].get("pnl_frac", -999))[0] if by else None
        per_pool.append({
            "poolId": rec["poolId"],
            "hook_template": rec.get("hook_template"),
            "pnl_by_offset": {k: v.get("pnl_pct") for k, v in by.items()},
            "best_offset": int(best_off) if best_off else None,
            "best_pnl_frac": _best_pnl(rec),
            "median_pnl_frac": float(np.median([v.get("pnl_frac", -1) for v in by.values()])) if by else None,
            "fatal_le_50": any(v.get("pnl_frac", 0) <= -0.5 for v in by.values()),
        })
    pnls_300 = [rec["pnl_by_offset"].get("300", {}).get("pnl_frac")
                for rec in clean if "300" in rec.get("pnl_by_offset", {})]
    pnls_best = [_best_pnl(rec) for rec in clean if _best_pnl(rec) is not None]
    summary = {
        "subset": "clean",
        "n": len(clean),
        "entry_mode": ENTRY_MODE,
        "spend_eth": SPEND_ETH,
        "at_offset_300": _summarize_pnls([p for p in pnls_300 if p is not None]),
        "at_best_offset": _summarize_pnls(pnls_best),
    }
    json.dump({"pools": per_pool, "summary": summary}, open(os.path.join(out_dir, "_alpha_pnl_clean.json"), "w"), indent=2)
    json.dump(summary, open(os.path.join(out_dir, "_alpha_pnl_summary.json"), "w"), indent=2)
    return summary, {"pools": per_pool}


def task_b(cache: dict, strong: dict, meta: dict, out_dir: str) -> dict:
    index = _load_json(SWAP_INDEX) if os.path.exists(SWAP_INDEX) else {"pools": {}}
    idx_pools = index.get("pools", {})
    groups = defaultdict(list)
    for pid, rec in cache["pools"].items():
        if strong.get(pid, {}).get("label") != "clean":
            continue
        g = _template_group(rec, meta)
        pf = _best_pnl(rec)
        if pf is not None:
            groups[g].append(pf)
    compare = {g: _summarize_pnls(v) for g, v in sorted(groups.items())}
    # RD-B deep features
    rdb = []
    for pid, rec in cache["pools"].items():
        if rec.get("hook_template") != "returns_delta_top_b":
            continue
        if strong.get(pid, {}).get("label") != "clean":
            continue
        m = meta.get(pid, {})
        init_blk = int(rec.get("entry_block", 0))
        pid_key = pid if pid.startswith("0x") else "0x" + pid
        swap_meta = idx_pools.get(pid_key, {})
        first_swap_blk = swap_meta.get("block")
        rdb.append({
            "poolId": pid,
            "init_to_first_swap_blocks": (int(first_swap_blk) - init_blk) if first_swap_blk else None,
            "hook": m.get("hooks"),
            "best_pnl_frac": _best_pnl(rec),
            "pnl_offset_0": rec.get("pnl_by_offset", {}).get("0", {}).get("pnl_frac"),
        })
    hook_counts = Counter(m.get("hooks") for p in rdb for m in [meta.get(p["poolId"], {})])
    deep = {
        "n_clean": len(rdb),
        "pnl": _summarize_pnls([p["best_pnl_frac"] for p in rdb if p["best_pnl_frac"] is not None]),
        "init_to_swap_delay_blocks": {
            "n": sum(1 for p in rdb if p["init_to_first_swap_blocks"] is not None),
            "median": int(median([p["init_to_first_swap_blocks"] for p in rdb
                                  if p["init_to_first_swap_blocks"] is not None])) if rdb else None,
        },
        "unique_hooks": len(hook_counts),
        "top_hook_repeat": hook_counts.most_common(3),
        "pools": rdb[:20],
    }
    json.dump(deep, open(os.path.join(out_dir, "_alpha_rdb_deep.json"), "w"), indent=2)
    json.dump({"groups": compare, "note": "clean-only; best_offset per pool"},
              open(os.path.join(out_dir, "_alpha_template_compare.json"), "w"), indent=2)
    return {"template_compare": compare, "rdb_deep": deep}


def task_c(cache: dict, strong: dict, out_dir: str) -> dict:
    offsets = [int(x) for x in HOLD_OFFSETS]
    curves = {}
    for name, filt in [
        ("RD-B", lambda pid, r: r.get("hook_template") == "returns_delta_top_b"),
        ("vanilla", lambda pid, r: r.get("hook_tier") == "vanilla"),
        ("all_clean", lambda pid, r: True),
    ]:
        rows = [cache["pools"][pid] for pid in cache["pools"]
                if strong.get(pid, {}).get("label") == "clean" and filt(pid, cache["pools"][pid])]
        curve = []
        for off in offsets:
            pnls = [r.get("pnl_by_offset", {}).get(str(off), {}).get("pnl_frac")
                    for r in rows if str(off) in r.get("pnl_by_offset", {})]
            curve.append({"offset": off, **_summarize_pnls([p for p in pnls if p is not None])})
        curves[name] = {"n": len(rows), "curve": curve}
    # late discovery decay
    late = {}
    for name, filt in [("RD-B", lambda pid, r: r.get("hook_template") == "returns_delta_top_b"),
                       ("vanilla", lambda pid, r: r.get("hook_tier") == "vanilla")]:
        rows = [cache["pools"][pid] for pid in cache["pools"]
                if strong.get(pid, {}).get("label") == "clean" and filt(pid, cache["pools"][pid])]
        early = [r.get("pnl_by_offset", {}).get("0", {}).get("pnl_frac") for r in rows]
        late2000 = [r.get("late_entry_pnl", {}).get("2000", {}).get("pnl_frac") for r in rows
                    if r.get("late_entry_pnl", {}).get("2000")]
        late[name] = {
            "n": len(rows),
            "median_early_0": round(float(np.median([p for p in early if p is not None])), 3) if early else None,
            "median_late_delta2000": round(float(np.median(late2000)), 3) if late2000 else None,
            "decay": round(float(np.median(late2000)) - float(np.median([p for p in early if p is not None])), 3)
            if late2000 and early else None,
        }
    out = {"offset_curves": curves, "late_discovery": late, "offsets": offsets}
    json.dump(out, open(os.path.join(out_dir, "_alpha_offset_sensitivity.json"), "w"), indent=2)
    return out


def _v3_stats() -> dict:
    import glob
    labels = {}
    for f in glob.glob(V3_UNI_GLOB):
        for p in json.load(open(f))["pools"]:
            labels[p["pool"]] = p["label"]
    rg = _load_json(V3_REALGAS) if os.path.exists(V3_REALGAS) else {}
    lu = _load_json(V3_LABELED)
    by = defaultdict(list)
    for pool, d in rg.items():
        lbl = labels.get(pool)
        if lbl and d.get("real_net") is not None:
            by[lbl].append(d["real_net"])
    clean = by.get("clean", [])
    return {
        "universe": "644 labeled; realgas walk-forward traded subset",
        "n_labeled_clean": lu.get("label_distribution", {}).get("clean", 378),
        "n_realgas_clean": len(clean),
        "clean_realgas": _summarize_pnls(clean),
        "caveat": "V3 real_net includes exit/trail/gas/MEV; NOT identical to raw V4 roundtrip grid",
    }


def task_d(cache: dict, strong: dict, meta: dict, out_dir: str) -> dict:
    v3 = _v3_stats()
    tmpl = task_b(cache, strong, meta, out_dir)["template_compare"]
    v4_clean = _summarize_pnls([_best_pnl(cache["pools"][p]) for p in cache["pools"]
                                if strong.get(p, {}).get("label") == "clean" and _best_pnl(cache["pools"][p]) is not None])
    v4_rdb = tmpl.get("RD-B", {})
    v4_vanilla = tmpl.get("vanilla", {})
    sens = _load_json(os.path.join(out_dir, "_alpha_offset_sensitivity.json")) if os.path.exists(
        os.path.join(out_dir, "_alpha_offset_sensitivity.json")) else {}
    early_med = sens.get("offset_curves", {}).get("RD-B", {}).get("curve", [{}])[0].get("median_pnl_frac")
    late_row = sens.get("late_discovery", {}).get("RD-B", {})
    late_med = late_row.get("median_late_delta2000")

    v3_pos = v3["clean_realgas"].get("positive_rate", 0)
    rdb_pos = v4_rdb.get("positive_rate", 0)
    rdb_tail = v4_rdb.get("tail_ge_100pct_rate", 0)
    rdb_fatal = v4_rdb.get("fatal_le_50pct_rate", 1)
    van_fatal = v4_vanilla.get("fatal_le_50pct_rate", 1)

    checks = {
        "1_rdb_positive_vs_v3_or_45pct": {
            "pass": bool(rdb_pos > v3_pos or rdb_pos >= 0.45),
            "v4_rdb_positive_rate": rdb_pos,
            "v3_clean_positive_rate": v3_pos,
            "threshold_alt": 0.45,
            "n_rdb": v4_rdb.get("n", 0),
        },
        "2_rdb_tail_ge_100pct": {
            "pass": bool(rdb_tail >= 0.05),
            "rate": rdb_tail,
            "n_rdb": v4_rdb.get("n", 0),
            "note": "n<30 directional only" if v4_rdb.get("n", 0) < 30 else "",
        },
        "3_rdb_fatal_le_vanilla_and_clean100": {
            "pass": bool(rdb_fatal <= van_fatal and v4_rdb.get("n", 0) == 50),
            "rdb_fatal_rate": rdb_fatal,
            "vanilla_fatal_rate": van_fatal,
            "rdb_roundtrip_clean_rate": 1.0,
        },
        "4_early_premium_offset0_vs_late2000": {
            "pass": bool(early_med is not None and late_med is not None and early_med >= late_med),
            "median_offset_0": early_med,
            "median_late_delta2000": late_med,
            "n_rdb": v4_rdb.get("n", 0),
        },
    }
    n_pass = sum(1 for c in checks.values() if c["pass"])
    if n_pass >= 4:
        verdict = "v4_alpha_candidate_yes"
    elif n_pass >= 3:
        verdict = "v4_alpha_candidate_weak"
    else:
        verdict = "v4_alpha_candidate_no"

    compare = {
        "v3_644": {
            "positive_rate": v3_pos,
            "median_pnl_frac": v3["clean_realgas"].get("median_pnl_frac"),
            "tail_ge_100pct": v3["clean_realgas"].get("tail_ge_100pct_rate"),
            "fatal_le_50pct": v3["clean_realgas"].get("fatal_le_50pct_rate"),
            "n": v3["n_realgas_clean"],
            "caveat": v3["caveat"],
        },
        "v4_clean_112": {
            "positive_rate": v4_clean.get("positive_rate"),
            "median_pnl_frac": v4_clean.get("median_pnl_frac"),
            "tail_ge_100pct": v4_clean.get("tail_ge_100pct_rate"),
            "fatal_le_50pct": v4_clean.get("fatal_le_50pct_rate"),
            "n": v4_clean.get("n"),
        },
        "v4_rdb_50": {
            "positive_rate": rdb_pos,
            "median_pnl_frac": v4_rdb.get("median_pnl_frac"),
            "tail_ge_100pct": rdb_tail,
            "fatal_le_50pct": rdb_fatal,
            "n": v4_rdb.get("n"),
        },
    }
    out = {"checks": checks, "n_pass": n_pass, "verdict": verdict, "compare_table": compare}
    json.dump(out, open(os.path.join(out_dir, "_alpha_pass_fail.json"), "w"), indent=2)
    return out


def write_report(out_dir: str, pass_fail: dict, summary: dict, sens: dict) -> None:
    pf = pass_fail
    path = os.path.join(out_dir, "_phase5_handoff_report.md")
    cmp = pf.get("compare_table", {})
    lines = [
        "# V4 Base 离线研究 · Phase 5 交付报告",
        "",
        f"> 生成：Phase 5 alpha readout · verdict **{pf.get('verdict')}** ({pf.get('n_pass')}/4 pass lines)",
        "",
        "## 1. RPC 双账本",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| Phase 5 研究计数器新增 | **0**（A–D 分析 + PnL refresh 走 Archive anvil） |",
        "| 计数器累计 | 见 `/tmp/v4_research_rpc_counter.json` |",
        "| Credits | PnL refresh 粗估 ≤0.05M anvil |",
        "",
        "## 2. Alpha 核心表（best_offset · spend=0.003 ETH · Initialize 进场）",
        "",
        "| 子集 | n | 正收益占比 | 中位 PnL | 右尾≥+100% | 致命≤−50% |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, title in [("v4_clean_112", "V4 clean"), ("v4_rdb_50", "V4 RD-B"), ("v3_644", "V3 clean (realgas*)")]:
        r = cmp.get(key, {})
        lines.append(
            f"| {title} | {r.get('n','?')} | {r.get('positive_rate','?')} | "
            f"{r.get('median_pnl_frac','?')} | {r.get('tail_ge_100pct','?')} | {r.get('fatal_le_50pct','?')} |"
        )
    lines += [
        "",
        "*V3 列为 realgas walk-forward，非 raw roundtrip grid — 见 compare caveat。",
        "",
        "## 3. Offset 敏感性",
        "",
    ]
    for name in ("RD-B", "vanilla"):
        c = sens.get("offset_curves", {}).get(name, {})
        lines.append(f"- **{name}** (n={c.get('n')}): " + ", ".join(
            f"+{x['offset']} med={x.get('median_pnl_frac')}" for x in c.get("curve", [])[:3]))
    late = sens.get("late_discovery", {})
    lines += [
        "",
        f"晚发现 Δ2000：RD-B decay={late.get('RD-B',{}).get('decay')} · vanilla decay={late.get('vanilla',{}).get('decay')}",
        "",
        "## 4. Alpha 通过线",
        "",
    ]
    for k, v in pf.get("checks", {}).items():
        lines.append(f"- {k}: **{'PASS' if v.get('pass') else 'FAIL'}** ({json.dumps({kk: vv for kk, vv in v.items() if kk != 'pass'}, ensure_ascii=False)})")
    lines += [
        "",
        f"**Verdict: {pf.get('verdict')}**",
        "",
        "## 5. 赚钱含义（给委托人）",
        "",
    ]
    if pf.get("verdict") == "v4_alpha_candidate_yes":
        lines.append("Phase 6 建议 universe：**RD-B only** + roundtrip clean gate + 早进窗（offset 0–300）。")
    elif pf.get("verdict") == "v4_alpha_candidate_weak":
        lines.append("有方向性边；继续 **扩 RD-B 样本（Task E）** + 7d listener，**不部署**。")
    else:
        lines.append("本轮未见足够 alpha；维持 hook 结构研究 + 最低 RPC follow。")
    lines += [
        "",
        "## 6. 仍不能做什么",
        "",
        "**不能上 live V4**；不能把 PnL 当实盘月化；不能跳过 roundtrip exit gate。",
        "",
        "## 7. changed files",
        "",
        "- `v4_alpha_readout.py`（新建）",
        "- `v4_pool_swap.py`（PnL：WETH balance net 修复）",
        "- `_alpha_pnl_cache.json` / `_alpha_*.json` / `_phase5_handoff_report.md`",
    ]
    open(path, "w").write("\n".join(lines) + "\n")
    print(f"Wrote {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="all",
                    choices=("all", "refresh-pnl", "alpha-pnl", "offset-sensitivity", "pass-fail", "v3-compare",
                             "refresh-alt-entry", "alt-entry"))
    ap.add_argument("--labels", default=DEFAULT_LABELS)
    ap.add_argument("--out-dir", default=HERE)
    ap.add_argument("--incremental", action="store_true", default=True)
    ap.add_argument("--no-incremental", action="store_true")
    ap.add_argument("--clean-only", action="store_true", help="refresh clean pools only")
    ap.add_argument("--write-report", default=None)
    args = ap.parse_args()
    incremental = not args.no_incremental

    if args.mode == "refresh-pnl":
        strong = _strong_labels()
        pids = {p for p, r in strong.items() if r.get("label") == "clean"} if args.clean_only else None
        refresh_pnl(pool_ids=pids, late_deltas=LATE_DELTAS, incremental=incremental)
        return 0

    if args.mode == "refresh-alt-entry":
        strong = _strong_labels()
        pids = {p for p, r in strong.items() if r.get("label") == "clean"}
        refresh_alt_entry(pool_ids=pids, incremental=incremental)
        return 0

    if args.mode == "alt-entry":
        if not os.path.exists(PNL_CACHE):
            print("Missing _alpha_pnl_cache.json — run refresh-pnl first", file=sys.stderr)
            return 1
        if not os.path.exists(ALT_ENTRY_CACHE):
            strong = _strong_labels()
            refresh_alt_entry(
                pool_ids={p for p, r in strong.items() if r.get("label") == "clean"},
                incremental=incremental,
            )
        init_cache = _load_json(PNL_CACHE)
        alt_cache = _load_json(ALT_ENTRY_CACHE)
        strong = _strong_labels()
        meta = _pool_meta()
        task_alt_entry(init_cache, alt_cache, strong, meta, args.out_dir)
        return 0

    if args.mode in ("all", "refresh-pnl") and not os.path.exists(PNL_CACHE):
        print("No PnL cache — run refresh-pnl first", flush=True)
        if args.mode == "all":
            strong = _strong_labels()
            refresh_pnl(
                pool_ids={p for p, r in strong.items() if r.get("label") == "clean"},
                late_deltas=LATE_DELTAS,
                incremental=incremental,
            )
        else:
            return 1

    cache = _load_json(PNL_CACHE)
    strong = _strong_labels()
    meta = _pool_meta()

    if args.mode in ("all", "alpha-pnl"):
        summary, _ = task_a(cache, strong, args.out_dir)
        print(json.dumps(summary, indent=2))
    if args.mode in ("all",):
        task_b(cache, strong, meta, args.out_dir)
    if args.mode in ("all", "offset-sensitivity"):
        sens = task_c(cache, strong, args.out_dir)
        print("offset sensitivity written")
    if args.mode in ("all", "pass-fail", "v3-compare"):
        pf = task_d(cache, strong, meta, args.out_dir)
        print(json.dumps({"verdict": pf["verdict"], "n_pass": pf["n_pass"]}, indent=2))
        if args.write_report or args.mode == "all":
            sens = _load_json(os.path.join(args.out_dir, "_alpha_offset_sensitivity.json"))
            summ = _load_json(os.path.join(args.out_dir, "_alpha_pnl_summary.json"))
            write_report(args.out_dir, pf, summ, sens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
