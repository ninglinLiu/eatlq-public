"""How wrong is the 4-samples-per-epoch stake estimate?

Densely re-samples a random subset of (gauge, epoch) cells at hourly resolution and
compares against the 4-sample mean actually stored in census.aero_gauge_stake. The point
is to publish the error distribution rather than assert the column is good.
"""

import random
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

R.RATE_LIMIT_PER_SEC = 42.0
DENSE = 168
N_CELLS = 60
SEED = 20260819


def main():
    panel = C.load_json("panel.json")
    ep_by_ts = {e["epoch_start"]: e for e in panel["epochs"]}

    cells = C.ch_rows("""
        SELECT gauge, pool, toUInt32(epoch_start) AS ep, staked_liquidity AS sl4,
               confidence, n_samples
        FROM census.aero_gauge_stake
        WHERE n_samples = 4 AND staked_liquidity > 0""")
    print(f"eligible cells: {len(cells)}")
    random.seed(SEED)
    pick = random.sample(cells, N_CELLS)

    jobs = []
    for c in pick:
        g = ep_by_ts[int(c["ep"])]
        step = (g["block_end"] - g["block_start"]) // DENSE
        for k in range(DENSE):
            jobs.append((c["pool"], c["gauge"], int(c["ep"]), g["block_start"] + k * step))
    print(f"{len(jobs)} dense samples -> {len(jobs)} eth_calls")

    vals = {}

    def work(batch):
        items = [("eth_call", [{"to": p, "data": C.SEL["stakedLiquidity()"]}, hex(bn)])
                 for p, g, e, bn in batch]
        res = R.rpc_batch(items)
        for (p, g, e, bn), (r, err) in zip(batch, res):
            if not err and r and r != "0x":
                vals.setdefault((g, e), []).append(R.dec_uint(r))

    batches = [jobs[i:i + 20] for i in range(0, len(jobs), 20)]
    with ThreadPoolExecutor(max_workers=30) as ex:
        for i, _ in enumerate(ex.map(work, batches)):
            if (i + 1) % 100 == 0:
                print(f"  {(i+1)*20}/{len(jobs)}", flush=True)

    rows, ratios = [], []
    for c in pick:
        key = (c["gauge"], int(c["ep"]))
        v = vals.get(key, [])
        if len(v) < DENSE * 0.8:
            continue
        dense = sum(v) / len(v)
        sl4 = float(c["sl4"])
        ratio = dense / sl4 if sl4 > 0 else None
        if ratio:
            ratios.append((ratio, c["confidence"]))
        rows.append((c["gauge"], c["pool"], int(c["ep"]), sl4, dense, len(v),
                     c["confidence"]))

    C.ch("DROP TABLE IF EXISTS census.aero_gauge_stake_validation")
    C.ch("""
    CREATE TABLE census.aero_gauge_stake_validation (
        gauge String, pool String, epoch_start DateTime,
        staked_liquidity_4sample Float64, staked_liquidity_168sample Float64,
        n_dense UInt16, confidence String
    ) ENGINE = MergeTree ORDER BY (gauge, epoch_start)
    """)
    C.ch_insert_tsv("census.aero_gauge_stake_validation",
                    ["gauge", "pool", "epoch_start", "staked_liquidity_4sample",
                     "staked_liquidity_168sample", "n_dense", "confidence"],
                    [(g, p, e, f"{a:.18g}", f"{b:.18g}", n, cf)
                     for g, p, e, a, b, n, cf in rows])
    print(f"\nvalidated {len(rows)} cells")

    print(C.ch("""
    SELECT confidence, count() n,
           round(quantileExact(0.1)(staked_liquidity_168sample/staked_liquidity_4sample),3) p10,
           round(quantileExact(0.5)(staked_liquidity_168sample/staked_liquidity_4sample),3) p50,
           round(quantileExact(0.9)(staked_liquidity_168sample/staked_liquidity_4sample),3) p90,
           round(max(staked_liquidity_168sample/staked_liquidity_4sample),1) max_ratio,
           countIf(abs(log2(staked_liquidity_168sample/staked_liquidity_4sample)) <= 1) within_2x,
           countIf(abs(log2(staked_liquidity_168sample/staked_liquidity_4sample)) <= log2(1.25)) within_25pct
    FROM census.aero_gauge_stake_validation
    GROUP BY confidence WITH TOTALS ORDER BY n DESC""", fmt="PrettyCompactMonoBlock"))


if __name__ == "__main__":
    main()
