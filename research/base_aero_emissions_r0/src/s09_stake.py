"""Step 4: the emission denominator -- staked liquidity in each CL gauge, per epoch.

What Slipstream actually divides by
-----------------------------------
CLGauge rewards accrue through CLPool.rewardGrowthGlobalX128, which advances by
    rewardRate * dt / stakedLiquidity
where CLPool.stakedLiquidity() is the *currently in-range* liquidity belonging to NFT
positions deposited in the gauge. Out-of-range staked positions earn nothing while out of
range. So the causal denominator for "what share of the epoch's AERO does my position
get" is the time-average of in-range stakedLiquidity, not pool TVL and not total staked
liquidity.

It is a path-dependent integral that cannot be recovered exactly without replaying every
swap and every stake event. We sample it at SAMPLES_PER_EPOCH evenly spaced blocks per
epoch through the archive RPC and store the mean, plus the dispersion of the samples so a
consumer can see how shaky the estimate is.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

SAMPLES_PER_EPOCH = 4
BLOCKS_PER_WEEK = 302400  # Base is a deterministic 2s chain over this panel
CKPT = f"{C.OUT}/stake_samples.jsonl"

# Archive eth_call at old blocks is latency-bound, not throughput-bound, so push
# concurrency up close to the provider's 50 req/s ceiling.
R.RATE_LIMIT_PER_SEC = 45.0
WORKERS = 40


def main():
    panel = C.load_json("panel.json")
    epochs = panel["epochs"]
    rows = C.ch_rows("SELECT pool, gauge, created_block FROM census.aero_gauge_map ORDER BY pool")
    print(f"{len(rows)} gauges, {len(epochs)} epochs, {SAMPLES_PER_EPOCH} samples/epoch")

    jobs = []
    for r in rows:
        cb = int(r["created_block"] or 0)
        for ep in epochs:
            b0, b1 = ep["block_start"], ep["block_end"]
            step = (b1 - b0) // SAMPLES_PER_EPOCH
            for k in range(SAMPLES_PER_EPOCH):
                bn = b0 + step // 2 + k * step
                if cb and bn < cb:
                    continue
                jobs.append((r["pool"], r["gauge"], ep["epoch_start"], bn))
    print(f"{len(jobs)} sample points -> {2*len(jobs)} eth_calls")

    out = {}
    if os.path.exists(CKPT):
        with open(CKPT) as fh:
            for line in fh:
                try:
                    g, ep, bn, sl, tl = json.loads(line)
                except Exception:  # noqa: BLE001 - tolerate a torn final line
                    continue
                out[(g, ep, bn)] = None if sl is None else (sl, tl)
        print(f"resumed {len(out)} samples from checkpoint")
    jobs = [j for j in jobs if (j[1], j[2], j[3]) not in out]
    print(f"{len(jobs)} still to do")

    ck = open(CKPT, "a")
    ck_lock = threading.Lock()

    def work(batch):
        items = []
        for pool, gauge, ep, bn in batch:
            items.append(("eth_call", [{"to": pool, "data": C.SEL["stakedLiquidity()"]}, hex(bn)]))
            items.append(("eth_call", [{"to": pool, "data": C.SEL["liquidity()"]}, hex(bn)]))
        res = R.rpc_batch(items)
        lines = []
        for i, (pool, gauge, ep, bn) in enumerate(batch):
            sl, sl_err = res[2 * i]
            tl, tl_err = res[2 * i + 1]
            if sl_err or tl_err or not sl or sl == "0x" or not tl or tl == "0x":
                val = None
            else:
                val = (R.dec_uint(sl), R.dec_uint(tl))
            out[(gauge, ep, bn)] = val
            lines.append(json.dumps([gauge, ep, bn,
                                     None if val is None else val[0],
                                     None if val is None else val[1]]))
        with ck_lock:
            ck.write("\n".join(lines) + "\n")
            ck.flush()

    batches = [jobs[i:i + 10] for i in range(0, len(jobs), 10)]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, _ in enumerate(ex.map(work, batches)):
            if (i + 1) % 200 == 0:
                print(f"  {(i+1)*10}/{len(jobs)} samples  rpc={R.STATS}", flush=True)
    ck.close()

    ok = sum(1 for v in out.values() if v is not None)
    print(f"samples ok={ok} empty/failed={len(out)-ok}")

    C.ch("DROP TABLE IF EXISTS census.aero_gauge_stake_samples")
    C.ch("""
    CREATE TABLE census.aero_gauge_stake_samples (
        gauge String, pool String, epoch_start DateTime, block_number UInt64,
        staked_liquidity Float64, pool_liquidity Float64
    ) ENGINE = MergeTree ORDER BY (gauge, epoch_start, block_number)
    """)
    pool_of = {r["gauge"]: r["pool"] for r in rows}
    C.ch_insert_tsv(
        "census.aero_gauge_stake_samples",
        ["gauge", "pool", "epoch_start", "block_number", "staked_liquidity", "pool_liquidity"],
        [(g, pool_of[g], ep, bn, f"{v[0]:.18g}", f"{v[1]:.18g}")
         for (g, ep, bn), v in out.items() if v is not None])
    print("samples written")


if __name__ == "__main__":
    main()
