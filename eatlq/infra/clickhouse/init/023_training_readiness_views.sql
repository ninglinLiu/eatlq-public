DROP VIEW IF EXISTS eatlq.sender_training_label_balance_v1;
DROP VIEW IF EXISTS eatlq.sender_training_examples_ready_v1;

CREATE VIEW eatlq.sender_training_examples_ready_v1
AS
SELECT
    split.chain_id AS chain_id,
    split.activity_date AS activity_date,
    split.sender_address AS sender_address,
    split.dataset_split AS dataset_split,
    split.split_bucket AS split_bucket,
    split.trade_count AS trade_count,
    split.v2_trade_count AS v2_trade_count,
    split.v3_trade_count AS v3_trade_count,
    split.v2_trade_share AS v2_trade_share,
    split.v3_trade_share AS v3_trade_share,
    split.distinct_venues AS distinct_venues,
    split.distinct_recipients AS distinct_recipients,
    split.distinct_sold_assets AS distinct_sold_assets,
    split.distinct_bought_assets AS distinct_bought_assets,
    split.avg_sold_amount_normalized AS avg_sold_amount_normalized,
    split.avg_bought_amount_normalized AS avg_bought_amount_normalized,
    split.protocol_preference AS protocol_preference,
    split.venue_concentration_band AS venue_concentration_band,
    split.token_concentration_band AS token_concentration_band,
    split.activity_band AS activity_band,
    split.recipient_diversity_band AS recipient_diversity_band,
    split.cross_protocol_flag AS cross_protocol_flag,
    split.multi_venue_flag AS multi_venue_flag,
    split.next_day_trade_count AS next_day_trade_count,
    split.next_day_v2_trade_count AS next_day_v2_trade_count,
    split.next_day_v3_trade_count AS next_day_v3_trade_count,
    split.next_day_v2_trade_share AS next_day_v2_trade_share,
    split.next_day_v3_trade_share AS next_day_v3_trade_share,
    split.next_day_active_flag AS next_day_active_flag,
    split.next_day_higher_trade_count_flag AS next_day_higher_trade_count_flag,
    split.next_day_cross_protocol_flag AS next_day_cross_protocol_flag,
    split.next_day_protocol_preference AS next_day_protocol_preference,
    multiIf(
        split.next_day_trade_count = 0, 'inactive',
        split.next_day_trade_count >= 20, 'hyperactive',
        split.next_day_trade_count >= 5, 'active',
        'light'
    ) AS next_day_activity_bucket,
    multiIf(
        split.next_day_trade_count = 0, 'inactive',
        split.next_day_higher_trade_count_flag = 1, 'higher',
        'flat_or_lower'
    ) AS next_day_growth_bucket,
    multiIf(
        split.next_day_active_flag = 0, 'inactive',
        split.next_day_protocol_preference = 'v2_only', 'active_v2_only',
        split.next_day_protocol_preference = 'v3_only', 'active_v3_only',
        split.next_day_protocol_preference = 'v2_leaning', 'active_v2_leaning',
        split.next_day_protocol_preference = 'v3_leaning', 'active_v3_leaning',
        'active_mixed'
    ) AS supervised_target_class,
    split.first_trade_at AS first_trade_at,
    split.latest_trade_at AS latest_trade_at,
    split.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_training_examples_split_v1 AS split;

CREATE VIEW eatlq.sender_training_label_balance_v1
AS
SELECT
    chain_id,
    dataset_split,
    supervised_target_class,
    next_day_activity_bucket,
    count() AS example_count,
    countIf(next_day_higher_trade_count_flag = 1) AS higher_trade_positive_examples,
    countIf(next_day_cross_protocol_flag = 1) AS cross_protocol_positive_examples,
    avg(trade_count) AS avg_current_trade_count,
    avg(next_day_trade_count) AS avg_next_day_trade_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.sender_training_examples_ready_v1
GROUP BY
    chain_id,
    dataset_split,
    supervised_target_class,
    next_day_activity_bucket;
