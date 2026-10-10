"""Step 3b: decode emission logs into census.aero_emissions and cross-check the two sources."""

import json
import sys
from collections import defaultdict

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

WAD = 10 ** 18


def read_jsonl(path):
    with open(path) as fh:
        for line in fh:
            yield json.loads(line)


def main():
    panel = C.load_json("panel.json")
    gmap = {r["gauge"].lower(): r["pool"].lower()
            for r in C.ch_rows("SELECT pool, gauge FROM census.aero_gauge_map")}
    print(f"cl gauges: {len(gmap)}")

    # ---- source A: Voter.DistributeReward(sender, gauge indexed, amount)
    a_rows, a_all_by_epoch, a_gauges = [], defaultdict(float), set()
    for lg in read_jsonl(f"{C.OUT}/logs_distribute_reward.jsonl"):
        gauge = "0x" + lg["topics"][2][-40:]
        amt = int(lg["data"][:66], 16) / WAD
        ts = lg["blockTimestamp"]
        a_all_by_epoch[R.epoch_start(ts)] += amt
        a_gauges.add(gauge)
        if gauge in gmap:
            a_rows.append((gauge, gmap[gauge], R.epoch_start(ts), amt,
                           lg["blockNumber"], ts, lg["txHash"], lg["logIndex"]))
    print(f"[A] DistributeReward logs: gauges seen={len(a_gauges)}, "
          f"rows matching cl gauges={len(a_rows)}")

    # ---- source B: CLGauge.NotifyReward(from indexed, amount)
    b_rows = []
    b_by_key = defaultdict(float)
    for lg in read_jsonl(f"{C.OUT}/logs_notify_reward.jsonl"):
        gauge = lg["address"]
        amt = int(lg["data"][:66], 16) / WAD
        ts = lg["blockTimestamp"]
        sender = "0x" + lg["topics"][1][-40:]
        b_rows.append((gauge, gmap.get(gauge, ""), R.epoch_start(ts), amt,
                       lg["blockNumber"], ts, lg["txHash"], lg["logIndex"], sender))
        b_by_key[(gauge, R.epoch_start(ts))] += amt
    senders = defaultdict(int)
    for r in b_rows:
        senders[r[8]] += 1
    print(f"[B] NotifyReward logs on cl gauges: {len(b_rows)}")
    print(f"[B] notifier addresses: {dict(sorted(senders.items(), key=lambda kv: -kv[1])[:5])}")
    print(f"    (Voter = {C.VOTER.lower()})")

    # ---- A vs B reconciliation at (gauge, epoch) level
    a_by_key = defaultdict(float)
    for g, p, e, amt, *_ in a_rows:
        a_by_key[(g, e)] += amt
    keys = set(a_by_key) | set(b_by_key)
    exact, diffs, only_a, only_b = 0, [], 0, 0
    for k in keys:
        av, bv = a_by_key.get(k), b_by_key.get(k)
        if av is None:
            only_b += 1
        elif bv is None:
            only_a += 1
        elif abs(av - bv) <= 1e-9 * max(1.0, av):
            exact += 1
        else:
            diffs.append((k, av, bv))
    print(f"[A vs B] keys={len(keys)} exact_match={exact} mismatch={len(diffs)} "
          f"only_A={only_a} only_B={only_b}")
    for d in sorted(diffs, key=lambda x: -abs(x[1] - x[2]))[:5]:
        print("   mismatch:", d)

    # ---- write census.aero_emissions from source B (gauge-side, authoritative event)
    C.ch("DROP TABLE IF EXISTS census.aero_emissions")
    C.ch("""
    CREATE TABLE census.aero_emissions (
        gauge String,
        pool String,
        epoch_start DateTime,
        aero_amount Float64,
        block_number UInt64,
        block_time DateTime,
        tx_hash String,
        log_index UInt32,
        notifier String,
        source String
    ) ENGINE = MergeTree ORDER BY (gauge, epoch_start, block_number, log_index)
    """)
    src = ("eth_getLogs NotifyReward(address,uint256) on CL gauge addresses, "
           "topic0=0x0956677529..., archive RPC, full panel, 0 failed chunks")
    n = C.ch_insert_tsv(
        "census.aero_emissions",
        ["gauge", "pool", "epoch_start", "aero_amount", "block_number", "block_time",
         "tx_hash", "log_index", "notifier", "source"],
        [(g, p, e, f"{amt:.18g}", bn, ts, tx, li, snd, src)
         for g, p, e, amt, bn, ts, tx, li, snd in b_rows])
    print(f"inserted {n} rows into census.aero_emissions")

    # ---- protocol-wide weekly totals (all gauges, for sanity check (a))
    C.ch("DROP TABLE IF EXISTS census.aero_emissions_protocol_weekly")
    C.ch("""
    CREATE TABLE census.aero_emissions_protocol_weekly (
        epoch_start DateTime, aero_total Float64, n_gauges UInt32, source String
    ) ENGINE = MergeTree ORDER BY epoch_start
    """)
    per_epoch_gauges = defaultdict(set)
    for lg in read_jsonl(f"{C.OUT}/logs_distribute_reward.jsonl"):
        per_epoch_gauges[R.epoch_start(lg["blockTimestamp"])].add(
            "0x" + lg["topics"][2][-40:])
    C.ch_insert_tsv(
        "census.aero_emissions_protocol_weekly",
        ["epoch_start", "aero_total", "n_gauges", "source"],
        [(e, f"{v:.18g}", len(per_epoch_gauges[e]),
          "Voter.DistributeReward over all gauges (eth_getLogs, full panel)")
         for e, v in sorted(a_all_by_epoch.items())])
    print(f"protocol weekly epochs: {len(a_all_by_epoch)}")

    C.save_json("emissions_recon.json", {
        "source_A_rows_all_gauges": sum(1 for _ in read_jsonl(f"{C.OUT}/logs_distribute_reward.jsonl")),
        "source_A_distinct_gauges": len(a_gauges),
        "source_A_rows_cl_gauges": len(a_rows),
        "source_B_rows": len(b_rows),
        "keys_compared": len(keys), "exact_match": exact,
        "mismatch": len(diffs), "only_A": only_a, "only_B": only_b,
        "notifiers": dict(senders),
    })


if __name__ == "__main__":
    main()
