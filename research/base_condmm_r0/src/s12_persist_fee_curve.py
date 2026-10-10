#!/usr/bin/env python3
"""Persist the true per-day fee curve to census.pool_fee_daily so it becomes the
canonical fee instrument, replacing the static field for every downstream round.

census.pool_fee_verified already existed from round 41 and already contained the
smoking gun (0x70acdf: snapshot 2500 vs implied 4.4 bps), but it holds only 2 window
starts, so it can flag a pool as suspect without letting a round price fee revenue
correctly day by day. This table is daily and covers the whole panel window, so a round
can join it directly instead of merely being warned.

univ3 fees are immutable and the s06 audit found 0/209 disagreements, so univ3 pools are
inserted at their static field and carry source='static_immutable'.
"""

import json
import subprocess
from pathlib import Path

OUT = Path("/root/base_condmm_r0/out")

DDL = """
CREATE TABLE IF NOT EXISTS census.pool_fee_daily
(
    pool       LowCardinality(String),
    day        Date,
    venue      LowCardinality(String),
    fee_ppm    UInt32,
    fee_bps    Float64,
    fee_static UInt32,
    source     LowCardinality(String)
)
ENGINE = ReplacingMergeTree
ORDER BY (pool, day)
"""


def ch(q, stdin=None, timeout=900):
    r = subprocess.run(["docker", "exec", "-i", "eatlq-clickhouse-1",
                        "clickhouse-client", "--query", q],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr[:3000])
    return r.stdout


def main():
    fc = json.loads((OUT / "fee_curve.json").read_text())
    curve = fc["curve"]
    uni = [json.loads(l) for l in open(OUT / "universe50.jsonl") if l.strip()]
    static = {p["pool"]: int(p["fee"]) for p in uni}
    venue = {p["pool"]: p["venue"] for p in uni}

    days = sorted(next(iter(curve.values())).keys())
    print(f"days {days[0]} .. {days[-1]}  ({len(days)})")

    ch(DDL)
    print("table ready")

    lines = []
    n_measured = n_static = n_gap = 0
    for pl, ser in curve.items():
        for d in days:
            v = ser.get(d)
            if v is None:
                n_gap += 1
                continue
            lines.append(f"{pl}\t{d}\t{venue.get(pl, 'unknown')}\t{v}\t"
                         f"{v / 100.0}\t{static.get(pl, 0)}\tonchain_fee_call")
            n_measured += 1
    for p in uni:
        if p["venue"] != "univ3":
            continue
        f = int(p["fee"])
        for d in days:
            lines.append(f"{p['pool']}\t{d}\tuniv3\t{f}\t{f / 100.0}\t{f}\t"
                         f"static_immutable")
            n_static += 1

    print(f"rows: measured={n_measured}  univ3_static={n_static}  gaps_skipped={n_gap}")
    ch("INSERT INTO census.pool_fee_daily FORMAT TabSeparated", stdin="\n".join(lines))
    ch("OPTIMIZE TABLE census.pool_fee_daily FINAL")

    print("\n" + ch("""
SELECT source, count() AS rows, uniqExact(pool) AS pools,
       round(avg(fee_bps),3) AS avg_bps
FROM census.pool_fee_daily GROUP BY source ORDER BY source FORMAT PrettyCompact"""))

    print("worst static-vs-measured disagreements, volume weighted:")
    print(ch("""
SELECT f.pool, any(f.venue) AS venue, any(u.symbol) AS symbol,
       any(f.fee_static) AS static_ppm,
       round(sum(f.fee_ppm * b.v) / sum(b.v), 1) AS vw_true_ppm,
       round(any(f.fee_static) / (sum(f.fee_ppm * b.v) / sum(b.v)), 3) AS static_over_true,
       round(sum(b.v), 0) AS weth_vol
FROM census.pool_fee_daily f
INNER JOIN (
    SELECT pool, toDate(bar) AS day, sum(toFloat64(vol_weth)) AS v
    FROM edge.bars_1h_year_50 GROUP BY pool, day
) b ON b.pool = f.pool AND b.day = f.day
INNER JOIN edge.bars_universe_50 u ON u.pool = f.pool
WHERE f.source = 'onchain_fee_call'
GROUP BY f.pool
HAVING weth_vol > 1000 AND (static_over_true > 1.5 OR static_over_true < 0.667)
ORDER BY weth_vol DESC LIMIT 25 FORMAT PrettyCompact"""))

    print("cross-check against round 41's census.pool_fee_verified (independent method):")
    print(ch("""
SELECT v.pool, any(u.symbol) AS symbol, v.window_start, v.n_obs,
       v.fee_snapshot, round(v.implied_bps, 2) AS implied_reverse_solved,
       round(avg(d.fee_bps), 2) AS onchain_this_round,
       round(avg(d.fee_bps) / v.implied_bps, 3) AS agreement,
       round(v.fee_snapshot / 100.0 / avg(d.fee_bps), 2) AS static_over_true
FROM census.pool_fee_verified v
INNER JOIN census.pool_fee_daily d ON d.pool = v.pool
    AND d.day >= v.window_start AND d.day < v.window_start + 30
INNER JOIN edge.bars_universe_50 u ON u.pool = v.pool
WHERE d.source = 'onchain_fee_call' AND v.n_obs > 20000 AND v.implied_bps > 0.2
GROUP BY v.pool, v.window_start, v.fee_snapshot, v.implied_bps, v.n_obs
ORDER BY v.n_obs DESC LIMIT 25 FORMAT PrettyCompact"""))

    print("agreement between the two independent methods, distribution:")
    print(ch("""
SELECT count() AS pairs,
       round(quantile(0.10)(r), 3) AS p10,
       round(quantile(0.50)(r), 3) AS median,
       round(quantile(0.90)(r), 3) AS p90,
       countIf(r BETWEEN 0.5 AND 2.0) AS within_2x
FROM (
    SELECT avg(d.fee_bps) / v.implied_bps AS r
    FROM census.pool_fee_verified v
    INNER JOIN census.pool_fee_daily d ON d.pool = v.pool
        AND d.day >= v.window_start AND d.day < v.window_start + 30
    WHERE d.source = 'onchain_fee_call' AND v.n_obs > 20000 AND v.implied_bps > 0.2
    GROUP BY v.pool, v.window_start, v.implied_bps
) FORMAT PrettyCompact"""))


if __name__ == "__main__":
    main()
