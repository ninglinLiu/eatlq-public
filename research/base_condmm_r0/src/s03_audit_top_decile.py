#!/usr/bin/env python3
"""Audit the top FV decile, which the pre-registered dose-response put at
approximately break-even. Per GOAL.md this decile is SECONDARY and cannot
produce a PASS; the purpose here is to characterise it so the round's report
can say honestly what it is and is not.

Applies the round-40 mandatory clauses: single-symbol concentration, leave-one-out,
venue composition (which decides whether the concurrent Aerodrome emissions
round's revenue term even applies to this subset), and capacity share.
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
    k = len(s)
    if k == 0:
        return None
    idx = RNG.integers(0, k, size=(B, k))
    m = s[idx].sum(1) / np.maximum(n[idx].sum(1), 1e-12)
    return {"mean": float(s.sum() / n.sum()),
            "lo": float(np.percentile(m, 2.5)),
            "hi": float(np.percentile(m, 97.5)),
            "blocks": k, "rows": int(n.sum())}


def main():
    txt = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
         "SELECT pool, venue, fee, symbol, tradeable_days, cost_p50 "
         "FROM edge.bars_universe_50 FORMAT TabSeparated"],
        capture_output=True, text=True, timeout=300).stdout
    meta = pd.DataFrame(
        [l.split("\t") for l in txt.rstrip("\n").split("\n") if l],
        columns=["pool", "venue", "fee_u", "symbol_u", "tradeable_days", "cost_p50"])
    meta["cost_p50"] = meta.cost_p50.astype(float)

    pos = pd.DataFrame(json.loads(POSITIONS.read_text()))
    pos["lr"] = np.log(pos.p_out / pos.p_entry)
    pos["quiet"] = pos.lr.abs() <= 0.02

    pan = pd.DataFrame(json.loads((ROOT / "out" / "panel.json").read_text()))
    out = {}

    for T in (1, 7, 30):
        p = pan.loc[(pan.L == 168) & pan.fv.notna(), ["pool", "entry", "fv"]]
        sub = pos.loc[pos["T"] == T]
        keys = sub[["entry", "pool"]].drop_duplicates().merge(p, on=["pool", "entry"])
        keys["pct"] = keys.groupby("entry")["fv"].rank(pct=True, method="average")
        top = sub.merge(keys.loc[keys.pct >= 0.90, ["entry", "pool", "fv"]],
                        on=["entry", "pool"]).merge(meta, on="pool", how="left")

        r = boot(top)
        by_sym = top.groupby("symbol")[OUTCOME].agg(["sum", "count", "mean"])
        tot = by_sym["sum"].sum()
        by_sym["share_of_net"] = by_sym["sum"] / tot if tot else np.nan
        by_sym = by_sym.sort_values("sum")

        # leave-one-symbol-out, worst lower bound
        worst, wsym = None, None
        for sym in top.symbol.unique():
            r2 = boot(top.loc[top.symbol != sym])
            if r2 and (worst is None or r2["lo"] < worst):
                worst, wsym = r2["lo"], sym

        # majors vs non-majors: the program's F9 finding is that only mid-cap
        # real assets survive the cost bound, so split explicitly
        MAJ = {"USDC", "USDbC", "USDT", "cbBTC", "cbETH", "wstETH", "WETH",
               "weETH", "rETH", "USDe", "EURC", "DAI", "USD+", "bsdETH", "tBTC"}
        top["is_major"] = top.symbol.isin(MAJ)

        out[f"T{T}"] = {
            "overall": r,
            "quiet_frac": float(top.quiet.mean()),
            "fee_bps": float(top.fee_bps.mean()),
            "il_bps": float(top.il_bps.mean()),
            "cost_bps": float(top.cost_strict_bps.mean()),
            "fee_over_il": float(top.fee_bps.mean() / max(top.il_bps.mean(), 1e-9)),
            "n_symbols": int(top.symbol.nunique()),
            "top_symbol": str(by_sym.index[-1]),
            "top_symbol_share_of_net": float(by_sym["share_of_net"].iloc[-1]),
            "loo_worst_lo": worst, "loo_worst_symbol": str(wsym),
            "venue_rows": top.venue.value_counts().to_dict(),
            "venue_net": top.groupby("venue")[OUTCOME].mean().to_dict(),
            "major": boot(top.loc[top.is_major]),
            "non_major": boot(top.loc[~top.is_major]),
            "major_rows": int(top.is_major.sum()),
            "non_major_rows": int((~top.is_major).sum()),
            "share_p50": float(top.share.median()),
            "share_p90": float(top.share.quantile(0.90)),
            "share_p99": float(top.share.quantile(0.99)),
            "cost_p50_of_selected": float(top.cost_p50.median()),
            "worst_symbols": by_sym.head(8).round(2).to_dict("index"),
            "best_symbols": by_sym.tail(8).round(2).to_dict("index"),
        }

        print(f"\n{'='*84}\nT={T}  top FV decile   n={r['rows']}  blocks={r['blocks']}")
        print(f"  net {r['mean']:>8.1f}  [{r['lo']:>8.1f}, {r['hi']:>8.1f}]")
        print(f"  quiet {top.quiet.mean():.3f}  fee {top.fee_bps.mean():.1f}  "
              f"IL {top.il_bps.mean():.1f}  cost {top.cost_strict_bps.mean():.1f}")
        print(f"  symbols {top.symbol.nunique()}  top={by_sym.index[-1]} "
              f"({by_sym['share_of_net'].iloc[-1]:.1%} of net)  "
              f"LOO worst lo={worst:.1f} ({wsym})")
        print(f"  venue rows {top.venue.value_counts().to_dict()}")
        print(f"  venue mean net {ll(top.groupby('venue')[OUTCOME].mean())}")
        mj, nm = out[f'T{T}']['major'], out[f'T{T}']['non_major']
        if mj:
            print(f"  MAJOR     n={mj['rows']:>5} net {mj['mean']:>8.1f} "
                  f"[{mj['lo']:>8.1f}, {mj['hi']:>8.1f}]")
        if nm:
            print(f"  NON-MAJOR n={nm['rows']:>5} net {nm['mean']:>8.1f} "
                  f"[{nm['lo']:>8.1f}, {nm['hi']:>8.1f}]")

    with open(ROOT / "out" / "top_decile_audit.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("\n[s03] wrote out/top_decile_audit.json")


def ll(series):
    return {k: round(v, 1) for k, v in series.items()}


if __name__ == "__main__":
    main()
