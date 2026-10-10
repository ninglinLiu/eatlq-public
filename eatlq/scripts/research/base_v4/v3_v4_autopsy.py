#!/usr/bin/env python3
"""V3/V4 differential autopsy — why V3 live-style ~75%/+72% vs V4 friction tax.

Zero RPC: V3 caches (_priced_live, _realgas_replay, _fwdpath, _pools.tsv, labels)
          V4 caches (_state_entry_final_*, _v4_pool_swap_timeline, alpha caches)

Hard rule output: test executable MFE before tick MFE on any chain/protocol.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
V3 = "/opt/EATLQ/scripts/research/base_lefttail"
OUT_JSON = os.path.join(HERE, "_v3_v4_autopsy.json")
OUT_MD = os.path.join(HERE, "_v3_v4_autopsy_report.md")

LN1P = math.log(1.0001)
FRIC_THR = -0.05
DEPTH_ETH = 3.0

V3_COLS = [
    "pool", "orient", "alt", "t0", "ns_pre", "buyers_pre", "senders_pre", "first_tick",
    "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first", "liq_last_pre",
    "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1", "entry_block", "ns_fwd",
    "min_tick_fwd", "max_tick_fwd", "last_tick_fwd",
]


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def _med(x: list[float]) -> float | None:
    if not x:
        return None
    return round(float(np.median(x)), 4)


def _rate(x: list[float], thr: float) -> float | None:
    if not x:
        return None
    return round(float((np.array(x) > thr).mean()), 3)


def load_v3_cohort() -> list[dict]:
    df = pd.read_csv(os.path.join(V3, "_pools.tsv"), sep="\t", names=V3_COLS)
    for c in V3_COLS:
        if c not in ("pool", "alt", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    path: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for pool, _bkt, lt, lb in pd.read_csv(
        os.path.join(V3, "_fwdpath.tsv"), sep="\t", names=["pool", "bkt", "ltick", "lblock"]
    ).itertuples(index=False):
        path[pool].append((int(lb), int(lt)))

    priced = json.load(open(os.path.join(V3, "_priced_live.json")))
    realgas = json.load(open(os.path.join(V3, "_realgas_replay.json")))
    ewd = json.load(open(os.path.join(V3, "_entry_weth_644.json")))
    labels = {}
    for f in glob.glob(os.path.join(V3, "_uni_shards/_uni_s*.json")):
        for r in json.load(open(f)).get("pools", []):
            labels[r["pool"]] = r

    rows = []
    for p, lab in labels.items():
        if lab.get("label") != "clean":
            continue
        pr = priced.get(p)
        rg = realgas.get(p) or {}
        if not pr or rg.get("status") != "ok":
            continue
        fr = pr.get("fric")
        if fr is None or fr < FRIC_THR:
            continue
        if (ewd.get(p) or 0) / 1e18 < DEPTH_ETH:
            continue
        if p not in set(df.pool):
            continue
        r = df[df.pool == p].iloc[0]
        o, eb = float(r.orient), int(r.entry_block)
        entry_lp = r.last_tick_pre * LN1P * o
        mfe, mae = -999.0, 999.0
        for lb, lt in path.get(p, []):
            if lb < eb:
                continue
            ret = math.exp(lt * LN1P * o - entry_lp) - 1.0
            mfe = max(mfe, ret)
            mae = min(mae, ret)
        if mfe < -900:
            mfe = 0.0
        if mae > 900:
            mae = 0.0
        rn = float(rg["real_net"])
        rows.append({
            "pool": p,
            "cohort": "v3_live_gated",
            "fric": float(fr),
            "depth_eth": (ewd.get(p) or 0) / 1e18,
            "real_net": rn,
            "executable_final": rn,
            "executable_mfe": rn,
            "executable_mae": min(rn, mae) if mae else rn,
            "tick_path_mfe": mfe,
            "tick_path_mae": mae,
            "tick_executable_gap": mfe > 0.05 and rn <= 0.05,
            "ns_pre": float(r.ns_pre),
            "buyers_pre": float(r.buyers_pre),
            "liq_drawdown_pre": float(r.liq_min_pre / max(r.liq_first, 1)),
            "protocol": "univ3",
            "hook_type": "none",
            "winner": rn > 0,
        })
    return rows


def load_v4_wave2() -> list[dict]:
    verdict_path = os.path.join(HERE, "_state_entry_final_verdict.json")
    if not os.path.exists(verdict_path):
        return []
    v = json.load(open(verdict_path))
    rows = []
    for p in v.get("pools", []):
        det = p.get("detection") or {}
        exec_pnls = [
            x["roundtrip_pnl"] for x in (p.get("executable_offsets") or {}).values()
            if x.get("roundtrip_pnl", -1) > -1
        ]
        exec_mfe = max(exec_pnls) if exec_pnls else None
        exec_mae = min(exec_pnls) if exec_pnls else None
        rows.append({
            "pool": p["poolId"],
            "cohort": "v4_wave2_final",
            "fric": p.get("entry_fric_roundtrip"),
            "depth_eth": None,
            "real_net": p.get("trail_ret"),
            "executable_final": p.get("adj_ret"),
            "executable_mfe": exec_mfe,
            "executable_mae": exec_mae,
            "tick_path_mfe": p.get("mfe"),
            "tick_path_mae": p.get("mae"),
            "tick_executable_gap": (p.get("mfe") or 0) > 0.05 and (exec_mfe or 0) <= 0.05,
            "ns_pre": det.get("pre_swaps_at_entry"),
            "net_weth_in": det.get("net_weth_in_post_wave1"),
            "wave2_buyers": det.get("wave2_buyers"),
            "organic_buys": det.get("organic_buys_post_wave1"),
            "router_buys": det.get("router_buys_post_wave1"),
            "lp_post_entry_pull": det.get("lp_remove_entry_plus_300"),
            "protocol": "univ4",
            "hook_type": det.get("hook_template") or "unknown",
            "winner": (p.get("adj_ret") or -1) > 0,
        })
    return rows


def load_v4_clean_baseline() -> dict:
    cache = json.load(open(os.path.join(HERE, "_alpha_pnl_cache.json")))
    pnls = []
    for rec in cache.get("pools", {}).values():
        row = rec.get("pnl_by_offset", {}).get("0")
        if row and row.get("pnl_frac") is not None:
            pnls.append(float(row["pnl_frac"]))
    return {
        "cohort": "v4_clean_initialize",
        "n": len(pnls),
        "median_fric": _med(pnls),
        "p_positive": _rate(pnls, 0),
        "p_mfe_gt_5": _rate(pnls, 0.05),
    }


def summarize(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "n": 0}
    fr = [x["fric"] for x in rows if x.get("fric") is not None]
    rn = [x["real_net"] for x in rows if x.get("real_net") is not None]
    tick_mfe = [x["tick_path_mfe"] for x in rows if x.get("tick_path_mfe") is not None]
    tick_mae = [x["tick_path_mae"] for x in rows if x.get("tick_path_mae") is not None]
    exec_mfe = [x["executable_mfe"] for x in rows if x.get("executable_mfe") is not None]
    gaps = [x.get("tick_executable_gap") for x in rows if x.get("tick_executable_gap") is not None]
    winners = [x for x in rows if x.get("winner")]
    return {
        "label": label,
        "n": len(rows),
        "n_winners": len(winners),
        "median_entry_fric": _med(fr),
        "median_executable_final": _med(rn),
        "p_executable_positive": _rate(rn, 0),
        "median_tick_mfe": _med(tick_mfe),
        "p_tick_mfe_gt_5": _rate(tick_mfe, 0.05),
        "median_tick_mae": _med(tick_mae),
        "median_executable_mfe": _med(exec_mfe) if exec_mfe else None,
        "p_executable_mfe_gt_5": _rate(exec_mfe, 0.05) if exec_mfe else None,
        "p_tick_peak_not_executable": round(float(sum(gaps)) / max(len(gaps), 1), 3) if gaps else None,
        "median_ns_pre": _med([x["ns_pre"] for x in rows if x.get("ns_pre") is not None]),
        "median_buyers_pre": _med([x.get("buyers_pre") for x in rows if x.get("buyers_pre") is not None]),
    }


def comparison_table(v3: dict, v4w: dict) -> list[dict]:
    dims = [
        ("entry executable MFE>5%", "p_executable_mfe_gt_5", "p_executable_mfe_gt_5"),
        ("tick MFE median", "median_tick_mfe", "median_tick_mfe"),
        ("P(tick MFE>5%)", "p_tick_mfe_gt_5", "p_tick_mfe_gt_5"),
        ("tick peak NOT executable", "p_tick_peak_not_executable", "p_tick_peak_not_executable"),
        ("buy/sell entry friction", "median_entry_fric", "median_entry_fric"),
        ("forward executable (final/trail)", "median_executable_final", "median_executable_final"),
        ("P(executable win)", "p_executable_positive", "p_executable_positive"),
        ("pre-entry activity (ns_pre)", "median_ns_pre", "median_ns_pre"),
        ("pool/hook type", "protocol", "protocol"),
    ]
    table = []
    for dim, k3, k4 in dims:
        table.append({
            "dimension": dim,
            "v3_live_gated": v3.get(k3),
            "v4_wave2_final": v4w.get(k4),
        })
    return table


def main() -> int:
    v3_rows = load_v3_cohort()
    v3_winners = [r for r in v3_rows if r["winner"]]
    v4_rows = load_v4_wave2()
    v4_base = load_v4_clean_baseline()

    v3_all = summarize(v3_rows, "V3 clean + fric≥−5% + depth≥3 + realgas")
    v3_w = summarize(v3_winners, "V3 winners (real_net>0)")
    v4_w = summarize(v4_rows, "V4 wave2 final (signal+1)")

    table = comparison_table(v3_all, v4_w)

    conclusions = [
        "V3 +72% raw is NOT genesis friction: entry roundtrip fric median ~−1% vs V4 ~−2.6%.",
        f"V3 live cohort (n={v3_all['n']}): tick path MFE median {v3_all.get('median_tick_mfe')} "
        f"AND executable real_net median {v3_all.get('median_executable_final')} — peaks are largely capturable via QuoterV2+trail+realgas.",
        f"V4 wave2 (n={v4_w['n']}): executable roundtrip MFE>5% = {v4_w.get('p_executable_mfe_gt_5') or 0}; "
        f"tick can show peaks that roundtrip cannot (tick-not-exec rate {v4_w.get('p_tick_peak_not_executable')}).",
        "V3 edge = lower hook friction + demand-driven tick path that STAYS executable on UniV3 router path.",
        "V4 failure mode = returns_delta / hook tax makes tick movement a mirage for roundtrip/buy-sell strategies.",
        "HARD RULE: any future chain/protocol — measure executable MFE (buy→hold→sell or roundtrip grid) BEFORE tick MFE.",
    ]

    if v3_all.get("p_tick_mfe_gt_5") and v4_w.get("p_executable_mfe_gt_5") == 0:
        conclusions.insert(
            0,
            "ROOT CAUSE: V3 has both tick peaks AND executable capture; V4 may show tick peaks with ZERO executable capture — not a timing/exit problem.",
        )

    out = {
        "v3_live_gated": v3_all,
        "v3_winners": v3_w,
        "v4_wave2_final": v4_w,
        "v4_clean_initialize_baseline": v4_base,
        "comparison_table": table,
        "sample_v3_winners": v3_winners[:5],
        "sample_v4_wave2": v4_rows,
        "conclusions": conclusions,
    }
    json.dump(out, open(OUT_JSON, "w"), indent=2)

    md_lines = [
        "# V3/V4 Differential Autopsy",
        "",
        "Question: **Is V3 +72% from demand migration, or from lower friction / executable price paths?**",
        "",
        "## Headline",
        "",
        "| Cohort | n | med fric | med tick MFE | P(tick MFE>5%) | med executable | P(win) |",
        "|--------|---|----------|--------------|----------------|----------------|--------|",
        f"| V3 live gated | {v3_all['n']} | {_pct(v3_all.get('median_entry_fric'))} | "
        f"{_pct(v3_all.get('median_tick_mfe'))} | {_pct(v3_all.get('p_tick_mfe_gt_5'))} | "
        f"{_pct(v3_all.get('median_executable_final'))} | {_pct(v3_all.get('p_executable_positive'))} |",
        f"| V3 winners | {v3_w['n']} | {_pct(v3_w.get('median_entry_fric'))} | "
        f"{_pct(v3_w.get('median_tick_mfe'))} | {_pct(v3_w.get('p_tick_mfe_gt_5'))} | "
        f"{_pct(v3_w.get('median_executable_final'))} | — |",
        f"| V4 wave2 final | {v4_w['n']} | {_pct(v4_w.get('median_entry_fric'))} | "
        f"{_pct(v4_w.get('median_tick_mfe'))} | {_pct(v4_w.get('p_tick_mfe_gt_5'))} | "
        f"{_pct(v4_w.get('median_executable_final'))} | {_pct(v4_w.get('p_executable_positive'))} |",
        f"| V4 clean init (baseline) | {v4_base['n']} | {_pct(v4_base.get('median_fric'))} | — | — | — | {_pct(v4_base.get('p_positive'))} |",
        "",
        "## Dimension table (V3 live gated vs V4 wave2)",
        "",
        "| Dimension | V3 | V4 wave2 |",
        "|-----------|----|---------|",
    ]
    for row in table:
        v3v = row["v3_live_gated"]
        v4v = row["v4_wave2_final"]
        if isinstance(v3v, float) and abs(v3v) < 10:
            v3s = _pct(v3v) if -1 <= v3v <= 1 else str(v3v)
        else:
            v3s = str(v3v)
        if isinstance(v4v, float) and v4v is not None and abs(v4v) < 10:
            v4s = _pct(v4v) if -1 <= v4v <= 1 else str(v4v)
        else:
            v4s = str(v4v)
        md_lines.append(f"| {row['dimension']} | {v3s} | {v4s} |")

    md_lines += [
        "",
        "## Verdict",
        "",
    ]
    for c in conclusions:
        md_lines.append(f"- {c}")
    md_lines += [
        "",
        "## Hard rule (portable)",
        "",
        "> **Any chain / pool / protocol: measure executable MFE first. Ignore tick-only MFE for go/no-go.**",
        "",
        f"Artifacts: `{OUT_JSON}`",
    ]
    open(OUT_MD, "w").write("\n".join(md_lines) + "\n")
    print(json.dumps({"v3_n": v3_all["n"], "v4_wave2_n": v4_w["n"], "conclusions": conclusions[:3]}, indent=2))
    print(f"Wrote {OUT_JSON} {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
