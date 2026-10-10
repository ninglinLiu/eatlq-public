"""Alpha bakeoff: net-of-cost matched ATT + walk-forward ranker, actor vs genesis vs combined."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from ..arms_v1 import prepare_splits
from ..common import OUT, load_freeze, save_json
from ..matched_incremental import _add_bins, _cluster_boot_mean
from ..ranking import _feat_matrix, _day_key
from ..scheduler import simulate_scheduled
from ..scorecard_v1 import paired_scheduled_delta
from ..v3_common import bh_correct, p_from_boot_frac_positive
from .common_alpha import ALPHA_OUT, ensure_alpha_out, load_alpha_cfg, utcnow


def _match_att_on(
    treat: pd.DataFrame,
    control: pd.DataFrame,
    *,
    outcome_col: str,
    max_pairs: int,
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    treat = _add_bins(treat)
    control = _add_bins(control)
    # also bin on cost if present
    for df in (treat, control):
        if "roundtrip_bps" in df.columns:
            try:
                df["cost_bin"] = pd.qcut(
                    pd.to_numeric(df["roundtrip_bps"], errors="coerce").fillna(1e9).rank(method="first"),
                    5, labels=False, duplicates="drop",
                ).astype(str)
            except ValueError:
                df["cost_bin"] = "0"
            df["match_stratum"] = df["match_stratum"].astype(str) + "|" + df["cost_bin"].astype(str)

    ctrl_pos: dict[str, list[int]] = {}
    for i, st in enumerate(control["match_stratum"].astype(str).to_numpy()):
        ctrl_pos.setdefault(st, []).append(i)
    for st in ctrl_pos:
        rng.shuffle(ctrl_pos[st])
    ptr = {st: 0 for st in ctrl_pos}

    t_pnl = pd.to_numeric(treat[outcome_col], errors="coerce").fillna(0.0).to_numpy()
    c_pnl = pd.to_numeric(control[outcome_col], errors="coerce").fillna(0.0).to_numpy()
    t_stratum = treat["match_stratum"].astype(str).to_numpy()
    t_token = treat["token"].astype(str).to_numpy()
    t_day = (
        treat["day"].astype(str).to_numpy()
        if "day" in treat.columns
        else pd.to_datetime(treat["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d").to_numpy()
    )

    deltas, toks, days = [], [], []
    n_unmatched = 0
    order = np.arange(len(treat))
    rng.shuffle(order)
    for ti in order:
        if len(deltas) >= max_pairs:
            break
        st = t_stratum[ti]
        if st not in ctrl_pos or ptr[st] >= len(ctrl_pos[st]):
            n_unmatched += 1
            continue
        cj = ctrl_pos[st][ptr[st]]
        ptr[st] += 1
        deltas.append(float(t_pnl[ti] - c_pnl[cj]))
        toks.append(t_token[ti])
        days.append(t_day[ti])

    delta = np.asarray(deltas, dtype=float)
    boot_tok = _cluster_boot_mean(delta, np.asarray(toks), n_boot, seed)
    boot_day = _cluster_boot_mean(delta, np.asarray(days), n_boot, seed + 1)
    obs_mean = float(delta.mean()) if len(delta) else 0.0
    rng2 = np.random.default_rng(seed + 9)
    perm_means = [float((delta * rng2.choice([-1.0, 1.0], size=len(delta))).mean()) for _ in range(min(200, n_boot))] if len(delta) else []
    perm_p = float(np.mean(np.asarray(perm_means) >= obs_mean)) if perm_means else 1.0
    return {
        "n_treat": int(len(treat)),
        "n_control": int(len(control)),
        "n_pairs": int(len(delta)),
        "n_unmatched": int(n_unmatched),
        "match_rate": float(len(delta) / max(1, len(treat))),
        "att_mean_delta_weth": obs_mean,
        "bootstrap_token": boot_tok,
        "bootstrap_day": boot_day,
        "permutation_p_one_sided": perm_p,
        "passes_token_p05": bool(boot_tok["p05"] > 0),
        "passes_day_p05": bool(boot_day["p05"] > 0),
    }


def _verdict_att(att: dict) -> str:
    if att["passes_token_p05"] and att["passes_day_p05"] and att["att_mean_delta_weth"] > 0:
        return "PROVEN"
    if att["att_mean_delta_weth"] <= 0 and not att["passes_day_p05"]:
        return "REFUTED"
    return "UNCONFIRMED"


def run_matched_bakeoff(d: pd.DataFrame) -> dict[str, Any]:
    cfg = load_alpha_cfg()["matched_incremental"]
    base = d[d["cost_known"] & d["pnl_net"].notna()].copy()
    if "day" not in base.columns:
        base["day"] = pd.to_datetime(base["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")

    results = {}
    pvals = []
    names = []
    for fam, score_col in [("actor", "actor_score"), ("genesis", "genesis_score"), ("combined", "combined_score")]:
        s = pd.to_numeric(base[score_col], errors="coerce").fillna(0.0)
        # Top decile among informative scores only (exclude mass-at-zero cold-start).
        informative = s > 0
        if informative.sum() < 500:
            # fall back to absolute top 10% of all rows
            thr = s.quantile(0.9)
            treat_m = s >= thr
        else:
            thr = s[informative].quantile(0.9)
            treat_m = informative & (s >= thr)
        treat = base.loc[treat_m]
        control = base.loc[~treat_m]
        print(
            f"[alpha/bakeoff] matched {fam}: treat={len(treat):,} ctrl={len(control):,} "
            f"thr={thr:.4f} informative={int(informative.sum()):,}",
            flush=True,
        )
        att = _match_att_on(
            treat, control,
            outcome_col="pnl_net",
            max_pairs=int(cfg["max_pairs"]),
            n_boot=int(cfg["n_boot"]),
            seed=int(cfg["seed"]) + hash(fam) % 1000,
        )
        v = _verdict_att(att)
        results[fam] = {"att": att, "verdict": v, "threshold": float(thr)}
        pvals.append(p_from_boot_frac_positive(att["bootstrap_day"]["frac_positive"], int(cfg["n_boot"])))
        names.append(fam)

    reject = bh_correct(pvals, alpha=float(load_alpha_cfg()["multiple_testing"]["alpha"]))
    for fam, rej, p in zip(names, reject, pvals):
        results[fam]["bh_reject"] = bool(rej)
        results[fam]["p_day"] = float(p)
        # PROVEN only if also BH-significant
        if results[fam]["verdict"] == "PROVEN" and not rej:
            results[fam]["verdict"] = "UNCONFIRMED"
    return {"families": results, "bh_pvals": dict(zip(names, pvals)), "bh_reject": dict(zip(names, reject))}


def _walk_forward_rank(
    d: pd.DataFrame,
    feat_cols: list[str],
    *,
    score_name: str,
) -> pd.DataFrame:
    folds = sorted([int(f) for f in d["fold"].dropna().unique() if int(f) >= 0])
    parts = []
    for fold in folds:
        train = d[(d["fold"] == fold) & (d["split"] == "TRAIN")]
        test = d[(d["fold"] == fold) & (d["split"] == "TEST")]
        if test.empty:
            continue
        if train.empty or len(train) < 80:
            sub = d[d["fold"] == fold]
            cut = sub["decision_time"].quantile(0.7)
            train = sub[sub["decision_time"] <= cut]
            test = sub[sub["decision_time"] > cut]
        if train.empty or test.empty:
            continue
        Xtr, used = _feat_matrix(train, feat_cols)
        ytr = pd.to_numeric(train["pnl_net"], errors="coerce").fillna(0).to_numpy()
        Xte, _ = _feat_matrix(test, used)
        est = HistGradientBoostingRegressor(
            max_depth=4, learning_rate=0.05, max_iter=120,
            min_samples_leaf=40, l2_regularization=1.0, random_state=42,
        )
        est.fit(Xtr, ytr)
        chunk = test.copy()
        chunk[score_name] = est.predict(Xte)
        parts.append(chunk)
    if not parts:
        # fallback single split
        cut = d["decision_time"].quantile(0.7)
        train, test = d[d["decision_time"] <= cut], d[d["decision_time"] > cut]
        Xtr, used = _feat_matrix(train, feat_cols)
        ytr = train["pnl_net"].fillna(0).to_numpy()
        Xte, _ = _feat_matrix(test, used)
        est = HistGradientBoostingRegressor(
            max_depth=4, learning_rate=0.05, max_iter=80,
            min_samples_leaf=40, l2_regularization=1.0, random_state=42,
        )
        est.fit(Xtr, ytr)
        test = test.copy()
        test[score_name] = est.predict(Xte)
        parts = [test]
    return pd.concat(parts, ignore_index=True)


def _eval_scheduled(frame: pd.DataFrame, score_col: str, cap: dict) -> dict:
    d = frame.copy()
    d["_sched_score"] = pd.to_numeric(d.get(score_col, 0), errors="coerce").fillna(0.0)
    # use pnl_net as the wallet pnl for scheduling evaluation by temporarily swapping
    d = d.copy()
    d["wallet_pnl_weth"] = pd.to_numeric(d["pnl_net"], errors="coerce").fillna(0.0)
    fills, monthly, summary = simulate_scheduled(
        d,
        score_col="_sched_score",
        max_concurrent=int(cap["max_concurrent"]),
        one_position_per_token=bool(cap["one_position_per_token"]),
        max_per_token=int(cap["max_per_token"]),
    )
    return {"summary": summary, "fills": fills}


def run_ranker_bakeoff(d: pd.DataFrame) -> dict[str, Any]:
    cfg = load_alpha_cfg()
    freeze = load_freeze()
    cap = cfg["ranker"]["primary_capacity"]
    actor_feats = list(cfg["features_actor_pit"])
    genesis_feats = list(cfg["features_genesis_pit"])
    combined_feats = actor_feats + genesis_feats

    base = d[d["cost_known"] & d["pnl_net"].notna()].copy()
    # attach walk-forward splits
    base = prepare_splits(base, freeze)
    if "day" not in base.columns:
        base["day"] = _day_key(base["decision_time"])

    # baselines on TEST+SEALED
    eval_mask = base["split"].isin(["TEST", "SEALED"])
    eval_df = base.loc[eval_mask].copy()
    rng = np.random.default_rng(42)
    eval_df["random_score"] = rng.random(len(eval_df))
    eval_df["size_score"] = pd.to_numeric(eval_df.get("quote_weth", 0), errors="coerce").fillna(0.0)

    family_scores = {}
    for fam, feats, sname in [
        ("actor", actor_feats, "rank_actor"),
        ("genesis", genesis_feats, "rank_genesis"),
        ("combined", combined_feats, "rank_combined"),
    ]:
        print(f"[alpha/bakeoff] ranker walk-forward {fam}…", flush=True)
        scored = _walk_forward_rank(base, feats, score_name=sname)
        family_scores[fam] = scored

    # evaluate on TEST only for primary, SEALED for replication
    out = {"families": {}, "baselines": {}}
    for split_name, split_val in [("TEST", "TEST"), ("SEALED", "SEALED")]:
        base_split = eval_df[eval_df["split"] == split_val]
        if base_split.empty:
            # if no sealed column properly set, take last 15% by time as sealed
            if split_val == "SEALED":
                cut = eval_df["decision_time"].quantile(0.85)
                base_split = eval_df[eval_df["decision_time"] > cut]
            else:
                cut = eval_df["decision_time"].quantile(0.85)
                base_split = eval_df[eval_df["decision_time"] <= cut]
        rnd = _eval_scheduled(base_split, "random_score", cap)
        size = _eval_scheduled(base_split, "size_score", cap)
        out["baselines"][split_name] = {
            "random": rnd["summary"],
            "size": size["summary"],
        }
        for fam, scored in family_scores.items():
            sub = scored[scored["split"] == split_val] if "split" in scored.columns else scored
            if sub.empty:
                # align by signal_id
                if "signal_id" in scored.columns and "signal_id" in base_split.columns:
                    sub = scored[scored["signal_id"].isin(base_split["signal_id"])]
                else:
                    sub = scored
            sname = f"rank_{fam}"
            if sname not in sub.columns:
                continue
            pol = _eval_scheduled(sub, sname, cap)
            # paired delta vs random on overlapping days
            try:
                paired = paired_scheduled_delta(pol["fills"], rnd["fills"])
            except Exception:
                paired = {"p05_day": None, "mean_daily_delta": None}
            out["families"].setdefault(fam, {})[split_name] = {
                "summary": pol["summary"],
                "paired_vs_random": paired,
            }

    # verdicts
    for fam in ["actor", "genesis", "combined"]:
        test = out["families"].get(fam, {}).get("TEST", {})
        sealed = out["families"].get(fam, {}).get("SEALED", {})
        paired = test.get("paired_vs_random") or {}
        day_p05 = paired.get("p05_day")
        mean_d = paired.get("mean_daily_delta")
        test_ok = bool(day_p05 is not None and day_p05 > 0 and (mean_d or 0) > 0)
        sealed_paired = sealed.get("paired_vs_random") or {}
        sealed_ok = bool(
            sealed_paired.get("p05_day") is not None
            and sealed_paired.get("p05_day") > 0
            and (sealed_paired.get("mean_daily_delta") or 0) > 0
        )
        if test_ok and sealed_ok:
            v = "PROVEN_ON_SEALED"
        elif test_ok:
            v = "UNCONFIRMED"
        elif mean_d is not None and mean_d <= 0:
            v = "REFUTED"
        else:
            v = "UNCONFIRMED"
        out["families"].setdefault(fam, {})["verdict"] = v
    return out


def run_alpha_bakeoff(panel: pd.DataFrame) -> dict[str, Any]:
    ensure_alpha_out()
    d = panel.copy()
    if "combined_score" not in d.columns:
        d["combined_score"] = (
            pd.to_numeric(d.get("actor_score", 0), errors="coerce").fillna(0)
            + pd.to_numeric(d.get("genesis_score", 0), errors="coerce").fillna(0)
        )

    print("[alpha/bakeoff] matched incremental…", flush=True)
    matched = run_matched_bakeoff(d)
    print("[alpha/bakeoff] ranker…", flush=True)
    ranker = run_ranker_bakeoff(d)

    report = {
        "task": "ALPHA_BAKEOFF",
        "generated_at": utcnow(),
        "outcome": "pnl_net",
        "matched": matched,
        "ranker": ranker,
        "n_universe": int(d["cost_known"].sum()) if "cost_known" in d.columns else int(len(d)),
    }
    save_json(ALPHA_OUT / "alpha_bakeoff.json", report)
    print("[alpha/bakeoff] done", flush=True)
    return report


if __name__ == "__main__":
    p = ALPHA_OUT / "panel_with_alpha_features.parquet"
    run_alpha_bakeoff(pd.read_parquet(p))
