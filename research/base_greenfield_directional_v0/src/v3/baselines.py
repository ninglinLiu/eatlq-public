"""C0–C3 capacity-matched baselines for V3 Rescue."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.v3.features import PRICE_ONLY


def _topk_mask(scores: np.ndarray, times: np.ndarray, k: int, thr: float | None = None) -> np.ndarray:
    selected = np.zeros(len(scores), dtype=bool)
    df = pd.DataFrame({"s": scores, "t": times})
    for _, g in df.groupby("t"):
        if len(g) >= 2:
            take = g["s"].nlargest(min(k, len(g))).index.to_numpy()
            selected[take] = True
        else:
            i = g.index[0]
            selected[i] = True if thr is None else bool(g.loc[i, "s"] >= thr)
    return selected


def apply_baselines(
    df: pd.DataFrame,
    *,
    n_select_target: int | None = None,
    top_k: int = 2,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Add baseline selection columns on the provided frame (typically DEV OOF rows)."""
    rng = rng or np.random.default_rng(0)
    out = df.copy()
    n = len(out)
    times = out["decision_time"].to_numpy()

    # C0: no trade
    out["sel_C0_NO_TRADE"] = False

    # C1: capacity-matched random — match dual-head select rate later; placeholder uses top_k density
    rand_scores = rng.random(n)
    out["score_C1"] = rand_scores
    out["sel_C1_CAPACITY_MATCHED_RANDOM"] = _topk_mask(rand_scores, times, top_k)

    # C2: price-path baseline — rank by early_ret / mom
    for c in PRICE_ONLY:
        if c not in out.columns:
            out[c] = 0.0
    score_c2 = (
        out["early_ret"].fillna(0.0)
        + 0.5 * out["mom_h2"].fillna(0.0)
        - 0.25 * out["pre_vol"].fillna(0.0)
    ).to_numpy()
    out["score_C2"] = score_c2
    out["sel_C2_PRICE_PATH_BASELINE"] = _topk_mask(score_c2, times, top_k)

    # C3: simple transparent rule — flow persist high, liq not withdrawing, both sides present
    rule = (
        (out.get("flow_persist_score", 0) >= 0.35)
        & (out.get("liq_drawdown", 0) > -0.5)
        & (out.get("buys_10m", 0) >= 1)
        & (out.get("sells_10m", 0) >= 1)
        & (out.get("buy_ok_proxy", 0) >= 1)
    )
    score_c3 = (
        out.get("flow_persist_score", pd.Series(0, index=out.index)).fillna(0)
        + out.get("depth_proxy", pd.Series(0, index=out.index)).fillna(0)
        - out.get("deployer_prior_fatal_rate", pd.Series(0, index=out.index)).fillna(0)
    ).to_numpy()
    score_c3 = np.where(rule.to_numpy(), score_c3, -1e9)
    out["score_C3"] = score_c3
    out["sel_C3_SIMPLE_TRANSPARENT_RULE"] = _topk_mask(score_c3, times, top_k, thr=0.0)

    if n_select_target is not None and n_select_target >= 0:
        # re-match C1 to exact count
        idx = np.where(out["formal_bucket"].to_numpy() if "formal_bucket" in out.columns else np.ones(n, dtype=bool))[0]
        pick = rng.choice(idx, size=min(n_select_target, len(idx)), replace=False) if len(idx) else []
        mask = np.zeros(n, dtype=bool)
        mask[list(pick)] = True
        out["sel_C1_CAPACITY_MATCHED_RANDOM"] = mask

    return out


BASELINE_SELECT_COLS = {
    "C0_NO_TRADE": "sel_C0_NO_TRADE",
    "C1_CAPACITY_MATCHED_RANDOM": "sel_C1_CAPACITY_MATCHED_RANDOM",
    "C2_PRICE_PATH_BASELINE": "sel_C2_PRICE_PATH_BASELINE",
    "C3_SIMPLE_TRANSPARENT_RULE": "sel_C3_SIMPLE_TRANSPARENT_RULE",
}
