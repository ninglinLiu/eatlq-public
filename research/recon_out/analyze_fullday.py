#!/usr/bin/env python3
"""Decode collateral asset + seized notional for the full-day stress/calm scans."""
from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict

F = json.load(open("/root/recon_out/fullday.json"))
M = json.load(open("/root/recon_out/morpho_markets.json"))
CT = {r["token"]: r for r in json.load(open("/root/recon_out/collateral_table.json"))}
EX = json.load(open("/root/recon_out/extra_syms.json"))

CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
DEC, SYM = {}, {}
for line in subprocess.run(CH + ["SELECT lower(token), symbol, decimals FROM census.tokens FORMAT TSV"],
                           capture_output=True).stdout.decode().strip().split("\n"):
    a, s, d = line.split("\t")
    DEC[a], SYM[a] = int(d), s
for a, s in EX.items():
    SYM.setdefault(a.lower(), s)
DEC.setdefault("0x4200000000000000000000000000000000000006", 18)
SYM.setdefault("0x4200000000000000000000000000000000000006", "WETH")

PX = {"cbBTC": 60000, "WETH": 1900, "USDC": 1.0, "USDbC": 1.0, "cbETH": 2050,
      "wstETH": 2300, "weETH": 2000, "rETH": 2100, "bsdETH": 1900, "wrsETH": 1950,
      "cbXRP": 2.0, "cbDOGE": 0.15, "SOL": 130, "cbADA": 0.5, "cbLTC": 90,
      "AERO": 0.6, "VVV": 3.0, "AAVE": 150, "TOSHI": 0.00005, "uSOL": 130,
      "VIRTUAL": 1.0, "LBTC": 60000, "USR": 1.0, "SKI": 0.005, "PERP": 0.3,
      "wbCOIN": 200, "stkWELL": 0.02, "WMTX": 0.3, "wBLT": 1.0, "xSolvBTC": 60000,
      "cbSOL": 130, "USDT": 1.0, "EURC": 1.1, "DEGEN": 0.005, "BRETT": 0.05,
      "MORPHO": 1.5, "WELL": 0.02, "KAITO": 1.0, "ZORA": 0.02, "tBTC": 60000,
      "ezETH": 2000, "superOETHb": 1900, "msETH": 1900, "USDz": 1.0, "oUSDT": 1.0}
DEEP = {"WETH", "USDC", "USDbC", "USDT", "cbBTC"}
LST = {"cbETH", "wstETH", "weETH", "rETH", "wrsETH", "bsdETH", "LBTC", "xSolvBTC",
       "USR", "ezETH", "superOETHb", "msETH", "tBTC"}
MORPHO_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
AAVE_LIQ = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

# --- market-id coverage check
allmids = Counter()
for d in F.values():
    for l in d["logs"]:
        if l["topics"][0] == MORPHO_LIQ:
            allmids[l["topics"][1]] += 1
res = sum(v for k, v in allmids.items() if k in M and "collateralToken" in M[k])
print(f"Morpho logs in full-day scans: {sum(allmids.values())}  "
      f"distinct market ids: {len(allmids)}  resolved: {res} "
      f"({res/sum(allmids.values())*100:.1f}%)  unresolved ids: "
      f"{len([k for k in allmids if k not in M])}")
print("top unresolved market ids:",
      [(k[:12], v) for k, v in allmids.most_common() if k not in M][:8])

rows = defaultdict(list)
for dt, d in sorted(F.items()):
    for l in d["logs"]:
        t0 = l["topics"][0]
        w = [l["data"][2:][i * 64:(i + 1) * 64] for i in range(len(l["data"][2:]) // 64)]
        if t0 == MORPHO_LIQ:
            mp = M.get(l["topics"][1])
            if not mp or "collateralToken" not in mp or len(w) < 5:
                continue
            ct = mp["collateralToken"].lower()
            amt = int(w[2], 16) / 10 ** DEC.get(ct, 18)
            proto = "MORPHO"
        elif t0 == AAVE_LIQ:
            ct = "0x" + l["topics"][1][-40:]
            if len(w) < 2:
                continue
            amt = int(w[1], 16) / 10 ** DEC.get(ct, 18)
            proto = "AAVEFORK"
        else:
            continue
        s = SYM.get(ct, "?")
        px = PX.get(s)
        if px is None:
            continue
        r = CT.get(ct, {})
        cls = "DEEP" if s in DEEP else ("LST" if s in LST else "NONMAJOR")
        rows[dt].append({"proto": proto, "sym": s, "usd": amt * px, "cls": cls,
                         "univ": bool(r.get("in_tradeable_universe")),
                         "cost": r.get("best_cost_bps"),
                         "blk": int(l["blockNumber"], 16)})

print(f"\n{'day':12s} {'tag':16s} {'dec':>5} {'notional$':>13} {'NM_n':>5} "
      f"{'NM_$':>12} {'NM>=10k':>8} {'NM>=10k&univ':>13} {'NMuniv_eps':>11}")
tot = defaultdict(int)
for dt, rs in sorted(rows.items()):
    nm = [r for r in rs if r["cls"] == "NONMAJOR"]
    nmb = [r for r in nm if r["usd"] >= 10_000]
    nmbu = [r for r in nmb if r["univ"]]
    bl = sorted({r["blk"] for r in nmbu})
    eps = 1 if bl else 0
    for a, b in zip(bl, bl[1:]):
        if b - a > 300:
            eps += 1
    print(f"{dt:12s} {F[dt]['tag']:16s} {len(rs):>5} {sum(r['usd'] for r in rs):>13,.0f} "
          f"{len(nm):>5} {sum(r['usd'] for r in nm):>12,.0f} {len(nmb):>8} "
          f"{len(nmbu):>13} {eps:>11}")
    if F[dt]["tag"].startswith("stress"):
        tot["days"] += 1
        tot["nmbu"] += len(nmbu)
        tot["eps"] += eps
        tot["nm_usd"] += sum(r["usd"] for r in nm)
        tot["all_usd"] += sum(r["usd"] for r in rs)

print(f"\nstress-day mean: NM>=10k&univ = {tot['nmbu']/tot['days']:.1f} events/day, "
      f"{tot['eps']/tot['days']:.1f} episodes/day")
print(f"stress-day non-major share of seized notional: "
      f"{tot['nm_usd']/tot['all_usd']*100:.2f}%")

print("\n=== every NON-MAJOR liquidation >= $10k in the 4 stress days ===")
allnm = [r | {"dt": dt} for dt, rs in rows.items() for r in rs
         if r["cls"] == "NONMAJOR" and r["usd"] >= 10_000]
for r in sorted(allnm, key=lambda x: -x["usd"]):
    print(f"  {r['dt']} blk={r['blk']} {r['sym']:8s} ${r['usd']:>12,.0f} "
          f"univ={r['univ']} cost={r['cost']}")
print(f"total: {len(allnm)}")

print("\n=== non-major collateral symbols across all scanned days ===")
c = Counter()
u = defaultdict(float)
for rs in rows.values():
    for r in rs:
        if r["cls"] == "NONMAJOR":
            c[r["sym"]] += 1
            u[r["sym"]] += r["usd"]
for s, n in c.most_common():
    row = next((x for x in CT.values() if x["symbol"] == s), {})
    print(f"  {s:12s} n={n:<5} sum_usd={u[s]:>13,.0f} "
          f"univ={row.get('in_tradeable_universe')} cost={row.get('best_cost_bps')}")
