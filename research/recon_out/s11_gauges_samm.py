"""Step 11: do vAMM pools carry live Aerodrome gauges (emissions), and the sAMM alt set.

census.aero_gauge_map is 157/157 CL pools and 0 vAMM pools, so the concurrent emissions
line currently cannot see vAMM at all. This asks Voter.gauges(pool) / isAlive(gauge)
directly for every pool that was active in the probe.
"""
import json
import subprocess
import sys

sys.path.insert(0, "/root/recon_out")
from eth_hash.auto import keccak

import rpc_probe as R

VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
WETH = "0x4200000000000000000000000000000000000006"


def s(sig):
    return keccak(sig.encode()).hex()[:8]


att = json.load(open("/root/recon_out/s03_attribute.json"))
cost = json.load(open("/root/recon_out/s08_cost_pinned.json"))
byrow = {r["pool"]: r for r in cost["rows"]}
aero = {k: v for k, v in att["pools"].items() if v["factory"] == att["aero_factory"]}
pools = list(aero)

res = R.multicall3([(VOTER, s("gauges(address)") + R.enc_addr(p)) for p in pools],
                   chunk=700)
gauge = {}
for p, (ok, d) in zip(pools, res):
    g = R.dec_addr(d) if ok else None
    gauge[p] = None if (g in (None, "0x0000000000000000000000000000000000000000")) else g
has = [p for p in pools if gauge[p]]
print(f"active Aerodrome vAMM/sAMM pools with a Voter gauge: {len(has):,} of {len(pools):,}")

alive = {}
if has:
    res = R.multicall3([(VOTER, s("isAlive(address)") + R.enc_addr(gauge[p])) for p in has],
                       chunk=700)
    for p, (ok, d) in zip(has, res):
        alive[p] = bool(R.dec_uint(d)) if ok else None
nalive = sum(1 for v in alive.values() if v)
print(f"  of those, gauge isAlive (currently receiving emissions): {nalive:,}")

tot = sum(byrow[p]["logs"] for p in pools)
lg = sum(byrow[p]["logs"] for p in pools if alive.get(p))
print(f"  share of sampled vAMM swap volume in live-gauge pools: {lg/tot*100:.1f}%")

g100 = [p for p in pools if alive.get(p) and byrow[p]["cost50"] is not None
        and byrow[p]["cost50"] <= 100]
print(f"  live-gauge pools also at <=100 bps one-way at $50: {len(g100)}")
newg = [p for p in g100 if byrow[p]["class"] == "new"]
print(f"    of which the alt is absent from census.pools entirely: {len(newg)}")

print(f"\ntop 15 live-gauge vAMM pools by sampled volume:")
print(f"{'pool':<44}{'symbol':<26}{'swaps':>7}{'win':>4}{'c50':>9}  class")
for p in sorted((p for p in pools if alive.get(p)),
                key=lambda p: -byrow[p]["logs"])[:15]:
    r = byrow[p]
    c = f"{r['cost50']:.1f}" if r["cost50"] is not None else "-"
    print(f"{p:<44}{str(r['symbol'])[:25]:<26}{r['logs']:>7,}{r['windows']:>4}"
          f"{c:>9}  {r['class']}")

# ---- sAMM alt set (fixes the s10 crash: iterate values, not keys) ----------
p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
                    "SELECT lower(alt) FROM edge.bars_universe_50"], capture_output=True)
alt50 = set(p.stdout.decode().split())
p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query",
                    "SELECT DISTINCT lower(t) FROM (SELECT token0 AS t FROM census.pools "
                    "UNION ALL SELECT token1 AS t FROM census.pools)"], capture_output=True)
census_tok = set(p.stdout.decode().split())

sact = [v for v in aero.values() if v["stable"]]
salts = {byrow[v["pool"]]["alt"] for v in sact if byrow[v["pool"]]["alt"]}
print(f"\n=== sAMM alt assets ===")
print(f"  active sAMM pools {len(sact)}, distinct alts {len(salts)}")
print(f"  already in edge.bars_universe_50 : {len(salts & alt50)}")
print(f"  in census.pools but not the panel: {len(salts & census_tok - alt50)}")
print(f"  absent from census.pools         : {len(salts - census_tok)}")
scont = [v for v in sact if byrow[v["pool"]]["windows"] >= 30]
print(f"  sAMM pools present in >=30 of 40 probe windows: {len(scont)}")
for v in sorted(scont, key=lambda v: -byrow[v["pool"]]["logs"]):
    r = byrow[v["pool"]]
    print(f"    {v['pool']}  {str(v['symbol'])[:24]:<25} fee={r['fee_bps']:<4} "
          f"win={r['windows']:<3} swaps={r['logs']:<6} class={r['class']}")

json.dump({"pools_with_gauge": len(has), "pools_alive": nalive, "n_pools": len(pools),
           "live_gauge_volume_share": lg / tot, "live_gauge_cheap100": len(g100),
           "live_gauge_cheap100_new": len(newg),
           "samm": {"active": len(sact), "alts": len(salts),
                    "alts_in_panel": len(salts & alt50),
                    "alts_census_only": len(salts & census_tok - alt50),
                    "alts_new": len(salts - census_tok),
                    "continuous_pools": len(scont)},
           "gauge_map": {p: {"gauge": gauge[p], "alive": alive.get(p)} for p in has},
           "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s11_gauges_samm.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
