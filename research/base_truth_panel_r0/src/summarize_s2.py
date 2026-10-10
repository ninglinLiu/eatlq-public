#!/usr/bin/env python3
"""Flatten slippage2_631.json (both directions) into a reviewable TSV."""
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
    d = json.load(open("out/slippage2_631.json"))
    syms = json.load(open("out/alt_symbols.json"))
    sizes = d["sizes_usd"]
    by: dict[str, dict] = collections.defaultdict(dict)
    for r in d["rows"]:
        by[r["pool"]][r["usd"]] = r

    cols = ["pool", "venue", "alt", "symbol", "is_major", "n_days", "n_swaps", "klass"]
    for s in sizes:
        cols += [f"buy_bps_{s}", f"sell_bps_{s}"]
    out = ["\t".join(cols)]
    for pool, sz in sorted(by.items(), key=lambda kv: -int(kv[1][sizes[0]]["n_swaps"])):
        a = sz[sizes[0]]
        sym = syms.get(a["alt"], "?")
        row = [pool, a["venue"], a["alt"], sym, str(int(sym in MAJOR)), a["n_days"],
               a["n_swaps"], a["klass"]]
        for s in sizes:
            r = sz.get(s, {})
            for k in ("buy_bps", "sell_bps"):
                v = r.get(k)
                row.append("" if v is None else f"{v:.1f}")
        out.append("\t".join(row))
    open("out/slippage2_631.tsv", "w").write("\n".join(out) + "\n")
    print(f"wrote out/slippage2_631.tsv  pools={len(by)}")

    print("\n=== worse-direction cost ceiling -> pools (non-major in parens) ===")
    print(f"{'ceiling':>9}" + "".join(f"{'$'+str(s):>14}" for s in sizes))
    for thr in (10, 30, 50, 100, 300):
        cells = []
        for s in sizes:
            v = [r for r in d["rows"] if r["usd"] == s and r["klass"] == "OK"
                 and r.get("worst_bps") is not None and r["worst_bps"] <= thr]
            nm = sum(1 for r in v if syms.get(r["alt"], "?") not in MAJOR)
            cells.append(f"{len(v)} ({nm})")
        print(f"{thr:>6} bps" + "".join(f"{c:>14}" for c in cells))

    print("\n=== exits cost more than entries: sell minus buy, among OK ===")
    print(f"{'usd':>7}{'n':>6}{'p50 diff':>11}{'p90 diff':>11}{'pools sell>2x buy':>20}")
    for s in sizes:
        v = [r for r in d["rows"] if r["usd"] == s and r["klass"] == "OK"]
        diff = sorted(r["sell_bps"] - r["buy_bps"] for r in v)
        worse = sum(1 for r in v if r["buy_bps"] > 0 and r["sell_bps"] > 2 * r["buy_bps"])
        q = lambda p: diff[min(int(p * len(diff)), len(diff) - 1)]  # noqa: E731
        print(f"{s:>7}{len(v):>6}{q(.5):>11.1f}{q(.9):>11.1f}{worse:>20}")
