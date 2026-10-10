DROP VIEW IF EXISTS eatlq.sender_inference_cohort_summary_v1;
DROP VIEW IF EXISTS eatlq.sender_inference_candidates_v1;

CREATE VIEW eatlq.sender_inference_candidates_v1
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
    multiIf(
        features.trade_count >= 100 AND features.v3_trade_share >= 0.8, 'high_priority_v3',
        features.trade_count >= 100, 'high_priority_mixed',
        features.trade_count >= 20, 'medium_priority',
        'tail'
    ) AS inference_priority_band,
    multiIf(
        features.venue_concentration_ratio >= 0.9, 'single_venue',
        features.venue_concentration_ratio >= 0.6, 'concentrated',
        features.venue_concentration_ratio >= 0.35, 'multi_venue',
        'distributed'
    ) AS inference_venue_shape,
    multiIf(
        features.token_concentration_ratio >= 0.9, 'single_token',
        features.token_concentration_ratio >= 0.6, 'concentrated',
        features.token_concentration_ratio >= 0.35, 'multi_token',
        'distributed'
    ) AS inference_token_shape,
    toUInt8(features.trade_count >= 20) AS min_activity_gate,
    toUInt8(features.v3_trade_share >= 0.8) AS v3_bias_gate,
    toUInt8(features.distinct_venues >= 2) AS venue_diversity_gate,
    features.first_trade_at AS first_trade_at,
    features.latest_trade_at AS latest_trade_at,
    features.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_training_dataset_v1 AS features
WHERE features.activity_date = (
    SELECT max(activity_date)
    FROM eatlq.sender_training_dataset_v1
);

CREATE VIEW eatlq.sender_inference_cohort_summary_v1
AS
SELECT
    chain_id,
    activity_date,
    inference_priority_band,
    protocol_preference,
    activity_band,
    count() AS candidate_count,
    countIf(min_activity_gate = 1) AS min_activity_gate_count,
    countIf(v3_bias_gate = 1) AS v3_bias_gate_count,
    countIf(venue_diversity_gate = 1) AS venue_diversity_gate_count,
    avg(trade_count) AS avg_trade_count,
    avg(venue_concentration_ratio) AS avg_venue_concentration_ratio,
    avg(token_concentration_ratio) AS avg_token_concentration_ratio,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.sender_inference_candidates_v1
GROUP BY
    chain_id,
    activity_date,
    inference_priority_band,
    protocol_preference,
    activity_band;
