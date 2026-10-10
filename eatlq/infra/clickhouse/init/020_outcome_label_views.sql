DROP VIEW IF EXISTS eatlq.sender_training_examples_v1;
DROP VIEW IF EXISTS eatlq.sender_forward_outcome_labels_v1;

CREATE VIEW eatlq.sender_forward_outcome_labels_v1
AS
SELECT
    base.chain_id AS chain_id,
    base.activity_date AS activity_date,
    base.sender_address AS sender_address,
    ifNull(next_day.trade_count, 0) AS next_day_trade_count,
    ifNull(next_day.v2_trade_count, 0) AS next_day_v2_trade_count,
    ifNull(next_day.v3_trade_count, 0) AS next_day_v3_trade_count,
    if(
        ifNull(next_day.trade_count, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(next_day.v2_trade_count) / toFloat64(next_day.trade_count)
    ) AS next_day_v2_trade_share,
    if(
        ifNull(next_day.trade_count, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(next_day.v3_trade_count) / toFloat64(next_day.trade_count)
    ) AS next_day_v3_trade_share,
    ifNull(next_day.distinct_venues, 0) AS next_day_distinct_venues,
    ifNull(next_day.distinct_recipients, 0) AS next_day_distinct_recipients,
    ifNull(next_day.avg_sold_amount_normalized, CAST(0, 'Float64')) AS next_day_avg_sold_amount_normalized,
    ifNull(next_day.avg_bought_amount_normalized, CAST(0, 'Float64')) AS next_day_avg_bought_amount_normalized,
    toUInt8(ifNull(next_day.trade_count, 0) > 0) AS next_day_active_flag,
    toUInt8(ifNull(next_day.trade_count, 0) > base.trade_count) AS next_day_higher_trade_count_flag,
    toUInt8(ifNull(next_day.v2_trade_count, 0) > 0 AND ifNull(next_day.v3_trade_count, 0) > 0) AS next_day_cross_protocol_flag,
    multiIf(
        ifNull(next_day.v2_trade_count, 0) > 0 AND ifNull(next_day.v3_trade_count, 0) = 0, 'v2_only',
        ifNull(next_day.v3_trade_count, 0) > 0 AND ifNull(next_day.v2_trade_count, 0) = 0, 'v3_only',
        (
            if(ifNull(next_day.trade_count, 0) = 0, CAST(0, 'Float64'), toFloat64(next_day.v2_trade_count) / toFloat64(next_day.trade_count))
        ) >= 0.8, 'v2_leaning',
        (
            if(ifNull(next_day.trade_count, 0) = 0, CAST(0, 'Float64'), toFloat64(next_day.v3_trade_count) / toFloat64(next_day.trade_count))
        ) >= 0.8, 'v3_leaning',
        'mixed'
    ) AS next_day_protocol_preference,
    base.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_trade_day_rollup_v1 AS base
LEFT JOIN eatlq.sender_trade_day_rollup_v1 AS next_day
    ON next_day.chain_id = base.chain_id
   AND next_day.sender_address = base.sender_address
   AND next_day.activity_date = addDays(base.activity_date, 1);

CREATE VIEW eatlq.sender_training_examples_v1
AS
SELECT
    features.chain_id AS chain_id,
    features.activity_date AS activity_date,
    features.sender_address AS sender_address,
    features.trade_count AS trade_count,
    features.v2_trade_count AS v2_trade_count,
    features.v3_trade_count AS v3_trade_count,
    features.v2_trade_share AS v2_trade_share,
    features.v3_trade_share AS v3_trade_share,
    features.distinct_venues AS distinct_venues,
    features.distinct_recipients AS distinct_recipients,
    features.distinct_sold_assets AS distinct_sold_assets,
    features.distinct_bought_assets AS distinct_bought_assets,
    features.avg_sold_amount_normalized AS avg_sold_amount_normalized,
    features.avg_bought_amount_normalized AS avg_bought_amount_normalized,
    features.top_venue_trade_count AS top_venue_trade_count,
    features.venue_concentration_ratio AS venue_concentration_ratio,
    features.top_token_trade_touch_count AS top_token_trade_touch_count,
    features.total_token_touches AS total_token_touches,
    features.token_concentration_ratio AS token_concentration_ratio,
    features.protocol_preference AS protocol_preference,
    features.venue_concentration_band AS venue_concentration_band,
    features.token_concentration_band AS token_concentration_band,
    features.activity_band AS activity_band,
    features.recipient_diversity_band AS recipient_diversity_band,
    features.cross_protocol_flag AS cross_protocol_flag,
    features.multi_venue_flag AS multi_venue_flag,
    ifNull(next_day.trade_count, 0) AS next_day_trade_count,
    ifNull(next_day.v2_trade_count, 0) AS next_day_v2_trade_count,
    ifNull(next_day.v3_trade_count, 0) AS next_day_v3_trade_count,
    if(
        ifNull(next_day.trade_count, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(next_day.v2_trade_count) / toFloat64(next_day.trade_count)
    ) AS next_day_v2_trade_share,
    if(
        ifNull(next_day.trade_count, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(next_day.v3_trade_count) / toFloat64(next_day.trade_count)
    ) AS next_day_v3_trade_share,
    ifNull(next_day.distinct_venues, 0) AS next_day_distinct_venues,
    ifNull(next_day.distinct_recipients, 0) AS next_day_distinct_recipients,
    ifNull(next_day.avg_sold_amount_normalized, CAST(0, 'Float64')) AS next_day_avg_sold_amount_normalized,
    ifNull(next_day.avg_bought_amount_normalized, CAST(0, 'Float64')) AS next_day_avg_bought_amount_normalized,
    toUInt8(ifNull(next_day.trade_count, 0) > 0) AS next_day_active_flag,
    toUInt8(ifNull(next_day.trade_count, 0) > features.trade_count) AS next_day_higher_trade_count_flag,
    toUInt8(ifNull(next_day.v2_trade_count, 0) > 0 AND ifNull(next_day.v3_trade_count, 0) > 0) AS next_day_cross_protocol_flag,
    multiIf(
        ifNull(next_day.v2_trade_count, 0) > 0 AND ifNull(next_day.v3_trade_count, 0) = 0, 'v2_only',
        ifNull(next_day.v3_trade_count, 0) > 0 AND ifNull(next_day.v2_trade_count, 0) = 0, 'v3_only',
        (
            if(ifNull(next_day.trade_count, 0) = 0, CAST(0, 'Float64'), toFloat64(next_day.v2_trade_count) / toFloat64(next_day.trade_count))
        ) >= 0.8, 'v2_leaning',
        (
            if(ifNull(next_day.trade_count, 0) = 0, CAST(0, 'Float64'), toFloat64(next_day.v3_trade_count) / toFloat64(next_day.trade_count))
        ) >= 0.8, 'v3_leaning',
        'mixed'
    ) AS next_day_protocol_preference,
    features.first_trade_at AS first_trade_at,
    features.latest_trade_at AS latest_trade_at,
    features.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_training_dataset_v1 AS features
LEFT JOIN eatlq.sender_trade_day_rollup_v1 AS next_day
    ON next_day.chain_id = features.chain_id
   AND next_day.sender_address = features.sender_address
   AND next_day.activity_date = addDays(features.activity_date, 1);
