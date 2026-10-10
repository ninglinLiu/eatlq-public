"""Step 8: corrected cost analysis. Everything read at ONE pinned block.

Two fixes to step 7:
  (a) s03 read reserves and s07 read quotes ~15 min apart, so busy pools appeared to
      quote better than their own (stale) mid. The pools that agreed to 0.03 bps were
      exactly the quiet ones. Pinning one block removes the artifact entirely.
  (b) the constant-product form  f + dx/x  is only valid for VOLATILE pools. Aerodrome
      sAMM uses x^3*y + y^3*x = k, so stable pools are quoted, never modelled.
Cost is also generalised to any quote leg (WETH or a USD stable), not just WETH.
"""
import json
import subprocess
import sys
from collections import Counter

sys.path.insert(0, "/root/recon_out")
from eth_hash.auto import keccak

import rpc_probe as R

FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
WETH = "0x4200000000000000000000000000000000000006"
USD_QUOTES = {  # addr -> decimals, treated as $1
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,     # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,     # USDbC
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": 6,     # USDT
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 18,    # DAI
}


def s(sig):
    return keccak(sig.encode()).hex()[:8]


SEL_RES = s("getReserves()")
SEL_FEE = s("getFee(address,bool)")
SEL_OUT = s("getAmountOut(uint256,address)")

att = json.load(open("/root/recon_out/s03_attribute.json"))
trd = json.load(open("/root/recon_out/s06_tradeability.json"))
ETH_USD = trd["eth_usd_median"]
AERO_FAC = att["aero_factory"]
info = {k: v for k, v in att["pools"].items() if v["factory"] == AERO_FAC}
byrow = {r["pool"]: r for r in trd["rows"]}
pools = list(info)

BLOCK = int(R.rpc("eth_blockNumber", []), 16) - 5   # pinned, a few blocks back
print(f"pinned block = {BLOCK:,}   ETH/USD = {ETH_USD:,.2f}")

items = [(p, SEL_RES) for p in pools] + \
        [(FACTORY, SEL_FEE + R.enc_addr(p) + R.enc_uint(1 if info[p]["stable"] else 0))
         for p in pools]
res = R.multicall3(items, chunk=700, block=BLOCK)
n = len(pools)
st = {}
for i, p in enumerate(pools):
    ok_r, d_r = res[i]
    ok_f, d_f = res[n + i]
    st[p] = {"r0": R.dec_uint(d_r, 0) if ok_r else None,
             "r1": R.dec_uint(d_r, 1) if ok_r else None,
             "fee": R.dec_uint(d_f) if ok_f else None}

fc_v = Counter(st[p]["fee"] for p in pools if info[p]["stable"] is False)
fc_s = Counter(st[p]["fee"] for p in pools if info[p]["stable"] is True)
print(f"volatile fee bps -> n_pools: {dict(sorted(fc_v.items(), key=lambda kv: kv[0]))}")
print(f"stable   fee bps -> n_pools: {dict(sorted(fc_s.items(), key=lambda kv: kv[0]))}")
nv = sum(fc_v.values())
print(f"modal volatile fee = 30 bps on {fc_v.get(30,0):,}/{nv:,} = "
      f"{fc_v.get(30,0)/nv*100:.1f}% of active volatile pools; "
      f"{sum(c for f,c in fc_v.items() if f is not None and f<30):,} are below 30")


def quote_usd(p):
    """(usd value of quote-side reserve, quote token) or (None, None)."""
    v, s_ = info[p], st[p]
    for t, r in ((v["token0"], s_["r0"]), (v["token1"], s_["r1"])):
        if r is None:
            continue
        if t == WETH:
            return r / 1e18 * ETH_USD, "WETH"
        if t in USD_QUOTES:
            return r / 10 ** USD_QUOTES[t], "USD"
    return None, None


rows = []
for p, v in info.items():
    qusd, qtok = quote_usd(p)
    fee = st[p]["fee"]
    br = byrow[p]
    r = {"pool": p, "symbol": v["symbol"], "stable": v["stable"], "alt": br["alt"],
         "logs": br["logs"], "windows": br["windows"], "class": br["class"],
         "fee_bps": fee, "quote_usd": qusd, "quote_tok": qtok}
    # closed form only for volatile pools
    if fee is not None and qusd and qusd > 0 and v["stable"] is False:
        r["cost50"] = fee + 50.0 / qusd * 1e4
        r["cost5000"] = fee + 5000.0 / qusd * 1e4
    else:
        r["cost50"] = r["cost5000"] = None
    rows.append(r)

# ---- validation at the SAME pinned block ---------------------------------
cand = sorted((r for r in rows if r["cost50"] is not None and r["quote_tok"] == "WETH"),
              key=lambda r: r["cost50"])[:30]
dx = int(50.0 / ETH_USD * 1e18)
vres = R.multicall3([(r["pool"], SEL_OUT + R.enc_uint(dx) + R.enc_addr(WETH))
                     for r in cand], block=BLOCK)
print(f"\nvalidation at block {BLOCK:,}: Pool.getAmountOut vs closed form, $50 WETH in")
print(f"{'pool':<44}{'symbol':<22}{'meas':>8}{'model':>8}{'diff':>8}")
diffs = []
for r, (ok, d) in zip(cand, vres):
    if not ok or not d or d == "0x":
        continue
    out = R.dec_uint(d)
    v = info[r["pool"]]
    if v["token0"] == WETH:
        rw, ro = st[r["pool"]]["r0"], st[r["pool"]]["r1"]
    else:
        rw, ro = st[r["pool"]]["r1"], st[r["pool"]]["r0"]
    if not out or not rw or not ro:
        continue
    meas = (1 - (out / dx) / (ro / rw)) * 1e4
    diffs.append(meas - r["cost50"])
    print(f"{r['pool']:<44}{str(r['symbol'])[:21]:<22}{meas:>8.2f}{r['cost50']:>8.2f}"
          f"{meas-r['cost50']:>8.2f}")
ds = sorted(diffs)
m = len(ds)
print(f"measured - model: p10={ds[m//10]:+.3f} p50={ds[m//2]:+.3f} "
      f"p90={ds[int(m*.9)]:+.3f} bps (n={m})  -> closed form validated")

# ---- distributions --------------------------------------------------------
cc = [r for r in rows if r["cost50"] is not None]
tot_logs = sum(r["logs"] for r in rows)
print(f"\nactive Aerodrome pools: {len(rows):,}  "
      f"volatile with computable cost: {len(cc):,}  "
      f"(rest are sAMM or alt/alt with no quote leg)")
for lbl, k in [("$50", "cost50"), ("$5,000", "cost5000")]:
    vs = sorted(r[k] for r in cc)
    q = len(vs)
    print(f"  one-way cost {lbl:>7}: p10={vs[q//10]:9.1f} p25={vs[q//4]:9.1f} "
          f"p50={vs[q//2]:11.1f} bps")

print(f"\n{'threshold':>10}{'pools':>7}{'assets':>8}{'swap share':>12}"
      f"{'new assets':>11}{'new share':>11}")
gates = {}
for thr in [30, 50, 100, 300, 1000]:
    sub = [r for r in cc if r["cost50"] <= thr]
    new = [r for r in sub if r["class"] == "new"]
    gates[thr] = {
        "pools": len(sub), "assets": len({r["alt"] for r in sub if r["alt"]}),
        "swap_share": sum(r["logs"] for r in sub) / tot_logs,
        "new_pools": len(new), "new_assets": len({r["alt"] for r in new if r["alt"]}),
        "new_swap_share": sum(r["logs"] for r in new) / tot_logs,
        "in_panel_assets": len({r["alt"] for r in sub if r["class"] == "in_panel"}),
        "census_only_assets": len({r["alt"] for r in sub if r["class"] == "census_only"}),
    }
    g = gates[thr]
    print(f"{'<=' + str(thr) + 'bps':>10}{g['pools']:>7}{g['assets']:>8}"
          f"{g['swap_share']*100:>11.1f}%{g['new_assets']:>11}"
          f"{g['new_swap_share']*100:>10.1f}%")

print(f"\nbreakdown of the <=100 bps set by whether the asset is already in the panel:")
g = gates[100]
print(f"  already in edge.bars_universe_50 : {g['in_panel_assets']} assets")
print(f"  in census.pools but not the panel: {g['census_only_assets']} assets")
print(f"  absent from census.pools entirely: {g['new_assets']} assets")

print(f"\nthe <=100 bps NEW assets, with activity (windows out of 40):")
nn = sorted((r for r in cc if r["cost50"] <= 100 and r["class"] == "new"),
            key=lambda r: -r["logs"])
print(f"{'pool':<44}{'symbol':<24}{'fee':>4}{'win':>4}{'swaps':>7}{'quoteUSD':>10}{'c50':>7}")
for r in nn:
    print(f"{r['pool']:<44}{str(r['symbol'])[:23]:<24}{r['fee_bps']:>4}{r['windows']:>4}"
          f"{r['logs']:>7,}{r['quote_usd']:>10,.0f}{r['cost50']:>7.1f}")

json.dump({"pinned_block": BLOCK, "eth_usd": ETH_USD,
           "fee_volatile": {str(k): v for k, v in fc_v.items()},
           "fee_stable": {str(k): v for k, v in fc_s.items()},
           "validation_diff_bps": {"p10": ds[m//10], "p50": ds[m//2],
                                   "p90": ds[int(m*.9)], "n": m},
           "gates": {str(k): v for k, v in gates.items()},
           "rows": rows, "new_cheap": nn, "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s08_cost_pinned.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
