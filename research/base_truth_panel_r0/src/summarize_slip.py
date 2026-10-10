#!/usr/bin/env python3
"""Flatten slippage_631.json into a reviewable TSV alongside the round-trip fee floor."""
from __future__ import annotations

import collections
import json

MAJOR = {
    "USDC", "USDbC", "cbETH", "AERO", "cbBTC", "wstETH", "weETH", "rETH", "USDT", "DAI",
    "EURC", "tBTC", "LBTC", "ezETH", "wrsETH", "superOETHb", "msETH", "USDS", "sUSDS",
    "GHO", "USDz", "cbADA", "cbDOGE", "cbXRP", "wM", "mBASIS", "USD+", "USDA", "crvUSD",
    "frxETH", "sfrxETH", "ETHx", "osETH", "rsETH", "uSOL", "uXRP", "uADA", "uDOGE",
}

if __name__ == "__main__":
    d = json.load(open("out/slippage_631.json"))
    syms = json.load(open("out/alt_symbols.json"))
    sizes = d["sizes_usd"]
    fee_floor = {}
    for ln in open("out/depth_631.tsv").read().strip().split("\n")[1:]:
        p = ln.split("\t")
        fee_floor[p[0]] = p[12]           # round-trip bps at $1000 = 2x fee

    by_pool: dict[str, dict] = collections.defaultdict(dict)
    for r in d["rows"]:
        by_pool[r["pool"]][r["usd"]] = r

    cols = (["pool", "venue", "alt", "symbol", "is_major", "n_days", "n_swaps", "klass",
             "fee_floor_rt_bps"] + [f"slip_bps_{s}" for s in sizes])
    out = ["\t".join(cols)]
    for pool, sz in sorted(by_pool.items(), key=lambda kv: -int(kv[1][sizes[0]]["n_swaps"])):
        a = sz[sizes[0]]
        sym = syms.get(a["alt"], "?")
        row = [pool, a["venue"], a["alt"], sym, str(int(sym in MAJOR)), a["n_days"],
               a["n_swaps"], a["klass"], fee_floor.get(pool, "")]
        for s in sizes:
            b = sz.get(s, {}).get("slip_bps")
            row.append("" if b is None else f"{b:.1f}")
        out.append("\t".join(row))
    open("out/slippage_631.tsv", "w").write("\n".join(out) + "\n")
    print(f"wrote out/slippage_631.tsv  pools={len(by_pool)}")

    print("\n=== pools under a one-way cost ceiling (non-major in parens) ===")
    print(f"{'ceiling':>9}" + "".join(f"{'$'+str(s):>14}" for s in sizes))
    for thr in (10, 30, 50, 100, 300):
        cells = []
        for s in sizes:
            v = [r for r in d["rows"] if r["usd"] == s and r["klass"] == "OK"
                 and r["slip_bps"] is not None and r["slip_bps"] <= thr]
            nm = sum(1 for r in v if syms.get(r["alt"], "?") not in MAJOR)
            cells.append(f"{len(v)} ({nm})")
        print(f"{thr:>6} bps" + "".join(f"{c:>14}" for c in cells))

    print("\n=== the survivors: non-major, <=30bps one-way at $5000 ===")
    v = [r for r in d["rows"] if r["usd"] == 5000 and r["klass"] == "OK"
         and r["slip_bps"] is not None and r["slip_bps"] <= 30
         and syms.get(r["alt"], "?") not in MAJOR]
    print(f"{'symbol':<12}{'slip_bps':>9}{'days':>6}{'swaps':>10}  venue")
    for r in sorted(v, key=lambda r: r["slip_bps"]):
        print(f"{syms.get(r['alt'],'?')[:12]:<12}{r['slip_bps']:>9.1f}{r['n_days']:>6}"
              f"{r['n_swaps']:>10}  {r['venue']}")
