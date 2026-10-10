"""Step 1: confirm the vAMM Swap topic0 by decoding a real log, and find the factory."""
import json
import sys

sys.path.insert(0, "/root/recon_out")
from eth_hash.auto import keccak

import rpc_probe as R

VAMM_SIG = "Swap(address,address,uint256,uint256,uint256,uint256)"
VAMM_TOPIC = "0x" + keccak(VAMM_SIG.encode()).hex()
UNIV2_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
CL_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# Candidate Aerodrome / Velodrome-fork factories on Base. Verified below, not assumed.
CANDIDATES = {
    "aerodrome_PoolFactory": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
    "aerodrome_Router": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
    "aerodrome_Voter": "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5",
}
SEL = {
    "allPoolsLength()": "0xefde4e64",
    "allPairsLength()": "0x574f2ba3",
    "factory()": "0xc45a0155",
    "token0()": "0x0dfe1681",
    "token1()": "0xd21220a7",
    "stable()": "0x22be3de1",
    "metadata()": "0x392f37e9",
    "symbol()": "0x95d89b41",
    "isPool(address)": "0x5b16ebb7",
    "poolFactory()": "0x4219dc40",
    "voter()": "0x46c96aac",
}

out = {"topic0": {"vamm_computed": VAMM_TOPIC, "vamm_sig": VAMM_SIG,
                  "univ2": UNIV2_TOPIC, "cl": CL_TOPIC}}
print(f"vAMM topic0 = {VAMM_TOPIC}\n  from sig  = {VAMM_SIG}")

head = int(R.rpc("eth_blockNumber", []), 16)
out["head_block"] = head
print(f"head block = {head:,}")

# --- factory probes -------------------------------------------------------
fac = {}
items = []
for name, addr in CANDIDATES.items():
    items += [(addr, SEL["allPoolsLength()"][2:]), (addr, SEL["allPairsLength()"][2:]),
              (addr, SEL["voter()"][2:])]
res = R.multicall3(items)
for i, (name, addr) in enumerate(CANDIDATES.items()):
    ok_len, d_len = res[i * 3]
    ok_pair, d_pair = res[i * 3 + 1]
    ok_v, d_v = res[i * 3 + 2]
    fac[name] = {
        "address": addr,
        "allPoolsLength": R.dec_uint(d_len) if ok_len else None,
        "allPairsLength": R.dec_uint(d_pair) if ok_pair else None,
        "voter": R.dec_addr(d_v) if ok_v else None,
    }
    print(f"{name} {addr}: allPoolsLength={fac[name]['allPoolsLength']} "
          f"allPairsLength={fac[name]['allPairsLength']} voter={fac[name]['voter']}")
out["factory_probe"] = fac

# --- pull real logs and decode -------------------------------------------
lo, hi = head - 1500, head
logs = R.rpc("eth_getLogs", [{"topics": [VAMM_TOPIC], "fromBlock": hex(lo), "toBlock": hex(hi)}])
print(f"\neth_getLogs topic0=vAMM blocks {lo}-{hi}: {len(logs)} logs")
out["probe_range"] = {"from": lo, "to": hi, "n_logs": len(logs)}

addrs = []
for lg in logs:
    a = lg["address"].lower()
    if a not in addrs:
        addrs.append(a)
print(f"distinct emitting contracts: {len(addrs)}")
out["n_distinct_pools_in_probe"] = len(addrs)

sample = addrs[:24]
items = []
for a in sample:
    items += [(a, SEL["factory()"][2:]), (a, SEL["token0()"][2:]),
              (a, SEL["token1()"][2:]), (a, SEL["stable()"][2:]),
              (a, SEL["symbol()"][2:])]
res = R.multicall3(items)
pools = []
for i, a in enumerate(sample):
    ok_f, d_f = res[i * 5]
    ok_0, d_0 = res[i * 5 + 1]
    ok_1, d_1 = res[i * 5 + 2]
    ok_s, d_s = res[i * 5 + 3]
    ok_y, d_y = res[i * 5 + 4]
    pools.append({
        "pool": a,
        "factory": R.dec_addr(d_f) if ok_f else None,
        "token0": R.dec_addr(d_0) if ok_0 else None,
        "token1": R.dec_addr(d_1) if ok_1 else None,
        "stable": bool(R.dec_uint(d_s)) if ok_s else None,
        "symbol": R.dec_string(d_y) if ok_y else None,
    })
out["sampled_pools"] = pools
print("\npool -> factory / stable / symbol")
for p in pools:
    print(f"  {p['pool']} fac={p['factory']} stable={p['stable']} sym={p['symbol']}")

# --- decode one real log word by word ------------------------------------
ex = logs[0]
d = ex["data"][2:]
out["decoded_example"] = {
    "tx": ex["transactionHash"], "block": int(ex["blockNumber"], 16),
    "log_index": int(ex["logIndex"], 16), "address": ex["address"].lower(),
    "n_topics": len(ex["topics"]), "topics": ex["topics"],
    "data_len_bytes": len(d) // 2,
    "topic1_as_address": "0x" + ex["topics"][1][-40:],
    "topic2_as_address": "0x" + ex["topics"][2][-40:],
    "amount0In": int(d[0:64], 16), "amount1In": int(d[64:128], 16),
    "amount0Out": int(d[128:192], 16), "amount1Out": int(d[192:256], 16),
}
print("\ndecoded example log:")
print(json.dumps(out["decoded_example"], indent=2))

# --- isPool() cross-check on the factory ----------------------------------
best = max((v for v in fac.values() if v["allPoolsLength"]),
           key=lambda v: v["allPoolsLength"], default=None)
if best:
    items = [(best["address"], SEL["isPool(address)"][2:] + R.enc_addr(p["pool"]))
             for p in pools]
    res = R.multicall3(items)
    ip = [(p["pool"], bool(R.dec_uint(d))) for p, (ok, d) in zip(pools, res)]
    out["isPool_check"] = {"factory": best["address"],
                           "n_true": sum(1 for _, v in ip if v), "n": len(ip),
                           "detail": ip}
    print(f"\nisPool() on {best['address']}: {out['isPool_check']['n_true']}/{len(ip)} true")

out["rpc_stats"] = dict(R.STATS)
print(f"\nRPC calls used: {R.STATS['calls']}")
json.dump(out, open("/root/recon_out/s01_identify.json", "w"), indent=2)
