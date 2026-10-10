"""Step 3: attribute every probed vAMM-topic0 pool to its factory + read its metadata.

topic0 b3e27736 = Swap(address,address,uint256,uint256,uint256,uint256) is the generic
Solidly signature. Any Velodrome/Solidly fork on Base emits it, so raw topic0 counts are
an UPPER bound on Aerodrome. This step splits the measured log volume by factory().
"""
import json
import sys
from collections import defaultdict

sys.path.insert(0, "/root/recon_out")
import rpc_probe as R

SEL_FACTORY = "c45a0155"
SEL_TOKEN0 = "0dfe1681"
SEL_TOKEN1 = "d21220a7"
SEL_STABLE = "22be3de1"
SEL_SYMBOL = "95d89b41"
SEL_RESERVES = "0902f1ac"

probe = json.load(open("/root/recon_out/s02_probe.json"))
counts = dict(probe["vamm_pool_log_counts"])
pools = list(counts)
print(f"attributing {len(pools):,} pools ({sum(counts.values()):,} sampled logs)")

items = []
for p in pools:
    items += [(p, SEL_FACTORY), (p, SEL_TOKEN0), (p, SEL_TOKEN1),
              (p, SEL_STABLE), (p, SEL_SYMBOL), (p, SEL_RESERVES)]
res = R.multicall3(items, chunk=600)

info = {}
for i, p in enumerate(pools):
    g = res[i * 6:(i + 1) * 6]
    info[p] = {
        "pool": p,
        "sampled_logs": counts[p],
        "factory": R.dec_addr(g[0][1]) if g[0][0] else None,
        "token0": R.dec_addr(g[1][1]) if g[1][0] else None,
        "token1": R.dec_addr(g[2][1]) if g[2][0] else None,
        "stable": bool(R.dec_uint(g[3][1])) if g[3][0] else None,
        "symbol": R.dec_string(g[4][1]) if g[4][0] else None,
        "reserve0": R.dec_uint(g[5][1], 0) if g[5][0] else None,
        "reserve1": R.dec_uint(g[5][1], 1) if g[5][0] else None,
    }

by_fac = defaultdict(lambda: {"pools": 0, "logs": 0, "stable": 0, "volatile": 0})
for v in info.values():
    f = v["factory"] or "UNRESOLVED"
    by_fac[f]["pools"] += 1
    by_fac[f]["logs"] += v["sampled_logs"]
    if v["stable"] is True:
        by_fac[f]["stable"] += 1
    elif v["stable"] is False:
        by_fac[f]["volatile"] += 1

tot_logs = sum(counts.values())
print(f"\n{'factory':<44} {'pools':>6} {'logs':>8} {'log%':>7} {'vol':>5} {'stbl':>5}")
rows = sorted(by_fac.items(), key=lambda kv: -kv[1]["logs"])
for f, d in rows:
    print(f"{f:<44} {d['pools']:>6,} {d['logs']:>8,} "
          f"{d['logs']/tot_logs*100:>6.2f}% {d['volatile']:>5} {d['stable']:>5}")

AERO_FAC = "0x420dd381b31aef6683db6b902084cb0ffece40da"
aero = by_fac.get(AERO_FAC, {"pools": 0, "logs": 0, "stable": 0, "volatile": 0})
print(f"\nAerodrome PoolFactory share of sampled topic0 volume: "
      f"{aero['logs']/tot_logs*100:.2f}%  ({aero['logs']:,}/{tot_logs:,} logs, "
      f"{aero['pools']:,} pools)")

# stable vs volatile split of ACTIVE Aerodrome pools, weighted by log volume
a_st = sum(v["sampled_logs"] for v in info.values()
           if v["factory"] == AERO_FAC and v["stable"] is True)
a_vo = sum(v["sampled_logs"] for v in info.values()
           if v["factory"] == AERO_FAC and v["stable"] is False)
print(f"Aerodrome sampled logs: vAMM(volatile)={a_vo:,} ({a_vo/max(a_st+a_vo,1)*100:.2f}%)  "
       f"sAMM(stable)={a_st:,} ({a_st/max(a_st+a_vo,1)*100:.2f}%)")

top = sorted((v for v in info.values() if v["factory"] == AERO_FAC),
             key=lambda v: -v["sampled_logs"])[:40]
print(f"\ntop 40 Aerodrome pools by sampled swap count:")
print(f"{'pool':<44}{'logs':>7} {'stable':>7}  symbol")
for v in top:
    print(f"{v['pool']:<44}{v['sampled_logs']:>7,} {str(v['stable']):>7}  {v['symbol']}")

json.dump({"by_factory": {k: dict(v) for k, v in rows},
           "aero_factory": AERO_FAC,
           "aero_share_of_topic0_logs": aero["logs"] / tot_logs,
           "aero_sampled_logs": aero["logs"], "total_sampled_logs": tot_logs,
           "aero_volatile_logs": a_vo, "aero_stable_logs": a_st,
           "pools": info, "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s03_attribute.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
