#!/usr/bin/env python3
"""Instrument audit: does the stored `fee` field in edge.bars_universe_50 equal the
pool's real on-chain fee?

Every C-family verdict prices fee revenue as notional * fee_field / 1e6. Round 37
already found one Aerodrome Slipstream pool whose static fee field disagreed with the
fee implied by its own swaps (3.64 bps stored vs 0.60 bps implied), because Slipstream
fees are settable per pool by the factory rather than pinned by tick spacing. If that
is widespread the fee field is a systematically wrong instrument and every C verdict
resting on it is void in an unknown direction.

Reads fee() and tickSpacing() from each pool contract at head and at a block near the
start of the panel window, then compares against the stored field. Read-only eth_call.
"""

import json
import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")
import rpc

OUT = "/root/base_condmm_r0/out"

SEL_FEE = "0xddca3f43"          # fee()
SEL_TICKSPACING = "0xd0c93a7c"  # tickSpacing()


def main():
    pools = [json.loads(l) for l in open(f"{OUT}/universe50.jsonl") if l.strip()]
    print(f"pools: {len(pools)}")

    head_n = int(rpc.rpc("eth_blockNumber", []), 16)
    early_n = head_n - int(360 * 86400 / 2)  # Base runs 2s blocks
    print(f"head block  : {head_n}")
    print(f"early block : {early_n}")

    reqs = []
    for p in pools:
        for blk in ("latest", hex(early_n)):
            reqs.append(("eth_call", [{"to": p["pool"], "data": SEL_FEE}, blk]))
            reqs.append(("eth_call", [{"to": p["pool"], "data": SEL_TICKSPACING}, blk]))
    print(f"rpc calls: {len(reqs)}")

    res = rpc.rpc_batch(reqs)

    rows = []
    k = 0
    for p in pools:
        rec = {"pool": p["pool"], "venue": p["venue"], "symbol": p["symbol"],
               "fee_field": int(p["fee"])}
        for tag in ("now", "early"):
            fee_hex, fee_err = res[k]
            ts_hex, ts_err = res[k + 1]
            k += 2
            rec[f"fee_chain_{tag}"] = None if fee_err else rpc.dec_uint(fee_hex)
            rec[f"ts_{tag}"] = None if ts_err else rpc.dec_int(ts_hex)
        rows.append(rec)

    json.dump(rows, open(f"{OUT}/onchain_fee_audit.json", "w"), indent=1)

    ok = [r for r in rows if r["fee_chain_now"] is not None]
    print(f"\nresolved fee() at head for {len(ok)}/{len(rows)} pools")

    mism = [r for r in ok if r["fee_chain_now"] != r["fee_field"]]
    print(f"stored field != on-chain fee() at head : {len(mism)}/{len(ok)}")

    drift = [r for r in ok if r["fee_chain_early"] is not None
             and r["fee_chain_early"] != r["fee_chain_now"]]
    print(f"on-chain fee() changed over the year   : {len(drift)}/{len(ok)}")

    by_venue = {}
    for r in ok:
        v = by_venue.setdefault(r["venue"], [0, 0])
        v[0] += 1
        if r["fee_chain_now"] != r["fee_field"]:
            v[1] += 1
    print("\nmismatch rate by venue:")
    for v, (n, m) in sorted(by_venue.items()):
        print(f"  {v:10s} {m:4d}/{n:4d}  {100.0 * m / n:5.1f}%")

    if mism:
        print("\n" + "=" * 100)
        print("MISMATCHES (stored fee field vs on-chain fee())")
        print("=" * 100)
        print(f"{'pool':44s} {'venue':8s} {'sym':10s} {'stored':>8s} {'chain':>8s} "
              f"{'early':>8s} {'ts':>7s}")
        for r in sorted(mism, key=lambda x: -abs(x["fee_chain_now"] - x["fee_field"]))[:80]:
            print(f"{r['pool']:44s} {r['venue']:8s} {r['symbol'][:10]:10s} "
                  f"{r['fee_field']:8d} {r['fee_chain_now']:8d} "
                  f"{str(r['fee_chain_early']):>8s} {str(r['ts_now']):>7s}")

    if drift:
        print("\n" + "=" * 100)
        print("FEE CHANGED DURING THE PANEL WINDOW (a static field cannot be right here)")
        print("=" * 100)
        for r in sorted(drift, key=lambda x: -abs(x["fee_chain_now"] - x["fee_chain_early"]))[:80]:
            print(f"{r['pool']:44s} {r['venue']:8s} {r['symbol'][:10]:10s} "
                  f"early={r['fee_chain_early']:8d} now={r['fee_chain_now']:8d} "
                  f"stored={r['fee_field']:8d}")

    print("\n" + "=" * 100)
    print("cbBTC pools (the cell driving the only positive C1b result)")
    print("=" * 100)
    print(f"{'pool':44s} {'venue':8s} {'stored':>8s} {'chain':>8s} {'early':>8s} {'ts':>7s}")
    for r in sorted([x for x in rows if x["symbol"] == "cbBTC"], key=lambda x: x["fee_field"]):
        print(f"{r['pool']:44s} {r['venue']:8s} {r['fee_field']:8d} "
              f"{str(r['fee_chain_now']):>8s} {str(r['fee_chain_early']):>8s} "
              f"{str(r['ts_now']):>7s}")

    print(f"\nrpc stats: {rpc.STATS}")


if __name__ == "__main__":
    main()
