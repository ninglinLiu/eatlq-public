#!/usr/bin/env python3
"""Size distribution of liquidations + cascade vs base-rate decomposition. Zero RPC."""
from __future__ import annotations

import json
import statistics
import subprocess
from collections import Counter, defaultdict

CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]


def q(sql):
    p = subprocess.run(CH + [sql], capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[:400])
    return p.stdout.decode()


D = json.load(open("/root/recon_out/sample_liq.json"))
M = json.load(open("/root/recon_out/morpho_markets.json"))
CT = {r["token"]: r for r in json.load(open("/root/recon_out/collateral_table.json"))}
SPAN, (B0, B1) = D["span"], D["panel"]
PANEL = B1 - B0 + 1

MORPHO_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
AAVE_LIQ = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
CV2_LIQ = "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52"

DEC = {}
for line in q("SELECT lower(token), decimals FROM census.tokens FORMAT TSV").strip().split("\n"):
    a, d = line.split("\t")
    DEC[a] = int(d)
DEC.setdefault("0x4200000000000000000000000000000000000006", 18)

# Reference USD prices. LABEL: hand-entered approximate spot, used ONLY for order-of-magnitude
# bucketing of seized notional. Not a measured panel price.
PX_APPROX = {
    "cbBTC": 60000, "WETH": 1900, "USDC": 1.0, "USDbC": 1.0, "cbETH": 2050,
    "wstETH": 2300, "weETH": 2000, "rETH": 2100, "bsdETH": 1900, "wrsETH": 1950,
    "cbXRP": 2.0, "cbDOGE": 0.15, "SOL": 130, "cbADA": 0.5, "cbLTC": 90,
    "AERO": 0.6, "VVV": 3.0, "AAVE": 150, "TOSHI": 0.00005, "uSOL": 130,
    "VIRTUAL": 1.0, "LBTC": 60000, "USR": 1.0, "SKI": 0.005, "PERP": 0.3,
    "wbCOIN": 200, "stkWELL": 0.02, "WMTX": 0.3, "wBLT": 1.0, "xSolvBTC": 60000,
    "PT-USDe-11DEC2025": 1.0, "PT-USR-24APR2025": 1.0,
}

wins = [w for w in D["windows"] if "error" not in w]
big = max(wins, key=lambda w: w["n"])
BIG_RANGE = (big["from"], big["to"])

# block -> date
bt = {}
for line in q(f"SELECT block_number, toDate(block_time) FROM eatlq.block_ts_dict "
              f"WHERE block_number BETWEEN {BIG_RANGE[0]} AND {BIG_RANGE[1]} "
              f"LIMIT 1 BY toDate(block_time) FORMAT TSV").strip().split("\n"):
    if line:
        b, d = line.split("\t")
        bt[int(b)] = d
print(f"cascade window {BIG_RANGE[0]}-{BIG_RANGE[1]} date(s): {sorted(set(bt.values()))}")

# date coverage of all sampled windows via block_ts_dict where available
have = q(f"SELECT min(block_number), max(block_number) FROM eatlq.block_ts_dict").strip()
print("block_ts_dict covers blocks:", have)


def seized(log):
    t0 = log["topics"][0]
    d = log["data"][2:]
    w = [d[i * 64:(i + 1) * 64] for i in range(len(d) // 64)]
    if t0 == MORPHO_LIQ:
        mid = log["topics"][1]
        mp = M.get(mid, {})
        ct = mp.get("collateralToken", "").lower()
        if len(w) < 5 or not ct:
            return None
        raw = int(w[2], 16)
        return ct, raw / 10 ** DEC.get(ct, 18)
    if t0 == AAVE_LIQ:
        ct = "0x" + log["topics"][1][-40:]
        if len(w) < 2:
            return None
        return ct, int(w[1], 16) / 10 ** DEC.get(ct, 18)
    return None


def usd(ct, amt):
    s = CT.get(ct, {}).get("symbol") or "?"
    p = PX_APPROX.get(s)
    return None if p is None else amt * p


groups = defaultdict(list)   # (proto, cascade?) -> [usd]
per_sym = defaultdict(list)
for w in wins:
    incasc = (w["from"] == BIG_RANGE[0])
    for l in w["logs"]:
        t0 = l["topics"][0]
        if t0 not in (MORPHO_LIQ, AAVE_LIQ):
            continue
        s = seized(l)
        if not s:
            continue
        ct, amt = s
        u = usd(ct, amt)
        if u is None:
            continue
        proto = "MORPHO" if t0 == MORPHO_LIQ else "AAVEFORK"
        groups[(proto, incasc)].append(u)
        per_sym[CT.get(ct, {}).get("symbol", "?")].append(u)

print("\n=== seized collateral notional, APPROX USD (hand-entered ref prices) ===")
print(f"{'group':22s} {'n':>6} {'p10':>10} {'p50':>10} {'p90':>10} {'p99':>12} {'max':>13} {'sum':>14}")
for k in sorted(groups):
    v = sorted(groups[k])
    if not v:
        continue
    def pc(p):
        return v[min(len(v) - 1, int(p * len(v)))]
    print(f"{str(k):22s} {len(v):>6} {pc(.10):>10,.0f} {pc(.50):>10,.0f} "
          f"{pc(.90):>10,.0f} {pc(.99):>12,.0f} {v[-1]:>13,.0f} {sum(v):>14,.0f}")

print("\n=== per-collateral seized notional (APPROX USD) ===")
print(f"{'symbol':20s} {'n':>5} {'p50':>11} {'p90':>12} {'max':>13} {'sum':>14} "
      f"{'univ':>6} {'bestbps':>8}")
for s, v in sorted(per_sym.items(), key=lambda x: -sum(x[1])):
    v = sorted(v)
    def pc(p):
        return v[min(len(v) - 1, int(p * len(v)))]
    row = next((r for r in CT.values() if r["symbol"] == s), {})
    print(f"{s:20s} {len(v):>5} {pc(.5):>11,.0f} {pc(.9):>12,.0f} {v[-1]:>13,.0f} "
          f"{sum(v):>14,.0f} {str(row.get('in_tradeable_universe')):>6} "
          f"{str(row.get('best_cost_bps')):>8}")

# ---- cascade vs base-rate extrapolation
nb = [w for w in wins if w["from"] != BIG_RANGE[0]]
print("\n=== cascade vs base rate ===")
for p, name in [(MORPHO_LIQ, "MORPHO"), (AAVE_LIQ, "AAVEFORK"), (CV2_LIQ, "MOONWELL_CV2")]:
    n_all = sum(1 for w in wins for l in w["logs"] if l["topics"][0] == p)
    n_nb = sum(1 for w in nb for l in w["logs"] if l["topics"][0] == p)
    print(f"{name:14s} sampled_all={n_all:<5} sampled_excl_cascade={n_nb:<5} "
          f"yr_all={n_all*PANEL/(len(wins)*SPAN):>9,.0f} "
          f"yr_excl={n_nb*PANEL/(len(nb)*SPAN):>9,.0f}")

# non-major share excluding cascade
MAJORS = {"WETH", "USDC", "USDbC", "USDT", "cbBTC", "cbETH", "wstETH", "weETH", "rETH",
          "wrsETH", "bsdETH", "LBTC", "xSolvBTC", "USR", "PT-USDe-11DEC2025",
          "PT-USR-24APR2025"}
cnt = Counter()
for w in nb:
    for l in w["logs"]:
        t0 = l["topics"][0]
        if t0 == MORPHO_LIQ:
            ct = M.get(l["topics"][1], {}).get("collateralToken", "").lower()
        elif t0 == AAVE_LIQ:
            ct = "0x" + l["topics"][1][-40:]
        else:
            continue
        cnt[CT.get(ct, {}).get("symbol", "?")] += 1
tot = sum(cnt.values())
maj = sum(v for k, v in cnt.items() if k in MAJORS)
print(f"\nEXCLUDING cascade: n={tot}  majors={maj} ({maj/tot*100:.1f}%)  "
      f"non-major={tot-maj} ({(tot-maj)/tot*100:.1f}%)")
print("non-major breakdown:", {k: v for k, v in cnt.most_common() if k not in MAJORS})
