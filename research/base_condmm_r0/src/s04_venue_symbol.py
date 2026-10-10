#!/usr/bin/env python3
"""Decompose the top-FV-decile venue split by symbol.

The decisive question the audit raised: is Aerodrome's positive mean in the top
FV decile a venue effect spanning several assets, or is it the same single-asset
cbBTC artifact that round 40's mandatory concentration clause was written for?

If it is cbBTC only, this round confirms C3's mechanism and nothing more, and
cbBTC is quarantined by GOAL_R3_CBBTC.md so it cannot serve as confirmation.
If it spans assets, it is the first venue-level candidate in 42 rounds and it
composes with the concurrent emissions round.

Descriptive only. GOAL.md forbids any verdict from this split.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_condmm_r0")
POSITIONS = Path("/root/base_fee_tier_r0/out/stage3_pnl_50.json")
OUTCOME = "net_strict_bps"
B = 10_000
RNG = np.random.default_rng(20260819)


def boot(df):
    g = df.groupby("entry")[OUTCOME].agg(["sum", "count"])
    s, n = g["sum"].to_numpy(float), g["count"].to_numpy(float)
    if not len(s):
        return None
    idx = RNG.integers(0, len(s), size=(B, len(s)))
    m = s[idx].sum(1) / np.maximum(n[idx].sum(1), 1e-12)
    return {"mean": float(s.sum() / n.sum()),
            "lo": float(np.percentile(m, 2.5)),
            "hi": float(np.percentile(m, 97.5)),
            "blocks": len(s), "rows": int(n.sum())}


def main():
    txt = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
         "SELECT pool, venue, symbol, fee, cost_p50, tradeable_days "
         "FROM edge.bars_universe_50 FORMAT TabSeparated"],
        capture_output=True, text=True, timeout=300).stdout
    meta = pd.DataFrame([l.split("\t") for l in txt.rstrip("\n").split("\n") if l],
                        columns=["pool", "venue", "sym_u", "fee_u",
                                 "cost_p50", "tradeable_days"])
    print("venues in universe:", meta.venue.value_counts().to_dict())

    pos = pd.DataFrame(json.loads(POSITIONS.read_text()))
    pan = pd.DataFrame(json.loads((ROOT / "out" / "panel.json").read_text()))
    out = {"venues_in_universe": meta.venue.value_counts().to_dict()}

    for T in (7, 30):
        p = pan.loc[(pan.L == 168) & pan.fv.notna(), ["pool", "entry", "fv"]]
        sub = pos.loc[pos["T"] == T]
        keys = sub[["entry", "pool"]].drop_duplicates().merge(p, on=["pool", "entry"])
        keys["pct"] = keys.groupby("entry")["fv"].rank(pct=True, method="average")
        top = sub.merge(keys.loc[keys.pct >= 0.90, ["entry", "pool"]],
                        on=["entry", "pool"]).merge(meta, on="pool", how="left")
        top["venue"] = top.venue.fillna("no_meta")

        print(f"\n{'='*96}\nT={T}: top FV decile, venue x symbol")
        print(f"{'venue':>10}{'symbol':>10}{'rows':>7}{'blk':>5}"
              f"{'net':>9}{'lo':>9}{'hi':>9}{'fee':>8}{'IL':>8}{'feeTier':>8}")
        rec = []
        for (v, s), g in top.groupby(["venue", "sym_u"]):
            if len(g) < 20:
                continue
            r = boot(g)
            rec.append({"venue": v, "symbol": s, **r,
                        "fee_bps": float(g.fee_bps.mean()),
                        "il_bps": float(g.il_bps.mean()),
                        "fee_tier": g.fee_u.iloc[0]})
            print(f"{v:>10}{s:>10}{r['rows']:>7}{r['blocks']:>5}"
                  f"{r['mean']:>9.1f}{r['lo']:>9.1f}{r['hi']:>9.1f}"
                  f"{g.fee_bps.mean():>8.1f}{g.il_bps.mean():>8.1f}"
                  f"{g.fee_u.iloc[0]:>8}")
        out[f"T{T}_venue_symbol"] = rec

        # Aerodrome with cbBTC removed -- the decisive number
        aero = top.loc[top.venue == "aero_cl"]
        for label, d in (("aero_all", aero),
                         ("aero_ex_cbBTC", aero.loc[aero.sym_u != "cbBTC"]),
                         ("univ3_all", top.loc[top.venue == "univ3"]),
                         ("univ3_ex_cbBTC", top.loc[(top.venue == "univ3")
                                                    & (top.sym_u != "cbBTC")]),
                         ("all_ex_cbBTC", top.loc[top.sym_u != "cbBTC"])):
            r = boot(d)
            out[f"T{T}_{label}"] = r
            if r:
                print(f"  {label:>18} n={r['rows']:>5} blocks={r['blocks']:>3} "
                      f"net {r['mean']:>8.1f} [{r['lo']:>8.1f}, {r['hi']:>8.1f}] "
                      f"symbols={d.sym_u.nunique()}")

        # per-symbol leave-one-out inside Aerodrome
        ax = aero.loc[aero.sym_u != "cbBTC"]
        if len(ax):
            worst, wsym = None, None
            for sym in ax.sym_u.unique():
                r2 = boot(ax.loc[ax.sym_u != sym])
                if r2 and (worst is None or r2["lo"] < worst):
                    worst, wsym = r2["lo"], sym
            out[f"T{T}_aero_ex_cbBTC_loo_worst"] = {"lo": worst, "symbol": str(wsym)}
            print(f"  aero ex-cbBTC LOO worst lo = {worst:.1f} (drop {wsym})")

    with open(ROOT / "out" / "venue_symbol.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("\n[s04] wrote out/venue_symbol.json")


if __name__ == "__main__":
    main()
