#!/usr/bin/env python3
"""Build the liquidated-collateral table and intersect with the tradeable pool universe.

Zero RPC. Reads only from ClickHouse (SELECT) and local recon_out json.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter

CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]


def q(sql):
    p = subprocess.run(CH + [sql], capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[:400])
    return p.stdout.decode()


M = json.load(open("/root/recon_out/morpho_markets.json"))
A = json.load(open("/root/recon_out/assets_raw.json"))
EX = json.load(open("/root/recon_out/extra_syms.json"))

morpho = Counter()
for v in M.values():
    if "collateralToken" in v:
        morpho[v["collateralToken"].lower()] += v["count"]
aave = Counter({k.lower(): v for k, v in A["aave_col"].items()})

toks = sorted(set(morpho) | set(aave))
inlist = ",".join(f"'{t}'" for t in toks)

sym = {}
for line in q(f"SELECT lower(token), symbol FROM census.tokens "
              f"WHERE lower(token) IN ({inlist}) FORMAT TSV").strip().split("\n"):
    if line:
        a, s = line.split("\t")
        sym[a] = s
for a, s in EX.items():
    sym.setdefault(a.lower(), s)

# tradeable universe (the $50 cheap+feasible universe used by the program)
univ = {}
for line in q("SELECT symbol, count() AS n_pools, min(cost_p50) AS best_cost, "
              "max(tradeable_days) AS td FROM edge.bars_universe_50 "
              "GROUP BY symbol FORMAT TSV").strip().split("\n"):
    s, n, c, td = line.split("\t")
    univ[s] = (int(n), float(c), int(td))

rows = []
for t in toks:
    s = sym.get(t, "?")
    u = univ.get(s)
    rows.append({
        "token": t, "symbol": s,
        "morpho_liq": morpho.get(t, 0), "aavefork_liq": aave.get(t, 0),
        "total_liq": morpho.get(t, 0) + aave.get(t, 0),
        "in_tradeable_universe": u is not None,
        "n_pools": u[0] if u else 0,
        "best_cost_bps": round(u[1], 2) if u else None,
        "tradeable_days": u[2] if u else None,
    })
rows.sort(key=lambda r: -r["total_liq"])

MAJORS = {"WETH", "USDC", "USDbC", "USDT", "cbBTC", "cbETH", "wstETH", "weETH",
          "rETH", "wrsETH", "bsdETH", "superOETHb", "msETH", "ezETH", "LBTC",
          "xSolvBTC", "USR", "EURC", "USD+", "oUSDT", "USDz"}

print(f"{'symbol':20s} {'token':44s} {'morpho':>7} {'aave*':>7} {'tot':>6} "
      f"{'inUniv':>7} {'pools':>6} {'bestbps':>8} {'days':>5}")
tot_all = tot_major = tot_alt = tot_alt_tradeable = 0
for r in rows:
    tot_all += r["total_liq"]
    if r["symbol"] in MAJORS:
        tot_major += r["total_liq"]
    else:
        tot_alt += r["total_liq"]
        if r["in_tradeable_universe"]:
            tot_alt_tradeable += r["total_liq"]
    print(f"{r['symbol']:20s} {r['token']:44s} {r['morpho_liq']:>7} "
          f"{r['aavefork_liq']:>7} {r['total_liq']:>6} "
          f"{str(r['in_tradeable_universe']):>7} {r['n_pools']:>6} "
          f"{str(r['best_cost_bps']):>8} {str(r['tradeable_days']):>5}")

print(f"\nsampled liquidation logs with resolvable collateral: {tot_all}")
print(f"  deep-major / LST / stable collateral : {tot_major} ({tot_major/tot_all*100:.2f}%)")
print(f"  non-major collateral                 : {tot_alt} ({tot_alt/tot_all*100:.2f}%)")
print(f"  non-major AND in $50 tradeable univ  : {tot_alt_tradeable} "
      f"({tot_alt_tradeable/tot_all*100:.2f}%)")

json.dump(rows, open("/root/recon_out/collateral_table.json", "w"), indent=1)
