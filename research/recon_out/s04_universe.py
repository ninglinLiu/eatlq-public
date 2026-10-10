"""Step 4: enumerate the Aerodrome PoolFactory universe (vAMM vs sAMM, WETH leg).

Also asks the on-chain FactoryRegistry whether more than one pool factory is
authorised, so the universe count cannot silently miss a legacy deployment.
"""
import json
import sys
from collections import Counter

sys.path.insert(0, "/root/recon_out")
from eth_hash.auto import keccak

import rpc_probe as R

FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
WETH = "0x4200000000000000000000000000000000000006"


def s(sig):
    return keccak(sig.encode()).hex()[:8]


# ---- 1. how many pool factories does the protocol itself recognise? --------
reg = None
for sig in ["factoryRegistry()"]:
    try:
        reg = R.dec_addr(R.eth_call(VOTER, "0x" + s(sig)))
        print(f"Voter.{sig} -> {reg}")
    except Exception as e:
        print(f"Voter.{sig} failed: {e}")

registry_facs = []
if reg:
    for sig in ["poolFactories()", "fallbackPoolFactories()"]:
        try:
            raw = R.eth_call(reg, "0x" + s(sig))
            n = R.dec_uint(raw, 1)
            facs = [R.dec_addr(raw, 2 + i) for i in range(n or 0)]
            print(f"FactoryRegistry.{sig} -> {n} : {facs}")
            if sig == "poolFactories()":
                registry_facs = facs
        except Exception as e:
            print(f"FactoryRegistry.{sig} failed: {str(e)[:120]}")

# ---- 2. enumerate allPools --------------------------------------------------
n_pools = R.dec_uint(R.eth_call(FACTORY, "0x" + s("allPoolsLength()")))
print(f"\nPoolFactory.allPoolsLength() = {n_pools:,}")

SEL_ALLPOOLS = s("allPools(uint256)")
addrs = []
CH = 2500
for lo in range(0, n_pools, CH):
    hi = min(lo + CH, n_pools)
    res = R.multicall3([(FACTORY, SEL_ALLPOOLS + R.enc_uint(i)) for i in range(lo, hi)],
                       chunk=CH)
    addrs += [R.dec_addr(d) if ok else None for ok, d in res]
    print(f"  allPools {lo:,}-{hi:,} ok  (rpc={R.STATS['calls']})", flush=True)
assert len(addrs) == n_pools
bad = sum(1 for a in addrs if a is None)
print(f"enumerated {len(addrs):,} pool addresses ({bad} failed)")

# ---- 3. metadata() for every pool -----------------------------------------
# metadata() -> (uint dec0, uint dec1, uint r0, uint r1, bool st, address t0, address t1)
SEL_META = s("metadata()")
meta = []
CH2 = 1200
for lo in range(0, n_pools, CH2):
    hi = min(lo + CH2, n_pools)
    res = R.multicall3([(a, SEL_META) for a in addrs[lo:hi]], chunk=CH2)
    for a, (ok, d) in zip(addrs[lo:hi], res):
        if not ok or len(d) < 2 + 7 * 64:
            meta.append({"pool": a, "ok": False})
            continue
        meta.append({"pool": a, "ok": True,
                     "dec0": R.dec_uint(d, 0), "dec1": R.dec_uint(d, 1),
                     "r0": R.dec_uint(d, 2), "r1": R.dec_uint(d, 3),
                     "stable": bool(R.dec_uint(d, 4)),
                     "token0": R.dec_addr(d, 5), "token1": R.dec_addr(d, 6)})
    print(f"  metadata {lo:,}-{hi:,} ok  (rpc={R.STATS['calls']})", flush=True)

okm = [m for m in meta if m["ok"]]
n_stable = sum(1 for m in okm if m["stable"])
n_vol = sum(1 for m in okm if not m["stable"])
weth = [m for m in okm if WETH.lower() in (m["token0"], m["token1"])]
weth_v = sum(1 for m in weth if not m["stable"])
weth_s = sum(1 for m in weth if m["stable"])

print(f"\n{'':<28}{'total':>9}{'volatile(vAMM)':>16}{'stable(sAMM)':>14}")
print(f"{'all pools':<28}{len(okm):>9,}{n_vol:>16,}{n_stable:>14,}")
print(f"{'with WETH leg':<28}{len(weth):>9,}{weth_v:>16,}{weth_s:>14,}")
print(f"metadata() failures: {len(meta)-len(okm)}")

# non-empty reserves = pool was ever funded
nonzero = [m for m in okm if (m["r0"] or 0) > 0 and (m["r1"] or 0) > 0]
print(f"pools with both reserves > 0 right now: {len(nonzero):,} "
      f"({sum(1 for m in nonzero if not m['stable']):,} vAMM / "
      f"{sum(1 for m in nonzero if m['stable']):,} sAMM)")

tok = Counter()
for m in okm:
    tok[m["token0"]] += 1
    tok[m["token1"]] += 1
print(f"distinct tokens across all Aerodrome vAMM/sAMM pools: {len(tok):,}")

json.dump({"factory": FACTORY.lower(), "voter": VOTER.lower(),
           "factory_registry": reg, "registry_pool_factories": registry_facs,
           "allPoolsLength": n_pools,
           "counts": {"resolved": len(okm), "volatile": n_vol, "stable": n_stable,
                      "weth_leg": len(weth), "weth_volatile": weth_v,
                      "weth_stable": weth_s, "meta_fail": len(meta) - len(okm),
                      "funded_now": len(nonzero),
                      "distinct_tokens": len(tok)},
           "pools": meta, "rpc_stats": dict(R.STATS)},
          open("/root/recon_out/s04_universe.json", "w"), indent=2)
print(f"\nRPC calls used: {R.STATS['calls']}")
