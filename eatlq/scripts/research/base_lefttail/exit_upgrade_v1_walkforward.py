#!/usr/bin/env python3
"""Exit upgrade v1 — P1 (activity death) + P2 (friction spike) walk-forward.

RESEARCH ONLY. Uses cached:
  _fwdbuckets.tsv  — per-bucket ns/buyers/liq (from fetch_fwdbuckets_safe.py)
  _fwdpath.tsv     — tick path + blocks
  _sellcurve.json  — QuoterV2 sell net at forward blocks
  _priced_live.json — entry friction baseline

P2 semantics (fixed vs v0):
  - quote_fail: sell net <= -99%
  - fric_spike: execution_quality = (1+net_b)/exp(tick_ret) vs entry (1+fric_entry)
                exit only if quality drops > spike_pp AND tick not already crashing

Walk-forward: expanding window, same months as harden_walkforward.py.
"""
from __future__ import annotations
import json, math, os, sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import load, build, LN1P, HOLD_BLOCKS  # noqa: E402

FRIC_THR = -0.05
HARD_STOP = math.log(0.5)
PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
SELLCURVE = "/opt/EATLQ/scripts/research/base_lefttail/_sellcurve.json"
FWDBKT = "/opt/EATLQ/scripts/research/base_lefttail/_fwdbuckets.tsv"
OUT_DIR = "/opt/EATLQ/mvp/audits/keystone_exit_upgrade"
TEST_MONTHS = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


@dataclass(frozen=True)
class RuleSpec:
    name: str
    family: str


def curve_get(curve: dict, pool: str, block: int) -> Optional[float]:
    v = curve.get(f"{pool}:{block}")
    return None if v is None else float(v)


def load_fwdbuckets() -> dict[str, list[tuple]]:
    """pool -> [(bkt, ns, buyers, liq, ltick, lblock), ...] sorted by bkt."""
    if not os.path.exists(FWDBKT):
        return {}
    out: dict[str, list] = defaultdict(list)
    with open(FWDBKT) as f:
        for ln in f:
            p = ln.strip().split("\t")
            if len(p) < 7:
                continue
            pool, bkt, ns, buyers, liq, ltick, lblock = p[0], int(p[1]), int(p[2]), int(p[3]), float(p[4]), int(p[5]), int(p[6])
            out[pool].append((bkt, ns, buyers, liq, ltick, lblock))
    for pool in out:
        out[pool].sort(key=lambda x: x[0])
    return out


def sim_hold4h(steps, entry_lp, orient, entry_block, head):
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    for bkt, ltick, lblock in steps:
        lp = ltick * LN1P * orient
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop"
    return steps[-1][2], "hold_end"


def sim_death_v1(fwd_steps, path_steps, entry_lp, orient, entry_block, head,
                 silent_bkt: int = 3, liq_drop: float = 0.50, ns_window: int = 3):
    """P1: forward swap silence OR liquidity collapse vs entry bucket."""
    if not fwd_steps:
        return sim_hold4h(path_steps, entry_lp, orient, entry_block, head)
    if not path_steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    entry_liq = fwd_steps[0][3] if fwd_steps else None
    fwd_by_bkt = {s[0]: s for s in fwd_steps}
    silent, ns_roll = 0, 0
    prev_bkt = None
    for bkt, ltick, lblock in path_steps:
        lp = ltick * LN1P * orient
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop"
        fb = fwd_by_bkt.get(bkt)
        ns = fb[1] if fb else 0
        liq = fb[3] if fb else None
        ns_roll += ns
        if prev_bkt is not None and bkt > prev_bkt:
            # reset rolling window per bucket step on path
            pass
        if ns == 0:
            silent += 1
        else:
            silent = 0
        if silent >= silent_bkt:
            return lblock, "death_silent"
        if entry_liq and liq and liq < entry_liq * (1 - liq_drop):
            return lblock, "death_liq"
        # rolling ns over last ns_window buckets ending here
        bkts = [b for b in range(max(0, bkt - ns_window + 1), bkt + 1)]
        roll_ns = sum(fwd_by_bkt.get(b, (b, 0, 0, 0, 0, 0))[1] for b in bkts)
        if bkt >= ns_window - 1 and roll_ns == 0:
            return lblock, "death_nswap"
        prev_bkt = bkt
    return path_steps[-1][2], "hold_end"


def sim_fric_spike(path_steps, pool, curve, entry_lp, orient, entry_block, head,
                   fric_entry: float, spike_pp: float = 0.12, tick_floor: float = -0.15):
    """P2 v1: friction worsening beyond entry baseline, not raw PnL drawdown."""
    if not path_steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    entry_q = 1.0 + fric_entry
    for bkt, ltick, lblock in path_steps:
        lp = ltick * LN1P * orient
        tick_ret = lp - entry_lp
        if (tick_ret) <= HARD_STOP:
            return lblock, "stop"
        net = curve_get(curve, pool, lblock)
        if net is None or net <= -0.999:
            return lblock, "quote_fail"
        tick_f = math.exp(tick_ret)
        if tick_f <= 1e-9:
            continue
        exec_q = (1.0 + net) / tick_f
        # only fire when price hasn't already collapsed (avoid double-counting stop)
        if tick_ret > tick_floor and exec_q < entry_q * (1.0 - spike_pp):
            return lblock, "fric_spike"
    return path_steps[-1][2], "hold_end"


def sim_combo(path_steps, fwd_steps, pool, curve, entry_lp, orient, entry_block, head, fric_entry: float):
    xb, why = sim_death_v1(fwd_steps, path_steps, entry_lp, orient, entry_block, head)
    if why.startswith("death") or why == "stop":
        return xb, why
    xb2, why2 = sim_fric_spike(path_steps, pool, curve, entry_lp, orient, entry_block, head, fric_entry)
    return xb2, why2


def stat(name: str, rets: list, reasons: list) -> dict:
    r = np.array([x for x in rets if x is not None])
    if len(r) == 0:
        return {"name": name, "n": 0}
    top1 = float(np.sort(r)[-1] / r[r > 0].sum()) if (r > 0).any() else 0.0
    return {
        "name": name, "n": len(r),
        "mean": float(r.mean()), "med": float(np.median(r)),
        "wr": float((r > 0).mean()), "fatal": float((r <= -0.5).mean()),
        "best": float(r.max()), "top1share": top1,
        "exit_reasons": dict(Counter(reasons).most_common(6)),
    }


def fmt(d: dict) -> str:
    if not d.get("n"):
        return f"{d['name']:20s} n=0"
    return (f"{d['name']:20s} n={d['n']:3d} mean={d['mean']:+.3f} med={d['med']:+.3f} "
            f"WR={d['wr']:.2f} fatal={d['fatal']:.3f}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df, path = load()
    X, y = build(df)
    base = json.load(open(PCACHE))
    curve = json.load(open(SELLCURVE))
    fwdb = load_fwdbuckets()
    df = df.copy()
    df["fric"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    df["mon"] = df.t0.dt.strftime("%Y-%m")

    has_bkt = len(fwdb)
    print("#### EXIT UPGRADE v1 WALK-FORWARD (research only) ####")
    print(f"pools={len(df)} fwdbuckets_pools={has_bkt} sellcurve_keys={len(curve)}")
    if has_bkt == 0:
        print("WARN: _fwdbuckets.tsv missing — P1 rules will noop to hold4h; run fetch_fwdbuckets_safe.py")

    rules: list[tuple[RuleSpec, Callable]] = [
        (RuleSpec("live_hold4h", "baseline"),
         lambda row, ps, fs: sim_hold4h(ps, row.last_tick_pre * LN1P * row.orient, row.orient, int(row.entry_block), head)),
        (RuleSpec("death_v1", "P1"),
         lambda row, ps, fs: sim_death_v1(fs, ps, row.last_tick_pre * LN1P * row.orient, row.orient, int(row.entry_block), head)),
        (RuleSpec("fric_spike12", "P2"),
         lambda row, ps, fs: sim_fric_spike(ps, row.pool, curve, row.last_tick_pre * LN1P * row.orient, row.orient,
                                             int(row.entry_block), head, float(base[row.pool]["fric"]), 0.12)),
        (RuleSpec("fric_spike20", "P2"),
         lambda row, ps, fs: sim_fric_spike(ps, row.pool, curve, row.last_tick_pre * LN1P * row.orient, row.orient,
                                             int(row.entry_block), head, float(base[row.pool]["fric"]), 0.20)),
        (RuleSpec("combo_p1p2", "P1+P2"),
         lambda row, ps, fs: sim_combo(ps, fs, row.pool, curve, row.last_tick_pre * LN1P * row.orient, row.orient,
                                        int(row.entry_block), head, float(base[row.pool]["fric"]))),
    ]

    head = max(int(k.split(":")[1]) for k in curve if ":" in k)
    monthly: dict[str, list[dict]] = {r.name: [] for r, _ in rules}
    pooled: dict[str, list[float]] = {r.name: [] for r, _ in rules}

    for tm in TEST_MONTHS:
        trm = df.mon < tm
        if trm.sum() < 150:
            print(f"  {tm}: TRAIN too small"); continue
        clf = HistGradientBoostingClassifier(max_iter=300, max_depth=4, learning_rate=0.05,
                                             l2_regularization=1.0, min_samples_leaf=20, random_state=0)
        clf.fit(X[trm.values], y[trm.values])
        df.loc[:, "p"] = clf.predict_proba(X)[:, 1]
        p_cut = df[trm & df.fric.notna() & (df.fric >= FRIC_THR)].p.quantile(0.6)
        keep = df[(df.mon == tm) & df.fric.notna() & (df.fric >= FRIC_THR) & (df.p <= p_cut)]
        print(f"\n  TEST {tm} | train={trm.sum()} gate+model n={len(keep)} p_cut={p_cut:.3f}")
        for spec, fn in rules:
            rets, reasons = [], []
            for _, row in keep.iterrows():
                ps = path.get(row.pool, [])
                fs = fwdb.get(row.pool, [])
                elp = row.last_tick_pre * LN1P * row.orient
                try:
                    xb, why = fn(row, ps, fs)
                except Exception:
                    continue
                net = curve_get(curve, row.pool, xb)
                if net is None:
                    continue
                rets.append(net)
                reasons.append(why)
            d = stat(spec.name, rets, reasons)
            monthly[spec.name].append({"month": tm, **d})
            pooled[spec.name].extend(rets)
            print(f"    {fmt(d)}")

    print("\n--- POOLED walk-forward ---")
    base_med = stat("live_hold4h", pooled["live_hold4h"], []).get("med", 0)
    summary = {"test_months": TEST_MONTHS, "fwdbuckets_pools": has_bkt, "pooled": {}, "monthly": monthly}
    for spec, _ in rules:
        d = stat(spec.name, pooled[spec.name], [])
        summary["pooled"][spec.name] = d
        delta = d.get("med", 0) - base_med if d.get("n") else None
        print(f"  {fmt(d)}" + (f"  med_delta={delta:+.3f}" if delta is not None else ""))

    json.dump(summary, open(f"{OUT_DIR}/v1_walkforward.json", "w"), indent=2)

    lines = [
        "# KEYSTONE 出场 v1 — Walk-forward 报告（research only，未上 live）",
        "",
        f"Forward buckets 池数: {has_bkt} | 基准: live_hold4h",
        "",
        "## Pooled 结果",
        "",
        "| 规则 | n | mean | med | WR | fatal | vs hold4h med |",
        "|------|---|------|-----|-----|-------|---------------|",
    ]
    for spec, _ in rules:
        d = summary["pooled"].get(spec.name, {})
        if not d.get("n"):
            continue
        dm = d["med"] - base_med
        lines.append(f"| {spec.name} | {d['n']} | {d['mean']:+.1%} | {d['med']:+.1%} | "
                     f"{d['wr']:.0%} | {d['fatal']:.0%} | {dm:+.1%} |")
    lines += [
        "",
        "## VPS / ClickHouse 安全",
        "",
        "- 单池查询，max_memory_usage=1GiB，CH RSS>1.4GiB 或 live 重查询时自动等待",
        "- 仅读 `uniswap_v3_swaps_decoded`，不写表、不改 ingestor/archiver",
        "- 未动 executor / signal-bridge / `.env`",
        "",
        "## 上 live 闸门",
        "",
        "1. v1 walk-forward 复验 + 你认可参数",
        "2. 高级 code review",
        "3. 书面同意 + paper 48h",
    ]
    open(f"{OUT_DIR}/v1_walkforward_cn.md", "w").write("\n".join(lines) + "\n")
    print(f"\nWrote {OUT_DIR}/v1_walkforward.json and v1_walkforward_cn.md")


if __name__ == "__main__":
    main()
