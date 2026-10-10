"""Step 5: would vAMM add INDEPENDENT tradeable assets, or duplicate the panel?

Pure ClickHouse + local JSON. Zero RPC, zero DDL, read-only SELECTs only.
The unit of independence is the ALT ASSET (token address), not the pool: a second
pool on an asset already in the panel is a correlated observation, not a new sample.
"""
import json
import subprocess
import sys
from collections import defaultdict

WETH = "0x4200000000000000000000000000000000000006"
STABLES = {  # quote assets: a pool of two of these adds no new alt
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": "USDT",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "DAI",
    "0x4200000000000000000000000000000000000006": "WETH",
}


def ch(q):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--query", q], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:500])
    return [l.split("\t") for l in p.stdout.decode().strip().split("\n") if l]


# ---------------------------------------------------------------- panel sets
univ50 = ch("SELECT lower(alt), symbol, venue, tradeable_days, cost_p50 "
            "FROM edge.bars_universe_50")
alt50 = {r[0] for r in univ50}
sym50 = {r[0]: r[1] for r in univ50}
print(f"edge.bars_universe_50: {len(univ50)} pools, {len(alt50)} distinct alt assets")

census_tok = {r[0] for r in ch(
    "SELECT DISTINCT lower(t) FROM (SELECT token0 AS t FROM census.pools "
    "UNION ALL SELECT token1 AS t FROM census.pools)")}
print(f"census.pools: {len(census_tok):,} distinct tokens across 38,767 CL pools")

# the actually-tradeable sets the research plan quotes (64 @>=300d, 138 @>=90d)
tradeable = {}
for lbl, days in [("ge300d", 300), ("ge90d", 90), ("ge180d", 180)]:
    rows = ch(f"SELECT lower(alt), symbol, tradeable_days FROM edge.bars_universe_50 "
              f"WHERE tradeable_days >= {days}")
    tradeable[lbl] = {r[0] for r in rows}
    print(f"  bars_universe_50 tradeable_days>={days}: {len(rows)} pools, "
          f"{len(tradeable[lbl])} distinct alts")

# ---------------------------------------------------------------- vAMM side
att = json.load(open("/root/recon_out/s03_attribute.json"))
AERO_FAC = att["aero_factory"]
vp = [v for v in att["pools"].values() if v["factory"] == AERO_FAC]
tot_logs = sum(v["sampled_logs"] for v in vp)
print(f"\nAerodrome pools active in probe: {len(vp):,}  sampled swaps {tot_logs:,}")


def alt_of(v):
    """The non-quote leg. None if both legs are quote assets (no new alt)."""
    t0, t1 = v["token0"], v["token1"]
    a0, a1 = t0 not in STABLES, t1 not in STABLES
    if a0 and not a1:
        return t0
    if a1 and not a0:
        return t1
    if a0 and a1:
        return t0  # alt/alt pair: attribute to token0, counted separately below
    return None


def sym_of(v, alt):
    """Parse 'vAMM-WETH/OVPP' -> the symbol on the alt side."""
    s = (v["symbol"] or "").split("-", 1)[-1]
    if "/" not in s:
        return s
    a, b = s.split("/", 1)
    return b if alt == v["token1"] else a


agg = defaultdict(lambda: {"logs": 0, "pools": 0, "syms": set(), "has_weth": False,
                           "stable_pools": 0, "alt_alt": 0})
quote_only = {"logs": 0, "pools": 0}
for v in vp:
    a = alt_of(v)
    if a is None:
        quote_only["logs"] += v["sampled_logs"]
        quote_only["pools"] += 1
        continue
    d = agg[a]
    d["logs"] += v["sampled_logs"]
    d["pools"] += 1
    d["syms"].add(sym_of(v, a))
    if WETH in (v["token0"], v["token1"]):
        d["has_weth"] = True
    if v["stable"]:
        d["stable_pools"] += 1
    if v["token0"] not in STABLES and v["token1"] not in STABLES:
        d["alt_alt"] += 1

print(f"quote/quote pools (WETH-USDC etc, add no alt): {quote_only['pools']} pools, "
      f"{quote_only['logs']:,} swaps ({quote_only['logs']/tot_logs*100:.1f}%)")
print(f"distinct alt assets traded on Aerodrome vAMM/sAMM in probe: {len(agg):,}")

# ---------------------------------------------------------------- overlap
buckets = {
    "in_bars_universe_50": lambda a: a in alt50,
    "in_census_pools_only": lambda a: a not in alt50 and a in census_tok,
    "genuinely_new": lambda a: a not in alt50 and a not in census_tok,
}
res = {}
alt_logs = sum(d["logs"] for d in agg.values())
for name, fn in buckets.items():
    sel = {a: d for a, d in agg.items() if fn(a)}
    lg = sum(d["logs"] for d in sel.values())
    res[name] = {"n_assets": len(sel), "sampled_logs": lg,
                 "share_of_alt_logs": lg / alt_logs if alt_logs else 0,
                 "share_of_all_logs": lg / tot_logs}
    print(f"\n{name}: {len(sel):,} assets, {lg:,} sampled swaps "
          f"({lg/alt_logs*100:.2f}% of alt-bearing vAMM volume, "
          f"{lg/tot_logs*100:.2f}% of all vAMM volume)")

# top new assets by volume
new = sorted(((a, d) for a, d in agg.items() if a not in alt50 and a not in census_tok),
             key=lambda kv: -kv[1]["logs"])
print(f"\ntop 25 GENUINELY NEW assets (absent from census.pools entirely):")
print(f"{'asset':<44}{'swaps':>7}{'pools':>6} weth  symbols")
for a, d in new[:25]:
    print(f"{a:<44}{d['logs']:>7,}{d['pools']:>6} {str(d['has_weth']):>5}  "
          f"{','.join(sorted(d['syms']))[:40]}")

# top assets already in the tradeable panel
dup = sorted(((a, d) for a, d in agg.items() if a in alt50), key=lambda kv: -kv[1]["logs"])
print(f"\ntop 25 assets ALREADY in edge.bars_universe_50 (duplicate, correlated):")
print(f"{'asset':<44}{'swaps':>7}{'pools':>6}  panel_sym  vamm_sym")
for a, d in dup[:25]:
    print(f"{a:<44}{d['logs']:>7,}{d['pools']:>6}  {sym50.get(a,''):<10} "
          f"{','.join(sorted(d['syms']))[:24]}")

# overlap against the actually-tradeable subsets
print()
for lbl, s in tradeable.items():
    hit = {a: d for a, d in agg.items() if a in s}
    print(f"vAMM assets that are in the tradeable {lbl} set: {len(hit)} of {len(s)} "
          f"({sum(d['logs'] for d in hit.values()):,} sampled swaps)")

out = {"panel": {"bars_universe_50_pools": len(univ50), "bars_universe_50_alts": len(alt50),
                 "census_tokens": len(census_tok),
                 "tradeable": {k: len(v) for k, v in tradeable.items()}},
       "vamm": {"active_pools_in_probe": len(vp), "sampled_logs": tot_logs,
                "distinct_alts": len(agg), "quote_only": quote_only,
                "alt_bearing_logs": alt_logs},
       "overlap": res,
       "top_new": [{"asset": a, "logs": d["logs"], "pools": d["pools"],
                    "has_weth": d["has_weth"], "syms": sorted(d["syms"]),
                    "stable_pools": d["stable_pools"]} for a, d in new[:120]],
       "top_dup": [{"asset": a, "logs": d["logs"], "pools": d["pools"],
                    "panel_symbol": sym50.get(a, ""), "syms": sorted(d["syms"])}
                   for a, d in dup[:80]],
       "tradeable_overlap": {lbl: sorted(a for a in agg if a in s)
                             for lbl, s in tradeable.items()}}
json.dump(out, open("/root/recon_out/s05_overlap.json", "w"), indent=2)
