#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase E1b: Tail-Risk Split.

Separate tail_score (tradable_tail) from risk_score (bad/fatal).
Find high-tail / low-risk sweet spot within T+15 ex-ante features.
"""
from __future__ import annotations

import json
import math
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from _pump_lowcost_lib import LC_ROOT, load_jsonl, save_json
from _pump_lowcost_phase_e1 import (
    HIT100_MC,
    TEST_DAYS,
    TRAIN_DAYS,
    load_replay_map,
    supplement_top5_replay,
)
from _pump_tail_realgas_replay import RPC, b58dec, collect_trades, load_meta, reserves_near, rpc_url, simulate_roundtrip, SOL_USD

OUT_DIR = Path(__file__).resolve().parent
E1_ROOT = LC_ROOT / "phase_e1"
E1B_ROOT = LC_ROOT / "phase_e1b"

TAIL_FEATURES = [
    "n_trades_15",
    "vSol_delta_5_15",
    "premium_0_15",
    "time_to_5th_s",
    "last_trade_before_15",
    "buy_sell_imbalance_15",
    "unique_buyers_15",
    "net_sol_15",
]

RISK_FEATURES = [
    "progress_15",
    "vSol_15",
    "premium_0_15",
    "n_trades_15",
    "sell_count_15",
    "buy_count_15",
    "unique_buyers_15",
    "buy_sell_imbalance_15",
]

LAMBDAS = [0.5, 1.0, 1.5, 2.0, 3.0]
TOP_FRACS = [0.01, 0.02, 0.05, 0.10]


def load_features() -> pd.DataFrame:
    p = E1_ROOT / "features.parquet"
    if not p.exists():
        raise SystemExit("Run Phase E1 first: features.parquet missing")
    df = pd.read_parquet(p)
    if "split" not in df:
        df["split"] = df["day"].apply(lambda d: "train" if d in TRAIN_DAYS else ("test" if d in TEST_DAYS else "other"))
    return df


def assign_labels(df: pd.DataFrame) -> pd.DataFrame:
    r = df["full_ret_15_90"]
    df = df.copy()
    df["label_tradable"] = (r >= 1.0).astype(int)
    df["label_big"] = (r >= 3.0).astype(int)
    df["label_bad"] = (r <= -0.25).astype(int)
    df["label_fatal"] = (r <= -0.50).astype(int)
    df["label_neutral"] = r.notna() & (r > -0.25) & (r < 1.0)
    cls = np.full(len(df), "unlabeled", dtype=object)
    m = r.notna()
    cls[m & (r >= 3.0)] = "big_tail"
    cls[m & (r >= 1.0) & (r < 3.0)] = "tradable_tail"
    cls[m & (r <= -0.50)] = "fatal"
    cls[m & (r <= -0.25) & (r > -0.50)] = "bad"
    cls[m & (r > -0.25) & (r < 1.0)] = "neutral"
    df["tail_class"] = cls
    # tradable group for comparisons includes big_tail (>=+100%)
    df["is_tradable_group"] = (r >= 1.0)
    return df


def sell_ratio(row: pd.Series) -> float | None:
    b = row.get("buy_count_15")
    s = row.get("sell_count_15")
    if pd.isna(b) or pd.isna(s) or b == 0:
        return None
    return float(s) / float(b)


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sell_buy_ratio_15"] = df.apply(sell_ratio, axis=1)
    return df


def group_compare(df: pd.DataFrame, a_mask: pd.Series, b_mask: pd.Series, a_name: str, b_name: str, feats: list[str]) -> list[dict]:
    sa = df[a_mask]
    sb = df[b_mask]
    rows = []
    for f in feats:
        va = sa[f].dropna()
        vb = sb[f].dropna()
        rows.append({
            "feature": f,
            "group_a": a_name,
            "group_b": b_name,
            "n_a": len(va),
            "n_b": len(vb),
            "median_a": float(va.median()) if len(va) else None,
            "median_b": float(vb.median()) if len(vb) else None,
            "mean_a": float(va.mean()) if len(va) else None,
            "mean_b": float(vb.mean()) if len(vb) else None,
            "delta_median": float(va.median() - vb.median()) if len(va) and len(vb) else None,
        })
    return rows


def hit100_nontradable_mask(df: pd.DataFrame) -> pd.Series:
    """Comparison group only — uses API ath from phase B, not as feature."""
    ath = {}
    for p in sorted((LC_ROOT / "phase_b").glob("*/coins.jsonl")):
        for row in load_jsonl(p):
            ath[row["mint"]] = row.get("ath_mc") or 0
    is_hit = df["mint"].map(lambda m: (ath.get(m, 0) or 0) >= HIT100_MC)
    trad = df["label_tradable"] == 1
    return is_hit & ~trad & df["full_ret_15_90"].notna()


def compare_hit100_nontradable(df: pd.DataFrame, feats: list[str]) -> list[dict]:
    trad = df[df["is_tradable_group"] == True]
    hit = df[hit100_nontradable_mask(df)]
    rows = []
    for f in feats:
        va = trad[f].dropna()
        vb = hit[f].dropna()
        rows.append({
            "feature": f,
            "group_a": "tradable_tail",
            "group_b": "hit100_nontradable",
            "n_a": len(va),
            "n_b": len(vb),
            "median_a": float(va.median()) if len(va) else None,
            "median_b": float(vb.median()) if len(vb) else None,
            "delta_median": float(va.median() - vb.median()) if len(va) and len(vb) else None,
        })
    return rows


def univariate_direction(train: pd.DataFrame, feat: str, y_col: str) -> dict:
    """Higher feature value → more y?"""
    sub = train[[feat, y_col]].dropna()
    if len(sub) < 20:
        return {"feature": feat, "lift_hi": None, "direction": "high"}
    med = sub[feat].median()
    hi = sub[feat] >= med
    base = sub[y_col].mean()
    p_hi = sub.loc[hi, y_col].mean()
    p_lo = sub.loc[~hi, y_col].mean()
    direction = "high" if p_hi >= p_lo else "low"
    lift = (p_hi / base) if base > 0 else None
    return {"feature": feat, "lift_hi": lift, "direction": direction, "p_hi": p_hi, "p_lo": p_lo, "base": base}


def build_weighted_score(
    df: pd.DataFrame,
    train_mask: pd.Series,
    feats: list[str],
    y_col: str,
    invert: set[str] | None = None,
    min_cov: int = 20,
) -> np.ndarray:
    invert = invert or set()
    tr = df[train_mask & df[y_col].notna()]
    weights: dict[str, float] = {}
    for f in feats:
        if f not in df.columns:
            continue
        if tr[f].notna().sum() < min_cov:
            continue
        uv = univariate_direction(tr, f, y_col)
        if uv.get("lift_hi") and uv["lift_hi"] > 1.05:
            weights[f] = math.log(uv["lift_hi"])
        elif f in invert or f in ("progress_15", "vSol_15", "sell_buy_ratio_15", "n_trades_15"):
            weights.setdefault(f, 0.8)
    usable = [f for f in weights if tr[f].notna().sum() >= min_cov]
    if not usable:
        usable = [f for f in feats if f in df.columns and tr[f].notna().sum() >= min_cov]
        weights = {f: 1.0 for f in usable[:1]}
    if not usable:
        return np.zeros(len(df))

    scores = np.zeros(len(df))
    for f in usable:
        w = weights.get(f, 1.0)
        valid_tr = tr[f].dropna()
        mu = valid_tr.median()
        sd = valid_tr.std() or 1.0
        z = (df[f].fillna(mu) - mu) / sd
        uv = univariate_direction(tr, f, y_col)
        if f in invert or uv.get("direction") == "low":
            z = -z
        scores += w * z.values
    return scores


def bucket_metrics(sub: pd.DataFrame) -> dict:
    r = sub["full_ret_15_90"].dropna()
    n = len(sub)
    nl = len(r)
    if nl == 0:
        return {"n": n, "n_labeled": 0}
    rets = r.values
    drop = rets.copy()
    if len(drop) > 1:
        drop = np.delete(drop, int(np.argmax(drop)))
    return {
        "n": n,
        "n_labeled": nl,
        "p_tradable": float((r >= 1.0).mean()),
        "p_big": float((r >= 3.0).mean()),
        "p_bad": float((r <= -0.25).mean()),
        "p_fatal": float((r <= -0.50).mean()),
        "median_ret": float(r.median()),
        "mean_ret": float(r.mean()),
        "drop_top1_mean": float(drop.mean()) if len(drop) else float(r.mean()),
    }


def eval_score(df: pd.DataFrame, scores: np.ndarray, fracs: list[float]) -> list[dict]:
    order = np.argsort(-scores)
    rows = []
    base = bucket_metrics(df)
    base["bucket"] = "ALL"
    rows.append(base)
    n = len(df)
    for frac in fracs:
        k = max(1, int(n * frac))
        sub = df.iloc[order[:k]]
        m = bucket_metrics(sub)
        m["bucket"] = f"top_{int(frac*100)}pct"
        m["lift_tradable"] = (m["p_tradable"] / base["p_tradable"]) if base.get("p_tradable", 0) > 0 and m.get("p_tradable") is not None else None
        rows.append(m)
    return rows


def sweet_spot_mask(df: pd.DataFrame, train: pd.DataFrame) -> pd.Series:
    """Sweet zone: active but not overheated."""
    med_n = train["n_trades_15"].median()
    med_dv = train["vSol_delta_5_15"].median()
    p80_prog = train["progress_15"].quantile(0.80)
    p80_prem = train["premium_0_15"].quantile(0.80)
    med_sell = train["sell_buy_ratio_15"].median()

    m = (
        (df["n_trades_15"].fillna(0) >= med_n)
        & (df["vSol_delta_5_15"].fillna(-999) >= med_dv)
        & (df["progress_15"].fillna(999) <= p80_prog)
        & (df["premium_0_15"].fillna(999) <= p80_prem)
    )
    sbr = df["sell_buy_ratio_15"]
    m &= sbr.isna() | (sbr <= max(med_sell, 0.5))
    return m


def replay_subset(df: pd.DataFrame, replay_map: dict[str, float]) -> tuple[dict[str, float], int]:
    pending = df[~df["mint"].isin(replay_map) | df["mint"].map(lambda m: replay_map.get(m) is None)]
    pending = pending.drop_duplicates("mint")
    if len(pending) == 0:
        return replay_map, 0

    meta = load_meta()
    for _, row in pending.iterrows():
        m = row["mint"]
        meta.setdefault(m, {})
        meta[m].update({
            "mint": m,
            "bonding_curve": row.get("bonding_curve"),
            "created_ms": int(row.get("created_ms") or 0),
        })

    rpc = RPC(rpc_url(), min_interval=0.20)
    tx_cache: dict = {}
    results = []
    for i, (_, row) in enumerate(pending.iterrows()):
        mint = row["mint"]
        bc = row.get("bonding_curve")
        cms = row.get("created_ms")
        if not bc or not cms:
            continue
        created = cms / 1000.0
        mint_b = b58dec(mint)
        trades, _ = collect_trades(rpc, bc, created + 95)
        ent = reserves_near(rpc, trades, created + 15, mint_b, tx_cache, before=True)
        ex = reserves_near(rpc, trades, created + 90, mint_b, tx_cache, before=True)
        sim = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), 10 / SOL_USD) if ent and ex else {}
        results.append({"mint": mint, "real_return_10U": sim.get("real_return"), "ok": sim.get("ok")})
        if (i + 1) % 20 == 0:
            print(f"  [E1b replay] {i+1}/{len(pending)} rpc={rpc.calls}", flush=True)

    out = E1B_ROOT / "subset_replay.json"
    save_json(out, {"meta": {"n": len(results), "rpc_calls": rpc.calls}, "results": results})
    updated = dict(replay_map)
    for r in results:
        if r.get("real_return_10U") is not None:
            updated[r["mint"]] = float(r["real_return_10U"])
    return updated, rpc.calls


def write_readout(path: Path, summary: dict) -> None:
    s = summary
    lines = [
        "# Phase E1b: Tail-Risk Split",
        "",
        f"> labeled n=**{s['n_labeled']}** | tradable=**{s['n_tradable']}** | bad=**{s['n_bad']}** | fatal=**{s['n_fatal']}**",
        "",
        "## 1. tradable_tail vs bad / fatal / hit100_nontradable（T+15 特征 median）",
        "",
        "| feature | tradable | bad | fatal | hit100_nontrad | Δ(trad-hit100) |",
        "|---------|----------|-----|-------|----------------|----------------|",
    ]
    key_feats = ["n_trades_15", "progress_15", "vSol_15", "vSol_delta_5_15", "premium_0_15",
                 "unique_buyers_15", "sell_buy_ratio_15", "buy_sell_imbalance_15"]
    pivot = {r["feature"]: r for r in s["compare_pivot"]}
    for f in key_feats:
        p = pivot.get(f, {})
        lines.append(
            f"| {f} | {_fm(p.get('tradable'))} | {_fm(p.get('bad'))} | {_fm(p.get('fatal'))} | "
            f"{_fm(p.get('hit100_nt'))} | {_fm(p.get('delta_trad_hit100'))} |"
        )

    lines.extend(["", "## 2. tail_score vs risk_score", ""])
    lines.append(f"- tail_score 特征: {', '.join(TAIL_FEATURES[:6])}...")
    lines.append(f"- risk_score 特征: progress_15↑, vSol_15↑, premium_0_15↑, n_trades_15↑, sell pressure↑, unique_buyers_15↓")

    lines.extend(["", "## 3. final_score = tail − λ×risk（full universe 4154）", ""])
    lines.append("| λ | bucket | n_labeled | p(trad) | p(+300%) | p(bad) | p(fatal) | median | mean | drop_top1 |")
    lines.append("|---|--------|-----------|---------|----------|--------|----------|--------|------|-----------|")
    for row in s["lambda_scan"][:25]:
        lines.append(
            f"| {row['lambda']} | {row['bucket']} | {row.get('n_labeled','')} | "
            f"{_fp(row.get('p_tradable'))} | {_fp(row.get('p_big'))} | {_fp(row.get('p_bad'))} | "
            f"{_fp(row.get('p_fatal'))} | {_fp(row.get('median_ret'), True)} | {_fp(row.get('mean_ret'), True)} | "
            f"{_fp(row.get('drop_top1_mean'), True)} |"
        )

    lines.extend(["", "## 4. 甜区规则（sweet spot）", ""])
    ss = s["sweet_spot"]
    lines.append(f"- 规则: n15≥med, vSol_Δ≥med, progress_15≤p80, premium_0_15≤p80, sell/buy 不高")
    lines.append(f"- 命中 n=**{ss.get('n_universe_hit', ss.get('n', 0))}** / labeled **{ss.get('n_labeled', 0)}**")
    lines.append(f"- p(tradable)=**{_fp(ss.get('p_tradable'))}** | p(bad)=**{_fp(ss.get('p_bad'))}** | p(fatal)=**{_fp(ss.get('p_fatal'))}**")
    lines.append(f"- median ret=**{_fp(ss.get('median_ret'), True)}** | mean=**{_fp(ss.get('mean_ret'), True)}**")

    lines.extend(["", "## 5. OOS test（06-12~06-15）", ""])
    oos = s["oos"]
    lines.append(f"- best λ=**{oos['best_lambda']}** bucket **{oos['best_bucket']}**")
    lines.append(f"- test p(tradable)=**{_fp(oos.get('p_tradable'))}** vs E1 top5 **{_fp(oos.get('e1_top5_p_tradable'))}**")
    lines.append(f"- test p(bad)=**{_fp(oos.get('p_bad'))}** vs E1 top5 **{_fp(oos.get('e1_top5_p_bad'))}**")
    lines.append(f"- test median=**{_fp(oos.get('median_ret'), True)}** vs E1 top5 **{_fp(oos.get('e1_top5_median'), True)}**")

    lines.extend(["", "## 6. 结论", "", s["verdict"], ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fm(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NA"
    return f"{v:.3f}"


def _fp(v, signed=False):
    if v is None:
        return "NA"
    if signed:
        return f"{v*100:+.1f}%"
    return f"{v*100:.1f}%"


def run_phase_e1b(skip_replay: bool = False) -> None:
    E1B_ROOT.mkdir(parents=True, exist_ok=True)
    df = add_derived(assign_labels(load_features()))
    labeled = df[df["full_ret_15_90"].notna()].copy()
    train = labeled[labeled["split"] == "train"]
    test = labeled[labeled["split"] == "test"]

    compare_feats = list(set(TAIL_FEATURES + RISK_FEATURES + ["sell_buy_ratio_15", "first_trade_delay"]))
    cmp_bad = group_compare(labeled, labeled["is_tradable_group"], labeled["tail_class"] == "bad", "tradable_tail", "bad", compare_feats)
    cmp_fatal = group_compare(labeled, labeled["is_tradable_group"], labeled["tail_class"] == "fatal", "tradable_tail", "fatal", compare_feats)
    cmp_hit = compare_hit100_nontradable(labeled, compare_feats)

    # pivot for readout
    pivot = []
    trad_med = {r["feature"]: r["median_a"] for r in cmp_hit}
    hit_med = {r["feature"]: r["median_b"] for r in cmp_hit}
    bad_med = {r["feature"]: r["median_b"] for r in cmp_bad}
    fatal_med = {r["feature"]: r["median_b"] for r in cmp_fatal}
    for f in compare_feats:
        pivot.append({
            "feature": f,
            "tradable": trad_med.get(f),
            "bad": bad_med.get(f),
            "fatal": fatal_med.get(f),
            "hit100_nt": hit_med.get(f),
            "delta_trad_hit100": (trad_med.get(f) - hit_med.get(f)) if trad_med.get(f) is not None and hit_med.get(f) is not None else None,
        })

    # tail_score: predict tradable
    tail_score = build_weighted_score(
        df, df["split"] == "train", TAIL_FEATURES, "label_tradable",
        invert=set(),  # direction auto from univariate
    )
    # risk_score: predict bad|fatal — higher risk_score = more bad
    labeled_train = df["split"] == "train"
    df["label_bad_or_fatal"] = ((df["label_bad"] == 1) | (df["label_fatal"] == 1)).astype(int)
    risk_score = build_weighted_score(
        df, labeled_train, RISK_FEATURES + ["sell_buy_ratio_15"], "label_bad_or_fatal",
        invert={"unique_buyers_15"},  # low buyers = higher risk
    )

    df["tail_score"] = tail_score
    df["risk_score"] = risk_score

    # lambda scan on full universe (4154)
    lambda_rows = []
    best_cfg = None
    best_key = (-1, 999)
    labeled_df = df[df["full_ret_15_90"].notna()].copy()
    e1_top5 = eval_score(df, df["score"].values if "score" in df.columns else np.zeros(len(df)), [0.05])
    e1_top5_m = next(x for x in e1_top5 if x["bucket"] == "top_5pct")

    for lam in LAMBDAS:
        df["final_score"] = df["tail_score"] - lam * df["risk_score"]
        buckets = eval_score(df, df["final_score"].values, TOP_FRACS)
        for b in buckets:
            lambda_rows.append({"lambda": lam, **b})
        top5 = next(x for x in buckets if x["bucket"] == "top_5pct")
        key = (top5.get("p_tradable") or 0, -(top5.get("p_bad") or 1), top5.get("median_ret") or -999)
        if key > best_key:
            best_key = key
            best_cfg = {"lambda": lam, "bucket": "top_5pct", **top5}

    # sweet spot
    ss_mask = sweet_spot_mask(df, train if len(train) > 30 else labeled)
    ss_labeled = df[ss_mask & df["full_ret_15_90"].notna()]
    ss_metrics = bucket_metrics(ss_labeled) if len(ss_labeled) else {"n": int(ss_mask.sum()), "n_labeled": 0}

    # OOS on test with best lambda
    best_lam = best_cfg["lambda"] if best_cfg else 1.5
    test_df = df[df["split"] == "test"].copy()
    test_df["final_score"] = test_df["tail_score"] - best_lam * test_df["risk_score"]
    test_labeled = test_df[test_df["full_ret_15_90"].notna()]
    test_buckets = eval_score(test_labeled, test_labeled["final_score"].values, [0.05]) if len(test_labeled) else []
    test_top5 = next((x for x in test_buckets if x["bucket"] == "top_5pct"), {})
    test_e1 = eval_score(test_labeled, test_labeled["score"].values if "score" in test_labeled.columns else np.zeros(len(test_labeled)), [0.05])
    test_e1_top5 = next((x for x in test_e1 if x["bucket"] == "top_5pct"), {})

    # daily split for best final_score top5 on full universe
    df["final_score"] = df["tail_score"] - best_lam * df["risk_score"]
    daily = []
    for day, g in df.groupby("day"):
        sc = g["final_score"].values
        b = eval_score(g, sc, [0.05])
        top5d = next(x for x in b if x["bucket"] == "top_5pct")
        daily.append({"day": day, **top5d})

    # Optional replay for promising subset
    rpc_used = 0
    replay_map = load_replay_map()
    candidate = ss_labeled
    meets = (
        ss_metrics.get("p_tradable", 0) >= 0.20
        and ss_metrics.get("p_bad", 1) <= 0.35
        and (ss_metrics.get("median_ret") or -1) > (e1_top5_m.get("median_ret") or -999)
        and (test_top5.get("p_bad") or 1) <= 0.50
    )
    if meets and not skip_replay:
        unreplayed = candidate[~candidate["mint"].isin(replay_map)]
        if len(unreplayed) > 0:
            print(f"Phase E1b: supplement replay for sweet spot n={len(unreplayed)}", flush=True)
            replay_map, rpc_used = replay_subset(unreplayed, replay_map)
            # refresh labels
            df["full_ret_15_90"] = df["mint"].map(replay_map).fillna(df["full_ret_15_90"])
            df = assign_labels(add_derived(df))
            ss_labeled = df[ss_mask & df["full_ret_15_90"].notna()]
            ss_metrics = bucket_metrics(ss_labeled)

    # verdict
    e1_med = e1_top5_m.get("median_ret") or -0.445
    e1_pbad = e1_top5_m.get("p_bad") or 0.52
    ss_ptrad = ss_metrics.get("p_tradable") or 0
    ss_pbad = ss_metrics.get("p_bad") or 1
    ss_med = ss_metrics.get("median_ret") or -999
    best_top5 = best_cfg or {}
    pass_risk = ss_ptrad >= 0.20 and ss_pbad <= 0.35 and ss_med > e1_med
    verdict = (
        f"**{'PASS' if pass_risk else 'PARTIAL/NO'} tail-risk split.** "
        f"关键发现：tradable progress_15 median **{trad_med.get('progress_15', 0):.2f}** "
        f"< hit100_nontradable **{hit_med.get('progress_15', 0):.2f}** — 右尾需「热度够但未打满」。"
        f" best final_score λ={best_lam} top5 p(trad)={_fp(best_top5.get('p_tradable'))} "
        f"p(bad)={_fp(best_top5.get('p_bad'))} median={_fp(best_top5.get('median_ret'), True)} "
        f"(E1 top5 median={_fp(e1_med, True)}, p_bad={_fp(e1_pbad)}). "
        f"甜区 n={ss_metrics.get('n_labeled',0)} p(trad)={_fp(ss_ptrad)} p(bad)={_fp(ss_pbad)} median={_fp(ss_med, True)}."
    )

    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "n_universe": len(df),
        "n_labeled": int(labeled.shape[0]),
        "n_tradable": int(labeled["is_tradable_group"].sum()),
        "n_bad": int((labeled["tail_class"] == "bad").sum()),
        "n_fatal": int((labeled["tail_class"] == "fatal").sum()),
        "compare_tradable_vs_bad": cmp_bad,
        "compare_tradable_vs_fatal": cmp_fatal,
        "compare_tradable_vs_hit100": cmp_hit,
        "compare_pivot": pivot,
        "lambda_scan": lambda_rows,
        "best_config": best_cfg,
        "sweet_spot": {**ss_metrics, "n_universe_hit": int(ss_mask.sum())},
        "daily_final_top5": daily,
        "oos": {
            "best_lambda": best_lam,
            "best_bucket": "top_5pct",
            **test_top5,
            "e1_top5_p_tradable": test_e1_top5.get("p_tradable"),
            "e1_top5_p_bad": test_e1_top5.get("p_bad"),
            "e1_top5_median": test_e1_top5.get("median_ret"),
        },
        "e1_top5_baseline": e1_top5_m,
        "rpc_supplement": rpc_used,
        "verdict": verdict,
    }

    df.to_parquet(E1B_ROOT / "features_scored.parquet", index=False)
    save_json(E1B_ROOT / "summary.json", summary)
    write_readout(OUT_DIR / "_pump_lowcost_phase_e1b_tail_risk_split_readout.md", summary)
    print(f"Phase E1b done: best_λ={best_lam} sweet_spot_labeled={ss_metrics.get('n_labeled',0)}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args()
    run_phase_e1b(skip_replay=args.skip_replay)
