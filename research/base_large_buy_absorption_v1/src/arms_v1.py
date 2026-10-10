"""Three frozen ablations + POOL_ONLY, walk-forward on decision_time=signal_t."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import calendar_walk_forward, load_freeze
from .signals import build_arm_panel

ARM_SCORE_COLS = {
    "POOL_ONLY": "pool_only_score",
    "LARGE_BUY_ONLY": "large_buy_score",
    "ABSORPTION_ONLY": "absorption_score",
    "LARGE_BUY_ABSORPTION": "lba_score",
}


def prepare_splits(panel: pd.DataFrame, freeze: dict | None = None) -> pd.DataFrame:
    freeze = freeze or load_freeze()
    wf = freeze["walk_forward"]
    splits = calendar_walk_forward(
        panel["decision_time"],
        train_days=int(wf["train_days"]),
        test_days=int(wf["test_days"]),
        embargo_seconds=int(wf["embargo_seconds"]),
        sealed_frac=float(wf["sealed_frac"]),
    )
    test_map = splits[splits["split"] == "TEST"][["decision_time", "fold"]]
    sealed = set(splits.loc[splits["split"] == "SEALED", "decision_time"].astype(int))
    d = panel.copy()
    d["is_sealed"] = d["decision_time"].astype(int).isin(sealed)
    fold_of = test_map.drop_duplicates("decision_time").set_index("decision_time")["fold"]
    d["fold"] = d["decision_time"].map(fold_of)
    train_map = splits[splits["split"] == "TRAIN"].drop_duplicates("decision_time").set_index("decision_time")["fold"]
    d["fold"] = d["fold"].fillna(d["decision_time"].map(train_map))
    d["split"] = np.where(
        d["is_sealed"], "SEALED",
        np.where(d["decision_time"].isin(set(test_map["decision_time"])), "TEST", "TRAIN"),
    )
    return d


def select_arm(
    frame: pd.DataFrame,
    arm: str,
    *,
    quantile: float = 0.80,
    top_k: int = 2,
    train_threshold: float | None = None,
) -> tuple[pd.Series, float]:
    col = ARM_SCORE_COLS[arm]
    s = pd.to_numeric(frame.get(col, 0), errors="coerce").fillna(0.0)
    eligible = pd.Series(True, index=frame.index)
    if "scorable_primary" in frame.columns:
        eligible &= frame["scorable_primary"].astype(bool)
    if "buy_status" in frame.columns:
        eligible &= frame["buy_status"].eq("SUCCESS")

    if arm == "LARGE_BUY_ONLY":
        eligible &= s > 0
    elif arm == "ABSORPTION_ONLY":
        eligible &= (pd.to_numeric(frame.get("absorbed", 0), errors="coerce").fillna(0) > 0)
    elif arm == "LARGE_BUY_ABSORPTION":
        eligible &= s > 0
        eligible &= (pd.to_numeric(frame.get("absorbed", 0), errors="coerce").fillna(0) > 0)
    # POOL_ONLY: all eligible

    thr = train_threshold
    if thr is None:
        base = s[eligible]
        thr = float(base.quantile(quantile)) if len(base) else 1e9
    selected = eligible & (s >= thr)

    if selected.any() and top_k > 0:
        tmp = frame.loc[selected, ["decision_time"]].copy()
        tmp["_score"] = s[selected]
        tmp = tmp.sort_values(["decision_time", "_score"], ascending=[True, False])
        tmp["_rank"] = tmp.groupby("decision_time").cumcount()
        keep_idx = tmp.index[tmp["_rank"] < top_k]
        selected = pd.Series(False, index=frame.index)
        selected.loc[keep_idx] = True
    return selected, thr


def run_arms_oos(buys_features: pd.DataFrame, labeled_by_arm: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Walk-forward on each arm's decision_time panel. Label: DIAGNOSTIC_REPLAY."""
    freeze = load_freeze()
    q = float(freeze["selection"]["score_quantile"])
    top_k = int(freeze["selection"]["top_k_per_decision_window"])
    arms = list(freeze["arms"])
    oof: dict[str, list[pd.DataFrame]] = {a: [] for a in arms}

    for arm in arms:
        panel = labeled_by_arm[arm]
        d = prepare_splits(panel, freeze)
        folds = sorted([f for f in d["fold"].dropna().unique() if int(f) >= 0])
        print(f"[arms] {arm} folds={folds} n={len(d):,}", flush=True)
        for fold in folds:
            train = d[(d["fold"] == fold) & (d["split"] == "TRAIN")]
            test = d[(d["fold"] == fold) & (d["split"] == "TEST")]
            if test.empty:
                sub = d[d["fold"] == fold]
                if sub.empty:
                    continue
                cut = sub["decision_time"].quantile(0.7)
                train = sub[sub["decision_time"] <= cut]
                test = sub[sub["decision_time"] > cut]
            if test.empty:
                continue
            _, thr = select_arm(train if len(train) else test, arm, quantile=q, top_k=top_k)
            sel, _ = select_arm(test, arm, quantile=q, top_k=top_k, train_threshold=thr)
            chunk = test.copy()
            chunk["policy_selected"] = sel.astype(bool)
            chunk["arm"] = arm
            chunk["train_threshold"] = thr
            chunk["historical_label"] = "DIAGNOSTIC_REPLAY"
            oof[arm].append(chunk)

    out = {}
    for arm, parts in oof.items():
        if parts:
            out[arm] = pd.concat(parts, ignore_index=True)
        else:
            panel = labeled_by_arm[arm]
            d = prepare_splits(panel, freeze)
            sub = d[~d["is_sealed"]].copy()
            sel, thr = select_arm(sub, arm, quantile=q, top_k=top_k)
            sub["policy_selected"] = sel
            sub["arm"] = arm
            sub["train_threshold"] = thr
            sub["fold"] = 0
            sub["historical_label"] = "DIAGNOSTIC_REPLAY"
            out[arm] = sub
        print(
            f"[arms] {arm}: oof_n={len(out[arm]):,} selected={int(out[arm]['policy_selected'].sum())}",
            flush=True,
        )
    return out


def capacity_matched_random(frame: pd.DataFrame, n_select: int, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    eligible = frame["buy_status"].eq("SUCCESS") if "buy_status" in frame.columns else pd.Series(True, index=frame.index)
    if "scorable_primary" in frame.columns:
        eligible &= frame["scorable_primary"].astype(bool)
    idx = frame.index[eligible].to_numpy()
    if len(idx) == 0 or n_select <= 0:
        return pd.Series(False, index=frame.index)
    pick = rng.choice(idx, size=min(n_select, len(idx)), replace=False)
    out = pd.Series(False, index=frame.index)
    out.loc[pick] = True
    return out


def size_permutation(frame: pd.DataFrame, score_col: str, seed: int = 42) -> pd.Series:
    """Permute relative-size scores within age/depth strata — destroys size info."""
    rng = np.random.default_rng(seed)
    s = pd.to_numeric(frame.get(score_col, 0), errors="coerce").fillna(0.0).to_numpy().copy()
    if "age_bucket" in frame.columns:
        for _, idx in frame.groupby("age_bucket", observed=False).groups.items():
            arr = list(idx)
            vals = s[arr]
            rng.shuffle(vals)
            s[arr] = vals
    else:
        rng.shuffle(s)
    return pd.Series(s, index=frame.index)
