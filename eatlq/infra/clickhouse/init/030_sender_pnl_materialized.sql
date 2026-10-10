-- 030_sender_pnl_materialized.sql
-- MANUAL APPLY ONLY — do not auto-migrate without ADR.
-- Materializes sender_pnl_summary_v1 view chain to avoid OOM on count()/GROUP BY.
--
-- Refresh (daily cron):
--   TRUNCATE TABLE eatlq.sender_token_pnl_mat;
--   INSERT INTO eatlq.sender_token_pnl_mat SELECT ... FROM eatlq.sender_token_pnl_v1;
--   TRUNCATE TABLE eatlq.sender_pnl_summary_mat;
--   INSERT INTO eatlq.sender_pnl_summary_mat SELECT ... FROM eatlq.sender_pnl_summary_v1;
--
-- Or use: python scripts/wallet_profile_v0/refresh_sender_pnl_mat.py

DROP TABLE IF EXISTS eatlq.sender_pnl_summary_mat;
DROP TABLE IF EXISTS eatlq.sender_token_pnl_mat;

CREATE TABLE eatlq.sender_token_pnl_mat
(
    chain_id UInt64,
    sender_address String,
    token_address String,
    bought_trade_count UInt64,
    sold_trade_count UInt64,
    total_bought_amount_normalized Float64,
    total_sold_amount_normalized Float64,
    net_amount_normalized Float64,
    has_round_trip UInt8,
    is_win UInt8,
    first_trade_at DateTime,
    latest_trade_at DateTime,
    hold_seconds Int64,
    refreshed_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(refreshed_at)
ORDER BY (chain_id, sender_address, token_address);

CREATE TABLE eatlq.sender_pnl_summary_mat
(
    chain_id UInt64,
    sender_address String,
    tokens_traded UInt64,
    round_trips UInt64,
    wins UInt64,
    win_rate Float64,
    avg_hold_seconds Float64,
    first_ever_trade DateTime,
    latest_trade DateTime,
    active_span_days Int32,
    refreshed_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(refreshed_at)
ORDER BY (chain_id, sender_address);
