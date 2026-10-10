"""Step 12: real continuity. 12 FULL DAYS (43,200 blocks each) spread across the panel.

The 40x1,000-block probes conflate low trade rate with discontinuity: a 1,000-block
window is 33 minutes, so a pool needs ~1 swap/33min to reliably appear. Full days remove
that confound and give a count directly comparable to the panel's "tradeable_days".

Also checks the structural claim that Aerodrome allows exactly one pool per
(token0, token1, stable) triple, i.e. there is no fee-tier choice within a pair.
"""
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/recon_out")
import rpc_probe as R

VAMM = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
PANEL_LO, PANEL_HI = 34_300_000, 50_085_519
DAY = 43_200          # 2 s/block
N_DAYS = 12
CH = 10_000

# ---- structural: pools per (token0, token1, stable) triple ------------------
uni = json.load(open("/root/recon_out/s04_universe.json"))
trip = defaultdict(int)
for m in uni["pools"]:
    if m.get("ok"):
        trip[(m["token0"], m["token1"], m["stable"])] += 1
from collections import Counter
tc = Counter(trip.values())
pairs = defaultdict(set)
for (t0, t1, st) in trip:
    pairs[(t0, t1)].add(st)
pc = Counter(len(v) for v in pairs.values())
print(f"Aerodrome pools per (token0,token1,stable) triple: {dict(tc)}")
print(f"  -> {'exactly one pool per triple, so NO fee-tier choice within a pair' if set(tc)== {1} else 'MULTIPLE pools per triple exist'}")
print(f"distinct token pairs: {len(pairs):,}; pairs offering both vAMM and sAMM: "
      f"{pc.get(2,0):,}; only one type: {pc.get(1,0):,}")

# ---- 12 full days ----------------------------------------------------------
step = (PANEL_HI - PANEL_LO + 1 - DAY) // (N_DAYS - 1)
days = [PANEL_LO + i * step for i in range(N_DAYS)]
jobs = []
for d in days:
    b = d
    while b < d + DAY:
        e = min(b + CH - 1, d + DAY - 1, PANEL_HI)
        jobs.append((d, b, e))
        b = e + 1
print(f"\n{N_DAYS} days x {DAY:,} blocks = {N_DAYS*DAY:,} blocks in {len(jobs)} calls "
      f"({N_DAYS*DAY/(PANEL_HI-PANEL_LO+1)*100:.2f}% of the panel)")


def work(j):
    d, b, e = j
    res, err = R.rpc_batch([("eth_getLogs", [{"topics": [VAMM], "fromBlock": hex(b),
                                              "toBlock": hex(e)}])])[0]
    if err:
        return d, None
    c = defaultdict(int)
    for l in res:
        c[l["address"].lower()] += 1
    return d, dict(c)


with ThreadPoolExecutor(max_workers=5) as ex:
    out = list(ex.map(work, jobs))

per_day = defaultdict(lambda: defaultdict(int))
fails = 0
for d, c in out:
    if c is None:
        fails += 1
        continue
    for p, n in c.items():
        per_day[d][p] += n
print(f"failed sub-calls: {fails}")
print(f"{'day_start_block':>16}{'swaps':>10}{'pools':>8}")
for d in days:
    print(f"{d:>16,}{sum(per_day[d].values()):>10,}{len(per_day[d]):>8,}")

pool_days = defaultdict(int)
pool_swaps = defaultdict(int)
for d in days:
    for p, n in per_day[d].items():
        pool_days[p] += 1
        pool_swaps[p] += n

# ---- join to cost / class -------------------------------------------------
cost = json.load(open("/root/recon_out/s08_cost_pinned.json"))
byrow = {r["pool"]: r for r in cost["rows"]}
alt_days = defaultdict(set)
alt_swaps = defaultdict(int)
alt_cls, alt_cost, alt_sym = {}, {}, {}
for d in days:
    for p, n in per_day[d].items():
        r = byrow.get(p)
        if not r or not r.get("alt"):
            continue
        a = r["alt"]
        alt_days[a].add(d)
        alt_swaps[a] += n
        alt_cls[a] = r["class"]
        alt_sym[a] = r["symbol"]
        c = r.get("cost50")
        if c is not None:
            alt_cost[a] = min(alt_cost.get(a, 9e18), c)

print(f"\ndistinct pools seen across the 12 days: {len(pool_days):,}")
print(f"distinct alt assets: {len(alt_days):,}")
print(f"\n{'gate':<46}{'all':>6}{'in_panel':>10}{'census':>8}{'new':>6}")


def cnt(f):
    sel = [a for a in alt_days if f(a)]
    return (len(sel), sum(1 for a in sel if alt_cls[a] == "in_panel"),
            sum(1 for a in sel if alt_cls[a] == "census_only"),
            sum(1 for a in sel if alt_cls[a] == "new"))


gates = [
    ("present on any of 12 days", lambda a: True),
    ("present on all 12 days", lambda a: len(alt_days[a]) == 12),
    ("all 12 days AND cost<=100bps", lambda a: len(alt_days[a]) == 12
     and alt_cost.get(a, 9e18) <= 100),
    ("all 12 days AND cost<=100 AND >=24 swaps/day", lambda a: len(alt_days[a]) == 12
     and alt_cost.get(a, 9e18) <= 100 and alt_swaps[a] / 12 >= 24),
    ("all 12 days AND cost<=30bps", lambda a: len(alt_days[a]) == 12
     and alt_cost.get(a, 9e18) <= 30),
]
res = {}
for lbl, f in gates:
    c = cnt(f)
    res[lbl] = {"all": c[0], "in_panel": c[1], "census_only": c[2], "new": c[3]}
    print(f"{lbl:<46}{c[0]:>6}{c[1]:>10}{c[2]:>8}{c[3]:>6}")

final = sorted((a for a in alt_days if len(alt_days[a]) == 12
                and alt_cost.get(a, 9e18) <= 100 and alt_cls[a] == "new"),
               key=lambda a: -alt_swaps[a])
print(f"\n=== NEW assets surviving 12/12 days AND <=100 bps at $50 ===")
print(f"{'asset':<44}{'sym':<22}{'swaps/day':>10}{'cost50':>8}")
for a in final:
    print(f"{a:<44}{str(alt_sym[a])[:21]:<22}{alt_swaps[a]/12:>10.1f}"
          f"{alt_cost[a]:>8.1f}")
print(f"=> {len(final)} genuinely new tradeable-candidate assets")

json.dump({"triples_per_pool": {str(k): v for k, v in tc.items()},
           "pairs_total": len(pairs), "pairs_both_types": pc.get(2, 0),
           "days": days, "day_blocks": DAY, "calls": len(jobs), "failed": fails,
           "per_day_totals": {str(d): sum(per_day[d].values()) for d in days},
           "per_day_pools": {str(d): len(per_day[d]) for d in days},
           "gates": res,
           "new_survivors": [{"asset": a, "symbol": alt_sym[a],
                              "swaps_per_day": alt_swaps[a] / 12,
                              "cost50": alt_cost[a]} for a in final],
           "pool_days": dict(pool_days), "pool_swaps": dict(pool_swaps),
           "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s12_continuity.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
