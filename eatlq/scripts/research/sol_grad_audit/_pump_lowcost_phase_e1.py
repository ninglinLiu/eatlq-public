#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase E1: T+15 ex-ante discriminant.

Uses only T+15-visible features to rank 4154 Phase B universe.
Labels from full replay (Phase C/D + optional top5% supplement).
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from _grad_audit_lib import load_deployer, load_early_buys, load_jsonl
from _pump_exante_rank import creator_feats
from _pump_lowcost_lib import COST_RT, LC_ROOT, save_json
from _pump_tail_realgas_replay import RPC, b58dec, collect_trades, load_meta, reserves_near, rpc_url, simulate_roundtrip, SOL_USD

OUT_DIR = Path(__file__).resolve().parent
PHASE_E1_ROOT = LC_ROOT / "phase_e1"

HIT100_MC = 15_000
TRAIN_DAYS = {f"2026-06-{d:02d}" for d in range(2, 12)}  # 06-02 ~ 06-11
TEST_DAYS = {f"2026-06-{d:02d}" for d in range(12, 16)}  # 06-12 ~ 06-15

FEATURE_COLS = [
    "n_trades_15",
    "buy_count_15",
    "sell_count_15",
    "unique_buyers_15",
    "buy_sell_imbalance_15",
    "first_trade_delay",
    "last_trade_before_15",
    "vSol_5",
    "vSol_15",
    "vSol_delta_5_15",
    "progress_15",
    "premium_0_15",
    "time_to_5th_s",
    "reached_birth",
    "prior_n",
    "prior_grad_rate",
    "prior_log_max_ath",
    "prior_med_t2ath",
    "has_snap_15",
    "sol_in_15",
    "net_sol_15",
]


def load_phase_b() -> list[dict]:
    rows: list[dict] = []
    for p in sorted((LC_ROOT / "phase_b").glob("*/coins.jsonl")):
        rows.extend(load_jsonl(p))
    return rows


def load_replay_map() -> dict[str, float]:
    out: dict[str, float] = {}
    c_path = LC_ROOT / "phase_c" / "full_replay.json"
    if c_path.exists():
        for r in json.loads(c_path.read_text()).get("results", []):
            v = r.get("real_return_10U")
            if v is not None:
                out[r["mint"]] = float(v)
    d_path = LC_ROOT / "phase_d" / "tail_autopsy.json"
    if d_path.exists():
        for t in json.loads(d_path.read_text()).get("tokens", []):
            w = (t.get("windows") or {}).get("t15_t90") or {}
            v = w.get("real_return_10U")
            if v is not None:
                out[t["mint"]] = float(v)
    e1_path = PHASE_E1_ROOT / "top5_replay.json"
    if e1_path.exists():
        for r in json.loads(e1_path.read_text()).get("results", []):
            v = r.get("real_return_10U")
            if v is not None:
                out[r["mint"]] = float(v)
    return out


def load_phase_d_meta() -> dict[str, dict]:
    p = LC_ROOT / "phase_d" / "tail_autopsy.json"
    if not p.exists():
        return {}
    return {t["mint"]: t for t in json.loads(p.read_text()).get("tokens", [])}


def snap_val(snaps: dict, t: str, key: str):
    s = snaps.get(t)
    if not s:
        return None
    return s.get(key)


def snapshot_features(row: dict) -> dict:
    snaps = row.get("snapshots") or {}
    p5 = snap_val(snaps, "5", "price")
    p15 = snap_val(snaps, "15", "price")
    v5 = snap_val(snaps, "5", "vSol")
    v15 = snap_val(snaps, "15", "vSol")
    t5 = snap_val(snaps, "5", "t_rel")
    t15 = snap_val(snaps, "15", "t_rel")

    rels = [s.get("t_rel") for s in snaps.values() if s and s.get("t_rel") is not None and s["t_rel"] <= 15]
    first_delay = min(rels) if rels else None
    last_before_15 = max(rels) if rels else None

    prem = (p15 / p5 - 1) if p5 and p15 and p5 > 0 else None
    return {
        "vSol_5": v5,
        "vSol_15": v15,
        "vSol_delta_5_15": (v15 - v5) if v5 is not None and v15 is not None else None,
        "progress_15": (v15 / 85.0) if v15 else None,
        "premium_0_15": prem,
        "first_trade_delay": first_delay,
        "last_trade_before_15": last_before_15,
        "has_snap_15": 1.0 if p15 else 0.0,
        "time_to_5th_s": row.get("time_to_5th_s"),
        "reached_birth": 1.0 if row.get("reached_birth") else 0.0,
    }


def earlybuy_features(eb: list, creator: str | None) -> dict:
    eb15 = [e for e in eb if (e.get("t_rel") or 999) <= 15]
    buys = [e for e in eb15 if e.get("is_buy")]
    sells = [e for e in eb15 if not e.get("is_buy")]
    wallets = {e["buyer"] for e in eb15 if e.get("buyer")}
    n = len(eb15)
    sol_b = sum(float(e.get("sol") or 0) for e in buys)
    sol_s = sum(float(e.get("sol") or 0) for e in sells)
    rels = [e.get("t_rel") for e in eb15 if e.get("t_rel") is not None]
    return {
        "n_trades_15": n,
        "buy_count_15": len(buys),
        "sell_count_15": len(sells),
        "unique_buyers_15": len(wallets),
        "buy_sell_imbalance_15": (len(buys) - len(sells)) / max(n, 1) if n else 0.0,
        "first_trade_delay": min(rels) if rels else None,
        "last_trade_before_15": max(rels) if rels else None,
        "sol_in_15": sol_b,
        "net_sol_15": sol_b - sol_s,
    }


def estimate_n_trades_15(row: dict, snaps_feats: dict) -> float | None:
    """Proxy: sig-page count unavailable without RPC; use snap activity + time_to_5th."""
    snaps = row.get("snapshots") or {}
    n_snap = sum(1 for k, s in snaps.items() if s and (s.get("t_rel") or 99) <= 15)
    t5 = row.get("time_to_5th_s")
    if t5 is not None and t5 <= 15:
        return max(n_snap, 5.0)
    if n_snap:
        return float(n_snap)
    return None


def build_feature_row(row: dict, eb_map: dict, deployer: dict, d_meta: dict) -> dict:
    mint = row["mint"]
    creator = row.get("creator")
    cms = row.get("created_ms") or 0
    feats = snapshot_features(row)
    feats.update(creator_feats(creator, cms, deployer))

    if mint in eb_map:
        eb = eb_map[mint].get("early_buys") or []
        eb_f = earlybuy_features(eb, creator)
        for k, v in eb_f.items():
            if v is not None:
                feats[k] = v
    elif mint in d_meta and d_meta[mint].get("n_trades_15s") is not None:
        feats["n_trades_15"] = float(d_meta[mint]["n_trades_15s"])

    if feats.get("n_trades_15") is None or (isinstance(feats.get("n_trades_15"), float) and math.isnan(feats["n_trades_15"])):
        est = estimate_n_trades_15(row, feats)
        if est is not None:
            feats["n_trades_15"] = est

    for c in FEATURE_COLS:
        feats.setdefault(c, np.nan)
    return feats


def assign_groups(row: dict, full_ret: float | None) -> str:
    ath = row.get("ath_mc") or 0
    if full_ret is not None and full_ret >= 1.0:
        return "tradable_tail"
    if ath >= HIT100_MC:
        return "hit100_nontradable"
    return "other"


def group_summary(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    rows = []
    for grp in ["tradable_tail", "hit100_nontradable", "random_control"]:
        sub = df[df["compare_group"] == grp]
        if grp == "random_control" and len(sub) == 0:
            continue
        r = {"group": grp, "n": len(sub)}
        for c in cols:
            s = sub[c].dropna()
            r[f"{c}_median"] = float(s.median()) if len(s) else None
            r[f"{c}_mean"] = float(s.mean()) if len(s) else None
        rows.append(r)
    return rows


def univariate_lift(df: pd.DataFrame, label_col: str = "tradable_tail") -> list[dict]:
    base = df[label_col].mean()
    rows = []
    for c in FEATURE_COLS:
        s = df[c].dropna()
        if len(s) < 50:
            continue
        med = s.median()
        hi = df[c] >= med
        lo = df[c] < med
        p_hi = df.loc[hi, label_col].mean() if hi.sum() else 0
        p_lo = df.loc[lo, label_col].mean() if lo.sum() else 0
        lift = (p_hi / base) if base > 0 else None
        rows.append({
            "feature": c,
            "base_rate": base,
            "p_hi": p_hi,
            "p_lo": p_lo,
            "lift_hi_vs_base": lift,
            "direction": "high" if p_hi >= p_lo else "low",
        })
    rows.sort(key=lambda x: -(x.get("lift_hi_vs_base") or 0))
    return rows


def simple_rank_score(df: pd.DataFrame, lifts: list[dict], train_mask: pd.Series) -> np.ndarray:
    """Weighted z-score sum using univariate lift direction on train set."""
    weights = {}
    for lv in lifts[:12]:
        f = lv["feature"]
        if lv.get("lift_hi_vs_base") and lv["lift_hi_vs_base"] > 1.05:
            weights[f] = math.log(lv["lift_hi_vs_base"])
    if not weights:
        weights = {"n_trades_15": 1.0, "vSol_delta_5_15": 1.0, "progress_15": 1.0}

    scores = np.zeros(len(df))
    tr = df[train_mask]
    for f, w in weights.items():
        if f not in df.columns:
            continue
        mu = tr[f].median()
        sd = tr[f].std() or 1.0
        z = (df[f].fillna(mu) - mu) / sd
        # flip if low bucket is better
        lv = next((x for x in lifts if x["feature"] == f), None)
        if lv and lv.get("direction") == "low":
            z = -z
        scores += w * z.values
    return scores


def bucket_lift(df: pd.DataFrame, scores: np.ndarray, ret_col: str, fracs: list[float]) -> list[dict]:
    base = df[ret_col].dropna()
    base_trad = (base >= 1.0).mean() if len(base) else 0
    base_big = (base >= 3.0).mean() if len(base) else 0
    base_bad = (base <= -0.25).mean() if len(base) else 0
    rows = [{
        "bucket": "ALL",
        "n": len(df),
        "n_labeled": int(df[ret_col].notna().sum()),
        "p_tradable": base_trad,
        "p_big_tradable": base_big,
        "p_bad": base_bad,
        "median_ret": float(base.median()) if len(base) else None,
    }]
    order = np.argsort(-scores)
    n = len(df)
    for frac in fracs:
        k = max(1, int(n * frac))
        idx = order[:k]
        sub = df.iloc[idx]
        labeled = sub[ret_col].dropna()
        rows.append({
            "bucket": f"top_{int(frac*100)}pct",
            "n": k,
            "n_labeled": int(sub[ret_col].notna().sum()),
            "p_tradable": float((labeled >= 1.0).mean()) if len(labeled) else None,
            "p_big_tradable": float((labeled >= 3.0).mean()) if len(labeled) else None,
            "p_bad": float((labeled <= -0.25).mean()) if len(labeled) else None,
            "median_ret": float(labeled.median()) if len(labeled) else None,
            "lift_tradable_vs_all": (float((labeled >= 1.0).mean()) / base_trad) if len(labeled) and base_trad > 0 else None,
        })
    return rows


def daily_performance(df: pd.DataFrame, ret_col: str) -> list[dict]:
    rows = []
    for day, sub in df.groupby("day"):
        sc = sub["score"].values
        b = bucket_lift(sub, sc, ret_col, [0.05])
        top5 = next(x for x in b if x["bucket"] == "top_5pct")
        rows.append({"day": day, **top5})
    return rows


def replay_window_one(rpc: RPC, row: dict, meta: dict, tx_cache: dict) -> dict:
    mint = row["mint"]
    m = meta.get(mint, {})
    bc = m.get("bonding_curve") or row.get("bonding_curve")
    cms = m.get("created_ms") or row.get("created_ms")
    if not bc or not cms:
        return {"mint": mint, "ok": False, "reason": "missing_bc"}
    created = cms / 1000.0
    mint_b = b58dec(mint)
    trades, _ = collect_trades(rpc, bc, created + 95)
    ent = reserves_near(rpc, trades, created + 15, mint_b, tx_cache, before=True)
    ex = reserves_near(rpc, trades, created + 90, mint_b, tx_cache, before=True)
    sim = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), 10 / SOL_USD) if ent and ex else {}
    return {
        "mint": mint,
        "ok": bool(ent and ex and sim.get("ok")),
        "real_return_10U": sim.get("real_return"),
        "day": row.get("day"),
        "score": row.get("score"),
    }


def supplement_top5_replay(df: pd.DataFrame, scores: np.ndarray, replay_map: dict) -> tuple[dict[str, float], int]:
    """Full replay top 5% by E1 score if missing."""
    order = np.argsort(-scores)
    k = max(1, int(len(df) * 0.05))
    top_idx = order[:k]
    pending = [df.iloc[i] for i in top_idx if df.iloc[i]["mint"] not in replay_map]
    if not pending:
        return replay_map, 0

    meta = load_meta()
    for _, row in df.iterrows():
        m = row["mint"]
        meta.setdefault(m, {})
        meta[m].update({"mint": m, "bonding_curve": row.get("bonding_curve"), "created_ms": int(row.get("created_ms") or 0)})

    rpc = RPC(rpc_url(), min_interval=0.20)
    tx_cache: dict = {}
    results = []
    for i, row in enumerate(pending):
        r = replay_window_one(rpc, row.to_dict(), meta, tx_cache)
        results.append(r)
        if (i + 1) % 20 == 0:
            print(f"  [E1 replay] {i+1}/{len(pending)} rpc={rpc.calls}", flush=True)

    out = PHASE_E1_ROOT / "top5_replay.json"
    save_json(out, {"meta": {"n": len(results), "rpc_calls": rpc.calls}, "results": results})
    updated = dict(replay_map)
    for r in results:
        if r.get("real_return_10U") is not None:
            updated[r["mint"]] = float(r["real_return_10U"])
    print(f"Phase E1 top5 replay: {len(pending)} new, rpc={rpc.calls}")
    return updated, rpc.calls


def _fl(v):
    return f"{v:.2f}" if v is not None else "-"


def _f(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NA"
    return f"{v:.2f}"


def _pct(v, signed=False):
    if v is None:
        return "NA"
    if signed:
        return f"{v*100:+.1f}%"
    return f"{v*100:.1f}%"


def write_readout(path: Path, summary: dict) -> None:
    s = summary
    lines = [
        "# Phase E1: T+15 Ex-ante Discriminant",
        "",
        f"> Universe n=**{s['n_universe']}** | labeled replay n=**{s['n_labeled']}** | "
        f"tradable_tail=**{s['n_tradable']}** | RPC supplement=**{s.get('rpc_supplement', 0)}**",
        "",
        "## 1. 三组对比（feature median）",
        "",
        "| group | n | n_trades_15 | unique_buyers_15 | vSol_delta_5_15 | progress_15 | premium_0_15 |",
        "|-------|---|-------------|------------------|-----------------|-------------|--------------|",
    ]
    for g in s["group_summary"]:
        lines.append(
            f"| {g['group']} | {g['n']} | {_f(g.get('n_trades_15_median'))} | "
            f"{_f(g.get('unique_buyers_15_median'))} | {_f(g.get('vSol_delta_5_15_median'))} | "
            f"{_f(g.get('progress_15_median'))} | {_f(g.get('premium_0_15_median'))} |"
        )

    lines.extend(["", "## 2. Univariate lift（tradable_tail vs base）", ""])
    lines.append("| feature | base | p(hi) | lift | dir |")
    lines.append("|---------|------|-------|------|-----|")
    for u in s["univariate_lift"][:15]:
        lines.append(
            f"| {u['feature']} | {u['base_rate']:.3f} | {u['p_hi']:.3f} | "
            f"{_fl(u.get('lift_hi_vs_base'))} | {u['direction']} |"
        )

    lines.extend(["", "## 3. Simple rank score — full universe top-k lift", ""])
    lines.append("| bucket | n | labeled | p(tradable) | p(+300%) | p(bad) | median ret | lift |")
    lines.append("|--------|---|---------|-------------|----------|--------|------------|------|")
    for b in s["universe_buckets"]:
        lines.append(
            f"| {b['bucket']} | {b['n']} | {b['n_labeled']} | "
            f"{_pct(b.get('p_tradable'))} | {_pct(b.get('p_big_tradable'))} | {_pct(b.get('p_bad'))} | "
            f"{_pct(b.get('median_ret'), signed=True)} | {_fl(b.get('lift_tradable_vs_all'))} |"
        )

    lines.extend(["", "## 4. OOS（train 06-02~06-11, test 06-12~06-15）", ""])
    oos = s["oos"]
    lines.append(f"- train labeled n={oos['train_labeled']} | test labeled n={oos['test_labeled']}")
    lines.append(f"- test top5% p(tradable)={_pct(oos['test_top5_p_tradable'])} vs ALL test={_pct(oos['test_all_p_tradable'])}")
    lines.append(f"- test top5% median ret={_pct(oos['test_top5_median'], signed=True)} vs ALL={_pct(oos['test_all_median'], signed=True)}")

    lines.extend(["", "## 5. Daily top5% p(tradable)", ""])
    lines.append("| day | top5 n | labeled | p(tradable) | median ret |")
    lines.append("|-----|--------|---------|-------------|------------|")
    for d in s["daily"]:
        lines.append(
            f"| {d['day']} | {d['n']} | {d['n_labeled']} | {_pct(d.get('p_tradable'))} | "
            f"{_pct(d.get('median_ret'), signed=True)} |"
        )

    lines.extend(["", "## 6. 结论", "", s["verdict"], ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_phase_e1(skip_replay: bool = False) -> None:
    PHASE_E1_ROOT.mkdir(parents=True, exist_ok=True)
    rows = load_phase_b()
    print(f"Phase E1: building features for {len(rows)} coins...", flush=True)

    eb_map = load_early_buys()
    print("Phase E1: loading deployer history...", flush=True)
    deployer = load_deployer()
    print(f"Phase E1: deployer creators={len(deployer)}", flush=True)
    d_meta = load_phase_d_meta()
    replay_map = load_replay_map()

    records = []
    for i, row in enumerate(rows):
        feats = build_feature_row(row, eb_map, deployer, d_meta)
        mint = row["mint"]
        full_ret = replay_map.get(mint)
        rec = {
            "mint": mint,
            "day": row.get("day"),
            "creator": row.get("creator"),
            "bonding_curve": row.get("bonding_curve"),
            "created_ms": row.get("created_ms"),
            "full_ret_15_90": full_ret,
            "tradable_tail": int(full_ret is not None and full_ret >= 1.0),
            "big_tradable_tail": int(full_ret is not None and full_ret >= 3.0),
            "bad_tail": int(full_ret is not None and full_ret <= -0.25),
            "compare_group": assign_groups(row, full_ret),
            **feats,
        }
        records.append(rec)
        if (i + 1) % 500 == 0:
            print(f"  features {i+1}/{len(rows)}", flush=True)

    df = pd.DataFrame(records)

    # random control: same size as tradable_tail among 'other' with label
    labeled = df[df["full_ret_15_90"].notna()].copy()
    n_trad = int(labeled["tradable_tail"].sum())
    others = df[df["compare_group"] == "other"]
    rng = random.Random(42)
    rc_idx = rng.sample(list(others.index), min(n_trad, len(others))) if n_trad and len(others) else []
    df["compare_group"] = df["compare_group"].where(~df.index.isin(rc_idx), "random_control")

    # Train/test split for score
    df["split"] = df["day"].apply(lambda d: "train" if d in TRAIN_DAYS else ("test" if d in TEST_DAYS else "other"))
    train_labeled = df[(df["split"] == "train") & df["full_ret_15_90"].notna()]
    lifts = univariate_lift(train_labeled if len(train_labeled) >= 30 else labeled, "tradable_tail")
    scores = simple_rank_score(df, lifts, df["split"] == "train")
    df["score"] = scores

    # Supplement top5 replay
    rpc_used = 0
    if not skip_replay:
        replay_map, rpc_used = supplement_top5_replay(df, scores, replay_map)
        df["full_ret_15_90"] = df["mint"].map(replay_map)
        df["tradable_tail"] = (df["full_ret_15_90"] >= 1.0).astype(int)
        df["big_tradable_tail"] = (df["full_ret_15_90"] >= 3.0).astype(int)
        df["bad_tail"] = (df["full_ret_15_90"] <= -0.25).astype(int)
        # refresh compare groups after new labels
        ath_map = {r["mint"]: r.get("ath_mc") for r in rows}
        df["compare_group"] = df.apply(
            lambda r: assign_groups({"ath_mc": ath_map.get(r["mint"])}, r["full_ret_15_90"] if pd.notna(r["full_ret_15_90"]) else None),
            axis=1,
        )
        n_trad = int(df["tradable_tail"].sum())
        others = df[df["compare_group"] == "other"]
        rc_idx = rng.sample(list(others.index), min(n_trad, len(others))) if n_trad and len(others) else []
        df.loc[rc_idx, "compare_group"] = "random_control"

    ret_col = "full_ret_15_90"
    universe_buckets = bucket_lift(df, scores, ret_col, [0.01, 0.05, 0.10])

    # OOS
    te = df[df["split"] == "test"]
    tr = df[df["split"] == "train"]
    te_buckets = bucket_lift(te, te["score"].values, ret_col, [0.05])
    te_top5 = next(x for x in te_buckets if x["bucket"] == "top_5pct")
    te_all = next(x for x in te_buckets if x["bucket"] == "ALL")

    daily = daily_performance(df, ret_col)

    # Verdict
    top5 = next(x for x in universe_buckets if x["bucket"] == "top_5pct")
    all_b = next(x for x in universe_buckets if x["bucket"] == "ALL")
    lift = top5.get("lift_tradable_vs_all") or 0
    rate_pass = lift >= 1.5 and (top5.get("p_tradable") or 0) > (all_b.get("p_tradable") or 0) * 1.3
    pnl_fail = (top5.get("median_ret") or 0) < -0.05 and (top5.get("p_bad") or 0) > 0.4
    verdict = (
        f"**{'PARTIAL' if rate_pass and pnl_fail else ('PASS' if rate_pass else 'WEAK/NO')} ex-ante discriminant.** "
        f"Top5% p(tradable)={_pct(top5.get('p_tradable'))} vs ALL={_pct(all_b.get('p_tradable'))} "
        f"(lift={lift:.2f}x, labeled n={top5.get('n_labeled')}). "
        f"Top5% median ret={_pct(top5.get('median_ret'), signed=True)}, p(bad)={_pct(top5.get('p_bad'))}. "
        f"OOS test top5 p(tradable)={_pct(te_top5.get('p_tradable'))}. "
        + ("Rate lift 存在但 median 仍为负、fatal25 偏高 → 仅适合作筛选信号，非可交易策略。" if rate_pass and pnl_fail else "")
    )

    summary = {
        "n_universe": len(df),
        "n_labeled": int(df[ret_col].notna().sum()),
        "n_tradable": int(df["tradable_tail"].sum()),
        "rpc_supplement": rpc_used,
        "group_summary": group_summary(df[df["compare_group"].isin(["tradable_tail", "hit100_nontradable", "random_control"])], FEATURE_COLS),
        "univariate_lift": lifts,
        "universe_buckets": universe_buckets,
        "daily": daily,
        "oos": {
            "train_labeled": int(tr[ret_col].notna().sum()),
            "test_labeled": int(te[ret_col].notna().sum()),
            "test_top5_p_tradable": te_top5.get("p_tradable"),
            "test_all_p_tradable": te_all.get("p_tradable"),
            "test_top5_median": te_top5.get("median_ret"),
            "test_all_median": te_all.get("median_ret"),
        },
        "verdict": verdict,
    }

    df.to_parquet(PHASE_E1_ROOT / "features.parquet", index=False)
    save_json(PHASE_E1_ROOT / "summary.json", summary)
    write_readout(OUT_DIR / "_pump_lowcost_phase_e1_exante_rank_readout.md", summary)
    print(f"Phase E1 done: labeled={summary['n_labeled']} tradable={summary['n_tradable']} rpc={rpc_used}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args()
    run_phase_e1(skip_replay=args.skip_replay)
