#!/usr/bin/env python3
"""Stratified eth_getLogs sample of liquidation events on Base over the 366-day panel.

Read-only. Writes only into /root/recon_out/.
Panel block range taken from census.cl_swaps: 34,300,000 .. 50,085,519.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/recon_out")
from probe_rpc import CALLS, get_logs  # noqa: E402

TOPICS = {
    "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41": "MORPHO_Liquidate",
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286": "AAVE_LiquidationCall",
    "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52": "CV2_LiquidateBorrow",
    "0x9850ab1af75177e4a9201c65a2cf7976d5d28e40ef63494b44366f86b2f9412e": "COMET_AbsorbCollateral",
    "0x1547a878dc89ad3c367b6338b4be6a65a5dd74fb77ae044da1e8747ef1f4f62f": "COMET_AbsorbDebt",
}

B0, B1 = 34_300_000, 50_085_519
SPAN = 10_000
N_WIN = int(sys.argv[1]) if len(sys.argv) > 1 else 60

step = (B1 - B0 - SPAN) // (N_WIN - 1)
windows = [(B0 + i * step, B0 + i * step + SPAN - 1) for i in range(N_WIN)]


def one(w):
    a, b = w
    r = get_logs([list(TOPICS.keys())], a, b)
    if "error" in r:
        return a, b, None, r["error"].get("message", "")[:80]
    return a, b, r["result"], None


if __name__ == "__main__":
    out = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for a, b, logs, err in ex.map(one, windows):
            if err:
                print(f"[win] {a}-{b} ERROR {err}", flush=True)
                out.append({"from": a, "to": b, "error": err})
                continue
            c = Counter(TOPICS[l["topics"][0]] for l in logs)
            out.append({"from": a, "to": b, "n": len(logs), "by_proto": dict(c),
                        "logs": logs})
            print(f"[win] {a}-{b} n={len(logs):<4} {dict(c)}", flush=True)

    ok = [w for w in out if "error" not in w]
    tot = Counter()
    for w in ok:
        tot.update(w["by_proto"])
    blocks_sampled = len(ok) * SPAN
    frac = blocks_sampled / (B1 - B0 + 1)
    print("\n=== SAMPLE SUMMARY ===")
    print(f"windows_ok={len(ok)}/{N_WIN}  blocks_sampled={blocks_sampled:,} "
          f"({frac*100:.3f}% of panel)")
    for k, v in sorted(tot.items(), key=lambda x: -x[1]):
        print(f"  {k:24s} sampled={v:<6} extrapolated_year={v/frac:,.0f}")
    print(f"  {'TOTAL':24s} sampled={sum(tot.values()):<6} "
          f"extrapolated_year={sum(tot.values())/frac:,.0f}")
    print("rpc_calls:", CALLS)
    json.dump({"windows": out, "totals": dict(tot), "frac": frac,
               "panel": [B0, B1], "span": SPAN, "calls": CALLS},
              open("/root/recon_out/sample_liq.json", "w"))
