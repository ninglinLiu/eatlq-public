"""Sanity check (a) input: Aerodrome's published weekly emission from the Minter itself.

Minter.Mint(address indexed _sender, uint256 _weekly, uint256 _circulating_supply,
            bool indexed _tail)  -- emitted once per epoch by updatePeriod().
"""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

WAD = 10 ** 18
MINT_TOPIC0 = "0xcd2127828092f586df56003faf212897e901bb47f89a227e1acd3a0738f1fe5f"


def main():
    panel = C.load_json("panel.json")
    logs, failed = R.scan_logs(C.MINTER, [MINT_TOPIC0],
                               panel["block_from"], panel["block_to"],
                               workers=12, progress=400)
    print(f"Minter Mint logs: {len(logs)}, failed chunks: {len(failed)}")
    assert not failed, failed[:3]

    rows = []
    for lg in logs:
        ts = int(lg["blockTimestamp"], 16)
        weekly = int(lg["data"][2:66], 16) / WAD
        circ = int(lg["data"][66:130], 16) / WAD
        tail = bool(int(lg["topics"][2], 16))
        rows.append((R.epoch_start(ts), weekly, circ, 1 if tail else 0,
                     int(lg["blockNumber"], 16), ts,
                     "Minter.Mint(address,uint256,uint256,bool) via eth_getLogs"))
    rows.sort()

    C.ch("DROP TABLE IF EXISTS census.aero_minter_weekly")
    C.ch("""
    CREATE TABLE census.aero_minter_weekly (
        epoch_start DateTime, weekly_aero Float64, circulating_supply Float64,
        tail UInt8, block_number UInt64, block_time DateTime, source String
    ) ENGINE = MergeTree ORDER BY epoch_start
    """)
    C.ch_insert_tsv("census.aero_minter_weekly",
                    ["epoch_start", "weekly_aero", "circulating_supply", "tail",
                     "block_number", "block_time", "source"],
                    [(e, f"{w:.18g}", f"{c:.18g}", t, bn, ts, s)
                     for e, w, c, t, bn, ts, s in rows])
    print(f"inserted {len(rows)} epochs")
    for e, w, c, t, bn, ts, _ in rows[:3] + rows[-3:]:
        print(f"  {R.iso(e)}  weekly={w:>14,.0f}  circ={c:>16,.0f}  tail={t}  "
              f"minted_at={R.iso(ts)}")


if __name__ == "__main__":
    main()
