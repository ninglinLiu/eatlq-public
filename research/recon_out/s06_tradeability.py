"""Step 6: are the genuinely-new vAMM assets actually TRADEABLE?

Two gates, both from the program's own prior work:
  continuity - re-probe the same 40 anchors, recording per-window presence. An asset
               present in few of 40 windows spread over 366 days cannot supply the
               ">=90 tradeable days" the panel requires.
  depth      - constant-product one-way slippage vs mid for a size dx against reserve x
               is  f + dx/x  to first order. This is the R1/R2 measure (single-pool
               ROUND trips are identically 2*fee on any AMM curve and measure nothing,
               ledger error #2), so only the one-way number is reported.
"""
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/recon_out")
from eth_hash.auto import keccak

import rpc_probe as R

VAMM = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
WETH = "0x4200000000000000000000000000000000000006"
PANEL_LO, PANEL_HI = 34_300_000, 50_085_519
N_PROBE, WIN = 40, 1_000

span = PANEL_HI - PANEL_LO + 1
step = span // N_PROBE
ranges = [(PANEL_LO + i * step, min(PANEL_LO + i * step + WIN - 1, PANEL_HI))
          for i in range(N_PROBE)]


def s(sig):
    return keccak(sig.encode()).hex()[:8]


# ------------------------------------------------- fee schedule
sf = R.dec_uint(R.eth_call(FACTORY, "0x" + s("stableFee()")))
vf = R.dec_uint(R.eth_call(FACTORY, "0x" + s("volatileFee()")))
print(f"PoolFactory.stableFee()   = {sf}  -> {sf/10000*10000:.1f} bps")
print(f"PoolFactory.volatileFee() = {vf}  -> {vf/10000*10000:.1f} bps")
print("(Aerodrome applies fee as amountIn*fee/10000, so the raw value IS bps)")

# ------------------------------------------------- ETH price
eth = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                      "--query", "SELECT median(eth_usd) FROM edge.eth_usd_1h"],
                     capture_output=True).stdout.decode().strip()
ETH_USD = float(eth)
print(f"median panel ETH/USD (edge.eth_usd_1h) = {ETH_USD:,.2f}")


# ------------------------------------------------- per-window presence
def probe(rng):
    lo, hi = rng
    res, err = R.rpc_batch([("eth_getLogs", [{"topics": [VAMM], "fromBlock": hex(lo),
                                              "toBlock": hex(hi)}])])[0]
    if err:
        return lo, None
    c = defaultdict(int)
    for l in res:
        c[l["address"].lower()] += 1
    return lo, dict(c)


with ThreadPoolExecutor(max_workers=6) as ex:
    windows = dict(ex.map(probe, ranges))
n_ok = sum(1 for v in windows.values() if v is not None)
print(f"\nre-probed {n_ok}/{N_PROBE} windows for per-window presence "
      f"(rpc={R.STATS['calls']})")

pool_windows = defaultdict(int)
pool_logs = defaultdict(int)
for lo, c in windows.items():
    if not c:
        continue
    for p, n in c.items():
        pool_windows[p] += 1
        pool_logs[p] += n

# ------------------------------------------------- depth from reserves
att = json.load(open("/root/recon_out/s03_attribute.json"))
AERO_FAC = att["aero_factory"]
info = {k: v for k, v in att["pools"].items() if v["factory"] == AERO_FAC}
uni = json.load(open("/root/recon_out/s04_universe.json"))
dec = {}
for m in uni["pools"]:
    if m.get("ok"):
        dec[m["token0"]] = m["dec0"]
        dec[m["token1"]] = m["dec1"]


def weth_reserve_usd(v):
    """USD value of the WETH side. Only defined for WETH-leg pools."""
    if v["token0"] == WETH:
        r = v["reserve0"]
    elif v["token1"] == WETH:
        r = v["reserve1"]
    else:
        return None
    if r is None:
        return None
    return r / 1e18 * ETH_USD


ov = json.load(open("/root/recon_out/s05_overlap.json"))
new_assets = {d["asset"] for d in ov["top_new"]}
alt50 = None  # recomputed below from CH for the full set
p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
                    "SELECT lower(alt) FROM edge.bars_universe_50"], capture_output=True)
alt50 = set(p.stdout.decode().split())
p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
                    "SELECT DISTINCT lower(t) FROM (SELECT token0 AS t FROM census.pools "
                    "UNION ALL SELECT token1 AS t FROM census.pools)"], capture_output=True)
census_tok = set(p.stdout.decode().split())

STABLES = {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
           "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",
           "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
           "0x50c5725949a6f0c72e6c4a641f24049a917db0cb", WETH}

rows = []
for pool, v in info.items():
    t0, t1 = v["token0"], v["token1"]
    a0, a1 = t0 not in STABLES, t1 not in STABLES
    alt = t0 if (a0 and not a1) else (t1 if (a1 and not a0) else (t0 if a0 and a1 else None))
    wr = weth_reserve_usd(v)
    fee_bps = sf if v["stable"] else vf
    rows.append({
        "pool": pool, "symbol": v["symbol"], "stable": v["stable"], "alt": alt,
        "logs": pool_logs.get(pool, v["sampled_logs"]),
        "windows": pool_windows.get(pool, 0),
        "weth_reserve_usd": wr, "fee_bps": fee_bps,
        "slip50_bps": (fee_bps + 50.0 / wr * 10000) if wr and wr > 0 else None,
        "slip5000_bps": (fee_bps + 5000.0 / wr * 10000) if wr and wr > 0 else None,
        "class": ("in_panel" if alt in alt50 else
                  ("census_only" if alt in census_tok else "new")) if alt else "quote_only",
    })

print(f"\n{'class':<12}{'pools':>7}{'assets':>8}{'w>=30':>7}{'w>=36':>7}"
      f"{'cheap50':>9}{'cheap+cont':>11}")
summary = {}
for cls in ["in_panel", "census_only", "new", "quote_only"]:
    sub = [r for r in rows if r["class"] == cls]
    assets = {r["alt"] for r in sub if r["alt"]}
    w30 = [r for r in sub if r["windows"] >= 30]
    w36 = [r for r in sub if r["windows"] >= 36]
    cheap = [r for r in sub if r["slip50_bps"] is not None and r["slip50_bps"] <= 30]
    both = [r for r in cheap if r["windows"] >= 36]
    summary[cls] = {
        "pools": len(sub), "assets": len(assets),
        "pools_w30": len(w30), "pools_w36": len(w36),
        "pools_cheap50": len(cheap), "pools_cheap_and_continuous": len(both),
        "assets_cheap_and_continuous": len({r["alt"] for r in both if r["alt"]}),
        "assets_w36": len({r["alt"] for r in w36 if r["alt"]}),
    }
    print(f"{cls:<12}{len(sub):>7,}{len(assets):>8,}{len(w30):>7}{len(w36):>7}"
          f"{len(cheap):>9}{len(both):>11}")

print("\n--- the decisive table: NEW assets that clear BOTH gates ---")
newq = sorted((r for r in rows if r["class"] == "new" and r["windows"] >= 36
               and r["slip50_bps"] is not None and r["slip50_bps"] <= 30),
              key=lambda r: -r["logs"])
print(f"{'pool':<44}{'sym':<24}{'win':>4}{'swaps':>7}{'wethUSD':>10}{'slip50':>8}")
for r in newq:
    print(f"{r['pool']:<44}{str(r['symbol'])[:23]:<24}{r['windows']:>4}{r['logs']:>7,}"
          f"{r['weth_reserve_usd']:>10,.0f}{r['slip50_bps']:>8.1f}")
print(f"=> {len(newq)} pools, {len({r['alt'] for r in newq})} distinct NEW assets")

print("\n--- same gates, assets ALREADY in bars_universe_50 (for scale) ---")
inq = sorted((r for r in rows if r["class"] == "in_panel" and r["windows"] >= 36
              and r["slip50_bps"] is not None and r["slip50_bps"] <= 30),
             key=lambda r: -r["logs"])
for r in inq[:20]:
    print(f"{r['pool']:<44}{str(r['symbol'])[:23]:<24}{r['windows']:>4}{r['logs']:>7,}"
          f"{r['weth_reserve_usd']:>10,.0f}{r['slip50_bps']:>8.1f}")
print(f"=> {len(inq)} pools, {len({r['alt'] for r in inq})} distinct already-in-panel assets")

print("\n--- census_only assets clearing both gates (new to the TRADEABLE set, "
      "but the CL panel already has price history) ---")
coq = sorted((r for r in rows if r["class"] == "census_only" and r["windows"] >= 36
              and r["slip50_bps"] is not None and r["slip50_bps"] <= 30),
             key=lambda r: -r["logs"])
for r in coq[:20]:
    print(f"{r['pool']:<44}{str(r['symbol'])[:23]:<24}{r['windows']:>4}{r['logs']:>7,}"
          f"{r['weth_reserve_usd']:>10,.0f}{r['slip50_bps']:>8.1f}")
print(f"=> {len(coq)} pools, {len({r['alt'] for r in coq})} distinct census-only assets")

json.dump({"fee": {"stableFee_bps": sf, "volatileFee_bps": vf}, "eth_usd_median": ETH_USD,
           "windows_ok": n_ok, "summary": summary, "rows": rows,
           "new_clearing_both": newq, "in_panel_clearing_both": inq,
           "census_only_clearing_both": coq,
           "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s06_tradeability.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
