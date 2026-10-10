"""Track B: signal-level covariate-matched incremental return (primary power test).

Treatment = LBA-eligible (large_buy_score>0 & absorbed>0).
Control = non-LBA buys in same universe, matched on depth/age/size/momentum strata.
Outcome = wallet_pnl_weth from lookahead-free / loss-truth labels.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import OUT, save_json, utcnow
from .v3_common import V3_OUT, ensure_v3_out, load_v3


def _add_bins(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    age = pd.to_numeric(d.get("pool_age_s", 0), errors="coerce").fillna(0)
    d["age_bucket"] = pd.cut(
        age,
        bins=[-1, 900, 3600, 21600, 86400, 1e18],
        labels=["0-15m", "15-60m", "1-6h", "6-24h", ">24h"],
    ).astype(str)
    d["_depth"] = pd.to_numeric(d.get("depth_score", 0), errors="coerce").fillna(0.0)
    d["_mom"] = pd.to_numeric(d.get("mom_from_open", 0), errors="coerce").fillna(0.0)
    d["_size"] = pd.to_numeric(d.get("rel_size", 0), errors="coerce").fillna(0.0)
    if d["_size"].abs().sum() == 0:
        d["_size"] = pd.to_numeric(d.get("quote_weth", 0), errors="coerce").fillna(0.0)

    def _decile(s: pd.Series) -> pd.Series:
        try:
            return pd.qcut(s.rank(method="first"), 10, labels=False, duplicates="drop").astype(str)
        except ValueError:
            return pd.Series("0", index=s.index)

    d["depth_bin"] = _decile(d["_depth"])
    d["size_bin"] = _decile(d["_size"])
    d["mom_bin"] = _decile(d["_mom"])
    d["match_stratum"] = (
        d["age_bucket"].astype(str) + "|"
        + d["depth_bin"].astype(str) + "|"
        + d["size_bin"].astype(str) + "|"
        + d["mom_bin"].astype(str)
    )
    return d


def _cluster_boot_mean(values: np.ndarray, clusters: np.ndarray, n_boot: int, seed: int) -> dict:
    if len(values) == 0:
        return {"mean": 0.0, "p05": 0.0, "frac_positive": 0.0, "n_clusters": 0}
    rng = np.random.default_rng(seed)
    s = pd.Series(values).groupby(clusters, sort=False).mean()
    g = s.to_numpy(dtype=float)
    idx = rng.integers(0, len(g), size=(n_boot, len(g)))
    boots = g[idx].mean(axis=1)
    return {
        "mean": float(boots.mean()),
        "p05": float(np.quantile(boots, 0.05)),
        "p50": float(np.quantile(boots, 0.50)),
        "p95": float(np.quantile(boots, 0.95)),
        "frac_positive": float((boots > 0).mean()),
        "n_clusters": int(len(g)),
    }


def _match_att(
    treat: pd.DataFrame,
    control: pd.DataFrame,
    *,
    max_pairs: int,
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    treat = _add_bins(treat)
    control = _add_bins(control)

    ctrl_pos: dict[str, list[int]] = {}
    for i, st in enumerate(control["match_stratum"].astype(str).to_numpy()):
        ctrl_pos.setdefault(st, []).append(i)
    for st in ctrl_pos:
        rng.shuffle(ctrl_pos[st])
    ptr = {st: 0 for st in ctrl_pos}

    t_pnl = pd.to_numeric(treat["wallet_pnl_weth"], errors="coerce").fillna(0.0).to_numpy()
    c_pnl = pd.to_numeric(control["wallet_pnl_weth"], errors="coerce").fillna(0.0).to_numpy()
    t_stratum = treat["match_stratum"].astype(str).to_numpy()
    t_token = treat["token"].astype(str).to_numpy()
    t_day = treat["day"].astype(str).to_numpy() if "day" in treat.columns else pd.to_datetime(
        treat["decision_time"], unit="s", utc=True
    ).dt.strftime("%Y-%m-%d").to_numpy()

    deltas, toks, days = [], [], []
    bal = {"t_size": [], "c_size": [], "t_depth": [], "c_depth": [], "t_mom": [], "c_mom": []}
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
        bal["t_size"].append(float(treat["_size"].iloc[ti]))
        bal["c_size"].append(float(control["_size"].iloc[cj]))
        bal["t_depth"].append(float(treat["_depth"].iloc[ti]))
        bal["c_depth"].append(float(control["_depth"].iloc[cj]))
        bal["t_mom"].append(float(treat["_mom"].iloc[ti]))
        bal["c_mom"].append(float(control["_mom"].iloc[cj]))

    delta = np.asarray(deltas, dtype=float)
    boot_tok = _cluster_boot_mean(delta, np.asarray(toks), n_boot, seed)
    boot_day = _cluster_boot_mean(delta, np.asarray(days), n_boot, seed + 1)

    # Permutation: shuffle treatment labels within strata via reshuffling deltas sign proxy
    # More honest: permute which of pair is treat — equivalent to flipping delta signs randomly
    rng2 = np.random.default_rng(seed + 9)
    perm_means = []
    for _ in range(min(200, n_boot)):
        signs = rng2.choice([-1.0, 1.0], size=len(delta))
        perm_means.append(float((delta * signs).mean()) if len(delta) else 0.0)
    obs_mean = float(delta.mean()) if len(delta) else 0.0
    perm_p = float(np.mean(np.asarray(perm_means) >= obs_mean)) if perm_means else 1.0

    def _smd(a, b):
        a, b = np.asarray(a, float), np.asarray(b, float)
        if len(a) == 0:
            return None
        pooled = np.sqrt(0.5 * (a.var() + b.var()))
        return float((a.mean() - b.mean()) / pooled) if pooled > 1e-12 else 0.0

    return {
        "n_treat": int(len(treat)),
        "n_control": int(len(control)),
        "n_pairs": int(len(delta)),
        "n_unmatched": int(n_unmatched),
        "match_rate": float(len(delta) / max(1, len(treat))),
        "att_mean_delta_weth": obs_mean,
        "att_sum_delta_weth": float(delta.sum()) if len(delta) else 0.0,
        "bootstrap_token": boot_tok,
        "bootstrap_day": boot_day,
        "permutation_p_one_sided": perm_p,
        "passes_token_p05": bool(boot_tok["p05"] > 0),
        "passes_day_p05": bool(boot_day["p05"] > 0),
        "balance_smd": {
            "size": _smd(bal["t_size"], bal["c_size"]),
            "depth": _smd(bal["t_depth"], bal["c_depth"]),
            "mom": _smd(bal["t_mom"], bal["c_mom"]),
        },
    }


def run_matched_incremental(labeled: pd.DataFrame | None = None) -> dict[str, Any]:
    ensure_v3_out()
    cfg = load_v3()["matched_incremental"]
    if labeled is None:
        # Prefer wait_absorption labels (5s absorb known); same PnL path for immediate would differ entry
        path = OUT / "panel" / "labeled_wait_absorption.parquet"
        labeled = pd.read_parquet(path)
    d = labeled.copy()
    if "day" not in d.columns:
        d["day"] = pd.to_datetime(d["decision_time"], unit="s", utc=True).dt.strftime("%Y-%m-%d")

    elig = pd.Series(True, index=d.index)
    if "scorable_primary" in d.columns:
        elig &= d["scorable_primary"].astype(bool)
    if "buy_status" in d.columns:
        elig &= d["buy_status"].eq("SUCCESS")
    d = d.loc[elig].copy()

    lbs = pd.to_numeric(d.get("large_buy_score", 0), errors="coerce").fillna(0.0)
    absb = pd.to_numeric(d.get("absorbed", 0), errors="coerce").fillna(0.0)
    treat_m = (lbs > 0) & (absb > 0)
    treat = d.loc[treat_m]
    control = d.loc[~treat_m]

    print(f"[v3/B] treat={len(treat):,} control={len(control):,}", flush=True)
    att = _match_att(
        treat,
        control,
        max_pairs=int(cfg["max_pairs"]),
        n_boot=int(cfg["n_boot"]),
        seed=int(cfg["seed"]),
    )

    # Proxy ablation: size/depth-only "treatment" vs residual absorption effect
    # Compare ATT of (large_buy only, ignore absorb) vs LBA; and (absorb only)
    lb_only = d.loc[lbs > 0]
    ctrl_lb = d.loc[lbs <= 0]
    att_lb = _match_att(lb_only, ctrl_lb, max_pairs=int(cfg["max_pairs"]), n_boot=int(cfg["n_boot"]), seed=int(cfg["seed"]) + 3)

    abs_only = d.loc[absb > 0]
    ctrl_abs = d.loc[absb <= 0]
    att_abs = _match_att(abs_only, ctrl_abs, max_pairs=int(cfg["max_pairs"]), n_boot=int(cfg["n_boot"]), seed=int(cfg["seed"]) + 5)

    # Within large-buy stratum: absorbed vs not (isolates absorption beyond size)
    within_lb = d.loc[lbs > 0].copy()
    att_within = _match_att(
        within_lb.loc[pd.to_numeric(within_lb["absorbed"], errors="coerce").fillna(0) > 0],
        within_lb.loc[pd.to_numeric(within_lb["absorbed"], errors="coerce").fillna(0) <= 0],
        max_pairs=int(cfg["max_pairs"]),
        n_boot=int(cfg["n_boot"]),
        seed=int(cfg["seed"]) + 7,
    )

    signal_proven = bool(
        att["passes_token_p05"] and att["passes_day_p05"] and att["att_mean_delta_weth"] > 0
    )
    if signal_proven:
        signal_v = "PROVEN"
    elif att["att_mean_delta_weth"] > 0 and (att["bootstrap_day"]["frac_positive"] > 0.7):
        signal_v = "UNCONFIRMED"
    elif att["att_mean_delta_weth"] <= 0 and not att["passes_day_p05"]:
        signal_v = "REFUTED"
    else:
        signal_v = "UNCONFIRMED"

    # Not just size/depth proxy: within-LB absorption ATT must remain positive & significant
    proxy_proven = bool(
        att_within["passes_token_p05"]
        and att_within["att_mean_delta_weth"] > 0
        and att["att_mean_delta_weth"] > att_lb["att_mean_delta_weth"]
    )
    if proxy_proven:
        proxy_v = "PROVEN"
    elif att_within["att_mean_delta_weth"] <= 0:
        proxy_v = "REFUTED"  # absorption adds nothing within size stratum → is size/depth proxy
    else:
        proxy_v = "UNCONFIRMED"

    report = {
        "task": "MATCHED_INCREMENTAL",
        "generated_at": utcnow(),
        "universe_n": int(len(d)),
        "att_lba_vs_nonlba": att,
        "att_large_buy_only": att_lb,
        "att_absorption_only": att_abs,
        "att_absorption_within_large_buy": att_within,
        "verdicts": {
            "SIGNAL_LEVEL_INCREMENTAL_RETURN": signal_v,
            "WHALE_NOT_JUST_SIZE_DEPTH_PROXY": proxy_v,
        },
        "interpretation": (
            "Primary power test is ATT of LBA-eligible vs matched non-LBA buys on wallet_pnl_weth. "
            "Proxy check: absorption must still help inside the large-buy stratum."
        ),
    }
    save_json(V3_OUT / "matched_incremental.json", report)
    print(
        f"[v3/B] ATT={att['att_mean_delta_weth']:.6f} pairs={att['n_pairs']} "
        f"day_p05={att['bootstrap_day']['p05']:.6f} → {signal_v}; proxy={proxy_v}",
        flush=True,
    )
    return report


if __name__ == "__main__":
    run_matched_incremental()
