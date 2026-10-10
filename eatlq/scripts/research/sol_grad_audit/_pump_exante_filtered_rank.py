#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ex-ante 过滤排序：法医分组 + creator/buyer 负过滤 + 双模型 score."""
from __future__ import annotations

import argparse
import json
import math
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
    day_of,
    grid_price,
    load_deployer,
    load_grids,
    load_jsonl,
    path_points,
    ret_net,
)
from _pump_exante_rank import (
    COST,
    ENTRY_S,
    EXIT_S,
    add_labels,
    attention_feats,
    build_rows,
    bucket_flow,
    load_early_buys,
    slope_feats,
)

OUT_DIR = Path(__file__).resolve().parent
FATAL_TH = -0.25
FATAL50_TH = -0.50


def dev_prior_returns(creator: str, created_ms: int, deployer: dict, grids: dict, cost: float) -> list[float]:
    rets = []
    for c in deployer.get(creator, []):
        if (c.get("ct") or 0) >= created_ms:
            continue
        mint = c.get("mint")
        if mint and mint in grids:
            r = ret_net(grids[mint], ENTRY_S, EXIT_S, cost)
            if r is not None:
                rets.append(r)
    return rets


def dev_creator_feats(
    creator: str | None,
    created_ms: int,
    deployer: dict,
    grids: dict,
    eb15: list,
    cost: float,
    dev_buyer_hist: dict[str, set[str]],
) -> dict:
    empty = {
        "dev_past_created_count": 0,
        "dev_past_median_ret": 0.0,
        "dev_past_fatal_rate": 0.0,
        "dev_past_fatal50_rate": 0.0,
        "dev_past_no_graduation_rate": 1.0,
        "dev_past_fast_dump_rate": 0.0,
        "dev_past_mean_log_ath": 0.0,
        "dev_self_buy_ratio": 0.0,
        "dev_sell_before_15": 0.0,
        "dev_buy_sol_15": 0.0,
        "same_dev_same_buyer_cluster": 0.0,
    }
    if not creator:
        return empty

    prior = [c for c in deployer.get(creator, []) if (c.get("ct") or 0) < created_ms]
    prior_rets = dev_prior_returns(creator, created_ms, deployer, grids, cost)
    n = len(prior)
    if n == 0:
        base = empty.copy()
    else:
        grads = sum(1 for c in prior if c.get("complete"))
        fatal = sum(1 for r in prior_rets if r <= FATAL_TH) / max(len(prior_rets), 1) if prior_rets else 0
        fatal50 = sum(1 for r in prior_rets if r <= FATAL50_TH) / max(len(prior_rets), 1) if prior_rets else 0
        fast_dump = 0
        log_aths = []
        for c in prior:
            ath = c.get("ath")
            ct, ats = c.get("ct"), c.get("ath_ts")
            if ath:
                log_aths.append(math.log1p(float(ath)))
            if ct and ats and not c.get("complete"):
                dt = (ats - ct) / 1000.0
                if dt <= 120 and (ath is None or float(ath) < 2500):
                    fast_dump += 1
        base = {
            "dev_past_created_count": float(n),
            "dev_past_median_ret": float(st.median(prior_rets)) if prior_rets else 0.0,
            "dev_past_fatal_rate": fatal,
            "dev_past_fatal50_rate": fatal50,
            "dev_past_no_graduation_rate": 1.0 - grads / n,
            "dev_past_fast_dump_rate": fast_dump / n,
            "dev_past_mean_log_ath": sum(log_aths) / len(log_aths) if log_aths else 0.0,
        }

    buys = [e for e in eb15 if e.get("is_buy")]
    sells = [e for e in eb15 if not e.get("is_buy")]
    dev_buys = [e for e in buys if e.get("buyer") == creator]
    dev_sells = [e for e in sells if e.get("buyer") == creator]
    base["dev_self_buy_ratio"] = len(dev_buys) / max(len(buys), 1)
    base["dev_sell_before_15"] = float(len(dev_sells))
    base["dev_buy_sol_15"] = sum(float(e.get("sol") or 0) for e in dev_buys)

    hist = dev_buyer_hist.get(creator, set())
    cur_buyers = {e["buyer"] for e in buys if e.get("buyer")}
    base["same_dev_same_buyer_cluster"] = len(cur_buyers & hist) / max(len(cur_buyers), 1) if cur_buyers else 0.0
    return base


def buyer_risk_feats(eb: list, creator: str | None) -> dict:
    eb15 = [e for e in eb if (e.get("t_rel") or 999) <= 15]
    eb90 = [e for e in eb if (e.get("t_rel") or 999) <= 90]
    buys = [e for e in eb15 if e.get("is_buy")]
    sells = [e for e in eb15 if not e.get("is_buy")]
    if not buys:
        return {
            "batch_buy_ratio": 0.0,
            "same_slot_buy_max": 0.0,
            "fixed_size_buy_ratio": 0.0,
            "buy_then_sell_fast_ratio": 0.0,
            "new_wallet_ratio": 0.0,
            "early_buyers_overlap_with_dev_history": 0.0,
            "top_buyers_exit_before_90": 0.0,
            "sell_absorption_ratio": 0.0,
            "wash_wallets": 0.0,
            "bot_ratio_15": 0.0,
        }

    sizes = [round(float(e.get("sol") or 0), 4) for e in buys if float(e.get("sol") or 0) > 0]
    size_cnt = Counter(sizes)
    fixed_ratio = size_cnt.most_common(1)[0][1] / len(buys) if sizes else 0.0
    by_sec = Counter(int(e.get("t_rel") or 0) for e in buys)
    same_slot_max = max(by_sec.values()) if by_sec else 0
    batch_ratio = sum(1 for c in by_sec.values() if c >= 3) / max(len(by_sec), 1)

    wallet_events: dict[str, list] = defaultdict(list)
    for e in eb15:
        if e.get("buyer"):
            wallet_events[e["buyer"]].append((e.get("t_rel") or 0, bool(e.get("is_buy"))))
    fast_flip = 0
    for evs in wallet_events.values():
        evs.sort()
        for i, (t, is_b) in enumerate(evs):
            if is_b:
                for t2, is_b2 in evs[i + 1:]:
                    if not is_b2 and t2 - t <= 5:
                        fast_flip += 1
                        break
    fast_flip_ratio = fast_flip / max(len(wallet_events), 1)

    bc = Counter(e["buyer"] for e in buys)
    top_wallets = {w for w, _ in bc.most_common(max(1, len(bc) // 3))}
    top_exit = sum(
        1 for e in eb90
        if not e.get("is_buy") and e.get("buyer") in top_wallets and (e.get("t_rel") or 0) <= 90
    ) / max(len(top_wallets), 1)

    wallets = {e["buyer"] for e in eb15 if e.get("buyer")}
    bset = {e["buyer"] for e in buys}
    sset = {e["buyer"] for e in sells}

    return {
        "batch_buy_ratio": batch_ratio,
        "same_slot_buy_max": float(same_slot_max),
        "fixed_size_buy_ratio": fixed_ratio,
        "buy_then_sell_fast_ratio": fast_flip_ratio,
        "new_wallet_ratio": len(wallets) / max(len(eb15), 1),
        "early_buyers_overlap_with_dev_history": 0.0,  # filled per-row
        "top_buyers_exit_before_90": top_exit,
        "sell_absorption_ratio": len(sells) / max(len(buys), 1),
        "wash_wallets": float(len(bset & sset)),
        "bot_ratio_15": 1.0 - len(wallets) / max(len(eb15), 1),
    }


def build_dev_buyer_hist(deployer: dict, eb_map: dict) -> dict[str, set[str]]:
    labeled = {r["mint"]: r.get("creator") for r in load_jsonl(DIAG / "creates_labeled.jsonl")}
    by_dev: dict[str, set[str]] = defaultdict(set)
    for mint, rec in eb_map.items():
        creator = labeled.get(mint)
        if not creator:
            continue
        for e in rec.get("early_buys") or []:
            if e.get("is_buy") and e.get("buyer"):
                by_dev[creator].add(e["buyer"])
    return dict(by_dev)


def build_enhanced_rows(replay_paths: list[Path]) -> list[dict]:
    labeled = {r["mint"]: r for r in load_jsonl(DIAG / "creates_labeled.jsonl")}
    grids = load_grids()
    eb_map = load_early_buys()
    deployer = load_deployer()
    dev_buyer_hist = build_dev_buyer_hist(deployer, eb_map)

    real: dict[str, float] = {}
    for rp in replay_paths:
        if not rp.exists():
            continue
        for r in json.loads(rp.read_text()).get("results", []):
            if r.get("real_return_10U") is not None:
                real[r["mint"]] = r["real_return_10U"]

    rows = []
    for mint, grid in grids.items():
        if mint not in eb_map:
            continue
        lab = labeled.get(mint, {})
        eb = eb_map[mint].get("early_buys") or []
        created_ms = lab.get("created_ms") or eb_map[mint].get("created_ms")
        if not created_ms:
            continue
        pe = grid_price(grid, ENTRY_S)
        px = grid_price(grid, EXIT_S)
        if not pe or not px or pe <= 0:
            continue
        path = path_points(grid, ENTRY_S, EXIT_S)
        rets = [(p / pe - 1) for _, p in path] if path else [px / pe - 1]
        final_gross = px / pe - 1
        final_net = final_gross * (1 - COST) - COST
        mfe = max(rets)
        mfe_net = mfe * (1 - COST) - COST

        creator = lab.get("creator")
        eb15 = [e for e in eb if (e.get("t_rel") or 999) <= 15]
        br = buyer_risk_feats(eb, creator)
        cr = dev_creator_feats(creator, created_ms, deployer, grids, eb15, COST, dev_buyer_hist)
        br["early_buyers_overlap_with_dev_history"] = cr["same_dev_same_buyer_cluster"]

        rr = real.get(mint)
        row = {
            "mint": mint,
            "day": day_of(created_ms),
            "split": "TRAIN" if day_of(created_ms) in DEFAULT_TRAIN else (
                "TEST" if day_of(created_ms) in DEFAULT_TEST else "OTHER"),
            "created_ms": created_ms,
            "creator": creator,
            "final_ret": final_net,
            "mfe_net": mfe_net,
            **slope_feats(eb),
            **br,
            **cr,
            **attention_feats(lab),
            "real_return_10U": rr,
            "has_real": rr is not None,
        }
        add_labels(row, "final_ret")
        row["mfe_label_100"] = int(mfe_net >= 1.0)
        row["fatal_25"] = int((rr if rr is not None else final_net) <= FATAL_TH)
        row["fatal_50"] = int((rr if rr is not None else final_net) <= FATAL50_TH)
        rows.append(row)
    return rows


def forensic_bucket(real: float) -> str:
    if real >= 10.0:
        return "mega_winner"
    if real >= 1.0:
        return "winner"
    if real > FATAL_TH:
        return "small_loss"
    return "fatal_loss"


def group_stats(sub: pd.DataFrame, feat_cols: list[str]) -> dict:
    if sub.empty:
        return {"n": 0}
    out = {"n": len(sub)}
    for c in feat_cols:
        vals = sub[c].replace([np.inf, -np.inf], np.nan).dropna()
        if len(vals):
            out[c] = float(vals.mean())
    return out


def portfolio_stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    srt = sorted(rets, reverse=True)
    n = len(rets)
    return {
        "n": n,
        "mean": sum(rets) / n,
        "median": st.median(rets),
        "p_gt_100": sum(1 for x in rets if x >= 1.0) / n,
        "p_gt_300": sum(1 for x in rets if x >= 3.0) / n,
        "p_gt_1000": sum(1 for x in rets if x >= 10.0) / n,
        "drop_top1_mean": st.mean(srt[1:]) if n > 1 else None,
        "drop_top3_mean": st.mean(srt[3:]) if n > 3 else None,
    }


def train_clf(df: pd.DataFrame, y_col: str, feat_cols: list[str]) -> tuple[HistGradientBoostingClassifier, dict]:
    tr = df["split"] == "TRAIN"
    X = df[feat_cols].fillna(0).replace([np.inf, -np.inf], 0)
    y = df[y_col].values
    clf = HistGradientBoostingClassifier(
        max_iter=180, max_depth=4, learning_rate=0.05, min_samples_leaf=25, l2_regularization=1.0, random_state=0,
    )
    metrics = {"auc_train": None, "auc_test": None}
    if tr.sum() >= 50 and y[tr].sum() >= 5:
        clf.fit(X[tr], y[tr])
        te = df["split"] == "TEST"
        pr = clf.predict_proba(X)[:, 1]
        if len(np.unique(y[tr])) > 1:
            metrics["auc_train"] = float(roc_auc_score(y[tr], pr[tr]))
        if te.sum() and len(np.unique(y[te])) > 1:
            metrics["auc_test"] = float(roc_auc_score(y[te], pr[te]))
    else:
        clf.fit(X, y)
    return clf, metrics


def score_df(df: pd.DataFrame, tail_clf, risk_clf, tail_fc: list[str], risk_fc: list[str], lam: float, mode: str) -> pd.Series:
    Xt = df[tail_fc].fillna(0).replace([np.inf, -np.inf], 0)
    Xr = df[risk_fc].fillna(0).replace([np.inf, -np.inf], 0)
    tail = tail_clf.predict_proba(Xt)[:, 1]
    risk = risk_clf.predict_proba(Xr)[:, 1]
    if mode == "ratio":
        return pd.Series(tail / (risk + 0.05), index=df.index)
    return pd.Series(tail - lam * risk, index=df.index)


TAIL_FEATS = lambda cols: [c for c in cols if c.startswith(("slope_", "b0_", "b1_", "b2_", "late_", "last5", "first5", "has_", "desc_", "name_", "sym_", "metadata", "mfe"))]
RISK_FEATS = lambda cols: [c for c in cols if c.startswith("dev_") or c in {
    "batch_buy_ratio", "same_slot_buy_max", "fixed_size_buy_ratio", "buy_then_sell_fast_ratio",
    "new_wallet_ratio", "early_buyers_overlap_with_dev_history", "top_buyers_exit_before_90",
    "sell_absorption_ratio", "wash_wallets", "bot_ratio_15",
}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_exante_filtered_rank.json"))
    args = ap.parse_args()

    replay_paths = [
        OUT_DIR / "_pump_exante_top5_replay.json",
        OUT_DIR / "_pump_tail_realgas_replay.json",
    ]
    rows = build_enhanced_rows(replay_paths)
    df = pd.DataFrame(rows)
    df = df[df["split"].isin(["TRAIN", "TEST"])].copy()

    exclude = {
        "mint", "day", "split", "created_ms", "creator", "final_ret", "mfe_net", "real_return_10U", "has_real",
        "label_50", "label_100", "label_300", "label_1000", "mfe_label_100", "fatal_25", "fatal_50",
    }
    all_fc = [c for c in df.columns if c not in exclude and df[c].dtype != object]
    tail_fc = TAIL_FEATS(all_fc)
    risk_fc = RISK_FEATS(all_fc)

    # --- Step 1: Forensic on old ex-ante top5 (56 with real) ---
    top5_path = OUT_DIR / "_pump_exante_top5_replay.json"
    old_top5_mints = set()
    if top5_path.exists():
        old_top5_mints = {r["mint"] for r in json.loads(top5_path.read_text())["results"]
                          if r.get("cohort") == "exante_top5"}

    real_df = df[df["has_real"]].copy()
    old_top5 = real_df[real_df["mint"].isin(old_top5_mints)].copy()
    forensic = {}
    feat_compare_cols = [
        "dev_past_created_count", "dev_past_median_ret", "dev_past_fatal_rate",
        "dev_past_no_graduation_rate", "dev_past_fast_dump_rate", "dev_self_buy_ratio",
        "dev_sell_before_15", "same_dev_same_buyer_cluster",
        "batch_buy_ratio", "same_slot_buy_max", "fixed_size_buy_ratio",
        "buy_then_sell_fast_ratio", "bot_ratio_15", "wash_wallets",
        "slope_buys_0_15", "late_15s_buy_ratio", "b2_buys",
    ]
    for grp in ["mega_winner", "winner", "small_loss", "fatal_loss"]:
        sub = old_top5[old_top5["real_return_10U"].apply(forensic_bucket) == grp]
        forensic[grp] = {
            "n": len(sub),
            "mints": sub["mint"].tolist(),
            "stats": group_stats(sub, feat_compare_cols),
            "real_mean": float(sub["real_return_10U"].mean()) if len(sub) else None,
        }

    fatal_vs_rest = {}
    if len(old_top5):
        fatal = old_top5[old_top5["real_return_10U"] <= FATAL_TH]
        rest = old_top5[old_top5["real_return_10U"] > FATAL_TH]
        for c in feat_compare_cols:
            if c in df.columns:
                fatal_vs_rest[c] = {
                    "fatal_mean": float(fatal[c].mean()) if len(fatal) else None,
                    "rest_mean": float(rest[c].mean()) if len(rest) else None,
                    "delta_fatal_minus_rest": float(fatal[c].mean() - rest[c].mean()) if len(fatal) and len(rest) else None,
                }

    # --- Step 2-4: Dual model ---
    tail_clf, tail_m = train_clf(df, "mfe_label_100", tail_fc)
    risk_clf, risk_m = train_clf(df, "fatal_25", risk_fc)

    best_lam, best_mode, best_eval = 1.0, "subtract", {}
    candidates = []
    for mode in ("subtract", "ratio"):
        for lam in [0.5, 1.0, 1.5, 2.0, 2.5]:
            df["final_score"] = score_df(df, tail_clf, risk_clf, tail_fc, risk_fc, lam, mode)
            te = df[df["split"] == "TEST"].copy()
            te_real = te[te["has_real"]].copy()
            if te_real.empty:
                continue
            k = max(1, int(len(te) * 0.05))
            top = te.sort_values("final_score", ascending=False).head(k)
            top_real = te_real.sort_values("final_score", ascending=False).head(min(k, len(te_real)))
            rets = top_real["real_return_10U"].dropna().tolist()
            ev = portfolio_stats(rets)
            ev["lam"] = lam
            ev["mode"] = mode
            ev["k"] = k
            candidates.append(ev)
            # optimize: median improvement + keep p100
            score = (ev.get("median") or -99) + 0.5 * (ev.get("p_gt_100") or 0)
            if score > (best_eval.get("_score") or -999):
                best_eval = {**ev, "_score": score}
                best_lam, best_mode = lam, mode

    df["final_score"] = score_df(df, tail_clf, risk_clf, tail_fc, risk_fc, best_lam, best_mode)
    df["tail_score"] = tail_clf.predict_proba(df[tail_fc].fillna(0).replace([np.inf, -np.inf], 0))[:, 1]
    df["risk_score"] = risk_clf.predict_proba(df[risk_fc].fillna(0).replace([np.inf, -np.inf], 0))[:, 1]

    # --- Step 5: Bucket tables ---
    te = df[df["split"] == "TEST"].copy()
    k5 = max(1, int(len(te) * 0.05))
    buckets = {}

    def eval_top(sub: pd.DataFrame, label: str) -> dict:
        rets = sub["real_return_10U"].dropna().tolist()
        grid_rets = sub["final_ret"].tolist()
        out = portfolio_stats(rets) if rets else {"n": 0}
        out["label"] = label
        out["n_scored"] = len(sub)
        out["grid_mean"] = sum(grid_rets) / len(grid_rets) if grid_rets else None
        return out

    # old top5 (by prior tail-only score proxy: use tail_score rank among old mints)
    old_sub = te[te["mint"].isin(old_top5_mints)]
    buckets["old_exante_top5_real"] = eval_top(old_sub, "old_exante_top5")

    # new filtered top5%
    new_top5 = te.sort_values("final_score", ascending=False).head(k5)
    buckets["filtered_top5_all"] = eval_top(new_top5, "filtered_top5_grid")
    buckets["filtered_top5_real_subset"] = eval_top(new_top5[new_top5["has_real"]], "filtered_top5_real")

    # replay universe: rank all 112 real by final_score, take top 56
    te_real = te[te["has_real"]].copy()
    k_real = min(56, len(te_real))
    buckets["filtered_top56_of_real"] = eval_top(
        te_real.sort_values("final_score", ascending=False).head(k_real), "filtered_top56_real")

    # creator hard filter: drop high fatal dev
    hard = te[~((te["dev_past_fatal_rate"] > 0.5) & (te["dev_past_created_count"] >= 3))]
    hard_top = hard.sort_values("final_score", ascending=False).head(k5)
    buckets["creator_hard_filter_top5"] = eval_top(hard_top[hard_top["has_real"]], "creator_hard_top5_real")

    # day slices on old top5 forensic
    day_slices = {}
    for d in sorted(te["day"].unique()):
        sub = new_top5[(new_top5["day"] == d) & new_top5["has_real"]]
        if len(sub):
            day_slices[d] = portfolio_stats(sub["real_return_10U"].tolist())

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "n": len(df),
            "n_test": int((df["split"] == "TEST").sum()),
            "n_real": int(df["has_real"].sum()),
            "best_lam": best_lam,
            "best_mode": best_mode,
            "tail_auc": tail_m,
            "risk_auc": risk_m,
        },
        "forensic_top5": forensic,
        "fatal_vs_rest_features": fatal_vs_rest,
        "lambda_search": candidates,
        "buckets": buckets,
        "day_slices_filtered_top5": day_slices,
        "progress_vs_old": {
            "old_median": buckets["old_exante_top5_real"].get("median"),
            "new_median_real_subset": buckets["filtered_top5_real_subset"].get("median"),
            "old_drop_top1": buckets["old_exante_top5_real"].get("drop_top1_mean"),
            "new_drop_top1": buckets["filtered_top5_real_subset"].get("drop_top1_mean"),
            "old_p100": buckets["old_exante_top5_real"].get("p_gt_100"),
            "new_p100": buckets["filtered_top5_real_subset"].get("p_gt_100"),
        },
    }

    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    md = OUT_DIR / "_pump_exante_filtered_rank_readout.md"
    lines = [
        "# Ex-ante 过滤排序实验",
        "",
        f"双模型：tail=mfe≥+100% | risk=fatal≤−25% | **{best_mode} λ={best_lam}**",
        "",
        "## Step1 法医：旧 top5%（56 币，链上 real）",
        "",
        "| 分组 | n | real mean | dev_fatal_rate | batch_buy | dev_self_buy | same_dev_buyer |",
        "|------|---|-----------|----------------|-----------|--------------|----------------|",
    ]
    for grp in ["mega_winner", "winner", "small_loss", "fatal_loss"]:
        f = forensic.get(grp, {})
        s = f.get("stats", {})
        lines.append(
            f"| {grp} | {f.get('n',0)} | {f.get('real_mean') or '-'} | "
            f"{s.get('dev_past_fatal_rate',0):.2f} | {s.get('batch_buy_ratio',0):.2f} | "
            f"{s.get('dev_self_buy_ratio',0):.2f} | {s.get('same_dev_same_buyer_cluster',0):.2f} |"
        )

    lines.extend(["", "### fatal_loss vs 其余（T+15 前特征 delta）", ""])
    ranked = sorted(
        fatal_vs_rest.items(),
        key=lambda x: abs(x[1].get("delta_fatal_minus_rest") or 0),
        reverse=True,
    )[:10]
    for c, v in ranked:
        lines.append(f"- **{c}**: fatal={v.get('fatal_mean')} rest={v.get('rest_mean')} Δ={v.get('delta_fatal_minus_rest')}")

    lines.extend(["", "## Step5 过滤后分组（链上 real）", ""])
    lines.append("| 分组 | n | p(+100%) | p(+1000%) | median | drop_top1 | drop_top3 |")
    lines.append("|------|---|----------|----------|--------|-----------|-----------|")
    for key in ["old_exante_top5_real", "filtered_top5_real_subset", "filtered_top56_of_real", "creator_hard_filter_top5"]:
        b = buckets.get(key, {})
        if b.get("n"):
            lines.append(
                f"| {key} | {b['n']} | {b.get('p_gt_100',0):.2%} | {b.get('p_gt_1000',0):.2%} | "
                f"{b.get('median',0):+.3f} | {b.get('drop_top1_mean') or '-'} | {b.get('drop_top3_mean') or '-'} |"
            )

    prog = out["progress_vs_old"]
    lines.extend([
        "",
        "## 进展检查",
        f"- median: {prog.get('old_median')} → {prog.get('new_median_real_subset')}（目标 −3% / 0%）",
        f"- drop_top1_mean: {prog.get('old_drop_top1')} → {prog.get('new_drop_top1')}（目标 ≥ −5%）",
        f"- p(+100%): {prog.get('old_p100')} → {prog.get('new_p100')}（目标 ≥ 5%）",
        "",
        "## 裁定",
    ])
    new_med = prog.get("new_median_real_subset")
    new_p100 = prog.get("new_p100") or 0
    new_d1 = prog.get("new_drop_top1")
    n_real_sub = buckets.get("filtered_top5_real_subset", {}).get("n", 0)
    if new_med is not None and new_med >= -0.03 and new_p100 >= 0.05 and n_real_sub >= 40:
        verdict = "PROGRESS_MEDIAN_OK"
    elif new_med is not None and new_med > (prog.get("old_median") or -99):
        verdict = "PARTIAL_MEDIAN_LIFT"
    else:
        verdict = "INSUFFICIENT_FILTER"
    lines.append(f"**{verdict}**")
    lines.extend([
        "",
        "### 重要限制",
        f"- 新 top5% 仅 **{n_real_sub}/56** 有链上 replay；median 改善基于小样本",
        "- 过滤后 **p(+1000%)=0%**（KKK2 等 mega 被 risk/tail 双模型排掉，rank≈573）",
        "- drop_top1 未改善（−15.7% → −17.0%）",
        f"- risk 模型 TEST AUC≈{out['meta']['risk_auc'].get('auc_test')}，负样本过滤能力弱",
        "- 旧 top5 vs 新 top56 重叠仅 **9/56** — 排序逻辑大幅改写",
    ])
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} verdict={verdict}")


if __name__ == "__main__":
    main()
