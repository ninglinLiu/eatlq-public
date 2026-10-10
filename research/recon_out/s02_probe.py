"""Step 2: measure vAMM Swap log density across the 366-day panel.

Design fixed before looking at any result:
  - panel = blocks 34,300,000 .. 50,085,519 (census.bf_progress min/max), 15,785,520 blocks
  - N_PROBE = 40 anchors, evenly spaced, window = 1,000 blocks each  (0.253% of panel)
  - at every anchor also probe the CL topic0, whose true count is known exactly from
    census.cl_swaps. That turns the extrapolation into a *validated* estimate rather
    than an assumption: the CL error is a direct read of the method's bias.
Also records, per log, the emitting contract so volume can be attributed to factories
(topic0 b3e27736 is the generic Solidly signature, not Aerodrome-exclusive).
"""
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/recon_out")
import rpc_probe as R

VAMM = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
CL = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
PANEL_LO, PANEL_HI = 34_300_000, 50_085_519
N_PROBE, WIN = 40, 1_000

span = PANEL_HI - PANEL_LO + 1
step = span // N_PROBE
anchors = [PANEL_LO + i * step for i in range(N_PROBE)]
ranges = [(a, min(a + WIN - 1, PANEL_HI)) for a in anchors]

print(f"panel {PANEL_LO:,}-{PANEL_HI:,} = {span:,} blocks; "
      f"{N_PROBE} probes x {WIN} blocks = {N_PROBE*WIN:,} "
      f"({N_PROBE*WIN/span*100:.3f}% of panel)")


def probe(args):
    (lo, hi), topic = args
    res, err = R.rpc_batch([("eth_getLogs", [{"topics": [topic],
                                              "fromBlock": hex(lo), "toBlock": hex(hi)}])])[0]
    if err:
        return (lo, hi, topic, None, str(err)[:200])
    return (lo, hi, topic, res, None)


jobs = [(r, VAMM) for r in ranges] + [(r, CL) for r in ranges]
with ThreadPoolExecutor(max_workers=6) as ex:
    results = list(ex.map(probe, jobs))

per_range = {}
vamm_pool_counter = Counter()
for lo, hi, topic, logs, err in results:
    key = f"{lo}"
    per_range.setdefault(key, {"from": lo, "to": hi, "n_blocks": hi - lo + 1})
    tag = "vamm" if topic == VAMM else "cl"
    if err:
        per_range[key][tag] = None
        per_range[key][tag + "_err"] = err
        continue
    per_range[key][tag] = len(logs)
    per_range[key][tag + "_pools"] = len({l["address"].lower() for l in logs})
    if topic == VAMM:
        for l in logs:
            vamm_pool_counter[l["address"].lower()] += 1

rows = sorted(per_range.values(), key=lambda r: r["from"])
tot_blocks = sum(r["n_blocks"] for r in rows)
tot_vamm = sum(r["vamm"] or 0 for r in rows)
tot_cl = sum(r["cl"] or 0 for r in rows)

print(f"\n{'from_block':>12} {'blocks':>7} {'vAMM':>8} {'vPools':>7} {'CL':>8} {'vAMM/CL':>8}")
for r in rows:
    ratio = (r["vamm"] / r["cl"]) if r.get("cl") else float("nan")
    print(f"{r['from']:>12,} {r['n_blocks']:>7,} {r['vamm']:>8,} "
          f"{r.get('vamm_pools', 0):>7,} {r['cl']:>8,} {ratio:>8.3f}")

print(f"\nTOTAL sampled blocks {tot_blocks:,}: vAMM={tot_vamm:,} CL={tot_cl:,} "
      f"ratio={tot_vamm/tot_cl:.4f}")
scale = span / tot_blocks
print(f"scale factor = {scale:.2f}")
print(f"extrapolated vAMM over panel = {tot_vamm*scale:,.0f}")
print(f"extrapolated CL   over panel = {tot_cl*scale:,.0f}  "
      f"(true census.cl_swaps = 424,433,536)")

out = {
    "design": {"panel_lo": PANEL_LO, "panel_hi": PANEL_HI, "panel_blocks": span,
               "n_probe": N_PROBE, "win": WIN, "sampled_blocks": tot_blocks,
               "sampled_frac": tot_blocks / span},
    "per_range": rows,
    "totals": {"vamm_logs": tot_vamm, "cl_logs": tot_cl,
               "vamm_over_cl": tot_vamm / tot_cl if tot_cl else None,
               "scale": scale,
               "vamm_extrapolated": tot_vamm * scale,
               "cl_extrapolated": tot_cl * scale},
    "vamm_pool_log_counts": vamm_pool_counter.most_common(),
    "rpc_stats": dict(R.STATS),
}
json.dump(out, open("/root/recon_out/s02_probe.json", "w"), indent=2)
print(f"\ndistinct vAMM pools seen across all probes: {len(vamm_pool_counter):,}")
print(f"RPC calls used: {R.STATS['calls']}  downloaded {R.STATS['bytes']/1e6:.1f} MB")
