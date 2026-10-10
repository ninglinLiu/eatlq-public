DROP VIEW IF EXISTS eatlq.sender_training_dataset_v1;
DROP VIEW IF EXISTS eatlq.sender_trade_day_rollup_v1;

CREATE VIEW eatlq.sender_trade_day_rollup_v1
AS
SELECT
    chain_id,
    toDate(block_time) AS activity_date,
    sender_address,
    count() AS trade_count,
    countIf(protocol_family = 'uniswap_v2') AS v2_trade_count,
    countIf(protocol_family = 'uniswap_v3') AS v3_trade_count,
    uniqExact(venue_address) AS distinct_venues,
    uniqExact(recipient_address) AS distinct_recipients,
    uniqExact(sold_token_address) AS distinct_sold_assets,
    uniqExact(bought_token_address) AS distinct_bought_assets,
    avg(sold_amount_normalized) AS avg_sold_amount_normalized,
    avg(bought_amount_normalized) AS avg_bought_amount_normalized,
    min(block_time) AS first_trade_at,
    max(block_time) AS latest_trade_at,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.swap_trade_events_all
WHERE isNotNull(block_time)
GROUP BY
    chain_id,
    activity_date,
    sender_address;

CREATE VIEW eatlq.sender_training_dataset_v1
AS
SELECT
    d.chain_id AS chain_id,
    d.activity_date AS activity_date,
    d.sender_address AS sender_address,
    d.trade_count AS trade_count,
    d.v2_trade_count AS v2_trade_count,
    d.v3_trade_count AS v3_trade_count,
    if(
        d.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(d.v2_trade_count) / toFloat64(d.trade_count)
    ) AS v2_trade_share,
    if(
        d.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(d.v3_trade_count) / toFloat64(d.trade_count)
    ) AS v3_trade_share,
    d.distinct_venues AS distinct_venues,
    d.distinct_recipients AS distinct_recipients,
    d.distinct_sold_assets AS distinct_sold_assets,
    d.distinct_bought_assets AS distinct_bought_assets,
    d.avg_sold_amount_normalized AS avg_sold_amount_normalized,
    d.avg_bought_amount_normalized AS avg_bought_amount_normalized,
    ifNull(venue.top_venue_trade_count, 0) AS top_venue_trade_count,
    if(
        d.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / toFloat64(d.trade_count)
    ) AS venue_concentration_ratio,
    ifNull(token.top_token_trade_touch_count, 0) AS top_token_trade_touch_count,
    ifNull(token.total_token_touches, 0) AS total_token_touches,
    if(
        ifNull(token.total_token_touches, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / toFloat64(token.total_token_touches)
    ) AS token_concentration_ratio,
    multiIf(
        d.v2_trade_count > 0 AND d.v3_trade_count = 0, 'v2_only',
        d.v3_trade_count > 0 AND d.v2_trade_count = 0, 'v3_only',
        toFloat64(d.v2_trade_count) / greatest(toFloat64(d.trade_count), 1.0) >= 0.8, 'v2_leaning',
        toFloat64(d.v3_trade_count) / greatest(toFloat64(d.trade_count), 1.0) >= 0.8, 'v3_leaning',
        'mixed'
    ) AS protocol_preference,
    multiIf(
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / greatest(toFloat64(d.trade_count), 1.0) >= 0.9, 'single_venue',
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / greatest(toFloat64(d.trade_count), 1.0) >= 0.6, 'concentrated',
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / greatest(toFloat64(d.trade_count), 1.0) >= 0.35, 'multi_venue',
        'distributed'
    ) AS venue_concentration_band,
    multiIf(
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / greatest(toFloat64(ifNull(token.total_token_touches, 0)), 1.0) >= 0.9, 'single_token',
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / greatest(toFloat64(ifNull(token.total_token_touches, 0)), 1.0) >= 0.6, 'concentrated',
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / greatest(toFloat64(ifNull(token.total_token_touches, 0)), 1.0) >= 0.35, 'multi_token',
        'distributed'
    ) AS token_concentration_band,
    multiIf(
        d.trade_count >= 20, 'hyperactive',
        d.trade_count >= 5, 'active',
        d.trade_count >= 1, 'steady',
        'sporadic'
    ) AS activity_band,
    multiIf(
        d.distinct_recipients >= 20, 'high',
        d.distinct_recipients >= 5, 'medium',
        d.distinct_recipients >= 2, 'low',
        'single'
    ) AS recipient_diversity_band,
    toUInt8(d.v2_trade_count > 0 AND d.v3_trade_count > 0) AS cross_protocol_flag,
    toUInt8(d.distinct_venues > 1) AS multi_venue_flag,
    d.first_trade_at AS first_trade_at,
    d.latest_trade_at AS latest_trade_at,
    d.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_trade_day_rollup_v1 AS d
LEFT JOIN (
    SELECT
        chain_id,
        activity_date,
        sender_address,
        max(trade_count) AS top_venue_trade_count
    FROM (
        SELECT
            chain_id,
            toDate(block_time) AS activity_date,
            sender_address,
            venue_address,
            count() AS trade_count
        FROM eatlq.swap_trade_events_all
        WHERE isNotNull(block_time)
        GROUP BY
            chain_id,
            activity_date,
            sender_address,
            venue_address
    )
    GROUP BY chain_id, activity_date, sender_address
) AS venue
    ON venue.chain_id = d.chain_id
   AND venue.activity_date = d.activity_date
   AND venue.sender_address = d.sender_address
LEFT JOIN (
    SELECT
        chain_id,
        activity_date,
        sender_address,
        max(trade_touch_count) AS top_token_trade_touch_count,
        sum(trade_touch_count) AS total_token_touches
    FROM (
        SELECT
            chain_id,
            activity_date,
            sender_address,
            token_address,
            count() AS trade_touch_count
        FROM (
            SELECT
                chain_id,
                toDate(block_time) AS activity_date,
                sender_address,
                sold_token_address AS token_address
            FROM eatlq.swap_trade_events_all
            WHERE isNotNull(block_time)

            UNION ALL

            SELECT
                chain_id,
                toDate(block_time) AS activity_date,
                sender_address,
                bought_token_address AS token_address
            FROM eatlq.swap_trade_events_all
            WHERE isNotNull(block_time)
        )
        GROUP BY
            chain_id,
            activity_date,
            sender_address,
            token_address
    )
    GROUP BY chain_id, activity_date, sender_address
) AS token
    ON token.chain_id = d.chain_id
   AND token.activity_date = d.activity_date
   AND token.sender_address = d.sender_address;
