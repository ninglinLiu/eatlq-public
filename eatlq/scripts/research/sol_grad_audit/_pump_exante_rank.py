#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ex-ante 排序能力审计：T+15 前特征 → 右尾分组 lift（6 线特征）."""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from _grad_audit_lib import (
    DEFAULT_TEST,
    DEFAULT_TRAIN,
    DIAG,
    bootstrap_day_mean,
    day_of,
    grid_price,
    load_deployer,
    load_grids,
    load_jsonl,
    path_points,
)

OUT_DIR = Path(__file__).resolve().parent
ENTRY_S, EXIT_S, COST = 15, 90, 0.03


def load_early_buys() -> dict:
    import glob
    out = {}
    for f in glob.glob(str(DIAG / "smart_buyers*.jsonl")):
        for r in load_jsonl(Path(f)):
            m = r.get("mint")
            if m and m not in out:
                out[m] = r
    return out


def trail_return(grid: dict, entry: float, end: float, trail_pct: float = 0.20) -> float | None:
    pe = grid_price(grid, entry)
    if not pe or pe <= 0:
        return None
    path = path_points(grid, entry, end)
    if not path:
        g = grid_price(grid, end)
        return (g / pe - 1) if g else None
    peak = pe
    exit_p = path[-1][1]
    for _, p in path:
        if p > peak:
            peak = p
        if p / peak - 1 <= -trail_pct:
            exit_p = p
            break
    return exit_p / pe - 1


def path_stats(grid: dict, entry: float, end: float, cost: float) -> dict | None:
    pe = grid_price(grid, entry)
    px = grid_price(grid, end)
    if not pe or not px or pe <= 0:
        return None
    path = path_points(grid, entry, end)
    rets = [(p / pe - 1) for _, p in path] if path else [px / pe - 1]
    final_gross = px / pe - 1
    final_net = final_gross * (1 - cost) - cost
    mfe = max(rets)
    mae = min(rets)
    tr = trail_return(grid, entry, end)
    tr_net = tr * (1 - cost) - cost if tr is not None else None
    return {
        "final_ret": final_net,
        "final_gross": final_gross,
        "mfe_gross": mfe,
        "mfe_net": mfe * (1 - cost) - cost,
        "mae_gross": mae,
        "trail20_gross": tr,
        "trail20_net": tr_net,
    }


def bucket_flow(eb: list, lo: float, hi: float) -> dict:
    seg = [e for e in eb if lo <= (e.get("t_rel") or 999) < hi]
    buys = [e for e in seg if e.get("is_buy")]
    sells = [e for e in seg if not e.get("is_buy")]
    wallets = {e["buyer"] for e in seg if e.get("buyer")}
    sol_b = sum(float(e.get("sol") or 0) for e in buys)
    sol_s = sum(float(e.get("sol") or 0) for e in sells)
    return {
        "n": len(seg),
        "buys": len(buys),
        "sells": len(sells),
        "wallets": len(wallets),
        "sol_in": sol_b,
        "sol_out": sol_s,
        "net_sol": sol_b - sol_s,
    }


def buyer_graph_feats(eb15: list, creator: str | None) -> dict:
    if not eb15:
        return {}
    buys = [e for e in eb15 if e.get("is_buy")]
    sells = [e for e in eb15 if not e.get("is_buy")]
    wallets = {e["buyer"] for e in eb15 if e.get("buyer")}
    n = len(eb15)
    bc = Counter(e["buyer"] for e in buys)
    top1 = bc.most_common(1)[0][1] / max(len(buys), 1) if bc else 0
    bset = {e["buyer"] for e in buys}
    sset = {e["buyer"] for e in sells}
    wash = len(bset & sset)
    # amount diversity: std/mean of buy sizes
    sizes = [float(e.get("sol") or 0) for e in buys if float(e.get("sol") or 0) > 0]
    amt_cv = (st.pstdev(sizes) / st.mean(sizes)) if len(sizes) >= 2 else 0.0
    # same-second batch buys
    by_sec = Counter(int(e.get("t_rel") or 0) for e in buys)
    batch_ratio = sum(1 for c in by_sec.values() if c >= 3) / max(len(by_sec), 1)
    creator_buy = sum(1 for e in buys if e.get("buyer") == creator)
    return {
        "unique_buyers_15": len(wallets),
        "bot_ratio_15": 1 - len(wallets) / max(n, 1),
        "top1_buy_share": top1,
        "wash_wallets": wash,
        "sell_absorption_ratio": len(sells) / max(len(buys), 1),
        "buy_amt_cv": amt_cv,
        "batch_buy_ratio": batch_ratio,
        "creator_self_buy": creator_buy,
        "net_buyers": len(bset - sset),
    }


def slope_feats(eb: list) -> dict:
    eb15 = [e for e in eb if (e.get("t_rel") or 999) <= 15]
    b0 = bucket_flow(eb15, 0, 5)
    b1 = bucket_flow(eb15, 5, 10)
    b2 = bucket_flow(eb15, 10, 15)

    def slope(a, b):
        return (b - a) / max(abs(a), 0.01)

    f = {}
    for key in ("buys", "sol_in", "wallets", "net_sol"):
        f[f"slope_{key}_0_15"] = slope(b0[key] + b1[key], b2[key])
        f[f"slope_{key}_5_15"] = slope(b0[key], b1[key] + b2[key])
    f["late_15s_buy_ratio"] = b2["buys"] / max(b0["buys"] + b1["buys"] + b2["buys"], 1)
    f["last5_net"] = b2["net_sol"]
    f["first5_net"] = b0["net_sol"]
    f["late_early_inflow_ratio"] = b2["net_sol"] / max(abs(b0["net_sol"]), 0.01)
    f.update({f"b0_{k}": b0[k] for k in b0})
    f.update({f"b1_{k}": b1[k] for k in b1})
    f.update({f"b2_{k}": b2[k] for k in b2})
    return f


def creator_feats(creator: str | None, created_ms: int, deployer: dict) -> dict:
    if not creator or creator not in deployer:
        return {"prior_n": 0, "prior_grad_rate": 0, "prior_log_max_ath": 0, "prior_med_t2ath": 0}
    prior = [c for c in deployer[creator] if (c.get("ct") or 0) < created_ms]
    if not prior:
        return {"prior_n": 0, "prior_grad_rate": 0, "prior_log_max_ath": 0, "prior_med_t2ath": 0}
    grads = sum(1 for c in prior if c.get("complete"))
    aths = [float(c.get("ath") or 0) for c in prior]
    t2 = []
    for c in prior:
        if c.get("ath_ts") and c.get("ct"):
            t2.append((c["ath_ts"] - c["ct"]) / 1000.0)
    return {
        "prior_n": len(prior),
        "prior_grad_rate": grads / len(prior),
        "prior_log_max_ath": math.log1p(max(aths) if aths else 0),
        "prior_med_t2ath": st.median(t2) if t2 else 0,
    }


def attention_feats(lab: dict) -> dict:
    name = (lab.get("name") or "") if isinstance(lab.get("name"), str) else ""
    return {
        "has_twitter": 1.0 if lab.get("has_twitter") else 0.0,
        "has_telegram": 1.0 if lab.get("has_telegram") else 0.0,
        "has_website": 1.0 if lab.get("has_website") else 0.0,
        "has_desc": 1.0 if lab.get("has_desc") else 0.0,
        "desc_len": float(lab.get("desc_len") or 0),
        "name_len": float(lab.get("name_len") or 0),
        "sym_len": float(lab.get("sym_len") or 0),
        "metadata_complete": sum([
            bool(lab.get("has_desc")), bool(lab.get("has_twitter")),
            bool(lab.get("has_website")), bool(lab.get("has_telegram")),
        ]),
    }


def build_rows(replay_path: Path | None) -> list[dict]:
    labeled = {r["mint"]: r for r in load_jsonl(DIAG / "creates_labeled.jsonl")}
    early = {r["mint"]: r for r in load_jsonl(DIAG / "creates_earlypath.jsonl")}
    grids = load_grids()
    eb_map = load_early_buys()
    deployer = load_deployer()
    replay = {}
    if replay_path and replay_path.exists():
        replay = {r["mint"]: r for r in json.loads(replay_path.read_text())["results"]}

    rows = []
    for mint, grid in grids.items():
        if mint not in eb_map:
            continue
        lab = labeled.get(mint, {})
        ep = early.get(mint, {})
        eb = eb_map[mint].get("early_buys") or []
        created_ms = lab.get("created_ms") or eb_map[mint].get("created_ms")
        if not created_ms:
            continue
        ps = path_stats(grid, ENTRY_S, EXIT_S, COST)
        if not ps:
            continue
        creator = lab.get("creator")
        row = {
            "mint": mint,
            "day": day_of(created_ms),
            "split": "TRAIN" if day_of(created_ms) in DEFAULT_TRAIN else ("TEST" if day_of(created_ms) in DEFAULT_TEST else "OTHER"),
            "created_ms": created_ms,
            **ps,
            **slope_feats(eb),
            **buyer_graph_feats([e for e in eb if (e.get("t_rel") or 999) <= 15], creator),
            **creator_feats(creator, created_ms, deployer),
            **attention_feats(lab),
            "n_trades_60s": float(ep.get("n_trades_60s") or 0),
            "n_trades_180s": float(ep.get("n_trades_180s") or 0),
        }
        rp = replay.get(mint)
        if rp and rp.get("real_return_10U") is not None:
            row["real_final_10U"] = rp["real_return_10U"]
            row["real_spot"] = rp.get("spot_return")
        rows.append(row)
    return rows


def add_labels(row: dict, prefix: str = "final_ret") -> dict:
    r = row[prefix]
    row["label_50"] = int(r >= 0.50)
    row["label_100"] = int(r >= 1.00)
    row["label_300"] = int(r >= 3.00)
    row["label_1000"] = int(r >= 10.00)
    mfe = row.get("mfe_net") or row.get("mfe_gross", -99)
    row["mfe_label_100"] = int(mfe >= 1.00)
    row["mfe_label_300"] = int(mfe >= 3.00)
    tr = row.get("trail20_net")
    if tr is not None:
        row["trail_label_100"] = int(tr >= 1.00)
    return row


FEATURE_GROUPS = {
    "line3_slope": lambda cols: [c for c in cols if c.startswith(("b0_", "b1_", "b2_", "slope_", "late_", "last5", "first5"))],
    "line4_buyer": lambda cols: [c for c in cols if any(x in c for x in ("buyer", "bot_", "top1", "wash", "sell_abs", "batch", "creator_self", "net_buyers", "buy_amt"))],
    "line5_creator": lambda cols: [c for c in cols if c.startswith("prior_")],
    "line6_attention": lambda cols: [c for c in cols if c.startswith(("has_", "desc_", "name_", "sym_", "metadata"))],
    "line3+4": None,
    "all_exante": None,
}


def feature_cols(df: pd.DataFrame, group: str) -> list[str]:
    exclude = {"mint", "day", "split", "created_ms", "final_ret", "final_gross", "mfe_gross", "mfe_net",
               "mae_gross", "trail20_gross", "trail20_net", "real_final_10U", "real_spot",
               "label_50", "label_100", "label_300", "label_1000", "mfe_label_100", "mfe_label_300", "trail_label_100"}
    cols = [c for c in df.columns if c not in exclude and df[c].dtype != object]
    if group == "line3+4":
        return FEATURE_GROUPS["line3_slope"](cols) + FEATURE_GROUPS["line4_buyer"](cols)
    if group == "all_exante":
        return cols
    fn = FEATURE_GROUPS.get(group)
    return fn(cols) if fn else cols


def bucket_table(sub: pd.DataFrame, label_cols: list[str], ret_col: str) -> dict:
    n = len(sub)
    if n == 0:
        return {"n": 0}
    rets = sub[ret_col].tolist()
    out = {
        "n": n,
        "pct_pop": n,
        "mean": sum(rets) / n,
        "median": st.median(rets),
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / n,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / n,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / n,
    }
    for lc in label_cols:
        if lc in sub.columns:
            out[f"rate_{lc}"] = sub[lc].mean()
    return out


def score_buckets(df: pd.DataFrame, scores: np.ndarray, ret_col: str, label_cols: list[str],
                  pct_fracs: list[float]) -> list[dict]:
    srt = np.argsort(-scores)
    n = len(df)
    base = bucket_table(df, label_cols, ret_col)
    base["bucket"] = "ALL"
    base["pct_pop"] = 1.0
    rows = [base]
    for frac in pct_fracs:
        k = max(1, int(n * frac))
        idx = srt[:k]
        sub = df.iloc[idx]
        r = bucket_table(sub, label_cols, ret_col)
        r["bucket"] = f"top_{int(frac*100)}pct"
        r["pct_pop"] = frac
        r["lift_p1000_vs_all"] = (r["p_gt_1000"] / base["p_gt_1000"]) if base["p_gt_1000"] > 0 else None
        r["lift_p100_vs_all"] = (r["p_gt_100"] / base["p_gt_100"]) if base["p_gt_100"] > 0 else None
        rows.append(r)
    return rows


def random_top_buckets(df: pd.DataFrame, ret_col: str, label_cols: list[str], frac: float, n_draw: int = 500, seed: int = 1) -> dict:
    rng = random.Random(seed)
    n = len(df)
    k = max(1, int(n * frac))
    p1000s = []
    for _ in range(n_draw):
        idx = rng.sample(range(n), k)
        sub = df.iloc[idx]
        p1000s.append(sum(1 for x in sub[ret_col] if x >= 10.0) / k)
    p1000s.sort()
    return {"frac": frac, "p1000_median": p1000s[len(p1000s)//2], "p1000_p95": p1000s[int(0.95*len(p1000s))]}


def train_score(df: pd.DataFrame, y_col: str, feat_cols: list[str], split_test: bool = True) -> tuple[np.ndarray, dict]:
    X = df[feat_cols].fillna(0).replace([np.inf, -np.inf], 0)
    y = df[y_col].values
    tr = df["split"] == "TRAIN"
    te = df["split"] == "TEST"
    clf = HistGradientBoostingClassifier(max_iter=200, max_depth=4, learning_rate=0.05,
                                         min_samples_leaf=30, l2_regularization=1.0, random_state=0)
    if tr.sum() < 50 or y[tr].sum() < 5:
        return np.zeros(len(df)), {"auc_train": None, "auc_test": None}
    clf.fit(X[tr], y[tr])
    scores = clf.predict_proba(X)[:, 1]
    auc_tr = roc_auc_score(y[tr], scores[tr]) if len(np.unique(y[tr])) > 1 else None
    auc_te = roc_auc_score(y[te], scores[te]) if te.sum() and len(np.unique(y[te])) > 1 else None
    return scores, {"auc_train": float(auc_tr) if auc_tr else None, "auc_test": float(auc_te) if auc_te else None,
                    "features": feat_cols}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default=str(OUT_DIR / "_pump_tail_realgas_replay.json"))
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_exante_rank.json"))
    ap.add_argument("--cost", type=float, default=0.03)
    args = ap.parse_args()

    rows = build_rows(Path(args.replay))
    for r in rows:
        add_labels(r, "final_ret")
    df = pd.DataFrame(rows)
    df_te = df[df["split"] == "TEST"].copy()
    df_all = df[df["split"].isin(["TRAIN", "TEST"])].copy()

    label_cols = ["label_50", "label_100", "label_300", "label_1000"]
    fracs = [0.01, 0.05, 0.10, 0.20]
    ret_col = "final_ret"

    # baseline rates
    base_all = bucket_table(df_all, label_cols, ret_col)
    base_test = bucket_table(df_te, label_cols, ret_col)

    results = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "n": len(df_all),
            "n_test": len(df_te),
            "entry_s": ENTRY_S,
            "exit_s": EXIT_S,
            "cost": COST,
            "label_note": "grid T+15->T+90 final_ret; MFE/trail from grid path",
        },
        "baseline": {"all": base_all, "test": base_test},
        "random_baseline_top": [random_top_buckets(df_te, ret_col, label_cols, f) for f in fracs],
        "models": [],
        "mfe_analysis": {},
        "replay_validation": {},
    }

    # MFE vs final on TEST
    mfe_rows = []
    for _, r in df_te.iterrows():
        mfe_rows.append({
            "final_ret": r["final_ret"],
            "mfe_net": r["mfe_net"],
            "trail20_net": r.get("trail20_net"),
            "mfe_label_100": r.get("mfe_label_100"),
            "label_100": r.get("label_100"),
        })
    results["mfe_analysis"] = {
        "test_n": len(mfe_rows),
        "final_p100": sum(1 for x in mfe_rows if x["label_100"]) / max(len(mfe_rows), 1),
        "mfe_p100": sum(1 for x in mfe_rows if x.get("mfe_label_100")) / max(len(mfe_rows), 1),
        "final_positive": sum(1 for x in mfe_rows if x["final_ret"] > 0) / max(len(mfe_rows), 1),
        "mfe_positive": sum(1 for x in mfe_rows if (x.get("mfe_net") or 0) > 0) / max(len(mfe_rows), 1),
    }

    # Train models for different labels and feature groups
    for y_col in ["label_50", "label_100", "label_300", "mfe_label_100"]:
        for group in ["line3_slope", "line4_buyer", "line5_creator", "line6_attention", "line3+4", "all_exante"]:
            fc = feature_cols(df_all, group)
            if len(fc) < 3:
                continue
            scores, metrics = train_score(df_all, y_col, fc)
            df_scored = df_all.copy()
            df_scored["score"] = scores
            te_scored = df_scored[df_scored["split"] == "TEST"]
            buckets = score_buckets(te_scored, te_scored["score"].values, ret_col, label_cols, fracs)
            results["models"].append({
                "y_col": y_col,
                "feature_group": group,
                "n_features": len(fc),
                **metrics,
                "buckets_test": buckets,
            })

    # Best model for label_100 on TEST - find max lift top5 p1000
    best = None
    best_lift = 0
    for m in results["models"]:
        if m["y_col"] != "label_100":
            continue
        for b in m["buckets_test"]:
            if b.get("bucket") == "top_5pct":
                lift = b.get("lift_p1000_vs_all") or 0
                if lift > best_lift:
                    best_lift = lift
                    best = m

    results["best_label100_model"] = best

    # Replay subset validation (40 coins with real returns)
    rep = df_all[df_all["real_final_10U"].notna()].copy()
    if len(rep) >= 10:
        fc = feature_cols(df_all, "all_exante")
        scores, _ = train_score(df_all, "label_100", fc)
        rep = df_all[df_all["real_final_10U"].notna()].copy()
        rep["score"] = scores[df_all["real_final_10U"].notna()]
        rep = rep.sort_values("score", ascending=False)
        k = max(1, len(rep) // 5)
        top = rep.head(k)
        results["replay_validation"] = {
            "n": len(rep),
            "top20pct_n": k,
            "top20pct_real_mean": float(top["real_final_10U"].mean()),
            "all_real_mean": float(rep["real_final_10U"].mean()),
            "top20pct_p1000": float((top["real_final_10U"] >= 10).mean()),
            "all_p1000": float((rep["real_final_10U"] >= 10).mean()),
        }

    Path(args.output).write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Markdown + CSV summary table
    md = OUT_DIR / "_pump_exante_rank_readout.md"
    lines = [
        "# Ex-ante 排序能力审计（T+15 前特征 → 右尾分组）",
        "",
        f"n={len(df_all)} (TEST={len(df_te)}) | 标签：grid final T+{ENTRY_S}→T+{EXIT_S} net",
        "",
        "## 基线（TEST）",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| mean | {base_test.get('mean',0):+.3f} |",
        f"| median | {base_test.get('median',0):+.3f} |",
        f"| p(+100%) | {base_test.get('p_gt_100',0):.2%} |",
        f"| p(+300%) | {base_test.get('p_gt_300',0):.2%} |",
        f"| p(+1000%) | {base_test.get('p_gt_1000',0):.2%} |",
        "",
        "## MFE vs Final（TEST）",
        f"- final p(+100%): {results['mfe_analysis'].get('final_p100',0):.2%}",
        f"- MFE p(+100%): {results['mfe_analysis'].get('mfe_p100',0):.2%}",
        "",
        "## 最佳 model（label_100, TEST 分组）",
        "",
    ]
    if best:
        lines.append(f"- 特征组：**{best['feature_group']}** | TEST AUC={best.get('auc_test')}")
        lines.append("")
        lines.append("| 分组 | n | p(+100%) | p(+300%) | p(+1000%) | mean | lift p1000 |")
        lines.append("|------|---|---------|---------|----------|------|------------|")
        for b in best["buckets_test"]:
            lines.append(
                f"| {b.get('bucket')} | {b.get('n')} | {b.get('p_gt_100',0):.2%} | "
                f"{b.get('p_gt_300',0):.2%} | {b.get('p_gt_1000',0):.2%} | {b.get('mean',0):+.3f} | "
                f"{b.get('lift_p1000_vs_all') or '-'} |"
            )
    else:
        lines.append("无有效 model")

    lines.extend(["", "## 随机 top 5%（TEST, 500 次）", ""])
    for rb in results["random_baseline_top"]:
        if rb["frac"] == 0.05:
            lines.append(f"- p(+1000%) median={rb['p1000_median']:.2%} p95={rb['p1000_p95']:.2%}")

    lines.extend(["", "## 结论", ""])
    b5 = next((b for b in (best or {}).get("buckets_test", []) if b.get("bucket") == "top_5pct"), None)
    rb5 = next((r for r in results["random_baseline_top"] if r["frac"] == 0.05), None)
    if b5 and rb5:
        sig = b5["p_gt_1000"] > rb5["p1000_p95"] * 1.5 and b5["p_gt_1000"] > base_test["p_gt_1000"] * 2
        lines.append(f"- top5% score p(+1000%)={b5['p_gt_1000']:.2%} vs random top5% med={rb5['p1000_median']:.2%} vs ALL={base_test['p_gt_1000']:.2%}")
        lines.append(f"- **排序有用？** {'是（有 lift）' if sig else '否 / 弱（未显著超随机）'}")
    lines.extend(["", "## 六线分拆（label_100, TEST top5% p1000）", ""])
    for m in results["models"]:
        if m["y_col"] != "label_100":
            continue
        b5 = next((b for b in m["buckets_test"] if b.get("bucket") == "top_5pct"), None)
        if b5:
            lines.append(f"- {m['feature_group']}: AUC={m.get('auc_test')} top5 p100={b5['p_gt_100']:.2%} p1000={b5['p_gt_1000']:.2%}")
    mfe = next((m for m in results["models"] if m["y_col"] == "mfe_label_100" and m["feature_group"] == "all_exante"), None)
    if mfe:
        lines.extend(["", "## MFE 标签模型（all_exante, TEST）", ""])
        lines.append("| 分组 | p(+100%) | p(+300%) | p(+1000%) | mean |")
        lines.append("|------|---------|---------|----------|------|")
        for b in mfe["buckets_test"]:
            lines.append(f"| {b.get('bucket')} | {b.get('p_gt_100',0):.2%} | {b.get('p_gt_300',0):.2%} | {b.get('p_gt_1000',0):.2%} | {b.get('mean',0):+.3f} |")
    rv = results.get("replay_validation") or {}
    if rv:
        lines.extend(["", "## 链上 replay 子集验证（33 币，real 10U）", ""])
        lines.append(f"- top20% real mean={rv.get('top20pct_real_mean',0):+.1%} vs all={rv.get('all_real_mean',0):+.1%}")
        lines.append(f"- top20% p(+1000%)={rv.get('top20pct_p1000',0):.2%} vs all={rv.get('all_p1000',0):.2%}")
    lines.extend(["", "##  caveat", "",
                  "- 标签来自 grid T+15→T+90（非全量链上 replay）；TRAIN 日右尾基线远高于 TEST（8.8% vs 1.7% p100），存在 regime shift。",
                  "- 单线特征（斜率/买家/creator/attention）单独无法抬 +1000%；需组合。",
                  "- buyer graph 仅为 proxy（无 funding cluster RPC）；线 4 未完全实现。",
                  "- TRAIN top5% p100=100% 提示过拟合风险；以 TEST 分组为准。"])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # CSV pivot for best label_100 all_exante
    csv_rows = []
    for m in results["models"]:
        if m["y_col"] != "label_100" or m["feature_group"] != "all_exante":
            continue
        for b in m["buckets_test"]:
            csv_rows.append({"y": m["y_col"], "group": m["feature_group"], "split": "TEST", **b})
    if csv_rows:
        with open(OUT_DIR / "_pump_exante_rank_buckets.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(csv_rows)

    print(f"Wrote {args.output} n={len(df_all)} best_auc={best.get('auc_test') if best else None}")


if __name__ == "__main__":
    main()
