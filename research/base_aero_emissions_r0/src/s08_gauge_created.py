"""Independent verification of the gauge map from Voter.GaugeCreated logs.

GaugeCreated(address indexed poolFactory, address indexed votingRewardsFactory,
             address indexed gaugeFactory, address pool, address bribeVotingReward,
             address feesVotingReward, address gauge)
pool and gauge are in data (words 0 and 3).
"""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R


def deploy_block(addr, hi):
    lo = 1
    if R.rpc("eth_getCode", [addr, hex(lo)]) not in ("0x", "0x0"):
        return lo
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if R.rpc("eth_getCode", [addr, hex(mid)]) in ("0x", "0x0"):
            lo = mid
        else:
            hi = mid
    return hi


def main():
    panel = C.load_json("panel.json")
    head = panel["head"]
    vb = deploy_block(C.VOTER, head)
    print(f"Voter first block with code: {vb} ({R.iso(R.block_timestamp(vb))})")

    logs, failed = R.scan_logs(
        C.VOTER,
        [C.TOPIC0["GaugeCreated(address,address,address,address,address,address,address,address)"]],
        vb, head, workers=12, progress=1000)
    print(f"GaugeCreated logs: {len(logs)}, failed chunks: {len(failed)}")
    if failed:
        C.save_json("failed_chunks_gaugecreated.json", failed)

    created = {}
    for lg in logs:
        d = lg["data"][2:]
        pool = "0x" + d[0:64][-40:]
        gauge = "0x" + d[3 * 64:4 * 64][-40:]
        gf = "0x" + lg["topics"][3][-40:]
        pf = "0x" + lg["topics"][1][-40:]
        created[pool] = {
            "gauge": gauge, "gauge_factory": gf, "pool_factory": pf,
            "block": int(lg["blockNumber"], 16),
            "ts": int(lg["blockTimestamp"], 16) if lg.get("blockTimestamp") else None,
        }
    C.save_json("gauge_created.json", created)
    print(f"distinct pools with a gauge ever created: {len(created)}")

    rows = C.ch_rows("SELECT pool, gauge, gauge_factory FROM census.aero_gauge_map")
    agree = disagree = missing = 0
    for r in rows:
        c = created.get(r["pool"].lower())
        if not c:
            missing += 1
        elif c["gauge"] == r["gauge"].lower() and c["gauge_factory"] == r["gauge_factory"].lower():
            agree += 1
        else:
            disagree += 1
            print("  DISAGREE", r["pool"], r["gauge"], c)
    print(f"gauge map vs GaugeCreated logs: agree={agree} disagree={disagree} "
          f"not_in_logs={missing}")

    # the 18 pools with no gauge: confirm they never appear in GaugeCreated either
    probs = C.load_json("gauge_map_problems.json")
    zero = [p["pool"] for p in probs if "address(0)" in p["reason"]]
    found = [p for p in zero if p in created]
    print(f"pools with Voter.gauges()==0: {len(zero)}; of those present in "
          f"GaugeCreated logs: {len(found)} {found}")

    # persist creation metadata, then fold it into the gauge map
    C.ch("DROP TABLE IF EXISTS census.aero_gauge_created")
    C.ch("""
    CREATE TABLE census.aero_gauge_created (
        pool String, gauge String, gauge_factory String, pool_factory String,
        created_block UInt64, created_time DateTime
    ) ENGINE = MergeTree ORDER BY pool
    """)
    C.ch_insert_tsv("census.aero_gauge_created",
                    ["pool", "gauge", "gauge_factory", "pool_factory",
                     "created_block", "created_time"],
                    [(p, c["gauge"], c["gauge_factory"], c["pool_factory"],
                      c["block"], c["ts"]) for p, c in created.items()])

    C.ch("DROP TABLE IF EXISTS census.aero_gauge_map_v2")
    C.ch("""
    CREATE TABLE census.aero_gauge_map_v2
    ENGINE = MergeTree ORDER BY pool AS
    SELECT m.*, c.created_block AS created_block, c.created_time AS created_time,
           if(c.gauge = m.gauge, 'Voter.gauges(pool) eth_call + Voter.GaugeCreated log',
              m.resolved_via) AS resolved_via_final
    FROM census.aero_gauge_map m
    LEFT JOIN census.aero_gauge_created c ON c.pool = m.pool
    """)
    C.ch("DROP TABLE census.aero_gauge_map")
    C.ch("RENAME TABLE census.aero_gauge_map_v2 TO census.aero_gauge_map")
    print(C.ch("SELECT count() n, min(created_block) minb, max(created_block) maxb, "
               "min(created_time) mint, max(created_time) maxt, countIf(created_block=0) missing "
               "FROM census.aero_gauge_map"))


if __name__ == "__main__":
    main()
