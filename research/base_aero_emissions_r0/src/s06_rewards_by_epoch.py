"""Cross-check logged NotifyReward against the gauge's own per-epoch accounting.

CLGauge.rewardsByEpoch(epochStart) is the contract's stored record of emissions credited
to that gauge for that epoch -- i.e. exactly what stakers can earn. It is the ground truth
for LP emissions, and it is NOT the same as Voter.DistributeReward for gauges subject to
emission redistribution.
"""

import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

WAD = 10 ** 18


def main():
    panel = C.load_json("panel.json")
    epochs = [e["epoch_start"] for e in panel["epochs"]]
    gauges = sorted({r["gauge"] for r in C.ch_rows("SELECT gauge FROM census.aero_gauge_map")})
    print(f"{len(gauges)} gauges x {len(epochs)} epochs = {len(gauges)*len(epochs)} eth_calls")

    out = {}
    todo = [(g, e) for g in gauges for e in epochs]

    def work(batch):
        items = [(g, C.SEL["rewardsByEpoch(uint256)"] + R.enc_uint(e)) for g, e in batch]
        res = R.eth_call_batch(items)
        for (g, e), (r, err) in zip(batch, res):
            out[(g, e)] = None if err else (R.dec_uint(r) or 0) / WAD

    batches = [todo[i:i + 20] for i in range(0, len(todo), 20)]
    with ThreadPoolExecutor(max_workers=12) as ex:
        for i, _ in enumerate(ex.map(work, batches)):
            if (i + 1) % 100 == 0:
                print(f"  {(i+1)*20}/{len(todo)}", flush=True)

    failed = [k for k, v in out.items() if v is None]
    print(f"eth_call failures: {len(failed)}")

    C.ch("DROP TABLE IF EXISTS census.aero_rewards_by_epoch")
    C.ch("""
    CREATE TABLE census.aero_rewards_by_epoch (
        gauge String, epoch_start DateTime, aero_amount Float64, source String
    ) ENGINE = MergeTree ORDER BY (gauge, epoch_start)
    """)
    src = "CLGauge.rewardsByEpoch(uint256) eth_call @latest (contract-stored per-epoch credit)"
    n = C.ch_insert_tsv(
        "census.aero_rewards_by_epoch",
        ["gauge", "epoch_start", "aero_amount", "source"],
        [(g, e, f"{v:.18g}", src) for (g, e), v in out.items() if v is not None])
    print(f"inserted {n} rows")

    print("\n--- rewardsByEpoch (state) vs NotifyReward (logs), per (gauge,epoch) ---")
    print(C.ch("""
    SELECT countIf(abs(s.aero_amount - l.aero_amount) <= 1e-9 * greatest(1, s.aero_amount)) AS exact,
           countIf(abs(s.aero_amount - l.aero_amount) > 1e-9 * greatest(1, s.aero_amount)) AS differ,
           countIf(l.aero_amount = 0 AND s.aero_amount > 0) AS state_only,
           count() AS total,
           round(max(abs(s.aero_amount - l.aero_amount)), 8) AS max_abs_diff
    FROM census.aero_rewards_by_epoch s
    LEFT JOIN (SELECT gauge, epoch_start, sum(aero_amount) aero_amount
               FROM census.aero_emissions GROUP BY gauge, epoch_start) l
      ON l.gauge = s.gauge AND l.epoch_start = s.epoch_start"""))

    print("--- rows present in logs but absent/zero in state ---")
    print(C.ch("""
    SELECT count() FROM (SELECT gauge, epoch_start, sum(aero_amount) a
                         FROM census.aero_emissions GROUP BY gauge, epoch_start) l
    LEFT JOIN census.aero_rewards_by_epoch s
      ON l.gauge = s.gauge AND l.epoch_start = s.epoch_start
    WHERE s.gauge = '' AND l.a > 0"""))


if __name__ == "__main__":
    main()
