#!/usr/bin/env python3
"""C1b test, executing GOAL.md sections 4-7 exactly as pre-registered.

Primary unit, the only one permitted to produce a PASS:
    predictor FV, lookback 168h, top quartile, T=7, pooled over c.

Verdict thresholds are fixed in GOAL.md and are not read from the data.
Zero RPC. Reads only; writes only under this workspace.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_condmm_r0")
POSITIONS = Path("/root/base_fee_tier_r0/out/stage3_pnl_50.json")

OUTCOME = "net_strict_bps"          # GOAL.md 2, fixed
TICK_LO, TICK_HI = 1e-30, 1e30      # GOAL.md 3
B = 10_000                          # GOAL.md 5
SPLIT = "2026-02-16"                # GOAL.md 5, fixed in advance
MIN_BLOCKS = 30                     # GOAL.md 5
CONC_LIMIT = 0.30                   # GOAL.md 5, round-40 mandatory clause
QUIET = 0.02                        # |log(p_out/p_entry)| <= 2%, GOAL.md 6

RNG = np.random.default_rng(20260819)


# ---------------------------------------------------------------- data

def load() -> tuple[pd.DataFrame, dict]:
    pos = pd.DataFrame(json.loads(POSITIONS.read_text()))
    n0 = len(pos)

    valid = (
        np.isfinite(pos.p_entry) & np.isfinite(pos.p_out)
        & (pos.p_entry > TICK_LO) & (pos.p_entry < TICK_HI)
        & (pos.p_out > TICK_LO) & (pos.p_out < TICK_HI)
        & np.isfinite(pos[OUTCOME]) & np.isfinite(pos.fee_bps) & np.isfinite(pos.il_bps)
    )
    dropped = pos.loc[~valid]
    pos = pos.loc[valid].copy()
    pos["lr"] = np.log(pos.p_out / pos.p_entry)
    pos["quiet"] = pos.lr.abs() <= QUIET

    pan = pd.DataFrame(json.loads((ROOT / "out" / "panel.json").read_text()))

    meta = {
        "positions_in": int(n0),
        "positions_kept": int(len(pos)),
        "dropped_tick_or_nonfinite": int(n0 - len(pos)),
        "dropped_symbols": dropped.symbol.value_counts().head(10).to_dict(),
        "panel_records": int(len(pan)),
    }
    return pos, pan, meta


# ------------------------------------------------------- bootstrap core

def day_blocks(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    g = df.groupby("entry")[OUTCOME].agg(["sum", "count"])
    return g["sum"].to_numpy(float), g["count"].to_numpy(float)


def boot_ci(s: np.ndarray, n: np.ndarray) -> dict:
    """Day-block bootstrap of the ratio estimator sum(net)/count. O(B x days)."""
    k = len(s)
    if k == 0:
        return {"mean": None, "lo": None, "hi": None, "blocks": 0}
    idx = RNG.integers(0, k, size=(B, k))
    means = s[idx].sum(1) / np.maximum(n[idx].sum(1), 1e-12)
    return {
        "mean": float(s.sum() / n.sum()),
        "lo": float(np.percentile(means, 2.5)),
        "hi": float(np.percentile(means, 97.5)),
        "blocks": int(k),
        "n_rows": int(n.sum()),
    }


# -------------------------------------------------------- selection

def select(pos: pd.DataFrame, pan: pd.DataFrame, pred: str, L: int,
           q: float, high: bool, T: int) -> pd.DataFrame:
    """Cross-sectional quantile selection on each entry date, GOAL.md 4."""
    p = pan.loc[(pan.L == L) & pan[pred].notna(), ["pool", "entry", pred]]
    sub = pos.loc[pos["T"] == T]
    # rank at (entry, pool) level: the predictor does not depend on c
    keys = sub[["entry", "pool"]].drop_duplicates().merge(p, on=["pool", "entry"])
    if keys.empty:
        return keys
    keys["pct"] = keys.groupby("entry")[pred].rank(pct=True, method="average")
    chosen = keys.loc[keys.pct <= q] if not high else keys.loc[keys.pct >= 1.0 - q]
    return sub.merge(chosen[["entry", "pool", pred, "pct"]], on=["entry", "pool"])


# ---------------------------------------------------------- reporting

def unit(pos, pan, pred, L, q, high, T, label, full_loo=False) -> dict:
    sel = select(pos, pan, pred, L, q, high, T)
    if sel.empty:
        return {"label": label, "empty": True}
    s, n = day_blocks(sel)
    res = boot_ci(s, n)
    res.update({
        "label": label, "predictor": pred, "L": L, "q": q,
        "direction": "high" if high else "low", "T": T,
        "fee_bps": float(sel.fee_bps.mean()),
        "il_bps": float(sel.il_bps.mean()),
        "cost_bps": float(sel.cost_strict_bps.mean()),
        "quiet_frac": float(sel.quiet.mean()),
        "n_symbols": int(sel.symbol.nunique()),
        "share_p50": float(sel.share.median()),
        "share_p90": float(sel.share.quantile(0.90)),
        "share_p99": float(sel.share.quantile(0.99)),
    })

    # concentration, round-40 mandatory clause
    by_sym = sel.groupby("symbol")[OUTCOME].sum()
    tot = by_sym.sum()
    if tot != 0:
        shares = (by_sym / tot).sort_values(ascending=False)
        res["top_symbol"] = str(shares.index[0])
        res["top_symbol_net_share"] = float(shares.iloc[0])
    else:
        res["top_symbol"], res["top_symbol_net_share"] = None, None

    # leave-one-symbol-out: worst case lower bound. Only the primary unit needs
    # this; secondary units cannot produce a PASS so the cheap share suffices.
    worst, worst_sym = None, None
    if full_loo:
        for sym in sel.symbol.unique():
            s2, n2 = day_blocks(sel.loc[sel.symbol != sym])
            r2 = boot_ci(s2, n2)
            if r2["lo"] is not None and (worst is None or r2["lo"] < worst):
                worst, worst_sym = r2["lo"], str(sym)
    res["loo_worst_lo"] = worst
    res["loo_worst_symbol"] = worst_sym

    # half-year stability, split fixed in advance
    for name, mask in (("h1", sel.entry < SPLIT), ("h2", sel.entry >= SPLIT)):
        s3, n3 = day_blocks(sel.loc[mask])
        res[name] = boot_ci(s3, n3)
    m1, m2 = res["h1"]["mean"], res["h2"]["mean"]
    res["same_sign"] = bool(m1 is not None and m2 is not None
                            and np.sign(m1) == np.sign(m2))

    # venue mix, for the concurrent emissions round
    res["venue_rows"] = sel.groupby(
        sel.pool.map(VENUE).fillna("unknown")).size().to_dict()
    return res


def verdict(u: dict) -> str:
    """GOAL.md 5, thresholds fixed in advance."""
    if u.get("empty"):
        return "EMPTY"
    lo, hi = u["lo"], u["hi"]
    if lo is None:
        return "EMPTY"
    conc_ok = (u["top_symbol_net_share"] is None
               or u["top_symbol_net_share"] <= CONC_LIMIT
               or (u["loo_worst_lo"] is not None and u["loo_worst_lo"] > 0))
    if lo > 0 and u["blocks"] >= MIN_BLOCKS and conc_ok and u["same_sign"]:
        return "CONDITIONAL_MM_PAYS"
    if hi < 0:
        return "NO_CONDITIONAL_MM_EDGE"
    return "UNDERPOWERED"


def dose_response(pos, pan, pred, L, T) -> list[dict]:
    p = pan.loc[(pan.L == L) & pan[pred].notna(), ["pool", "entry", pred]]
    sub = pos.loc[pos["T"] == T]
    keys = sub[["entry", "pool"]].drop_duplicates().merge(p, on=["pool", "entry"])
    keys["pct"] = keys.groupby("entry")[pred].rank(pct=True, method="average")
    keys["dec"] = np.clip((keys.pct * 10).astype(int), 0, 9)
    m = sub.merge(keys[["entry", "pool", "dec"]], on=["entry", "pool"])
    rows = []
    for d, g in m.groupby("dec"):
        s, n = day_blocks(g)
        r = boot_ci(s, n)
        rows.append({"decile": int(d) + 1, "net_bps": r["mean"],
                     "lo": r["lo"], "hi": r["hi"],
                     "fee_bps": float(g.fee_bps.mean()),
                     "il_bps": float(g.il_bps.mean()),
                     "quiet_frac": float(g.quiet.mean()),
                     "n_rows": int(len(g))})
    return rows


# ------------------------------------------------------------- main

VENUE: dict[str, str] = {}


def main():
    global VENUE
    import subprocess
    txt = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
         "SELECT pool, venue FROM edge.bars_universe_50 FORMAT TabSeparated"],
        capture_output=True, text=True, timeout=300).stdout
    VENUE = dict(l.split("\t") for l in txt.rstrip("\n").split("\n") if l)

    pos, pan, meta = load()
    print(json.dumps(meta, indent=1))

    out = {"meta": meta, "goal": "GOAL.md", "outcome": OUTCOME,
           "thresholds": {"B": B, "split": SPLIT, "min_blocks": MIN_BLOCKS,
                          "conc_limit": CONC_LIMIT, "quiet": QUIET}}

    # ---- baseline: unconditional, for reference (not a verdict unit)
    for T in (1, 7, 30):
        s, n = day_blocks(pos.loc[pos["T"] == T])
        r = boot_ci(s, n)
        r["quiet_frac"] = float(pos.loc[pos["T"] == T, "quiet"].mean())
        out[f"baseline_T{T}"] = r
        print(f"[baseline] T={T:>2} mean={r['mean']:>9.1f} "
              f"[{r['lo']:>9.1f},{r['hi']:>9.1f}] blocks={r['blocks']:>3} "
              f"quiet={r['quiet_frac']:.3f}")

    # ---- PRIMARY unit
    prim = unit(pos, pan, "fv", 168, 0.25, True, 7, "PRIMARY fv_168 q25 T7",
                full_loo=True)
    prim["verdict"] = verdict(prim)
    out["primary"] = prim
    print("\n=== PRIMARY ===")
    print(json.dumps({k: v for k, v in prim.items()
                      if k not in ("venue_rows", "h1", "h2")}, indent=1))
    print("h1:", prim["h1"], "\nh2:", prim["h2"])

    # ---- secondary units, descriptive only
    sec = []
    grid = [("fv", True), ("rbar", True), ("sigma_rob", False), ("prior_net", True)]
    for pred, high in grid:
        if pred == "prior_net":
            continue
        for L in (24, 168, 720):
            for q in (0.10, 0.25, 0.50):
                for T in (1, 7, 30):
                    if (pred, L, q, T) == ("fv", 168, 0.25, 7):
                        continue
                    u = unit(pos, pan, pred, L, q, high,
                             T, f"{pred}_L{L}_q{int(q*100)}_T{T}")
                    u["verdict"] = "SECONDARY_DESCRIPTIVE"
                    sec.append(u)
    out["secondary"] = sec

    out["dose_response_fv168_T7"] = dose_response(pos, pan, "fv", 168, 7)
    out["dose_response_fv168_T1"] = dose_response(pos, pan, "fv", 168, 1)

    with open(ROOT / "out" / "verdict.json", "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\n[s02] wrote out/verdict.json  secondary units={len(sec)}")
    print(f"[s02] PRIMARY VERDICT = {prim['verdict']}")


if __name__ == "__main__":
    main()
