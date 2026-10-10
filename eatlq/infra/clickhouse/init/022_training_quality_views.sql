DROP VIEW IF EXISTS eatlq.sender_training_daily_dataset_summary_v1;
DROP VIEW IF EXISTS eatlq.sender_training_split_summary_v1;

CREATE VIEW eatlq.sender_training_split_summary_v1
AS
SELECT
    chain_id,
    dataset_split,
    count() AS example_count,
    countIf(next_day_active_flag = 1) AS next_day_active_examples,
    countIf(next_day_higher_trade_count_flag = 1) AS next_day_higher_trade_examples,
    countIf(next_day_cross_protocol_flag = 1) AS next_day_cross_protocol_examples,
    avg(trade_count) AS avg_trade_count,
    avg(next_day_trade_count) AS avg_next_day_trade_count,
    avg(venue_concentration_ratio) AS avg_venue_concentration_ratio,
    avg(token_concentration_ratio) AS avg_token_concentration_ratio,
    min(activity_date) AS first_activity_date,
    max(activity_date) AS latest_activity_date,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.sender_training_examples_split_v1
GROUP BY chain_id, dataset_split;

CREATE VIEW eatlq.sender_training_daily_dataset_summary_v1
AS
SELECT
    chain_id,
    activity_date,
    count() AS example_count,
    countIf(dataset_split = 'train') AS train_examples,
    countIf(dataset_split = 'validation') AS validation_examples,
    countIf(dataset_split = 'test') AS test_examples,
    countIf(next_day_active_flag = 1) AS next_day_active_examples,
    countIf(next_day_higher_trade_count_flag = 1) AS next_day_higher_trade_examples,
    countIf(next_day_cross_protocol_flag = 1) AS next_day_cross_protocol_examples,
    avg(trade_count) AS avg_trade_count,
    avg(next_day_trade_count) AS avg_next_day_trade_count,
    max(latest_ingested_at) AS latest_ingested_at
FROM eatlq.sender_training_examples_split_v1
GROUP BY chain_id, activity_date;
