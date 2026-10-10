#!/usr/bin/env python3
"""Active-exposure (butterfly) model.

Question answered with REAL market data, not a guessed multiplier:
  "When a large buyer (relative to pool WETH depth) enters a fresh pool, does the
   pool become unsellable (rug / blacklist / death) more often than when only small
   buyers enter?"

Method (causal-ish, observational):
  - Universe = pools we actually priced + real-gas replayed (the gated set).
  - For each pool, fetch the pool's WETH reserve at the entry block (depth).
  - From on-chain swaps in the holding window, find the LARGEST single buy
    (WETH -> token) and express it as a share of that depth: max_buy_share.
  - Label the pool "destroyed" if it became unsellable/rug:
      exit_sell == 'revert'  (real-gas sell reverts at exit)  OR
      real_net <= -0.99      (death / ~total loss).
  - Estimate P(destroyed | max_buy_share bucket). If destruction rises with
    buyer share, that rise is the *active-exposure penalty*: being a big fish
    is itself a risk factor, separate from the token's intrinsic quality.

Output: a penalty curve mapping OUR intended position share -> incremental
destruction probability, plus per-pool shares for $100 vs $1000 sizing.
"""
from __future__ import annotations
import json, os, subprocess, sys
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keystone_live as K
import replay_realgas_sellsim as RG
from web3 import Web3

CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
REPLAY = os.path.join(os.path.dirname(__file__), "_realgas_replay.json")
RES_CACHE = os.path.join(os.path.dirname(__file__), "_pool_weth_reserve.json")
FLOW_CACHE = os.path.join(os.path.dirname(__file__), "_pool_maxbuy_flow.json")
OUT = os.path.join(os.path.dirname(__file__), "_active_exposure.json")
WETH = RG.WETH
_BAL_SIG = "0x70a08231"
ETH_USD = 3000.0
SHARE_BUCKETS = [0.0, 0.01, 0.025, 0.05, 0.10, 0.25, 0.50, 1.01]


def weth_reserve_at(pool, block, cache):
    key = f"{pool}:{block}"
    if key in cache:
        return cache[key]
    data = _BAL_SIG + Web3.to_checksum_address(pool)[2:].rjust(64, "0").lower()
    try:
        r = RG.w3.provider.make_request("eth_call", [{"to": WETH, "data": data}, RG._block_tag(block)])
        v = RG._parse_uint((r.get("result") or "0x"))
    except Exception:
        v = None
    cache[key] = v
    if len(cache) % 25 == 0:
        json.dump(cache, open(RES_CACHE, "w"))
    return v


def max_buy_weth(pool, orient, b0, b1, cache):
    # buy = WETH into pool. orient=1 (alt=token0,WETH=token1): amount1>0, weth=amount1.
    #       orient=-1 (WETH=token0): amount0>0, weth=amount0.
    key = f"{pool}:{int(b0)}:{int(b1)}"
    if key in cache:
        c = cache[key]
        return c.get("max_buy"), c.get("n_buys", 0)
    if orient == 1:
        cond, leg = "amount1 > 0", "amount1"
    else:
        cond, leg = "amount0 > 0", "amount0"
    sql = f"""
    SELECT maxIf(abs(toFloat64({leg})), {cond}) AS max_buy,
           countIf({cond}) AS n_buys
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id=8453 AND pool_address='{pool}'
      AND block_number > {int(b0)} AND block_number <= {int(b1)}
    FORMAT TabSeparated
    """
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        cache[key] = {"max_buy": None, "n_buys": 0}
        return None, 0
    parts = r.stdout.strip().split("\t")
    try:
        mb, nb = float(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        mb, nb = None, 0
    cache[key] = {"max_buy": mb, "n_buys": nb}
    if len(cache) % 25 == 0:
        json.dump(cache, open(FLOW_CACHE, "w"))
    return mb, nb


def main():
    df, _ = K.load()
    df = df[["pool", "orient", "alt"]].drop_duplicates("pool").set_index("pool")
    replay = json.load(open(REPLAY))
    rescache = json.load(open(RES_CACHE)) if os.path.exists(RES_CACHE) else {}
    flowcache = json.load(open(FLOW_CACHE)) if os.path.exists(FLOW_CACHE) else {}

    rows = []
    pools = [p for p in replay if replay[p].get("status") == "ok" and p in df.index]
    print(f"active-exposure universe: {len(pools)} pools", flush=True)
    for i, pool in enumerate(pools, 1):
        rr = replay[pool]
        b0, b1 = rr.get("entry_block"), rr.get("exit_block")
        orient = int(df.loc[pool, "orient"])
        real_net = rr.get("real_net", rr.get("cached_net"))
        exit_sell = rr.get("exit_sell")
        if not b0 or not b1 or b1 <= b0:
            continue
        reserve = weth_reserve_at(pool, b0, rescache)
        if not reserve or reserve <= 0:
            continue
        max_buy, n_buys = max_buy_weth(pool, orient, b0, b1, flowcache)
        if max_buy is None:
            max_buy = 0.0
        # Split destruction: revert == TRUE unsellable (rug/blacklist/honeypot),
        # death == price wipeout but still sellable. Butterfly hypothesis is about
        # being turned unsellable, so the penalty is keyed on REVERT.
        rug = (exit_sell == "revert")
        death = (not rug) and (real_net is not None and real_net <= -0.99)
        rows.append({
            "pool": pool, "reserve_wei": reserve, "max_buy_wei": max_buy,
            "max_buy_share": max_buy / reserve if reserve else None,
            "n_buys": n_buys, "real_net": real_net, "exit_sell": exit_sell,
            "rug": bool(rug), "death": bool(death),
            "share_100usd": (100.0 / ETH_USD * 1e18) / reserve,
            "share_1000usd": (1000.0 / ETH_USD * 1e18) / reserve,
        })
        if i % 50 == 0:
            print(f"  {i}/{len(pools)}", flush=True)
    json.dump(rescache, open(RES_CACHE, "w"))
    json.dump(flowcache, open(FLOW_CACHE, "w"))
    R = pd.DataFrame(rows)
    R = R[R.max_buy_share.notna()]
    print(f"\nusable pools: {len(R)}  rug(revert)={int(R.rug.sum())}  death(price)={int(R.death.sum())}")

    # Only pools with REAL participation (>=3 buys) isolate the exposure question
    # from dead-on-arrival pools that are 'destroyed' purely by silence.
    live = R[R.n_buys >= 3]
    print(f"live pools (>=3 buys): {len(live)}  rug={int(live.rug.sum())}  death={int(live.death.sum())}")

    def curve_for(frame, col, label):
        print(f"\n=== P({label}) by LARGEST observed buyer share of pool WETH depth ===")
        out = []
        base = None
        for lo, hi in zip(SHARE_BUCKETS[:-1], SHARE_BUCKETS[1:]):
            sub = frame[(frame.max_buy_share >= lo) & (frame.max_buy_share < hi)]
            if len(sub) == 0:
                continue
            rate = float(sub[col].mean())
            if base is None:
                base = rate
            out.append({"share_lo": lo, "share_hi": hi, "n": len(sub),
                        "rate": rate, "penalty_vs_base": max(0.0, rate - base)})
            print(f"  share [{lo:.3f},{hi:.3f}) n={len(sub):4d} {label}={rate:.3f} "
                  f"penalty=+{max(0.0, rate-base):.3f}")
        return out

    rug_curve = curve_for(live, "rug", "rug/unsellable")
    death_curve = curve_for(live, "death", "price-death")

    def implied(curve, share):
        for c in curve:
            if c["share_lo"] <= share < c["share_hi"]:
                return c["penalty_vs_base"]
        return curve[-1]["penalty_vs_base"] if curve else 0.0

    for col, curve in (("rug_pen", rug_curve), ("death_pen", death_curve)):
        live[col + "_100"] = live.share_100usd.map(lambda s: implied(curve, s))
        live[col + "_1000"] = live.share_1000usd.map(lambda s: implied(curve, s))
    frac_1000_over5 = float((live.share_1000usd > 0.05).mean())
    print("\n=== implied active-exposure penalty (live pools) ===")
    print(f"  $1000 median pool share={live.share_1000usd.median():.4f}  "
          f"frac $1000>5% depth={frac_1000_over5:.1%}")
    print(f"  rug penalty:   $100={live.rug_pen_100.mean():.3f}  $1000={live.rug_pen_1000.mean():.3f}")
    print(f"  death penalty: $100={live.death_pen_100.mean():.3f}  $1000={live.death_pen_1000.mean():.3f}")
    print("\nCONFOUND WARNING: max_buy_share is the LARGEST buyer that *actually* "
          "entered. High-share pools self-select for live/real pools, so a flat/negative "
          "observed penalty does NOT prove our own $1000 is safe. Treat this as a lower "
          "bound on exposure risk, not a guarantee.")

    json.dump({
        "rug_curve": rug_curve,
        "death_curve": death_curve,
        "size_penalty_rug": {"100": float(live.rug_pen_100.mean()), "1000": float(live.rug_pen_1000.mean())},
        "size_penalty_death": {"100": float(live.death_pen_100.mean()), "1000": float(live.death_pen_1000.mean())},
        "median_share": {"100": float(live.share_100usd.median()), "1000": float(live.share_1000usd.median())},
        "frac_1000_over_5pct_depth": frac_1000_over5,
        "n_pools_all": len(R), "n_pools_live": len(live),
        "n_rug": int(R.rug.sum()), "n_death": int(R.death.sum()),
        "confound_note": "max_buy_share is observed largest buyer; high-share pools "
                         "self-select for live pools. Observational penalty is a lower bound.",
        "per_pool": R.set_index("pool")[["max_buy_share", "share_100usd", "share_1000usd",
                                         "rug", "death", "real_net"]].to_dict("index"),
    }, open(OUT, "w"), indent=2)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
