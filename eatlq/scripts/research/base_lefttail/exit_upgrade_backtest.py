#!/usr/bin/env python3
"""Exit-policy upgrade backtest (RESEARCH ONLY — no live / executor changes).

Compares candidate exit rules vs KEYSTONE baseline on holdout gate+model pools,
using pre-fetched QuoterV2 sell curves (_sellcurve.json) + forward tick paths
(_fwdpath.tsv). All rules are causal: only data up to the exit bucket is used.

Priority hypotheses (user-approved research agenda):
  P1  Activity death     — forward tick path stalls (swap silence proxy)
  P2  Quote deterioration — sellable net drops vs running peak (QuoterV2 path)
  P3  Trail variants     — wider trail vs hold-only (live-aligned)
  P4  Post-entry micro   — heat decay / post-peak momentum flip (path-only)

Run:
  python3 scripts/research/base_lefttail/exit_upgrade_backtest.py

Output:
  mvp/audits/keystone_exit_upgrade/summary.json
  mvp/audits/keystone_exit_upgrade/verdict_cn.md
"""
from __future__ import annotations
import json, math, os, sys
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

sys.path.insert(0, "/opt/EATLQ/scripts/research/base_lefttail")
from keystone_live import load, build, LN1P, HOLD_BLOCKS  # noqa: E402

FRIC_THR = -0.05
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402

PCACHE = "/opt/EATLQ/scripts/research/base_lefttail/_priced_live.json"
SELLCURVE = "/opt/EATLQ/scripts/research/base_lefttail/_sellcurve.json"
OUT_DIR = "/opt/EATLQ/mvp/audits/keystone_exit_upgrade"
HARD_STOP = math.log(0.5)
BKT_SEC = 600  # 10 min per forward bucket in _fwdpath.tsv


@dataclass(frozen=True)
class RuleSpec:
    name: str
    family: str
    desc: str


def curve_get(curve: dict, pool: str, block: int) -> Optional[float]:
    v = curve.get(f"{pool}:{block}")
    return None if v is None else float(v)


def sim_exit_baseline(steps, entry_lp, orient, entry_block, head, trail=None, hold_only=False):
    """Match harden_exit.py / keystone_live exit_block."""
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    peak = entry_lp
    for bkt, ltick, lblock in steps:
        lp = ltick * LN1P * orient
        peak = max(peak, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop"
        if (not hold_only) and trail is not None and (lp - peak) <= math.log(1 - trail):
            return lblock, "trail"
    return steps[-1][2], "hold_end"


def sim_exit_activity(steps, entry_lp, orient, entry_block, head,
                      silent_bkt: int = 3, gap_bkt: int = 3):
    """P1: exit if price tick unchanged for N consecutive buckets, or bucket index gap."""
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    peak = entry_lp
    prev_ltick, prev_bkt = None, None
    silent_run = 0
    for bkt, ltick, lblock in steps:
        lp = ltick * LN1P * orient
        peak = max(peak, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop"
        if prev_ltick is not None and ltick == prev_ltick:
            silent_run += 1
        else:
            silent_run = 0
        if prev_bkt is not None and (bkt - prev_bkt) >= gap_bkt:
            return lblock, "death_gap"
        if silent_run >= silent_bkt:
            return lblock, "death_silent"
        prev_ltick, prev_bkt = ltick, bkt
    return steps[-1][2], "hold_end"


def sim_exit_quote_peak(steps, pool, curve, entry_block, head,
                        drawdown_from_peak: float = 0.25, floor_net: float = -0.35):
    """P2: at each bucket block, if QuoterV2 sellable net falls from running peak, exit."""
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    peak_net = -999.0
    for bkt, ltick, lblock in steps:
        net = curve_get(curve, pool, lblock)
        if net is None or net <= -0.999:
            return lblock, "quote_fail"
        peak_net = max(peak_net, net)
        if net <= floor_net:
            return lblock, "quote_floor"
        if peak_net > -0.5 and (peak_net - net) >= drawdown_from_peak:
            return lblock, "quote_dd"
        if lblock >= min(entry_block + HOLD_BLOCKS, head):
            break
    return steps[-1][2], "hold_end"


def sim_exit_micro(steps, entry_lp, orient, entry_block, head,
                   decay_ratio: float = 0.35, flip_ret: float = -0.08):
    """P4: path-only post-entry microstructure (no buyer labels in fwdpath)."""
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    lps = [s[1] * LN1P * orient for s in steps]
    dlp = [lps[i] - lps[i - 1] for i in range(1, len(lps))]
    for i, (bkt, ltick, lblock) in enumerate(steps):
        if (lps[i] - entry_lp) <= HARD_STOP:
            return lblock, "stop"
        if i >= 3:
            early = np.mean(np.abs(dlp[:2]))
            late = np.mean(np.abs(dlp[i - 1:i + 1]))
            if early > 1e-9 and late / early < decay_ratio:
                return lblock, "heat_decay"
        if i >= 2:
            peak_i = int(np.argmax(lps[: i + 1]))
            if peak_i < i and dlp[i - 1] < flip_ret and dlp[i - 2] < flip_ret:
                return lblock, "mom_flip"
    return steps[-1][2], "hold_end"


def sim_exit_combo(steps, entry_lp, orient, entry_block, head, pool, curve):
    """Stack P1 + P2 + hold-only (live-aligned): first trigger wins."""
    if not steps:
        return min(entry_block + HOLD_BLOCKS, head), "silent"
    peak_lp = entry_lp
    peak_net = -999.0
    prev_ltick, prev_bkt = None, None
    silent_run = 0
    for bkt, ltick, lblock in steps:
        lp = ltick * LN1P * orient
        peak_lp = max(peak_lp, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop"
        net = curve_get(curve, pool, lblock)
        if net is not None:
            if net <= -0.999:
                return lblock, "quote_fail"
            peak_net = max(peak_net, net)
            if peak_net > -0.5 and (peak_net - net) >= 0.25:
                return lblock, "quote_dd"
        if prev_ltick is not None and ltick == prev_ltick:
            silent_run += 1
        else:
            silent_run = 0
        if prev_bkt is not None and (bkt - prev_bkt) >= 3:
            return lblock, "death_gap"
        if silent_run >= 3:
            return lblock, "death_silent"
        prev_ltick, prev_bkt = ltick, bkt
    return steps[-1][2], "hold_end"


def stat(name: str, rets: np.ndarray, reasons: list[str]) -> dict:
    if len(rets) == 0:
        return {"name": name, "n": 0}
    top1 = float(np.sort(rets)[-1] / rets[rets > 0].sum()) if (rets > 0).any() else 0.0
    from collections import Counter
    rc = Counter(reasons)
    return {
        "name": name,
        "n": int(len(rets)),
        "mean": float(rets.mean()),
        "med": float(np.median(rets)),
        "wr": float((rets > 0).mean()),
        "fatal": float((rets <= -0.5).mean()),
        "best": float(rets.max()),
        "top1share": top1,
        "exit_reasons": dict(rc.most_common(8)),
    }


def fmt(d: dict) -> str:
    if d.get("n", 0) == 0:
        return f"{d['name']:22s} n=0"
    return (f"{d['name']:22s} n={d['n']:3d} mean={d['mean']:+.3f} med={d['med']:+.3f} "
            f"WR={d['wr']:.2f} fatal={d['fatal']:.3f} best={d['best']:+.2f} top1={d['top1share']:.2f}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df, path = load()
    X, y = build(df)
    cut = df.t0.quantile(0.6)
    tr, te = df.t0 <= cut, df.t0 > cut
    clf = HistGradientBoostingClassifier(
        max_iter=300, max_depth=4, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=20, random_state=0,
    )
    clf.fit(X[tr], y[tr])
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    base = json.load(open(PCACHE))
    df["fric"] = df.pool.map(lambda x: base.get(x, {}).get("fric"))
    curve = json.load(open(SELLCURVE))
    head = max(int(x.split(":")[1]) for x in curve.keys() if ":" in x)

    gated = df[df.fric.notna() & (df.fric >= FRIC_THR)]
    p_cut = df[tr & df.fric.notna() & (df.fric >= FRIC_THR)].p.quantile(0.6)
    holdout = df[te & df.fric.notna() & (df.fric >= FRIC_THR) & (df.p <= p_cut)]

    print(f"#### EXIT UPGRADE BACKTEST (research only) ####")
    print(f"holdout gate+model n={len(holdout)} p_cut={p_cut:.3f} sellcurve blocks={len(curve)}")

    rules: list[tuple[RuleSpec, Callable]] = [
        (RuleSpec("baseline_trail35", "baseline", "Current research default"),
         lambda s, elp, o, eb, p: sim_exit_baseline(s, elp, o, eb, head, trail=0.35)),
        (RuleSpec("live_hold4h", "baseline", "Live executor: hold-only + hard stop"),
         lambda s, elp, o, eb, p: sim_exit_baseline(s, elp, o, eb, head, hold_only=True)),
        (RuleSpec("trail50", "P3", "Wider trail 50%"),
         lambda s, elp, o, eb, p: sim_exit_baseline(s, elp, o, eb, head, trail=0.50)),
        (RuleSpec("death_silent3", "P1", "3 buckets (~30m) unchanged tick"),
         lambda s, elp, o, eb, p: sim_exit_activity(s, elp, o, eb, head, silent_bkt=3)),
        (RuleSpec("death_gap3", "P1", "Bucket index gap >=3 (~30m path hole)"),
         lambda s, elp, o, eb, p: sim_exit_activity(s, elp, o, eb, head, silent_bkt=999, gap_bkt=3)),
        (RuleSpec("quote_dd25", "P2", "Sellable net -25pp from Quoter peak"),
         lambda s, elp, o, eb, p: sim_exit_quote_peak(s, p, curve, eb, head, 0.25, -0.35)),
        (RuleSpec("quote_dd15", "P2", "Tighter sellable net -15pp from peak"),
         lambda s, elp, o, eb, p: sim_exit_quote_peak(s, p, curve, eb, head, 0.15, -0.35)),
        (RuleSpec("micro_decay", "P4", "Heat decay: late bucket vol << early"),
         lambda s, elp, o, eb, p: sim_exit_micro(s, elp, o, eb, head)),
        (RuleSpec("combo_death+quote", "P1+P2", "Silent/gap OR quote dd + hold cap"),
         lambda s, elp, o, eb, p: sim_exit_combo(s, elp, o, eb, head, p, curve)),
    ]

    results = []
    for spec, fn in rules:
        rets, reasons = [], []
        for _, row in holdout.iterrows():
            steps = path.get(row.pool, [])
            elp = row.last_tick_pre * LN1P * row.orient
            eb = int(row.entry_block)
            xb, why = fn(steps, elp, row.orient, eb, row.pool)
            net = curve_get(curve, row.pool, xb)
            if net is None:
                continue
            rets.append(net)
            reasons.append(why)
        d = stat(spec.name, np.array(rets), reasons)
        d["family"] = spec.family
        d["desc"] = spec.desc
        results.append(d)
        print(fmt(d))

    baseline_row = next(r for r in results if r["name"] == "live_hold4h")
    baseline_med = baseline_row.get("med")
    baseline_mean = baseline_row.get("mean")
    print("\n--- vs live_hold4h ---")
    for d in results:
        if d.get("n", 0) and baseline_med is not None:
            print(f"  {d['name']:22s} med_delta={d['med']-baseline_med:+.3f}  mean_delta={d['mean']-baseline_mean:+.3f}")

    out = {"holdout_n": len(holdout), "p_cut": float(p_cut), "rules": results}
    json.dump(out, open(f"{OUT_DIR}/summary.json", "w"), indent=2)

    # Verdict markdown
    best = max((r for r in results if r.get("n", 0) > 0), key=lambda x: x["med"], default=None)
    base = next(r for r in results if r["name"] == "live_hold4h")
    lines = [
        "# KEYSTONE 出场升级 — 回测裁决（仅 research，未上 live）",
        "",
        f"Holdout gate+model n={len(holdout)} | 基准: `live_hold4h` med={base.get('med', 0):+.3f}",
        "",
        "## 结论摘要",
        "",
    ]
    if best and best["name"] != "live_hold4h" and best["med"] > base.get("med", -9):
        lines.append(f"- 中位数优于 live 基准的最佳规则: **{best['name']}** (med {best['med']:+.3f})")
    else:
        lines.append("- **尚无规则在中位数上稳定击败 `live_hold4h` 基准**；上 live 前需更多验证或 richer forward 特征。")
    lines += [
        "",
        "## 规则结果",
        "",
        "| 规则 | 族 | n | mean | med | WR | fatal |",
        "|------|----|---|------|-----|-----|-------|",
    ]
    for d in results:
        if not d.get("n"):
            continue
        lines.append(
            f"| {d['name']} | {d['family']} | {d['n']} | {d['mean']:+.1%} | {d['med']:+.1%} | "
            f"{d['wr']:.0%} | {d['fatal']:.0%} |"
        )
    lines += [
        "",
        "## 上 live 闸门（必须全部满足）",
        "",
        "1. 本脚本 + walk-forward 复验通过",
        "2. 代码 review（你 + 高级 review）",
        "3. 你书面同意",
        "4. Paper-forward ≥48h",
        "",
        "## 局限",
        "",
        "- P1/P4 使用 `_fwdpath` tick 代理活性，**无 per-bucket swap/buyer 计数**；完整版需扩展 ClickHouse SQL。",
        "- P2 依赖 `_sellcurve.json`（QuoterV2 真卖价），仅覆盖 gated 池 forward 块。",
        "- 未含 MEV / live 延迟。",
        "",
        f"复跑: `python3 scripts/research/base_lefttail/exit_upgrade_backtest.py`",
    ]
    open(f"{OUT_DIR}/verdict_cn.md", "w").write("\n".join(lines) + "\n")
    print(f"\nWrote {OUT_DIR}/summary.json and verdict_cn.md")


if __name__ == "__main__":
    main()
