"""Sanity checks (a)(b)(c) and the emission-APR distribution."""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C


def show(title, sql):
    print(f"\n=== {title} ===")
    print(C.ch(sql, fmt="PrettyCompactMonoBlock"))


def main():
    # ---------------------------------------------------------------- (c) epochs
    show("(c) epoch boundaries land on Thursday 00:00 UTC",
         """
    SELECT src, count() AS n_epochs,
           groupUniqArray(toDayOfWeek(epoch_start)) AS dow_iso,
           groupUniqArray(toString(toTime(epoch_start))) AS time_of_day
    FROM (
        SELECT 'aero_emissions'        AS src, epoch_start FROM census.aero_emissions
        UNION ALL SELECT 'aero_gauge_stake', epoch_start FROM census.aero_gauge_stake
        UNION ALL SELECT 'aero_minter_weekly', epoch_start FROM census.aero_minter_weekly
        UNION ALL SELECT 'aero_rewards_by_epoch', epoch_start FROM census.aero_rewards_by_epoch
    ) GROUP BY src ORDER BY src""")

    show("(c) observed NotifyReward timing relative to its epoch start",
         """
    SELECT round(quantileExact(0.5)(block_time - epoch_start)) AS med_sec_after_epoch_open,
           round(quantileExact(0.99)(block_time - epoch_start)) AS p99_sec,
           max(block_time - epoch_start) AS max_sec,
           countIf(block_time < epoch_start) AS before_epoch_start,
           countIf(block_time - epoch_start > 604800) AS after_epoch_end
    FROM census.aero_emissions""")

    # ---------------------------------------------------------------- (a) minter
    show("(a) protocol-wide distributed AERO vs Minter weekly schedule",
         """
    SELECT count() AS epochs_compared,
           round(sum(p.aero_total)) AS total_distributed,
           round(sum(m.weekly_aero)) AS total_minted_for_gauges,
           round(sum(p.aero_total) / sum(m.weekly_aero), 5) AS aggregate_ratio,
           round(min(p.aero_total / m.weekly_aero), 5) AS min_ratio,
           round(quantileExact(0.5)(p.aero_total / m.weekly_aero), 5) AS median_ratio,
           round(max(p.aero_total / m.weekly_aero), 5) AS max_ratio
    FROM census.aero_minter_weekly m
    INNER JOIN census.aero_emissions_protocol_weekly p ON p.epoch_start = m.epoch_start""")

    show("(a) our 157 CL gauges as a share of all gauge emissions",
         """
    SELECT toDate(e.epoch_start) AS epoch,
           round(sum(e.aero_amount)) AS cl_157_aero,
           round(any(p.aero_total)) AS all_gauges_aero,
           round(any(m.weekly_aero)) AS minter_weekly,
           round(sum(e.aero_amount) / any(p.aero_total), 4) AS share_of_all_gauges
    FROM census.aero_emissions e
    LEFT JOIN census.aero_emissions_protocol_weekly p ON p.epoch_start = e.epoch_start
    LEFT JOIN census.aero_minter_weekly m ON m.epoch_start = e.epoch_start
    GROUP BY epoch ORDER BY epoch""")

    # ---------------------------------------------------------------- (b) APR
    C.ch("DROP TABLE IF EXISTS census.aero_emission_apr")
    C.ch("""
    CREATE TABLE census.aero_emission_apr
    ENGINE = MergeTree ORDER BY (pool, epoch_start) AS
    SELECT e.pool AS pool, e.gauge AS gauge, e.epoch_start AS epoch_start,
           e.aero_amount AS aero_amount,
           c.aero_usd AS aero_usd,
           e.aero_amount * c.aero_usd AS emission_usd,
           s.staked_liquidity AS staked_liquidity,
           s.staked_liquidity_usd_ub AS staked_liquidity_usd_ub,
           s.confidence AS stake_confidence,
           s.staked_share AS staked_share,
           e.aero_amount * c.aero_usd / s.staked_liquidity_usd_ub AS weekly_return_lb,
           52.0 * e.aero_amount * c.aero_usd / s.staked_liquidity_usd_ub AS apr_lb
    FROM (SELECT gauge, pool, epoch_start, sum(aero_amount) AS aero_amount
          FROM census.aero_emissions GROUP BY gauge, pool, epoch_start) e
    INNER JOIN census.aero_gauge_stake s
            ON s.gauge = e.gauge AND s.epoch_start = e.epoch_start
    INNER JOIN census.aero_pool_epoch_ctx c
            ON c.pool = e.pool AND c.epoch_start = e.epoch_start
    WHERE s.staked_liquidity_usd_ub > 0 AND c.aero_usd > 0
    """)

    show("(b) emission APR lower bound -- distribution over (pool, epoch)",
         """
    SELECT count() AS n_pool_epochs, uniqExact(pool) AS pools, uniqExact(epoch_start) AS epochs,
           round(quantileExact(0.10)(apr_lb) * 100, 1) AS p10_pct,
           round(quantileExact(0.50)(apr_lb) * 100, 1) AS median_pct,
           round(quantileExact(0.90)(apr_lb) * 100, 1) AS p90_pct,
           round(quantileExact(0.99)(apr_lb) * 100, 1) AS p99_pct,
           round(max(apr_lb) * 100, 1) AS max_pct,
           countIf(apr_lb > 10.0) AS n_above_1000pct
    FROM census.aero_emission_apr""")

    show("(b) same, restricted to high-confidence stake estimates",
         """
    SELECT count() AS n_pool_epochs,
           round(quantileExact(0.10)(apr_lb) * 100, 1) AS p10_pct,
           round(quantileExact(0.50)(apr_lb) * 100, 1) AS median_pct,
           round(quantileExact(0.90)(apr_lb) * 100, 1) AS p90_pct,
           countIf(apr_lb > 10.0) AS n_above_1000pct
    FROM census.aero_emission_apr WHERE stake_confidence = 'high'""")

    show("(b) top 10 pools by mean APR lower bound (>=4 epochs)",
         """
    SELECT a.pool AS pool, u.symbol AS alt_symbol, u.fee AS fee,
           count() AS epochs,
           round(avg(a.apr_lb) * 100, 1) AS mean_apr_lb_pct,
           round(quantileExact(0.5)(a.apr_lb) * 100, 1) AS med_apr_lb_pct,
           round(sum(a.emission_usd)) AS total_emission_usd,
           round(avg(a.staked_liquidity_usd_ub)) AS mean_staked_usd_ub,
           topK(1)(a.stake_confidence)[1] AS modal_confidence
    FROM census.aero_emission_apr a
    LEFT JOIN edge.bars_universe_50 u ON u.pool = a.pool
    GROUP BY pool, alt_symbol, fee HAVING epochs >= 4
    ORDER BY mean_apr_lb_pct DESC LIMIT 10""")

    show("(b) top 10 single (pool, epoch) APR lower bounds",
         """
    SELECT a.pool, u.symbol AS alt, toDate(a.epoch_start) AS epoch,
           round(a.apr_lb * 100, 1) AS apr_lb_pct,
           round(a.aero_amount) AS aero, round(a.aero_usd, 4) AS aero_usd,
           round(a.emission_usd) AS emission_usd,
           round(a.staked_liquidity_usd_ub) AS staked_usd_ub,
           a.stake_confidence, round(a.staked_share, 4) AS staked_share
    FROM census.aero_emission_apr a
    LEFT JOIN edge.bars_universe_50 u ON u.pool = a.pool
    ORDER BY apr_lb_pct DESC LIMIT 10""")

    show("emission coverage: pool-epochs with emissions but no usable stake denominator",
         """
    SELECT countIf(s.gauge = '') AS no_stake_row,
           countIf(s.gauge != '' AND s.staked_liquidity_usd_ub = 0) AS stake_row_no_usd,
           countIf(s.staked_liquidity_usd_ub > 0) AS usable,
           count() AS total
    FROM (SELECT gauge, pool, epoch_start FROM census.aero_emissions
          GROUP BY gauge, pool, epoch_start) e
    LEFT JOIN census.aero_gauge_stake s
           ON s.gauge = e.gauge AND s.epoch_start = e.epoch_start""")


if __name__ == "__main__":
    main()
