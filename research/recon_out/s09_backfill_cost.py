"""Step 9: backfill cost for vAMM over the same 366 days, in the program's own units.

Reference (measured, from base_truth_panel_r0/out/backfill_year.log + census.bf_progress):
  CL topic0, blocks 34,300,000-50,085,519 = 15,785,520 blocks
    1,579 chunks x 10,000 blocks, 0 unaligned  -> exactly 1 eth_getLogs per chunk
    424,433,536 rows | 378.3 GB downloaded | 300.2 min | 31,560 credits (20/call, QuickNode Base)
    census.cl_swaps on disk: 46.45 GiB compressed = 117.5 bytes/row

This step measures vAMM at the PRODUCTION chunk size (10,000 blocks, 4 workers) so the
extrapolation uses the same units rather than rescaling from 1,000-block probes.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/recon_out")
import rpc_probe as R

VAMM = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
PANEL_LO, PANEL_HI = 34_300_000, 50_085_519
CHUNK = 10_000
N = 6

span = PANEL_HI - PANEL_LO + 1
n_chunks = -(-span // CHUNK)
step = (span // CHUNK) // N
anchors = [PANEL_LO + i * step * CHUNK for i in range(N)]

# reference numbers, measured not assumed
REF = {"chunks": 1579, "rows": 424_433_536, "gb": 302.48 + 75.8, "minutes": 243.6 + 56.6,
       "credits": 23_400 + 8_160, "disk_bytes_per_row": 117.5,
       "download_bytes_per_log": 302.48e9 / 339_173_337}
print(f"reference CL backfill: {REF['chunks']:,} chunks | {REF['rows']:,} rows | "
      f"{REF['gb']:.1f} GB | {REF['minutes']:.1f} min | {REF['credits']:,} credits")
print(f"  download {REF['download_bytes_per_log']:.0f} bytes/log, "
      f"disk {REF['disk_bytes_per_row']:.1f} bytes/row")
print(f"\nvAMM chunks needed over the same window: {n_chunks:,} x {CHUNK:,} blocks")


def one(a):
    b = min(a + CHUNK - 1, PANEL_HI)
    t0 = time.time()
    before = R.STATS["bytes"]
    res, err = R.rpc_batch([("eth_getLogs", [{"topics": [VAMM], "fromBlock": hex(a),
                                              "toBlock": hex(b)}])])[0]
    dt = time.time() - t0
    nb = R.STATS["bytes"] - before
    if err:
        return {"from": a, "err": str(err)[:150]}
    return {"from": a, "to": b, "n_logs": len(res), "bytes": nb, "seconds": dt}


t0 = time.time()
with ThreadPoolExecutor(max_workers=4) as ex:   # same WORKERS as backfill_cl_swaps.py
    out = list(ex.map(one, anchors))
wall = time.time() - t0

ok = [r for r in out if "n_logs" in r]
print(f"\n{'from_block':>12}{'logs':>9}{'MB':>8}{'B/log':>8}{'sec':>7}")
for r in ok:
    print(f"{r['from']:>12,}{r['n_logs']:>9,}{r['bytes']/1e6:>8.1f}"
          f"{r['bytes']/max(r['n_logs'],1):>8.0f}{r['seconds']:>7.1f}")

tot_logs = sum(r["n_logs"] for r in ok)
tot_bytes = sum(r["bytes"] for r in ok)
bpl = tot_bytes / tot_logs
print(f"\n{len(ok)} chunks: {tot_logs:,} logs, {tot_bytes/1e6:.1f} MB, "
      f"{bpl:.0f} bytes/log, wall {wall:.1f}s with 4 workers "
      f"({wall/len(ok):.2f} s/chunk, {tot_bytes/1e6/wall:.1f} MB/s)")

# --- extrapolate ----------------------------------------------------------
probe = json.load(open("/root/recon_out/s02_probe.json"))
est_rows_probe = probe["totals"]["vamm_extrapolated"]
cl_meas = probe["totals"]["cl_extrapolated"]
cl_true = REF["rows"]
bias = cl_meas / cl_true
est_rows_corr = est_rows_probe / bias
print(f"\nrow estimate")
print(f"  from 40x1,000-block probes            : {est_rows_probe:,.0f}")
print(f"  same method applied to CL             : {cl_meas:,.0f} vs true {cl_true:,} "
      f"-> bias {(bias-1)*100:+.2f}%")
print(f"  bias-corrected vAMM row estimate      : {est_rows_corr:,.0f}")
est_rows_chunk = tot_logs / (len(ok) * CHUNK) * span
print(f"  independent estimate from {len(ok)} full 10k chunks: {est_rows_chunk:,.0f}")

lo_est, hi_est = min(est_rows_corr, est_rows_chunk), max(est_rows_corr, est_rows_chunk)
mid = (lo_est + hi_est) / 2

gb = mid * bpl / 1e9
disk_gb = mid * REF["disk_bytes_per_row"] / 1e9
credits = n_chunks * 20
# two wall-clock bounds: download-throughput-bound, and per-chunk-latency-bound
rate_ref = REF["gb"] * 1e9 / (REF["minutes"] * 60)
min_dl = gb * 1e9 / rate_ref / 60
min_lat = n_chunks * (wall / len(ok)) / 60

print(f"\n=== vAMM BACKFILL COST, 366 days, blocks {PANEL_LO:,}-{PANEL_HI:,} ===")
print(f"  eth_getLogs calls   : {n_chunks:,}  (10,000-block chunks, 1 call each; the CL "
      f"run needed 0 halvings and vAMM is 11x sparser)")
print(f"  credits             : {credits:,}  (QuickNode Base = 20 credits/call, flat)")
print(f"  rows                : {mid:,.0f}   [{lo_est:,.0f} .. {hi_est:,.0f}]")
print(f"  download            : {gb:.1f} GB  ({bpl:.0f} bytes/log measured)")
print(f"  ClickHouse disk     : {disk_gb:.1f} GB  (at cl_swaps' {REF['disk_bytes_per_row']} "
      f"B/row; vAMM has 4 numeric cols vs 5, so this is a mild over-estimate)")
print(f"  wall clock          : {max(min_dl, min_lat):.0f} min  "
      f"(download-bound {min_dl:.0f}, per-chunk-latency-bound {min_lat:.0f})")
print(f"  vs CL year backfill : {credits/REF['credits']*100:.0f}% of the credits, "
      f"{gb/REF['gb']*100:.1f}% of the download, {mid/REF['rows']*100:.1f}% of the rows")

# --- time trend -----------------------------------------------------------
rows = probe["per_range"]
h = len(rows) // 2
q = len(rows) // 4
print(f"\n=== vAMM time trend (why WHEN the rows land matters) ===")
print(f"{'segment':<18}{'vAMM':>9}{'CL':>10}{'vAMM/CL':>9}")
for lbl, sl in [("Q1 2025-08..11", rows[:q]), ("Q2 2025-11..02", rows[q:2*q]),
                ("Q3 2026-02..05", rows[2*q:3*q]), ("Q4 2026-05..08", rows[3*q:])]:
    v = sum(r["vamm"] or 0 for r in sl)
    c = sum(r["cl"] or 0 for r in sl)
    print(f"{lbl:<18}{v:>9,}{c:>10,}{v/c:>9.3f}")
first, last = rows[0], rows[-1]
print(f"first probe {first['from']:,}: vAMM {first['vamm']:,}  "
      f"last probe {last['from']:,}: vAMM {last['vamm']:,}  "
      f"-> decline {first['vamm']/last['vamm']:.1f}x")

json.dump({"reference": REF, "chunks_needed": n_chunks, "measured_chunks": ok,
           "bytes_per_log": bpl, "wall_per_chunk_s": wall / len(ok),
           "estimates": {"rows_mid": mid, "rows_lo": lo_est, "rows_hi": hi_est,
                         "probe_bias_vs_cl_truth": bias,
                         "credits": credits, "download_gb": gb, "disk_gb": disk_gb,
                         "wall_min_download_bound": min_dl,
                         "wall_min_latency_bound": min_lat},
           "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s09_backfill_cost.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
