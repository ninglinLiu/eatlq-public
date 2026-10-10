"""Step 7: real per-pool fees + validate the reserve-based cost against Pool.getAmountOut.

The program's cost definition (base_truth_panel_r0/src/cost_model.py L19) is
    cost_bps ~= 1e4*fee_rate + 1e4*slip        (ONE WAY, fee + slippage vs mid)
and its "cheap" tier is cost < 30 bps. So the comparison here is apples to apples.

Validation: Pool.getAmountOut(amountIn, tokenIn) is the pool's own pricing function,
including fee. Comparing it to (fee + dx/x) tests both my reserve read and my fee read
without needing a router or state-override.
"""
import json
import subprocess
import sys

sys.path.insert(0, "/root/recon_out")
from eth_hash.auto import keccak

import rpc_probe as R

FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
WETH = "0x4200000000000000000000000000000000000006"


def s(sig):
    return keccak(sig.encode()).hex()[:8]


SEL_GETFEE = s("getFee(address,bool)")
SEL_AMTOUT = s("getAmountOut(uint256,address)")

att = json.load(open("/root/recon_out/s03_attribute.json"))
trd = json.load(open("/root/recon_out/s06_tradeability.json"))
ETH_USD = trd["eth_usd_median"]
AERO_FAC = att["aero_factory"]
info = {k: v for k, v in att["pools"].items() if v["factory"] == AERO_FAC}
byrow = {r["pool"]: r for r in trd["rows"]}
pools = list(info)

# ---- real per-pool fee ---------------------------------------------------
items = [(FACTORY, SEL_GETFEE + R.enc_addr(p) + R.enc_uint(1 if info[p]["stable"] else 0))
         for p in pools]
res = R.multicall3(items, chunk=700)
fees = {}
for p, (ok, d) in zip(pools, res):
    fees[p] = R.dec_uint(d) if ok else None
from collections import Counter
fc_v = Counter(fees[p] for p in pools if info[p]["stable"] is False)
fc_s = Counter(fees[p] for p in pools if info[p]["stable"] is True)
print(f"getFee() across {len(pools):,} active Aerodrome pools")
print(f"  volatile (vAMM) fee distribution (bps): {dict(sorted(fc_v.items()))}")
print(f"  stable   (sAMM) fee distribution (bps): {dict(sorted(fc_s.items()))}")
vol_min = min(f for f in fc_v if f is not None)
print(f"  MINIMUM volatile fee observed: {vol_min} bps  "
      f"-> a vAMM volatile pool cannot be under the program's 30 bps cheap tier "
      f"{'(fee alone >= 30)' if vol_min >= 30 else '(some are below)'}")

# ---- validate reserve math against the pool's own quoter -----------------
cand = sorted((p for p in pools if byrow[p]["weth_reserve_usd"]),
              key=lambda p: -byrow[p]["weth_reserve_usd"])[:30]
dx_weth = int(50.0 / ETH_USD * 1e18)
items = [(p, SEL_AMTOUT + R.enc_uint(dx_weth) + R.enc_addr(WETH)) for p in cand]
res = R.multicall3(items)

uni = json.load(open("/root/recon_out/s04_universe.json"))
decs = {}
for m in uni["pools"]:
    if m.get("ok"):
        decs[m["token0"]] = m["dec0"]
        decs[m["token1"]] = m["dec1"]

print(f"\nvalidation: measured vs closed-form one-way cost, $50 WETH in "
      f"(ETH={ETH_USD:,.0f})")
print(f"{'pool':<44}{'symbol':<22}{'meas_bps':>9}{'model_bps':>10}{'diff':>8}")
vrows = []
for p, (ok, d) in zip(cand, res):
    v = info[p]
    if not ok or d in (None, "0x"):
        continue
    out_raw = R.dec_uint(d)
    if not out_raw:
        continue
    other = v["token1"] if v["token0"] == WETH else v["token0"]
    r_weth = v["reserve0"] if v["token0"] == WETH else v["reserve1"]
    r_oth = v["reserve1"] if v["token0"] == WETH else v["reserve0"]
    if not r_weth or not r_oth:
        continue
    mid = r_oth / r_weth                      # other per WETH, raw units
    eff = out_raw / dx_weth
    meas = (1 - eff / mid) * 1e4
    model = byrow[p]["slip50_bps"]
    if model is None:
        continue
    print(f"{p:<44}{str(v['symbol'])[:21]:<22}{meas:>9.2f}{model:>10.2f}"
          f"{meas-model:>8.2f}")
    vrows.append({"pool": p, "symbol": v["symbol"], "measured_bps": meas,
                  "model_bps": model, "fee_bps": fees[p], "stable": v["stable"]})

if vrows:
    ds = sorted(r["measured_bps"] - r["model_bps"] for r in vrows)
    n = len(ds)
    print(f"\nmeasured - model: p10={ds[n//10]:+.2f}  p50={ds[n//2]:+.2f}  "
          f"p90={ds[int(n*0.9)]:+.2f} bps  (n={n})")

# ---- honest distribution of one-way $50 cost, using REAL fees -------------
rows = []
for p, v in info.items():
    br = byrow[p]
    f = fees[p]
    wr = br["weth_reserve_usd"]
    rows.append({**br, "fee_bps_real": f,
                 "cost50": (f + 50.0 / wr * 1e4) if (f is not None and wr and wr > 0) else None,
                 "cost5000": (f + 5000.0 / wr * 1e4) if (f is not None and wr and wr > 0) else None})

wl = [r for r in rows if r["cost50"] is not None]
print(f"\nWETH-leg active Aerodrome pools with a computable cost: {len(wl):,} of {len(rows):,}")
for lbl, key in [("$50", "cost50"), ("$5,000", "cost5000")]:
    vs = sorted(r[key] for r in wl)
    n = len(vs)
    print(f"  one-way cost {lbl:>7}: p10={vs[n//10]:8.1f} p50={vs[n//2]:8.1f} "
          f"p90={vs[int(n*.9)]:9.1f}  <=30bps: {sum(1 for x in vs if x<=30):>4} "
          f"<=100bps: {sum(1 for x in vs if x<=100):>4}")

# volume-weighted: where does the swap volume sit in cost space?
tot = sum(r["logs"] for r in wl)
for thr in [30, 50, 100, 300]:
    sub = [r for r in wl if r["cost50"] <= thr]
    print(f"  share of sampled vAMM swaps in pools with $50 cost <= {thr:>3} bps: "
          f"{sum(r['logs'] for r in sub)/tot*100:5.1f}%  ({len(sub)} pools)")

# panel comparison
p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
                    "SELECT quantiles(0.1,0.5,0.9)(cost_p50), countIf(cost_p50<=30), "
                    "countIf(cost_p50<=100), count() FROM edge.bars_universe_50"],
                   capture_output=True).stdout.decode().strip()
print(f"\nfor scale, edge.bars_universe_50 cost_p50 (same one-way definition, $50):")
print(f"  quantiles(p10,p50,p90), n<=30, n<=100, n = {p}")

json.dump({"fee_distribution": {"volatile": {str(k): v for k, v in fc_v.items()},
                                "stable": {str(k): v for k, v in fc_s.items()}},
           "min_volatile_fee_bps": vol_min,
           "validation": vrows, "rows": rows},
          open("/root/recon_out/s07_cost_check.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
