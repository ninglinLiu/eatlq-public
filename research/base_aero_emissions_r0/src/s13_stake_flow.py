"""Independent cross-check on the stake denominator: reconstruct total staked liquidity
from CLGauge Deposit/Withdraw events.

Deposit(address indexed user, uint256 indexed tokenId, uint128 indexed liquidityToStake)
Withdraw(address indexed user, uint256 indexed tokenId, uint128 indexed liquidityToStake)

Running sum(Deposit) - sum(Withdraw) from each gauge's creation gives TOTAL staked
liquidity (in-range and out-of-range). It must dominate the in-range
CLPool.stakedLiquidity() we sampled -- that inequality is the actual test.
"""

import json
import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

R.RATE_LIMIT_PER_SEC = 45.0
OUTF = f"{C.OUT}/logs_stake_flow.jsonl"


def main():
    panel = C.load_json("panel.json")
    rows = C.ch_rows("SELECT gauge, pool, created_block FROM census.aero_gauge_map")
    gauges = sorted(r["gauge"] for r in rows)
    start = min(int(r["created_block"]) for r in rows if int(r["created_block"]) > 0)
    end = panel["block_to"]
    print(f"scanning Deposit/Withdraw on {len(gauges)} gauges, blocks {start}..{end}")

    logs, failed = R.scan_logs(
        gauges,
        [[C.TOPIC0["Deposit(address,uint256,uint128)"],
          C.TOPIC0["Withdraw(address,uint256,uint128)"]]],
        start, end, workers=30, progress=500)
    print(f"logs: {len(logs)}, failed chunks: {len(failed)}")
    if failed:
        C.save_json("failed_chunks_stakeflow.json", failed)

    dep = C.TOPIC0["Deposit(address,uint256,uint128)"]
    out = []
    with open(OUTF, "w") as fh:
        for lg in logs:
            kind = "deposit" if lg["topics"][0] == dep else "withdraw"
            liq = int(lg["topics"][3], 16)
            ts = int(lg["blockTimestamp"], 16)
            rec = (lg["address"].lower(), kind, R.epoch_start(ts),
                   int(lg["blockNumber"], 16), ts, int(lg["topics"][2], 16), liq)
            out.append(rec)
            fh.write(json.dumps(rec) + "\n")

    C.ch("DROP TABLE IF EXISTS census.aero_stake_flow")
    C.ch("""
    CREATE TABLE census.aero_stake_flow (
        gauge String, kind String, epoch_start DateTime, block_number UInt64,
        block_time DateTime, token_id UInt256, liquidity Float64
    ) ENGINE = MergeTree ORDER BY (gauge, block_number)
    """)
    C.ch_insert_tsv("census.aero_stake_flow",
                    ["gauge", "kind", "epoch_start", "block_number", "block_time",
                     "token_id", "liquidity"],
                    [(g, k, e, bn, ts, str(tid), f"{liq:.18g}")
                     for g, k, e, bn, ts, tid, liq in out])
    print(f"inserted {len(out)} stake-flow events")

    # cumulative total staked liquidity per (gauge, epoch)
    C.ch("DROP TABLE IF EXISTS census.aero_stake_cumulative")
    C.ch("""
    CREATE TABLE census.aero_stake_cumulative
    ENGINE = MergeTree ORDER BY (gauge, epoch_start) AS
    SELECT gauge, epoch_start,
           sum(net) OVER (PARTITION BY gauge ORDER BY epoch_start
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS total_staked_liquidity,
           net AS net_flow, n_events
    FROM (
        SELECT gauge, epoch_start,
               sum(if(kind = 'deposit', liquidity, -liquidity)) AS net,
               count() AS n_events
        FROM census.aero_stake_flow GROUP BY gauge, epoch_start
    ) ORDER BY gauge, epoch_start
    """)

    print("\n--- does cumulative Deposit-Withdraw ever go negative? ---")
    print(C.ch("""
    SELECT countIf(total_staked_liquidity < 0) AS negative_rows, count() AS rows,
           round(min(total_staked_liquidity), 2) AS most_negative
    FROM census.aero_stake_cumulative""", fmt="PrettyCompactMonoBlock"))

    print("--- TEST: total staked (flow) must dominate in-range staked (sampled) ---")
    print(C.ch("""
    SELECT count() AS compared,
           countIf(c.total_staked_liquidity + 1 >= s.staked_liquidity) AS consistent,
           countIf(c.total_staked_liquidity + 1 < s.staked_liquidity) AS violations,
           round(quantileExact(0.5)(s.staked_liquidity
                 / nullIf(c.total_staked_liquidity, 0)), 4) AS med_inrange_over_total,
           round(quantileExact(0.9)(s.staked_liquidity
                 / nullIf(c.total_staked_liquidity, 0)), 4) AS p90_inrange_over_total
    FROM census.aero_gauge_stake s
    INNER JOIN census.aero_stake_cumulative c
            ON c.gauge = s.gauge AND c.epoch_start = s.epoch_start
    WHERE c.total_staked_liquidity > 0 AND s.staked_liquidity > 0""",
               fmt="PrettyCompactMonoBlock"))


if __name__ == "__main__":
    main()
