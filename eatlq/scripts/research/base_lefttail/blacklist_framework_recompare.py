#!/usr/bin/env python3
"""Re-compare blacklist frameworks vs live outcomes (2026-06-29 refresh).

Scenarios (644 labeled universe, $500 wallet, $50 stake, max 3 concurrent):
  A) conservative_F_on      — 245 deployer + 33 safe skeleton, TRAIL 0.35
  B) aggressive_F_off       — F off, 2 PARADAENGI deployers only, TRAIL 0.25 (live until 6/25)
  C) post_fix_20260629      — 33 skeleton + 245 deployer + PARADAENGI skel + 3 deployers, TRAIL 0.25
  D) skeleton_paradaengi    — model+depth+fric + block ONLY PARADAENGI skeleton (rename defense)
  E) deployer_3_para_only   — model+depth+fric + block 3 known PARADAENGI deployers only
  F) skeleton_safe_33       — model+depth+fric + 33 FP-validated skeletons only

Also quantifies「换名不换药」(same skeleton, different token address) FP/rug tradeoff.

RPC-free — reuses aggressive_live_rebacktest caches.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aggressive_live_rebacktest as A  # noqa
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/blacklist_framework_recompare_20260629"
PARADAENGI_SKELETON = "0xa7df50c84afb0036133e169f94babbda439bc8ef4af178deff5a00cf470c3f25"
PARADAENGI_DEPLOYERS_3 = {
    "0x715c98cedbd0c242e257986535bbf6e1fba764be",
    "0xc8512b5324402be9ab986d011c9adef8992f076f",
    "0x1c80fcf62dab78142c2918f7d9819d1aa40661f9",
}


def _base_model_depth(df: pd.DataFrame) -> pd.Series:
    return (df.ns_pre >= A.MIN_PRE) & (df.p <= A.P_CUT) & (df.entry_weth >= A.MIN_DEPTH_ETH) & df.gate_fric


def gate_scenarios(df, creators, token_skel, dep_full, skel_full):
    base = _base_model_depth(df)
    dep_hit = df.token.map(lambda t: (creators.get(t) or {}).get("creator", "").lower() in dep_full)
    skel_hit = df.token.map(lambda t: token_skel.get(t) in skel_full)
    para_skel = df.token.map(lambda t: token_skel.get(t) == PARADAENGI_SKELETON)
    para_dep = df.token.map(
        lambda t: (creators.get(t) or {}).get("creator", "").lower() in PARADAENGI_DEPLOYERS_3
    )
    para_dep2 = df.token.map(
        lambda t: (creators.get(t) or {}).get("creator", "").lower() in L.PARADAENGI_DEPLOYERS
    )
    return {
        "A_conservative_F_on_TRAIL035": base & ~(dep_hit | skel_hit),
        "B_aggressive_F_off_TRAIL025": base & ~para_dep2,
        "C_post_fix_20260629_TRAIL025": base & ~(dep_hit | skel_hit | para_skel),
        "D_skeleton_paradaengi_only": base & ~para_skel,
        "E_deployer_3_para_only": base & ~para_dep,
        "F_skeleton_safe_33_only": base & ~skel_hit,
    }


def _fix_G_mask(df, creators, token_skel, dep_full, skel_full):
    """F full blacklist EXCEPT do not block PARADAENGI skeleton (6/24 live compromise)."""
    base = _base_model_depth(df)
    dep_hit = df.token.map(lambda t: (creators.get(t) or {}).get("creator", "").lower() in dep_full)
    skel_other = df.token.map(
        lambda t: (sk := token_skel.get(t)) in skel_full and sk != PARADAENGI_SKELETON
    )
    return base & ~(dep_hit | skel_other)


def skeleton_overlap_analysis(df, token_skel):
    """Rename attack: same skeleton clusters in labeled set."""
    by_skel: dict[str, list] = defaultdict(list)
    for r in df.itertuples(index=False):
        sk = token_skel.get(r.token)
        if sk:
            by_skel[sk].append(r)
    rows = []
    for sk, items in sorted(by_skel.items(), key=lambda x: -len(x[1])):
        labs = Counter(x.label for x in items)
        mech = Counter(getattr(x, "mechanism", None) for x in items)
        clean = labs.get("clean", 0)
        dr = labs.get("delayed_rug", 0)
        rows.append({
            "skeleton": sk,
            "n_tokens": len(items),
            "clean_n": clean,
            "delayed_rug_n": dr,
            "is_paradaengi": sk == PARADAENGI_SKELETON,
            "mechanisms": dict(mech),
        })
    para = next((x for x in rows if x["is_paradaengi"]), None)
    overlap_rug_clean = [x for x in rows if x["clean_n"] and x["delayed_rug_n"]]
    return {
        "unique_skeletons": len(rows),
        "skeletons_with_both_rug_and_clean": len(overlap_rug_clean),
        "paradaengi_cluster": para,
        "top_overlap": sorted(overlap_rug_clean, key=lambda x: x["clean_n"], reverse=True)[:10],
    }


def scenario_metrics(df, path, priced, mask, trail, tag):
    tr = A.build_trades(df, path, priced, mask, trail=trail)
    sim = A.portfolio_sim(tr)
    pool = A.pool_metrics(tr)
    base_pass = int(mask.sum())
    dr_pass = int((mask & (df.label == "delayed_rug")).sum())
    clean_pass = int((mask & (df.label == "clean")).sum())
    clean_blocked = int((~mask & (df.label == "clean") & _base_model_depth(df)).sum())
    dr_blocked = int((~mask & (df.label == "delayed_rug") & _base_model_depth(df)).sum())
    # PnL of clean winners blocked by blacklist (opportunity cost)
    blk = ~mask & (df.label == "clean") & _base_model_depth(df) & df.tradeable
    miskill_pnl = float((df[blk].ret * A.STAKE).sum()) if blk.any() else 0.0
    return {
        "tag": tag,
        "trail": trail,
        "gate_pass_n": base_pass,
        "delayed_rug_pass": dr_pass,
        "clean_pass": clean_pass,
        "delayed_rug_blocked": dr_blocked,
        "clean_blocked_on_model_depth": clean_blocked,
        "miskill_winner_pnl_usd": miskill_pnl,
        "portfolio": sim,
        "pool_level": pool,
    }


def live_pnl_snapshot():
    import subprocess
    keys = subprocess.check_output(
        ["docker", "exec", "eatlq-redis-1", "redis-cli", "--scan", "--pattern", "positions:realized_pnl:2026-06*"],
        text=True,
    ).strip().split("\n")
    rows = {}
    for k in keys:
        if not k:
            continue
        v = subprocess.check_output(["docker", "exec", "eatlq-redis-1", "redis-cli", "GET", k], text=True).strip()
        if v:
            rows[k.split(":")[-1]] = int(v) / 1e18
    return rows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    _, path = K.load()
    priced = json.load(open(K.PCACHE))
    df = L.run_lens(L.load_base_df(), "qv2")
    dep_full, skel_full, creators, token_skel = L._load_blacklists()
    # ensure paradaengi skeleton in skel set for scenario C
    skel_with_para = set(skel_full) | {PARADAENGI_SKELETON}

    masks = gate_scenarios(df, creators, token_skel, dep_full, skel_with_para)
    masks["G_F_full_minus_para_skel"] = _fix_G_mask(df, creators, token_skel, dep_full, skel_with_para)

    trails = {
        "A_conservative_F_on_TRAIL035": 0.35,
        "B_aggressive_F_off_TRAIL025": 0.25,
        "C_post_fix_20260629_TRAIL025": 0.25,
        "D_skeleton_paradaengi_only": 0.25,
        "E_deployer_3_para_only": 0.25,
        "F_skeleton_safe_33_only": 0.25,
        "G_F_full_minus_para_skel": 0.25,
    }

    scenarios = []
    for tag, mask in masks.items():
        scenarios.append(scenario_metrics(df, path, priced, mask, trails[tag], tag))

    agg = next(s for s in scenarios if s["tag"].startswith("B_"))
    con = next(s for s in scenarios if s["tag"].startswith("A_"))
    fix = next(s for s in scenarios if s["tag"].startswith("C_"))
    gcomp = next(s for s in scenarios if s["tag"].startswith("G_"))

    def _mo(s):
        return s["portfolio"].get("monthly_usd", 0.0)

    def _ratio(a, b):
        if not b:
            return None
        return (a - b) / abs(b)

    comparison = {
        "aggressive_vs_conservative": {
            "aggressive_monthly_usd": _mo(agg),
            "conservative_monthly_usd": _mo(con),
            "delta_usd": _mo(agg) - _mo(con),
            "aggressive_pct_of_conservative": _mo(agg) / _mo(con) if _mo(con) else None,
            "profit_given_up_pct_if_conservative": 1 - (_mo(con) / _mo(agg)) if _mo(agg) else None,
            "aggressive_trades": agg["portfolio"].get("n"),
            "conservative_trades": con["portfolio"].get("n"),
            "aggressive_delayed_rug_trades": agg["portfolio"].get("delayed_rug_n"),
            "conservative_delayed_rug_trades": con["portfolio"].get("delayed_rug_n"),
            "aggressive_fatal_n": agg["portfolio"].get("fatal_n"),
            "conservative_fatal_n": con["portfolio"].get("fatal_n"),
        },
        "post_fix_vs_aggressive": {
            "post_fix_monthly_usd": _mo(fix),
            "aggressive_monthly_usd": _mo(agg),
            "delta_usd": _mo(fix) - _mo(agg),
            "profit_kept_pct": _mo(fix) / _mo(agg) if _mo(agg) else None,
            "profit_given_up_pct": 1 - (_mo(fix) / _mo(agg)) if _mo(agg) else None,
        },
        "compromise_G_vs_aggressive": {
            "G_monthly_usd": _mo(gcomp),
            "aggressive_monthly_usd": _mo(agg),
            "delta_usd": _mo(gcomp) - _mo(agg),
            "profit_kept_pct": _mo(gcomp) / _mo(agg) if _mo(agg) else None,
            "note": "G = 6/24 live compromise: F full but PARADAENGI skeleton NOT seeded",
        },
    }

    skel_analysis = skeleton_overlap_analysis(df, token_skel)

    # tokens hit by paradaengi skeleton in labeled set
    para_tokens = df[df.token.map(lambda t: token_skel.get(t) == PARADAENGI_SKELETON)]
    para_detail = []
    for r in para_tokens.itertuples(index=False):
        para_detail.append({
            "pool": r.pool,
            "token": r.token,
            "label": r.label,
            "mechanism": getattr(r, "mechanism", None),
            "ret_if_traded": float(r.ret) if r.ret == r.ret else None,
            "entry_weth": float(r.entry_weth),
        })

    live = {}
    try:
        live = live_pnl_snapshot()
    except Exception as e:
        live = {"error": str(e)}

    report = {
        "meta": {
            "ts": pd.Timestamp.now("UTC").isoformat(),
            "universe_n": len(df),
            "wallet_usd": A.WALLET,
            "stake_usd": A.STAKE,
            "max_concurrent": A.MAX_CONC,
            "note": "Symbol blacklist not in 644 labels (no ticker field); PARADAENGI skeleton proxy for rename-same-code",
        },
        "headline_comparison": comparison,
        "scenarios": scenarios,
        "skeleton_rename_analysis": skel_analysis,
        "paradaengi_skeleton_tokens_in_universe": para_detail,
        "live_realized_pnl_eth_june2026": live,
        "live_incidents": {
            "paradaengi_20260623_eth": -0.114,
            "paradaengi_20260629_eth": -0.0302,
            "total_paradaengi_eth": -0.1442,
            "note": "Live losses NOT in backtest (PARADAENGI not in 644 labeled set)",
        },
    }

    out = os.path.join(OUT_DIR, "blacklist_framework_recompare.json")
    json.dump(report, open(out, "w"), indent=2, default=str)

    print("=== BLACKLIST FRAMEWORK RECOMPARE (2026-06-29) ===\n")
    hc = comparison["aggressive_vs_conservative"]
    print(f"Aggressive vs Conservative:")
    print(f"  monthly ${hc['aggressive_monthly_usd']:.0f} vs ${hc['conservative_monthly_usd']:.0f}  "
          f"(aggressive keeps {100*(1-(hc['profit_given_up_pct_if_conservative'] or 0)):.0f}% of conservative upside inverted)")
    print(f"  trades {hc['aggressive_trades']} vs {hc['conservative_trades']}  "
          f"rug_trades {hc['aggressive_delayed_rug_trades']} vs {hc['conservative_delayed_rug_trades']}")
    print(f"  fatals {hc['aggressive_fatal_n']} vs {hc['conservative_fatal_n']}\n")

    pf = comparison["post_fix_vs_aggressive"]
    print(f"Post-fix (6/29) vs Aggressive:")
    print(f"  monthly ${pf['post_fix_monthly_usd']:.0f} vs ${pf['aggressive_monthly_usd']:.0f}  "
          f"keeps {100*(pf['profit_kept_pct'] or 0):.1f}% of aggressive PnL\n")

    print("All scenarios:")
    for s in scenarios:
        p = s["portfolio"]
        print(f"  {s['tag'][:28]:28s} n={p.get('n',0):3d}  ${p.get('monthly_usd',0):7.0f}/mo  "
              f"fatal={p.get('fatal_n',0)}  dr_pass={s['delayed_rug_pass']:3d}  "
              f"clean_blk={s['clean_blocked_on_model_depth']:3d}  "
              f"miskill=${s['miskill_winner_pnl_usd']:.0f}")

    pc = skel_analysis.get("paradaengi_cluster")
    print(f"\nPARADAENGI skeleton in 644: {pc}")
    print(f"Live PARADAENGI losses: {report['live_incidents']['total_paradaengi_eth']:.4f} ETH")
    print(f"June live realized: {live}")
    print(f"\nwrote {out}")
    return report


if __name__ == "__main__":
    main()
