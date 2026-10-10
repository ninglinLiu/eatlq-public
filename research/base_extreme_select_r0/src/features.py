"""Build the causal feature table and the +200% label for the spike classifier.

Every window ends at the current bar. Nothing is standardised with full-sample
statistics. The label looks 24 bars forward and is deliberately an upper bound
(perfect-foresight maximum close), as recorded in GOAL.md section 2.
"""
import io
import subprocess

import pandas as pd

OUT = "/root/base_extreme_select_r0/out"

SQL = r"""
WITH b AS (
    SELECT s.pool AS pool, s.bar AS bar, s.n_swaps AS n_swaps, s.vol_weth AS vol_weth,
           s.open AS open, s.high AS high, s.low AS low, s.close AS close, s.vwap AS vwap,
           u.venue AS venue, u.fee AS fee, u.cost_p50 AS cost_p50,
           u.tradeable_days AS tradeable_days
    FROM edge.bars_1h_year_50 s
    INNER JOIN edge.bars_universe_50 u ON u.pool = s.pool
    WHERE s.close > 0 AND s.open > 0 AND s.high > 0 AND s.low > 0
),
f AS (
    SELECT b.pool AS pool, b.bar AS bar, b.n_swaps AS n_swaps, b.vol_weth AS vol_weth,
           b.high AS high, b.low AS low, b.close AS close, b.vwap AS vwap,
           b.venue AS venue, b.fee AS fee, b.cost_p50 AS cost_p50,
           b.tradeable_days AS tradeable_days,
           ifNull(fl.net_weth, 0) AS net_weth, ifNull(fl.gross_weth, 0) AS gross_weth,
           ifNull(fl.ofi, 0) AS ofi, ifNull(e.eth_usd, 0) AS eth_usd
    FROM b
    LEFT JOIN edge.flow_1h_year_50 fl ON fl.pool = b.pool AND fl.bar = b.bar
    LEFT JOIN edge.eth_usd_1h e ON e.bar = b.bar
),
g AS (
    SELECT pool, bar, n_swaps, vol_weth, high, low, close, vwap, venue, fee, cost_p50,
           tradeable_days, net_weth, gross_weth, ofi, eth_usd, log(close) AS lc
    FROM f
),
h AS (
    SELECT pool, bar, n_swaps, vol_weth, high, low, close, vwap, venue, fee, cost_p50,
           tradeable_days, net_weth, gross_weth, ofi, eth_usd, lc,
           any(lc) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) AS lc1,
           any(lc) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 4 PRECEDING AND 4 PRECEDING) AS lc4,
           any(lc) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND 24 PRECEDING) AS lc24,
           any(lc) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 168 PRECEDING AND 168 PRECEDING) AS lc168,
           row_number() OVER (PARTITION BY pool ORDER BY bar) AS rn
    FROM g
),
k AS (
    SELECT pool, bar, n_swaps, vol_weth, high, low, close, vwap, venue, fee, cost_p50,
           tradeable_days, net_weth, gross_weth, ofi, eth_usd,
           lc, lc1, lc4, lc24, lc168, rn,
           -- the first bar of a pool has no predecessor; a zero-filled lc1 there would
           -- inject log(close) as a return and poison every trailing vol window after it
           if(rn = 1, 0, lc - lc1) AS ret1
    FROM h
),
w AS (
    SELECT pool, bar, close, cost_p50, venue, fee, tradeable_days, high, low, vwap,
           n_swaps, vol_weth, net_weth, gross_weth, ofi, eth_usd,
           lc, lc1, lc4, lc24, lc168, rn,
           stddevPop(ret1) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS rv4,
           stddevPop(ret1) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS rv24,
           stddevPop(ret1) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 168 PRECEDING AND CURRENT ROW) AS rv168,
           avg(vol_weth) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS v24,
           avg(vol_weth) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 168 PRECEDING AND CURRENT ROW) AS v168,
           avg(n_swaps) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS s24,
           avg(n_swaps) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 168 PRECEDING AND CURRENT ROW) AS s168,
           max(high) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 168 PRECEDING AND CURRENT ROW) AS hh168,
           min(low) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 168 PRECEDING AND CURRENT ROW) AS ll168,
           sum(net_weth) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS nf4,
           sum(net_weth) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS nf24,
           sum(gross_weth) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS gf24,
           avg(ofi) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS ofi24,
           min(bar) OVER (PARTITION BY pool) AS first_bar,
           count() OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS n_hist,
           -- forward label window, the only place the future is touched
           max(close) OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 1 FOLLOWING AND 24 FOLLOWING) AS fmax24,
           count() OVER (PARTITION BY pool ORDER BY bar ROWS BETWEEN 1 FOLLOWING AND 24 FOLLOWING) AS n_fwd
    FROM k
)
SELECT pool, bar, venue, fee, cost_p50, tradeable_days,
       toFloat32(lc - lc1) AS r1, toFloat32(lc - lc4) AS r4,
       toFloat32(lc - lc24) AS r24, toFloat32(lc - lc168) AS r168,
       toFloat32(rv4) AS rv4, toFloat32(rv24) AS rv24, toFloat32(rv168) AS rv168,
       toFloat32(if(v24 > 0, vol_weth / v24, 0)) AS vr24,
       toFloat32(if(v168 > 0, vol_weth / v168, 0)) AS vr168,
       toFloat32(if(v168 > 0, v24 / v168, 0)) AS vr24_168,
       toFloat32(if(s24 > 0, n_swaps / s24, 0)) AS sr24,
       toFloat32(if(s168 > 0, s24 / s168, 0)) AS sr24_168,
       toFloat32(log(v24 + 1e-12)) AS lv24,
       toFloat32(log(n_swaps + 1)) AS lns,
       toFloat32(if(hh168 > 0, log(close / hh168), 0)) AS pos_hi,
       toFloat32(if(ll168 > 0, log(close / ll168), 0)) AS pos_lo,
       toFloat32(if(close > 0, (high - low) / close, 0)) AS rng,
       toFloat32(if(close > 0, (close - vwap) / close, 0)) AS vwap_gap,
       toFloat32(if(gf24 > 0, nf24 / gf24, 0)) AS nfr24,
       toFloat32(if(v24 > 0, nf4 / v24, 0)) AS nfr4,
       toFloat32(ofi) AS ofi, toFloat32(ofi24) AS ofi24,
       toFloat32(dateDiff('hour', first_bar, bar)) AS age_h,
       toFloat32(n_hist) AS n_hist,
       toFloat32(if(fmax24 > 0 AND close > 0,
                    1e4 * (fmax24 / close - 1) - 2 * cost_p50 - 9.7, -1e9)) AS net_up_bps
FROM w
WHERE n_fwd = 24 AND n_hist > 168 AND isFinite(r168) AND isFinite(rv168)
"""


def main():
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_memory_usage=40000000000", "--max_bytes_before_external_group_by=8000000000",
         "--query", SQL + " FORMAT Parquet"],
        capture_output=True, timeout=7200)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-4000:])
    df = pd.read_parquet(io.BytesIO(r.stdout))
    df.to_parquet(f"{OUT}/features.parquet", index=False)
    print(f"rows {len(df):,}  cols {len(df.columns)}")
    for thr, name in ((20000, "+200%"), (5000, "+50%"), (1000, "+10%")):
        y = (df["net_up_bps"] >= thr)
        print(f"  {name:>6}: {y.sum():,} positives, base rate {100 * y.mean():.4f}%")


if __name__ == "__main__":
    main()
