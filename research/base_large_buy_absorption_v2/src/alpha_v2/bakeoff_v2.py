"""Alpha Hunt v2 bakeoff: actor_xpool vs clone_family vs combined."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..alpha.alpha_bakeoff import (
    _match_att_on,
    _verdict_att,
    _walk_forward_rank,
    _eval_scheduled,
)
from ..arms_v1 import prepare_splits
from ..common import load_freeze, save_json
from ..ranking import _day_key
from ..scorecard_v1 import paired_scheduled_delta
from ..v3_common import bh_correct, p_from_boot_frac_positive
from .common_v2 import ALPHA_V2_OUT, ensure_alpha_v2_out, load_alpha_v2_cfg, utcnow

FAMILIES = [
    ("actor_xpool", "actor_xpool_score"),
    ("clone_family", "family_score"),
    ("combined", "combined_score"),
]


def run_matched_bakeoff_v2(d: pd.DataFrame) -> dict[str, Any]:
    cfg = load_alpha_v2_cfg()["matched_incremental"]
    base = d[d["cost_known"] & d["pnl_net"].notna()].copy()
    if "day" not in base.columns:
        base["day"] = pd.to_datetime(base["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")

    results = {}
    pvals, names = [], []
    for fam, score_col in FAMILIES:
        s = pd.to_numeric(base[score_col], errors="coerce").fillna(0.0)
        informative = s > 0
        if informative.sum() < 500:
            thr = s.quantile(0.9)
            treat_m = s >= thr
        else:
            thr = float(s[informative].quantile(0.9))
            treat_m = informative & (s >= thr)
        treat = base.loc[treat_m]
        control = base.loc[~treat_m]
        print(
            f"[alpha_v2/bakeoff] matched {fam}: treat={len(treat):,} ctrl={len(control):,} "
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

    reject = bh_correct(pvals, alpha=float(load_alpha_v2_cfg()["multiple_testing"]["alpha"]))
    for fam, rej, p in zip(names, reject, pvals):
        results[fam]["bh_reject"] = bool(rej)
        results[fam]["p_day"] = float(p)
        if results[fam]["verdict"] == "PROVEN" and not rej:
            results[fam]["verdict"] = "UNCONFIRMED"
    return {"families": results, "bh_pvals": dict(zip(names, pvals)), "bh_reject": dict(zip(names, reject))}


def run_ranker_bakeoff_v2(d: pd.DataFrame) -> dict[str, Any]:
    cfg = load_alpha_v2_cfg()
    freeze = load_freeze()
    cap = cfg["ranker"]["primary_capacity"]
    actor_feats = list(cfg["features_actor_xpool_pit"])
    family_feats = list(cfg["features_clone_family_pit"])
    combined_feats = actor_feats + family_feats

    base = d[d["cost_known"] & d["pnl_net"].notna()].copy()
    base = prepare_splits(base, freeze)
    if "day" not in base.columns:
        base["day"] = _day_key(base["decision_time"])

    eval_mask = base["split"].isin(["TEST", "SEALED"])
    eval_df = base.loc[eval_mask].copy()
    rng = np.random.default_rng(42)
    eval_df["random_score"] = rng.random(len(eval_df))
    eval_df["size_score"] = pd.to_numeric(eval_df.get("quote_weth", 0), errors="coerce").fillna(0.0)

    family_scores = {}
    for fam, feats, sname in [
        ("actor_xpool", actor_feats, "rank_actor_xpool"),
        ("clone_family", family_feats, "rank_clone_family"),
        ("combined", combined_feats, "rank_combined"),
    ]:
        print(f"[alpha_v2/bakeoff] ranker walk-forward {fam}…", flush=True)
        family_scores[fam] = _walk_forward_rank(base, feats, score_name=sname)

    out: dict[str, Any] = {"families": {}, "baselines": {}}
    for split_name, split_val in [("TEST", "TEST"), ("SEALED", "SEALED")]:
        base_split = eval_df[eval_df["split"] == split_val]
        if base_split.empty:
            cut = eval_df["decision_time"].quantile(0.85)
            base_split = eval_df[eval_df["decision_time"] > cut] if split_val == "SEALED" else eval_df[eval_df["decision_time"] <= cut]
        rnd = _eval_scheduled(base_split, "random_score", cap)
        size = _eval_scheduled(base_split, "size_score", cap)
        out["baselines"][split_name] = {"random": rnd["summary"], "size": size["summary"]}
        for fam, scored in family_scores.items():
            sub = scored[scored["split"] == split_val] if "split" in scored.columns else scored
            if sub.empty and "signal_id" in scored.columns and "signal_id" in base_split.columns:
                sub = scored[scored["signal_id"].isin(base_split["signal_id"])]
            sname = f"rank_{fam}"
            if sname not in sub.columns:
                continue
            pol = _eval_scheduled(sub, sname, cap)
            try:
                paired = paired_scheduled_delta(pol["fills"], rnd["fills"])
            except Exception:
                paired = {"p05_day": None, "mean_daily_delta": None}
            out["families"].setdefault(fam, {})[split_name] = {
                "summary": pol["summary"], "paired_vs_random": paired,
            }

    for fam in ["actor_xpool", "clone_family", "combined"]:
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


def run_alpha_bakeoff_v2(panel: pd.DataFrame) -> dict[str, Any]:
    ensure_alpha_v2_out()
    d = panel.copy()
    if "combined_score" not in d.columns:
        d["combined_score"] = (
            pd.to_numeric(d.get("actor_xpool_score", 0), errors="coerce").fillna(0)
            + pd.to_numeric(d.get("family_score", 0), errors="coerce").fillna(0)
        )
    print("[alpha_v2/bakeoff] matched incremental…", flush=True)
    matched = run_matched_bakeoff_v2(d)
    print("[alpha_v2/bakeoff] ranker…", flush=True)
    ranker = run_ranker_bakeoff_v2(d)
    report = {
        "task": "ALPHA_BAKEOFF_V2",
        "generated_at": utcnow(),
        "outcome": "pnl_net",
        "matched": matched,
        "ranker": ranker,
        "n_universe": int(d["cost_known"].sum()) if "cost_known" in d.columns else int(len(d)),
    }
    save_json(ALPHA_V2_OUT / "alpha_bakeoff.json", report)
    print("[alpha_v2/bakeoff] done", flush=True)
    return report
