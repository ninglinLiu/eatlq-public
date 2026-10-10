#!/usr/bin/env python3
"""Re-backtest 644 labeled universe under CURRENT live config + size 0.0302 ETH.

Two return lenses (same gate stack, different PnL ground truth):
  A) QuoterV2 — `_priced_live.json` net + friction gate (eth_call ruler)
  B) Fork real-gas — `_realgas_replay.json` real_net (fork buy/sell + gas)

Gates replayed offline:
  MIN_PRE>=5, p_fatal<=0.27, friction>=-5%, depth>=3 ETH,
  F deployer blacklist (245), F skeleton blacklist (33).

Not replayed (live-only): sell-sim, external-risk, E-window 1800s, MEV.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import risk_rebacktest as R  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/live_config_rebacktest_20260624"

# ── current live config ──
MIN_PRE = 5
P_CUT = 0.27
MIN_DEPTH_ETH = 3.0
FRIC_THR = R.FRIC_THR  # -0.05
SIZE_ETH = 0.0302
ETH_USD = 1658.75  # go-live cross-check price
SIZE_USD = SIZE_ETH * ETH_USD
GAS_USD_RT = R.GAS_USD_RT  # 0.15 per round-trip
HAIRCUT = 0.02
START, CAP, FRAC = 10000.0, SIZE_USD, 0.10

PARADAENGI_DEPLOYERS = {
    "0x715c98cedbd0c242e257986535bbf6e1fba764be",
    "0xc8512b5324402be9ab986d011c9adef8992f076f",
}


def _load_labels() -> dict:
    labels = {}
    for f in glob.glob(os.path.join(HERE, "_uni_shards/_uni_s*.json")):
        for r in json.load(open(f)).get("pools", []):
            labels[r["pool"]] = r
    return labels


def _load_blacklists() -> tuple[set, set, dict]:
    deployers = set(json.load(open(
        "/opt/EATLQ/services/executor/fixtures/safe_rug_deployers.json"))["deployers"])
    deployers |= PARADAENGI_DEPLOYERS
    skeletons = set(json.load(open(os.path.join(HERE, "_rug_seed_validated.json")))["safe_skeletons"])
    creators = json.load(open(
        "/opt/EATLQ/mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json"
    )).get("token_creators", {})
    token_skel = json.load(open(os.path.join(HERE, "_token_skeleton_644.json")))
    return deployers, skeletons, creators, token_skel


def load_base_df() -> pd.DataFrame:
    df, _ = K.load()
    clf, X, tr, te = R.fit_model(df)
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    _, y = K.build(df)
    df["y_fatal"] = y
    labels = _load_labels()
    ewd = json.load(open(os.path.join(HERE, "_entry_weth_644.json")))
    df = df[df.pool.isin(labels)].copy()
    df["label"] = df.pool.map(lambda p: labels[p]["label"])
    df["mechanism"] = df.pool.map(lambda p: labels[p].get("mechanism"))
    df["flip_offset"] = df.pool.map(lambda p: labels[p].get("flip_offset"))
    df["token"] = df.pool.map(lambda p: labels[p]["token"].lower())
    df["entry_weth"] = df.pool.map(lambda p: (ewd.get(p) or 0) / 1e18)
    return df


def adj_ret(raw_net: float) -> float:
    if raw_net is None:
        return None
    return float(raw_net) - GAS_USD_RT / SIZE_USD - HAIRCUT


def simulate_monthly(sub: pd.DataFrame) -> tuple[float, float]:
    sub = sub.sort_values("t0")
    eq, peak, mdd = START, START, 0.0
    for r in sub.itertuples(index=False):
        stake = min(SIZE_USD, CAP, eq * FRAC)
        eq += stake * r.ret
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1.0)
    if len(sub) < 2:
        return 0.0, 0.0
    span = max((sub.t0.max() - sub.t0.min()).days, 1)
    monthly = (eq / START) ** (30.0 / span) - 1.0 if eq > 0 else -1.0
    return monthly, mdd


def metrics(df: pd.DataFrame, traded_mask, size_usd: float) -> dict:
    base_cw = (df.label == "clean") & df.tradeable & (df.ret > 0)
    dr = df.label == "delayed_rug"
    fast = dr & (df.flip_offset == 30)
    tr = traded_mask & df.tradeable
    t = df[tr]
    blocked_cw = base_cw & ~traded_mask
    monthly, mdd = simulate_monthly(t)
    fatal = t.ret <= -0.5 if len(t) else pd.Series(dtype=bool)
    return {
        "pass_n": int(tr.sum()),
        "dr_total": int(dr.sum()),
        "dr_traded": int((tr & dr).sum()),
        "dr_blocked": int((dr & ~traded_mask).sum()),
        "dr_recall": int((dr & ~traded_mask).sum()) / max(int(dr.sum()), 1),
        "fast_total": int(fast.sum()),
        "fast_traded": int((tr & fast).sum()),
        "fast_blocked": int((fast & ~traded_mask).sum()),
        "miskill_winners": int(blocked_cw.sum()),
        "miskill_pnl_usd": float((df[blocked_cw].ret * size_usd).sum()) if blocked_cw.any() else 0.0,
        "monthly": monthly,
        "mdd": mdd,
        "fatal_rate": float(fatal.mean()) if len(t) else 0.0,
        "fatal_n": int(fatal.sum()) if len(t) else 0,
        "mean_ret": float(t.ret.mean()) if len(t) else 0.0,
        "tradeable_n": int(df.tradeable.sum()),
    }


def gate_masks(df: pd.DataFrame, deployers: set, skeletons: set,
               creators: dict, token_skel: dict) -> dict[str, pd.Series]:
    model = (df.ns_pre >= MIN_PRE) & (df.p <= P_CUT)
    depth = df.entry_weth >= MIN_DEPTH_ETH
    dep_hit = df.token.map(lambda t: (creators.get(t) or {}).get("creator", "").lower() in deployers)
    skel_hit = df.token.map(lambda t: token_skel.get(t) in skeletons)
    f_blk = dep_hit | skel_hit
    return {
        "model_only": model,
        "model_depth": model & depth,
        "live_full": model & depth & ~f_blk,
        "blocked_deployer": model & depth & dep_hit,
        "blocked_skeleton": model & depth & skel_hit & ~dep_hit,
        "blocked_depth": model & ~depth,
        "blocked_fric": model & depth & df.tradeable & (df.fric < FRIC_THR),
    }


def fork_crosstab(df: pd.DataFrame, traded_mask) -> dict:
    """224-style crosstab on pools with fork replay + QV2 net at exit."""
    fork = {}
    for fn in ("_set131_forkreplay.json", "_set217_forkreplay.json"):
        p = os.path.join(HERE, fn)
        if os.path.exists(p):
            for t in json.load(open(p)).get("trades", []):
                fork[t["pool"]] = t
    rows = []
    for _, r in df[traded_mask & df.tradeable].iterrows():
        ft = fork.get(r.pool)
        if not ft:
            continue
        qv2_ok = (r.raw_net or 0) > -0.5
        fork_ok = ft.get("real_tradeable_at_exit", False)
        rows.append({"pool": r.pool, "qv2_ok": qv2_ok, "fork_ok": fork_ok,
                     "label": r.label, "real_slippage": ft.get("real_slippage")})
    if not rows:
        return {"n": 0}
    d = pd.DataFrame(rows)
    ct = {
        "(qv2_ok, fork_ok)": int(((d.qv2_ok) & (d.fork_ok)).sum()),
        "(qv2_ok, fork_bad)": int(((d.qv2_ok) & (~d.fork_ok)).sum()),
        "(qv2_bad, fork_ok)": int(((~d.qv2_ok) & (d.fork_ok)).sum()),
        "(qv2_bad, fork_bad)": int(((~d.qv2_ok) & (~d.fork_ok)).sum()),
    }
    missed = d[(d.qv2_ok) & (~d.fork_ok)]["pool"].tolist()
    slips = d[d.qv2_ok & d.fork_ok]["real_slippage"].dropna()
    return {
        "n": len(d),
        "crosstab": ct,
        "qv2_miss_fork": missed,
        "slip_median": float(slips.median()) if len(slips) else None,
        "slip_min": float(slips.min()) if len(slips) else None,
    }


def run_lens(df: pd.DataFrame, lens: str) -> pd.DataFrame:
    priced = json.load(open(K.PCACHE))
    realgas = json.load(open(os.path.join(HERE, "_realgas_replay.json")))
    rows = []
    for pool in df.pool:
        pr = priced.get(pool) or {}
        if lens == "qv2":
            if pr.get("why") != "ok":
                rows.append((None, None, pr.get("why")))
                continue
            fric = pr.get("fric")
            raw = pr.get("net")
        else:  # fork
            rg = realgas.get(pool) or {}
            if rg.get("status") != "ok":
                rows.append((None, None, rg.get("status")))
                continue
            fric = pr.get("fric") if pr.get("why") == "ok" else None
            raw = rg.get("real_net")
        if fric is None or fric < FRIC_THR:
            rows.append((None, fric, "fric"))
            continue
        if raw is None:
            rows.append((None, fric, "no_ret"))
            continue
        rows.append((adj_ret(raw), fric, "ok"))
    df = df.copy()
    df["ret"] = [x[0] for x in rows]
    df["fric"] = [x[1] for x in rows]
    df["raw_net"] = df.pool.map(lambda p: (priced.get(p) or {}).get("net") if lens == "qv2"
                                 else (realgas.get(p) or {}).get("real_net"))
    df["why"] = [x[2] for x in rows]
    df["tradeable"] = df.ret.notna()
    df["gate_fric"] = df.fric.notna() & (df.fric >= FRIC_THR)
    return df


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    deployers, skeletons, creators, token_skel = _load_blacklists()
    base = load_base_df()

    report = {
        "meta": {
            "ts": datetime.now(timezone.utc).isoformat(),
            "config": {
                "MIN_PRE": MIN_PRE, "P_CUT": P_CUT, "MIN_DEPTH_ETH": MIN_DEPTH_ETH,
                "FRIC_THR": FRIC_THR, "SIZE_ETH": SIZE_ETH, "SIZE_USD": SIZE_USD,
                "GAS_USD_RT": GAS_USD_RT, "HAIRCUT": HAIRCUT,
                "deployer_blacklist_n": len(deployers),
                "skeleton_blacklist_n": len(skeletons),
            },
            "not_replayed": ["sell_sim", "external_risk", "E_window_1800s", "MEV"],
            "n_labeled": len(base),
        },
        "lenses": {},
    }

    for lens in ("qv2", "fork"):
        df = run_lens(base, lens)
        masks = gate_masks(df, deployers, skeletons, creators, token_skel)
        fric_ok = df.gate_fric
        live = masks["live_full"] & fric_ok
        v0 = masks["model_only"] & fric_ok
        depth = masks["model_depth"] & fric_ok

        lens_out = {
            "baseline_model_fric_no_depth": metrics(df, v0, SIZE_USD),
            "plus_depth_ge3": metrics(df, depth, SIZE_USD),
            "live_full_gates": metrics(df, live, SIZE_USD),
            "gate_breakdown_pass_model": {k: int(v.sum()) for k, v in masks.items()},
            "blocked_by_reason": {
                "depth": int(masks["blocked_depth"].sum()),
                "fric_fail": int((masks["model_depth"] & ~fric_ok).sum()),
                "deployer": int((masks["blocked_deployer"] & fric_ok).sum()),
                "skeleton_only": int((masks["blocked_skeleton"] & fric_ok).sum()),
            },
            "fork_crosstab_on_live_passed": fork_crosstab(df, live),
        }
        # compare vs old $100 config (same gates)
        df_old = df.copy()
        df_old["ret"] = df.raw_net.map(
            lambda x: float(x) - GAS_USD_RT / 100.0 - HAIRCUT if x is not None else None)
        df_old["tradeable"] = df_old.ret.notna()
        lens_out["live_at_old_size_100usd"] = metrics(df_old, live, 100.0)
        lens_out["depth_at_old_size_100usd"] = metrics(df_old, depth, 100.0)
        report["lenses"][lens] = lens_out

        print(f"\n=== LENS: {lens.upper()} | size=${SIZE_USD:.2f} ({SIZE_ETH} ETH) ===")
        for name, key in [("model+fric (no depth)", "baseline_model_fric_no_depth"),
                          ("+depth≥3", "plus_depth_ge3"),
                          ("LIVE full gates", "live_full_gates")]:
            m = lens_out[key]
            print(f"  {name:22s} pass={m['pass_n']:3d} dr_blk={m['dr_blocked']:3d}/262 "
                  f"fatal={m['fatal_rate']:.1%}({m['fatal_n']}) monthly={m['monthly']:+.1%} "
                  f"miskill={m['miskill_winners']}")
        ct = lens_out["fork_crosstab_on_live_passed"]
        if ct.get("n"):
            print(f"  fork crosstab n={ct['n']} {ct['crosstab']}")

    out_json = os.path.join(OUT_DIR, "live_config_rebacktest.json")
    json.dump(report, open(out_json, "w"), indent=2, default=float)
    print(f"\nwrote {out_json}")
    return report


if __name__ == "__main__":
    main()
