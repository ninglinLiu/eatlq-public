#!/usr/bin/env python3
"""THE key count: how many liquidations are BOTH large enough to move a pool AND on an
asset whose Base pool we can actually trade at $50? Zero RPC."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

D = json.load(open("/root/recon_out/sample_liq.json"))
M = json.load(open("/root/recon_out/morpho_markets.json"))
CT = {r["token"]: r for r in json.load(open("/root/recon_out/collateral_table.json"))}
SPAN, (B0, B1) = D["span"], D["panel"]
PANEL = B1 - B0 + 1
MORPHO_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
AAVE_LIQ = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

import subprocess
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
DEC = {}
for line in subprocess.run(CH + ["SELECT lower(token), decimals FROM census.tokens FORMAT TSV"],
                           capture_output=True).stdout.decode().strip().split("\n"):
    a, d = line.split("\t")
    DEC[a] = int(d)
DEC.setdefault("0x4200000000000000000000000000000000000006", 18)

PX = {"cbBTC": 60000, "WETH": 1900, "USDC": 1.0, "USDbC": 1.0, "cbETH": 2050,
      "wstETH": 2300, "weETH": 2000, "rETH": 2100, "bsdETH": 1900, "wrsETH": 1950,
      "cbXRP": 2.0, "cbDOGE": 0.15, "SOL": 130, "cbADA": 0.5, "cbLTC": 90,
      "AERO": 0.6, "VVV": 3.0, "AAVE": 150, "TOSHI": 0.00005, "uSOL": 130,
      "VIRTUAL": 1.0, "LBTC": 60000, "USR": 1.0, "SKI": 0.005, "PERP": 0.3,
      "wbCOIN": 200, "stkWELL": 0.02, "WMTX": 0.3, "wBLT": 1.0, "xSolvBTC": 60000,
      "PT-USDe-11DEC2025": 1.0, "PT-USR-24APR2025": 1.0}
DEEP = {"WETH", "USDC", "USDbC", "USDT", "cbBTC"}          # sub-1bps, no impact at any size we can trade
LST = {"cbETH", "wstETH", "weETH", "rETH", "wrsETH", "bsdETH", "LBTC", "xSolvBTC",
       "USR", "PT-USDe-11DEC2025", "PT-USR-24APR2025"}

wins = [w for w in D["windows"] if "error" not in w]
big = max(wins, key=lambda w: w["n"])
scale = PANEL / (len(wins) * SPAN)

recs = []
for w in wins:
    casc = w["from"] == big["from"]
    for l in w["logs"]:
        t0 = l["topics"][0]
        if t0 == MORPHO_LIQ:
            ct = M.get(l["topics"][1], {}).get("collateralToken", "").lower()
            d = l["data"][2:]
            wd = [d[i * 64:(i + 1) * 64] for i in range(len(d) // 64)]
            if len(wd) < 5 or not ct:
                continue
            amt = int(wd[2], 16) / 10 ** DEC.get(ct, 18)
            proto = "MORPHO"
        elif t0 == AAVE_LIQ:
            ct = "0x" + l["topics"][1][-40:]
            d = l["data"][2:]
            wd = [d[i * 64:(i + 1) * 64] for i in range(len(d) // 64)]
            if len(wd) < 2:
                continue
            amt = int(wd[1], 16) / 10 ** DEC.get(ct, 18)
            proto = "AAVEFORK"
        else:
            continue
        r = CT.get(ct, {})
        sym = r.get("symbol", "?")
        px = PX.get(sym)
        if px is None:
            continue
        recs.append({"proto": proto, "sym": sym, "usd": amt * px, "casc": casc,
                     "blk": int(l["blockNumber"], 16),
                     "univ": bool(r.get("in_tradeable_universe")),
                     "cost": r.get("best_cost_bps")})

print(f"decoded records: {len(recs)}  (of which cascade: {sum(r['casc'] for r in recs)})")


def rep(label, f):
    sel = [r for r in recs if f(r)]
    nb = [r for r in sel if not r["casc"]]
    blocks = sorted({r["blk"] for r in sel})
    eps = 1 if blocks else 0
    for a, b in zip(blocks, blocks[1:]):
        if b - a > 300:
            eps += 1
    print(f"{label:58s} n={len(sel):>5} yr={len(sel)*scale:>9,.0f} | "
          f"n_exclCasc={len(nb):>4} yr_exclCasc={len(nb)*PANEL/((len(wins)-1)*SPAN):>8,.0f} | "
          f"episodes={eps:>3} yr_eps={eps*scale:>8,.0f}")


print("\n=== BUCKETS (approx-USD seized notional) ===")
for thr in [0, 1_000, 10_000, 50_000, 250_000]:
    print(f"--- seized >= ${thr:,}")
    rep("  ALL collateral", lambda r, t=thr: r["usd"] >= t)
    rep("  DEEP major (WETH/USDC/USDbC/cbBTC)", lambda r, t=thr: r["usd"] >= t and r["sym"] in DEEP)
    rep("  LST / pegged wrapper", lambda r, t=thr: r["usd"] >= t and r["sym"] in LST)
    rep("  NON-major (the only bucket with impact upside)",
        lambda r, t=thr: r["usd"] >= t and r["sym"] not in DEEP and r["sym"] not in LST)
    rep("  NON-major AND in $50 tradeable universe",
        lambda r, t=thr: r["usd"] >= t and r["sym"] not in DEEP and r["sym"] not in LST and r["univ"])
    rep("  NON-major AND in universe AND best_cost<=30bps",
        lambda r, t=thr: r["usd"] >= t and r["sym"] not in DEEP and r["sym"] not in LST
        and r["univ"] and (r["cost"] or 1e9) <= 30)

print("\n=== non-major, >= $10k, listing every sampled instance ===")
for r in sorted([r for r in recs if r["usd"] >= 10_000 and r["sym"] not in DEEP
                 and r["sym"] not in LST], key=lambda x: -x["usd"]):
    print(f"  blk={r['blk']} {r['proto']:9s} {r['sym']:8s} ${r['usd']:>12,.0f} "
          f"casc={r['casc']} univ={r['univ']} cost={r['cost']}")
