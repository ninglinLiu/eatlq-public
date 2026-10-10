"""Step 3a: scan AERO emission notifications over the panel.

Two independent log sources:
  A. Voter.DistributeReward(sender, gauge indexed, amount)  -- one address, all gauges
  B. CLGauge.NotifyReward(from indexed, amount)             -- the 157 CL gauges
They should agree 1:1 because Voter.distribute() is what calls gauge.notifyRewardAmount().
"""

import json
import sys
import time

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

OUTA = f"{C.OUT}/logs_distribute_reward.jsonl"
OUTB = f"{C.OUT}/logs_notify_reward.jsonl"


def dump(path, logs):
    with open(path, "w") as fh:
        for lg in logs:
            fh.write(json.dumps({
                "blockNumber": int(lg["blockNumber"], 16),
                "blockTimestamp": int(lg["blockTimestamp"], 16)
                if lg.get("blockTimestamp") else None,
                "address": lg["address"].lower(),
                "topics": lg["topics"],
                "data": lg["data"],
                "txHash": lg["transactionHash"],
                "logIndex": int(lg["logIndex"], 16),
            }) + "\n")


def main():
    panel = C.load_json("panel.json")
    b_from, b_to = panel["block_from"], panel["block_to"]
    gauges = sorted({r["gauge"].lower() for r in C.ch_rows(
        "SELECT gauge FROM census.aero_gauge_map")})
    print(f"panel blocks {b_from}..{b_to} ({b_to - b_from:,} blocks); cl gauges={len(gauges)}")

    which = sys.argv[1] if len(sys.argv) > 1 else "both"

    if which in ("A", "both"):
        t = time.time()
        print("[A] Voter.DistributeReward over panel ...", flush=True)
        logs, failed = R.scan_logs(
            C.VOTER, [C.TOPIC0["DistributeReward(address,address,uint256)"]],
            b_from, b_to, workers=12, progress=200)
        print(f"[A] {len(logs)} logs, {len(failed)} failed chunks, {time.time()-t:.0f}s")
        if failed:
            C.save_json("failed_chunks_A.json", failed)
        dump(OUTA, logs)

    if which in ("B", "both"):
        t = time.time()
        print("[B] CLGauge.NotifyReward over panel ...", flush=True)
        logs, failed = R.scan_logs(
            gauges, [C.TOPIC0["NotifyReward(address,uint256)"]],
            b_from, b_to, workers=12, progress=200)
        print(f"[B] {len(logs)} logs, {len(failed)} failed chunks, {time.time()-t:.0f}s")
        if failed:
            C.save_json("failed_chunks_B.json", failed)
        dump(OUTB, logs)

    print("rpc stats:", R.STATS)


if __name__ == "__main__":
    main()
