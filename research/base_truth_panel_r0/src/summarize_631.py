#!/usr/bin/env python3
"""Flatten depth_631.json into a reviewable TSV plus the tables the report cites."""
from __future__ import annotations

import collections
import json

MAJOR = {
    "USDC", "USDbC", "cbETH", "AERO", "cbBTC", "wstETH", "weETH", "rETH", "USDT", "DAI",
    "EURC", "tBTC", "LBTC", "ezETH", "wrsETH", "superOETHb", "msETH", "USDS", "sUSDS",
    "GHO", "USDz", "cbADA", "cbDOGE", "cbXRP", "wM", "mBASIS", "USD+", "USDA", "crvUSD",
    "frxETH", "sfrxETH", "ETHx", "osETH", "rsETH", "uSOL", "uXRP", "uADA", "uDOGE",
}
SIZES = [50, 250, 1000, 5000]


def q(v: list[float], p: float) -> float:
    return v[min(int(p * len(v)), len(v) - 1)] if v else float("nan")


if __name__ == "__main__":
    d = json.load(open("out/depth_631.json"))
    syms = json.load(open("out/alt_symbols.json"))
    by_pool: dict[str, dict] = collections.defaultdict(dict)
    for r in d["rows"]:
        by_pool[r["pool"]][r["usd"]] = r

    cols = ["pool", "venue", "factory", "alt", "symbol", "is_major", "n_days", "n_swaps",
            "dialect", "klass"] + [f"rt_bps_{s}" for s in SIZES]
    lines = ["\t".join(cols)]
    for pool, sz in sorted(by_pool.items(), key=lambda kv: -int(kv[1][SIZES[0]]["n_swaps"])):
        a = sz[SIZES[0]]
        sym = syms.get(a["alt"], "?")
        row = [pool, a["venue"], a["factory"], a["alt"], sym, str(int(sym in MAJOR)),
               a["n_days"], a["n_swaps"], a.get("dialect", ""), a["klass"]]
        for s in SIZES:
            b = sz.get(s, {}).get("rt_bps")
            row.append("" if b is None else f"{b:.1f}")
        lines.append("\t".join(row))
    open("out/depth_631.tsv", "w").write("\n".join(lines) + "\n")
    print(f"wrote out/depth_631.tsv  pools={len(by_pool)}")

    ok = [r for r in d["rows"] if r["usd"] == 1000 and r["klass"] == "OK"]
    print(f"\nOK at $1000: {len(ok)}/631 = {100*len(ok)/631:.1f}%")

    print("\n=== cost ceiling -> tradeable pools ($1000) ===")
    print(f"{'ceiling':>10} {'pools':>6} {'major':>6} {'non-major':>10}")
    for thr in (10, 30, 60, 100, 200, 500):
        sel = [r for r in ok if r["rt_bps"] <= thr]
        maj = sum(1 for r in sel if syms.get(r["alt"], "?") in MAJOR)
        print(f"{thr:>7} bps {len(sel):>6} {maj:>6} {len(sel)-maj:>10}")

    print("\n=== venue ===")
    for ven in ("aero_cl", "univ3"):
        v = sorted(r["rt_bps"] for r in ok if r["venue"] == ven)
        nm = sorted(r["rt_bps"] for r in ok
                    if r["venue"] == ven and syms.get(r["alt"], "?") not in MAJOR)
        print(f"  {ven:<9} pools={len(v):>4} p50={q(v,.5):>7.1f}  "
              f"non_major_pools={len(nm):>4} non_major_p50={q(nm,.5):>7.1f}")

    print("\n=== size sensitivity among OK ===")
    print(f"{'usd':>6} {'p10':>7} {'p50':>7} {'p90':>8}")
    for s in SIZES:
        v = sorted(r["rt_bps"] for r in d["rows"] if r["usd"] == s and r["klass"] == "OK")
        print(f"{s:>6} {q(v,.1):>7.1f} {q(v,.5):>7.1f} {q(v,.9):>8.1f}")
