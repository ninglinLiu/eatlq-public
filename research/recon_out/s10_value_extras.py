"""Step 10: final cost numbers with a CI, plus the three secondary value claims.

(a) emissions: does the concurrent emissions line's gauge map point at vAMM pools?
(b) sAMM: the stable pools are the structurally-quiet assets C3 was pre-registered on.
(c) cross-venue: how many assets would gain a second venue (already killed as arb,
    but relevant as an independent price series).
"""
import json
import statistics
import subprocess
import sys

WETH = "0x4200000000000000000000000000000000000006"


def ch(q):
    p = subprocess.run(["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
                        "--query", q], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:400])
    return [l.split("\t") for l in p.stdout.decode().strip().split("\n") if l]


probe = json.load(open("/root/recon_out/s02_probe.json"))
uni = json.load(open("/root/recon_out/s04_universe.json"))
att = json.load(open("/root/recon_out/s03_attribute.json"))
cost = json.load(open("/root/recon_out/s08_cost_pinned.json"))
ov = json.load(open("/root/recon_out/s05_overlap.json"))

# ---------------------------------------------------------------- 1. cost, with CI
CL_TRUE = 424_433_536
SPAN = 15_785_520
rows = probe["per_range"]
per_blk = [r["vamm"] / r["n_blocks"] for r in rows if r["vamm"] is not None]
n = len(per_blk)
mean = statistics.mean(per_blk)
se = statistics.stdev(per_blk) / n ** 0.5
bias = probe["totals"]["cl_extrapolated"] / CL_TRUE
est = mean * SPAN / bias
lo, hi = (mean - 1.96 * se) * SPAN / bias, (mean + 1.96 * se) * SPAN / bias
BPL = 828.0          # directly measured, single call, 26,802 logs (CL control: 892 == logged 892)
DISK_BPR = 117.5     # census.cl_swaps compressed bytes/row
CHUNKS = 1579
print("=== vAMM BACKFILL COST (366 d, blocks 34,300,000-50,085,519) ===")
print(f"  rows        : {est:,.0f}   95% CI [{lo:,.0f} .. {hi:,.0f}]  "
      f"(bias-corrected by {(bias-1)*100:+.2f}% from the CL control)")
print(f"  calls       : {CHUNKS:,} eth_getLogs @ 10,000 blocks")
print(f"  credits     : {CHUNKS*20:,}")
print(f"  download    : {est*BPL/1e9:.1f} GB  ({BPL:.0f} B/log measured)")
print(f"  disk        : {est*DISK_BPR/1e9:.1f} GB")
print(f"  wall clock  : {est*BPL/1e9*1000/20.7/60:.0f} min at the CL run's measured "
      f"20.7 MB/s aggregate (4 workers)")
print(f"  as % of the CL year backfill: credits 100%, download "
      f"{est*BPL/1e9/378.3*100:.1f}%, rows {est/CL_TRUE*100:.1f}%")

# ---------------------------------------------------------------- 2. emissions / gauges
gm = ch("SELECT count(), uniqExact(pool) FROM census.aero_gauge_map")
print(f"\n=== (a) emissions: census.aero_gauge_map has {gm[0][0]} rows, "
      f"{gm[0][1]} distinct pools ===")
cols = [r[0] for r in ch("SELECT name FROM system.columns WHERE database='census' "
                         "AND table='aero_gauge_map'")]
print(f"  columns: {cols}")
gpools = {r[0].lower() for r in ch("SELECT DISTINCT lower(pool) FROM census.aero_gauge_map")}
vamm_all = {m["pool"] for m in uni["pools"] if m.get("ok")}
cl_pools = {r[0].lower() for r in ch("SELECT DISTINCT lower(pool) FROM census.pools")}
print(f"  gauge-mapped pools that are Aerodrome vAMM/sAMM pools: "
      f"{len(gpools & vamm_all)} of {len(gpools)}")
print(f"  gauge-mapped pools that are CL pools already in census.pools: "
      f"{len(gpools & cl_pools)} of {len(gpools)}")
print(f"  gauge-mapped pools in NEITHER: {len(gpools - vamm_all - cl_pools)}")

# ---------------------------------------------------------------- 3. sAMM
sam = [m for m in uni["pools"] if m.get("ok") and m["stable"]]
sam_weth = [m for m in sam if WETH in (m["token0"], m["token1"])]
print(f"\n=== (b) sAMM stable pools (relevant to the pre-registered C3 line) ===")
print(f"  total sAMM pools on the factory : {len(sam):,}")
print(f"  with a WETH leg                 : {len(sam_weth):,}")
active = {k: v for k, v in att["pools"].items()
          if v["factory"] == att["aero_factory"] and v["stable"]}
print(f"  active in the probe             : {len(active):,} pools, "
      f"{att['aero_stable_logs']:,} sampled swaps "
      f"({att['aero_stable_logs']/att['aero_sampled_logs']*100:.2f}% of Aerodrome volume)")
byrow = {r["pool"]: r for r in cost["rows"]}
sa = sorted(active.values(), key=lambda v: -byrow[v["pool"]]["logs"])
print(f"\n  {'pool':<44}{'symbol':<26}{'fee':>4}{'win':>4}{'swaps':>7}")
for v in sa[:20]:
    r = byrow[v["pool"]]
    print(f"  {v['pool']:<44}{str(v['symbol'])[:25]:<26}{str(r['fee_bps']):>4}"
          f"{r['windows']:>4}{r['logs']:>7,}")
alt50 = {r[0] for r in ch("SELECT lower(alt) FROM edge.bars_universe_50")}
s_alts = {byrow[v["pool"]]["alt"] for v in active if byrow[v["pool"]]["alt"]}
print(f"\n  distinct alts on active sAMM pools: {len(s_alts)}; "
      f"already in bars_universe_50: {len(s_alts & alt50)}; new to it: {len(s_alts - alt50)}")

# ---------------------------------------------------------------- 4. cross-venue
print(f"\n=== (c) cross-venue: assets that would gain a second, independent venue ===")
o = ov["overlap"]
print(f"  assets already in edge.bars_universe_50 that also trade on vAMM: "
      f"{o['in_bars_universe_50']['n_assets']} "
      f"({o['in_bars_universe_50']['share_of_alt_logs']*100:.1f}% of alt-bearing "
      f"vAMM volume)")
g100 = cost["gates"]["100"]
print(f"  of those, at <=100 bps one-way at $50: {g100['in_panel_assets']} assets")
print(f"  NOTE: cross-venue arbitrage is closed (ARB_CLOSED_BY_BOTS, 86/86 negative), so "
      f"the value here is a second price series, not a spread trade.")

json.dump({"cost": {"rows_est": est, "rows_lo": lo, "rows_hi": hi,
                    "bias": bias, "bytes_per_log": BPL, "disk_bytes_per_row": DISK_BPR,
                    "chunks": CHUNKS, "credits": CHUNKS * 20,
                    "download_gb": est * BPL / 1e9, "disk_gb": est * DISK_BPR / 1e9,
                    "wall_min": est * BPL / 1e9 * 1000 / 20.7 / 60},
           "gauges": {"rows": int(gm[0][0]), "pools": int(gm[0][1]),
                      "on_vamm": len(gpools & vamm_all), "on_cl": len(gpools & cl_pools),
                      "neither": len(gpools - vamm_all - cl_pools)},
           "samm": {"total": len(sam), "weth_leg": len(sam_weth), "active": len(active),
                    "alts": len(s_alts), "alts_in_panel": len(s_alts & alt50),
                    "alts_new": len(s_alts - alt50)}},
          open("/root/recon_out/s10_value_extras.json", "w"), indent=2)
