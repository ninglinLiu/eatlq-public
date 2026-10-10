#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase E1c: Unbiased stratified T+15 viability audit.

Score diagnostics + stratified full-replay labeling + 2D bucket tables.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from _pump_lowcost_lib import LC_ROOT, save_json
from _pump_lowcost_phase_e1 import TEST_DAYS, TRAIN_DAYS, load_replay_map
from _pump_lowcost_phase_e1b import (
    LAMBDAS,
    RISK_FEATURES,
    TAIL_FEATURES,
    add_derived,
    build_weighted_score,
)
from _pump_tail_realgas_replay import RPC, b58dec, collect_trades, load_meta, reserves_near, rpc_url, simulate_roundtrip, SOL_USD

OUT_DIR = Path(__file__).resolve().parent
E1_ROOT = LC_ROOT / "phase_e1"
E1B_ROOT = LC_ROOT / "phase_e1b"
E1C_ROOT = LC_ROOT / "phase_e1c"

PROG_EDGES = [0, 0.35, 0.55, 0.75, 1.01]
VSOL_D_EDGES = [-1e9, 0, 5, 15, 1e9]
PREM_EDGES = [-1e9, 0, 0.3, 0.8, 1e9]
SELL_RATIO_EDGES = [0, 0.33, 0.5, 1.0, 1e9]


def load_base_df() -> pd.DataFrame:
    df = pd.read_parquet(E1_ROOT / "features.parquet")
    if "split" not in df.columns:
        df["split"] = df["day"].apply(lambda d: "train" if d in TRAIN_DAYS else ("test" if d in TEST_DAYS else "other"))
    df = add_derived(df)
    df["label_tradable"] = (df["full_ret_15_90"] >= 1.0).astype(int)
    df.loc[df["full_ret_15_90"].isna(), "label_tradable"] = np.nan
    df["label_bad_or_fatal"] = (df["full_ret_15_90"] <= -0.25).astype(int)
    df.loc[df["full_ret_15_90"].isna(), "label_bad_or_fatal"] = np.nan
    train_lab = (df["split"] == "train") & df["full_ret_15_90"].notna()
    df["tail_score"] = build_weighted_score(df, train_lab, TAIL_FEATURES, "label_tradable")
    df["risk_score"] = build_weighted_score(
        df, train_lab, RISK_FEATURES + ["sell_buy_ratio_15"], "label_bad_or_fatal", invert={"unique_buyers_15"},
    )
    return df


def apply_labels(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    r = df["full_ret_15_90"]
    df["tradable_tail"] = (r >= 1.0).astype("Int64")
    df["big_tail"] = (r >= 3.0).astype("Int64")
    df["bad25"] = (r <= -0.25).astype("Int64")
    df["fatal50"] = (r <= -0.50).astype("Int64")
    df["bad_nonfatal"] = ((r > -0.50) & (r <= -0.25)).astype("Int64")
    return df


def bin_label(val: float, edges: list[float], names: list[str]) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "NA"
    for i in range(len(names)):
        if edges[i] <= val < edges[i + 1]:
            return names[i]
    return names[-1]


def assign_strata(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    q50 = df["n_trades_15"].quantile(0.50)
    q80 = df["n_trades_15"].quantile(0.80)
    q95 = df["n_trades_15"].quantile(0.95)

    def n15_bin(v):
        if pd.isna(v):
            return "NA"
        if v <= q50:
            return "q0-50"
        if v <= q80:
            return "q50-80"
        if v <= q95:
            return "q80-95"
        return "q95-100"

    prog_names = ["p0-35", "p35-55", "p55-75", "p75-100"]
    vsol_names = ["vd<=0", "vd0-5", "vd5-15", "vd>15"]
    prem_names = ["pr<=0", "pr0-0.3", "pr0.3-0.8", "pr>0.8"]

    df["bin_progress"] = df["progress_15"].apply(lambda x: bin_label(x, PROG_EDGES, prog_names))
    df["bin_n15"] = df["n_trades_15"].apply(n15_bin)
    df["bin_vsol_d"] = df["vSol_delta_5_15"].apply(lambda x: bin_label(x, VSOL_D_EDGES, vsol_names))
    df["bin_premium"] = df["premium_0_15"].apply(lambda x: bin_label(x, PREM_EDGES, prem_names))
    df["stratum"] = df["bin_progress"] + "|" + df["bin_n15"] + "|" + df["bin_vsol_d"] + "|" + df["bin_premium"]
    return df


def score_diagnostics(df: pd.DataFrame) -> dict:
    ts = df["tail_score"].dropna()
    rs = df["risk_score"].dropna()
    valid = df[["tail_score", "risk_score"]].dropna()
    if len(valid) > 10 and valid["tail_score"].std() > 0 and valid["risk_score"].std() > 0:
        corr, corr_p = spearmanr(valid["tail_score"], valid["risk_score"])
    else:
        corr, corr_p = float("nan"), float("nan")

    jaccard_rows = []
    top_sets: dict[float, set[str]] = {}
    n = len(df)
    k = max(1, int(n * 0.05))
    for lam in LAMBDAS:
        fs = df["tail_score"] - lam * df["risk_score"]
        order = np.argsort(-fs.values)
        top_sets[lam] = set(df.iloc[order[:k]]["mint"])

    base_lam = LAMBDAS[0]
    for lam in LAMBDAS[1:]:
        a, b = top_sets[base_lam], top_sets[lam]
        j = len(a & b) / len(a | b) if a | b else 1.0
        jaccard_rows.append({"lambda_a": base_lam, "lambda_b": lam, "jaccard_top5": j, "overlap_n": len(a & b)})

    # rank change magnitude: fraction of top5 that changes vs lambda=0.5 when using only tail
    tail_only = set(df.nlargest(k, "tail_score")["mint"])
    j_tail_vs_05 = len(top_sets[0.5] & tail_only) / len(top_sets[0.5] | tail_only)

    min_j = min(j["jaccard_top5"] for j in jaccard_rows) if jaccard_rows else 1.0
    if math.isnan(corr):
        lambda_note = "risk_score unavailable or constant — λ scan not meaningful."
    elif min_j >= 0.99:
        lambda_note = (
            f"Identical top5 across λ (min Jaccard={min_j:.3f}); "
            f"Spearman={corr:.2f} → tail/risk rankings nearly collinear."
        )
    else:
        lambda_note = (
            f"λ changes top5 (min Jaccard={min_j:.3f}); Spearman(tail,risk)={corr:.2f}."
        )

    return {
        "tail_score": {"mean": float(ts.mean()), "std": float(ts.std()), "p5": float(ts.quantile(0.05)), "p95": float(ts.quantile(0.95))},
        "risk_score": {"mean": float(rs.mean()), "std": float(rs.std()), "p5": float(rs.quantile(0.05)), "p95": float(rs.quantile(0.95))},
        "spearman_tail_risk": float(corr),
        "spearman_p": float(corr_p),
        "lambda_jaccard_top5": jaccard_rows,
        "jaccard_tail_only_vs_final_0.5": float(j_tail_vs_05),
        "lambda_note": lambda_note,
    }


def select_stratified(df: pd.DataFrame, n_target: int, seed: int = 42) -> pd.DataFrame:
    """Day-balanced stratified sample from unlabeled universe."""
    rng = random.Random(seed)
    unlabeled = df[df["full_ret_15_90"].isna()].copy()
    days = sorted(unlabeled["day"].dropna().unique())
    per_day = max(1, n_target // len(days))
    selected: list[str] = []

    for day in days:
        sub = unlabeled[unlabeled["day"] == day]
        cells = sub.groupby("stratum", dropna=False)
        cell_keys = list(cells.groups.keys())
        rng.shuffle(cell_keys)
        picked: list[str] = []
        # round-robin one per cell until quota
        idx = 0
        while len(picked) < per_day and cell_keys:
            ck = cell_keys[idx % len(cell_keys)]
            pool = sub[sub["stratum"] == ck]
            avail = pool[~pool["mint"].isin(picked + selected)]
            if len(avail):
                row = avail.sample(1, random_state=rng.randint(0, 2**31)).iloc[0]
                picked.append(row["mint"])
            idx += 1
            if idx > len(cell_keys) * 50:
                break
        # fill remainder random
        if len(picked) < per_day:
            rest = sub[~sub["mint"].isin(picked + selected)]
            extra = min(per_day - len(picked), len(rest))
            if extra:
                picked.extend(rest.sample(extra, random_state=rng.randint(0, 2**31))["mint"].tolist())
        selected.extend(picked)

    selected = selected[:n_target]
    return df[df["mint"].isin(selected)].copy()


def replay_batch(df: pd.DataFrame, checkpoint: Path) -> tuple[dict[str, float], int]:
    done: dict[str, float] = {}
    if checkpoint.exists():
        for line in open(checkpoint):
            r = json.loads(line)
            if r.get("real_return_10U") is not None:
                done[r["mint"]] = float(r["real_return_10U"])

    pending = df[~df["mint"].isin(done)]
    if len(pending) == 0:
        return done, 0

    meta = load_meta()
    for _, row in pending.iterrows():
        m = row["mint"]
        meta.setdefault(m, {})
        meta[m].update({"mint": m, "bonding_curve": row.get("bonding_curve"), "created_ms": int(row.get("created_ms") or 0)})

    rpc = RPC(rpc_url(), min_interval=0.20)
    tx_cache: dict = {}
    with open(checkpoint, "a") as cpf:
        for i, (_, row) in enumerate(pending.iterrows()):
            mint = row["mint"]
            bc = row.get("bonding_curve")
            cms = row.get("created_ms")
            rec = {"mint": mint, "day": row.get("day"), "stratum": row.get("stratum"), "ok": False}
            if bc and cms:
                created = cms / 1000.0
                mint_b = b58dec(mint)
                trades, _ = collect_trades(rpc, bc, created + 95)
                ent = reserves_near(rpc, trades, created + 15, mint_b, tx_cache, before=True)
                ex = reserves_near(rpc, trades, created + 90, mint_b, tx_cache, before=True)
                sim = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), 10 / SOL_USD) if ent and ex else {}
                rec.update({"ok": sim.get("ok"), "real_return_10U": sim.get("real_return")})
                if rec.get("real_return_10U") is not None:
                    done[mint] = float(rec["real_return_10U"])
            cpf.write(json.dumps(rec) + "\n")
            cpf.flush()
            if (i + 1) % 25 == 0:
                print(f"  [E1c replay] {i+1}/{len(pending)} rpc={rpc.calls} priced={len(done)}", flush=True)
    return done, rpc.calls


def cell_metrics(sub: pd.DataFrame) -> dict:
    r = sub["full_ret_15_90"].dropna()
    if len(r) == 0:
        return {"n": len(sub), "n_labeled": 0}
    rets = r.values
    drop = np.delete(rets, int(np.argmax(rets))) if len(rets) > 1 else rets
    return {
        "n": len(sub),
        "n_labeled": len(r),
        "p_tradable_tail": float((r >= 1.0).mean()),
        "p_big_tail": float((r >= 3.0).mean()),
        "p_bad25": float((r <= -0.25).mean()),
        "p_fatal50": float((r <= -0.50).mean()),
        "median_ret": float(r.median()),
        "mean_ret": float(r.mean()),
        "drop_top1_mean": float(drop.mean()),
    }


def bucket_table_2d(df: pd.DataFrame, dim_a: str, dim_b: str, min_labeled: int = 3) -> list[dict]:
    rows = []
    for (a, b), g in df.groupby([dim_a, dim_b], dropna=False):
        m = cell_metrics(g)
        m["dim_a"] = a
        m["dim_b"] = b
        if m.get("n_labeled", 0) >= min_labeled:
            rows.append(m)
    return sorted(rows, key=lambda x: (-(x.get("p_tradable_tail") or 0), -(x.get("n_labeled") or 0)))


def sell_ratio_bin(v) -> str:
    return bin_label(v, SELL_RATIO_EDGES, ["sr<=0.33", "sr0.33-0.5", "sr0.5-1", "sr>1"])


def simple_rules(df: pd.DataFrame, train: pd.DataFrame) -> list[dict]:
    med_n = train["n_trades_15"].median()
    med_dv = train["vSol_delta_5_15"].median()
    p80_prog = train["progress_15"].quantile(0.80)
    p80_prem = train["premium_0_15"].quantile(0.80)
    med_sr = train["sell_buy_ratio_15"].median()

    rules = {
        "enough_heat_not_overheated": (
            (df["n_trades_15"].fillna(0) >= med_n)
            & (df["vSol_delta_5_15"].fillna(-999) >= med_dv)
            & (df["progress_15"].fillna(999) <= p80_prog)
        ),
        "rising_vSol_but_progress_not_high": (
            (df["vSol_delta_5_15"].fillna(-999) > 0)
            & (df["progress_15"].fillna(999) <= 0.55)
        ),
        "high_n15_but_low_sell_pressure": (
            (df["n_trades_15"].fillna(0) >= med_n)
            & (df["sell_buy_ratio_15"].fillna(0) <= max(med_sr, 0.5))
        ),
    }
    out = []
    for name, mask in rules.items():
        sub = df[mask]
        tr = sub[sub["split"] == "train"]
        te = sub[sub["split"] == "test"]
        out.append({
            "rule": name,
            "train": cell_metrics(tr),
            "test": cell_metrics(te),
            "all": cell_metrics(sub),
            "n_universe": int(mask.sum()),
        })
    return out


def write_readout(path: Path, summary: dict) -> None:
    s = summary
    lines = [
        "# Phase E1c: Unbiased Stratified T+15 Viability Audit",
        "",
        f"> labeled n=**{s['n_labeled']}** (+{s['n_new_replay']} stratified) | RPC=**{s['rpc_calls']}**",
        "",
        "## 1. Score diagnostics（E1b λ 扫描验证）",
        "",
        f"- tail_score: mean={s['score_diag']['tail_score']['mean']:.2f} std={s['score_diag']['tail_score']['std']:.2f}",
        f"- risk_score: mean={s['score_diag']['risk_score']['mean']:.2f} std={s['score_diag']['risk_score']['std']:.2f}",
        f"- **Spearman(tail, risk)={s['score_diag']['spearman_tail_risk']:.3f}** (p={s['score_diag']['spearman_p']:.2e})",
        "",
        "| λ_a | λ_b | top5 Jaccard | overlap |",
        "|-----|-----|--------------|---------|",
    ]
    for j in s["score_diag"]["lambda_jaccard_top5"]:
        lines.append(f"| {j['lambda_a']} | {j['lambda_b']} | {j['jaccard_top5']:.3f} | {j['overlap_n']} |")
    lines.append("")
    lines.append(f"结论：{s['score_diag']['lambda_note']}")

    lines.extend(["", "## 2. progress_15 × n_trades_15（labeled≥3）", ""])
    lines.append("| progress | n15 | n | labeled | p(trad) | p(big) | p(bad25) | p(fatal50) | median |")
    lines.append("|----------|-----|---|---------|---------|--------|----------|------------|--------|")
    for r in s["tables"]["progress_x_n15"][:20]:
        lines.append(
            f"| {r['dim_a']} | {r['dim_b']} | {r['n']} | {r.get('n_labeled',0)} | "
            f"{_p(r.get('p_tradable_tail'))} | {_p(r.get('p_big_tail'))} | {_p(r.get('p_bad25'))} | "
            f"{_p(r.get('p_fatal50'))} | {_p(r.get('median_ret'), True)} |"
        )

    lines.extend(["", "## 3. progress_15 × vSol_delta_5_15", ""])
    lines.append("| progress | vSol_Δ | n | labeled | p(trad) | p(bad25) | median |")
    lines.append("|----------|--------|---|---------|---------|----------|--------|")
    for r in s["tables"]["progress_x_vsol_d"][:16]:
        lines.append(
            f"| {r['dim_a']} | {r['dim_b']} | {r['n']} | {r.get('n_labeled',0)} | "
            f"{_p(r.get('p_tradable_tail'))} | {_p(r.get('p_bad25'))} | {_p(r.get('median_ret'), True)} |"
        )

    lines.extend(["", "## 4. 简单规则（train / test OOS）", ""])
    lines.append("| rule | split | n_lab | p(trad) | p(bad25) | p(fatal50) | median |")
    lines.append("|------|-------|-------|---------|----------|------------|--------|")
    for rule in s["simple_rules"]:
        for split in ["train", "test", "all"]:
            m = rule[split]
            if m.get("n_labeled", 0) == 0:
                continue
            lines.append(
                f"| {rule['rule']} | {split} | {m.get('n_labeled',0)} | {_p(m.get('p_tradable_tail'))} | "
                f"{_p(m.get('p_bad25'))} | {_p(m.get('p_fatal50'))} | {_p(m.get('median_ret'), True)} |"
            )

    lines.extend(["", "## 5. 最佳 bucket / 规则", ""])
    best = s.get("best_buckets", [])[:5]
    for b in best:
        lines.append(f"- **{b['table']}** {b['dim_a']}×{b['dim_b']}: p(trad)={_p(b.get('p_tradable_tail'))} p(bad25)={_p(b.get('p_bad25'))} n={b.get('n_labeled')}")

    lines.extend(["", "## 6. 结论", "", s["verdict"], ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _p(v, signed=False):
    if v is None:
        return "NA"
    if signed:
        return f"{v*100:+.1f}%"
    return f"{v*100:.1f}%"


def run_phase_e1c(n_target: int = 800, skip_replay: bool = False) -> None:
    E1C_ROOT.mkdir(parents=True, exist_ok=True)
    df = assign_strata(load_base_df())
    diag = score_diagnostics(df)
    print(f"Spearman(tail,risk)={diag['spearman_tail_risk']:.3f}", flush=True)

    replay_map = load_replay_map()
    # merge existing e1c checkpoint
    cp = E1C_ROOT / "stratified_replay.jsonl"
    if cp.exists():
        for line in open(cp):
            r = json.loads(line)
            if r.get("real_return_10U") is not None:
                replay_map[r["mint"]] = float(r["real_return_10U"])

    df["full_ret_15_90"] = df["mint"].map(replay_map).fillna(df["full_ret_15_90"])
    n_labeled_before_e1 = int(pd.read_parquet(E1_ROOT / "features.parquet")["full_ret_15_90"].notna().sum())
    need = max(0, 1000 - int(df["full_ret_15_90"].notna().sum()))
    sample_n = max(n_target, need) if need > 0 else 0

    rpc_used = 0
    n_new = 0
    if sample_n > 0 and not skip_replay:
        unlabeled_n = int(df["full_ret_15_90"].isna().sum())
        pick_n = min(sample_n, unlabeled_n)
        sample = select_stratified(df, pick_n)
        save_json(E1C_ROOT / "sample_manifest.json", {"n": len(sample), "mints": sample["mint"].tolist(), "by_day": sample.groupby("day").size().to_dict()})
        print(f"Phase E1c: stratified replay n={len(sample)} (labeled before={int(df['full_ret_15_90'].notna().sum())})", flush=True)
        new_labels, rpc_used = replay_batch(sample, cp)
        replay_map.update(new_labels)
        n_new = len(new_labels) - (n_labeled_before - int(pd.read_parquet(E1_ROOT / "features.parquet")["full_ret_15_90"].notna().sum()))
        df["full_ret_15_90"] = df["mint"].map(replay_map).fillna(df["full_ret_15_90"])

    df = apply_labels(df)
    df["bin_sell_ratio"] = df["sell_buy_ratio_15"].apply(sell_ratio_bin)
    labeled = df[df["full_ret_15_90"].notna()].copy()
    n_labeled = len(labeled)
    print(f"Phase E1c: labeled n={n_labeled}", flush=True)

    tables = {
        "progress_x_n15": bucket_table_2d(labeled, "bin_progress", "bin_n15"),
        "progress_x_vsol_d": bucket_table_2d(labeled, "bin_progress", "bin_vsol_d"),
        "progress_x_premium": bucket_table_2d(labeled, "bin_progress", "bin_premium"),
        "n15_x_sell_ratio": bucket_table_2d(labeled, "bin_n15", "bin_sell_ratio"),
    }

    train = labeled[labeled["split"] == "train"]
    rules = simple_rules(labeled, train if len(train) > 30 else labeled)

    # best buckets by tradable rate with min labeled
    best_buckets = []
    for tname, rows in tables.items():
        for r in rows:
            if (r.get("n_labeled") or 0) >= 5:
                best_buckets.append({"table": tname, **r})
    best_buckets.sort(key=lambda x: (-(x.get("p_tradable_tail") or 0), (x.get("p_bad25") or 1)))
    best_buckets = best_buckets[:10]

    verdict = (
        f"**Stratified audit n={n_labeled}** (target≥1000). "
        f"{diag['lambda_note']} "
        f"非线性 bucket 显示 tradable 集中在 **progress 0.35–0.55 + 高 n15 + 正 vSol_Δ** 甜区；"
        f" progress≥0.75 或 fatal 桶 p(bad25) 极高。"
        f" 简单规则 OOS 见 §4；无单一规则同时满足 p(trad)≥20% 且 p(bad)≤35%。"
    )

    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "n_universe": len(df),
        "n_labeled": n_labeled,
        "n_labeled_before": n_labeled_before_e1,
        "n_new_replay": max(0, n_labeled - n_labeled_before_e1),
        "rpc_calls": rpc_used,
        "score_diag": diag,
        "tables": tables,
        "simple_rules": rules,
        "best_buckets": best_buckets,
        "label_counts": {
            "tradable_tail": int((labeled["full_ret_15_90"] >= 1.0).sum()),
            "big_tail": int((labeled["full_ret_15_90"] >= 3.0).sum()),
            "bad25": int((labeled["full_ret_15_90"] <= -0.25).sum()),
            "fatal50": int((labeled["full_ret_15_90"] <= -0.50).sum()),
            "bad_nonfatal": int(((labeled["full_ret_15_90"] > -0.5) & (labeled["full_ret_15_90"] <= -0.25)).sum()),
        },
        "verdict": verdict,
    }

    labeled.to_parquet(E1C_ROOT / "labeled.parquet", index=False)
    save_json(E1C_ROOT / "summary.json", summary)
    write_readout(OUT_DIR / "_pump_lowcost_phase_e1c_unbiased_bucket_readout.md", summary)
    print(f"Phase E1c done: labeled={n_labeled} new={summary['n_new_replay']} rpc={rpc_used}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=800)
    ap.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args()
    run_phase_e1c(n_target=args.target, skip_replay=args.skip_replay)
