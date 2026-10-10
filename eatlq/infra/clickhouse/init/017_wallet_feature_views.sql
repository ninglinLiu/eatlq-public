DROP VIEW IF EXISTS eatlq.sender_feature_store_v1;
DROP VIEW IF EXISTS eatlq.sender_venue_trade_profile;

CREATE VIEW eatlq.sender_venue_trade_profile
AS
SELECT
    src.chain_id AS chain_id,
    src.sender_address AS sender_address,
    src.protocol_family AS protocol_family,
    src.venue_address AS venue_address,
    argMax(src.venue_label, src.row_latest_ingested_at) AS venue_label,
    argMax(src.fee_bps, src.row_latest_ingested_at) AS fee_bps,
    count() AS trade_count,
    uniqExact(src.tx_hash) AS distinct_txs,
    uniqExact(src.sold_token_address) AS distinct_sold_assets,
    uniqExact(src.bought_token_address) AS distinct_bought_assets,
    min(src.block_time) AS first_trade_at,
    max(src.block_time) AS latest_trade_at,
    max(src.row_latest_ingested_at) AS latest_ingested_at
FROM (
    SELECT
        chain_id,
        sender_address,
        protocol_family,
        venue_address,
        venue_label,
        fee_bps,
        tx_hash,
        sold_token_address,
        bought_token_address,
        block_time,
        latest_ingested_at AS row_latest_ingested_at
    FROM eatlq.swap_trade_events_all
) AS src
GROUP BY
    src.chain_id,
    src.sender_address,
    src.protocol_family,
    src.venue_address;

CREATE VIEW eatlq.sender_feature_store_v1
AS
SELECT
    sp.chain_id AS chain_id,
    sp.sender_address AS sender_address,
    sp.trade_count AS trade_count,
    sp.v2_trade_count AS v2_trade_count,
    sp.v3_trade_count AS v3_trade_count,
    if(
        sp.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(sp.v2_trade_count) / toFloat64(sp.trade_count)
    ) AS v2_trade_share,
    if(
        sp.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(sp.v3_trade_count) / toFloat64(sp.trade_count)
    ) AS v3_trade_share,
    sp.distinct_venues AS distinct_venues,
    sp.distinct_sold_assets AS distinct_sold_assets,
    sp.distinct_bought_assets AS distinct_bought_assets,
    ifNull(act.active_days, 0) AS active_days,
    ifNull(act.active_hours, 0) AS active_hours,
    ifNull(act.distinct_recipients, 0) AS distinct_recipients,
    if(
        ifNull(act.active_days, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(sp.trade_count) / toFloat64(act.active_days)
    ) AS avg_trades_per_active_day,
    if(
        ifNull(act.active_hours, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(sp.trade_count) / toFloat64(act.active_hours)
    ) AS avg_trades_per_active_hour,
    ifNull(venue.top_venue_trade_count, 0) AS top_venue_trade_count,
    ifNull(token.top_token_trade_touch_count, 0) AS top_token_trade_touch_count,
    ifNull(token.total_token_touches, 0) AS total_token_touches,
    if(
        sp.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / toFloat64(sp.trade_count)
    ) AS venue_concentration_ratio,
    if(
        ifNull(token.total_token_touches, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / toFloat64(token.total_token_touches)
    ) AS token_concentration_ratio,
    ifNull(token.net_buyer_token_count, 0) AS net_buyer_token_count,
    ifNull(token.net_seller_token_count, 0) AS net_seller_token_count,
    ifNull(token.net_flat_token_count, 0) AS net_flat_token_count,
    toUInt8(sp.v2_trade_count > 0 AND sp.v3_trade_count > 0) AS cross_protocol_flag,
    toUInt8(sp.distinct_venues > 1) AS multi_venue_flag,
    sp.first_trade_at AS first_trade_at,
    sp.latest_trade_at AS latest_trade_at,
    sp.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_swap_profile AS sp
LEFT JOIN (
    SELECT
        chain_id,
        sender_address,
        uniqExactIf(toDate(block_time), isNotNull(block_time)) AS active_days,
        uniqExactIf(toStartOfHour(block_time), isNotNull(block_time)) AS active_hours,
        uniqExact(recipient_address) AS distinct_recipients
    FROM eatlq.swap_trade_events_all
    GROUP BY chain_id, sender_address
) AS act
    ON act.chain_id = sp.chain_id
   AND act.sender_address = sp.sender_address
LEFT JOIN (
    SELECT
        chain_id,
        sender_address,
        max(trade_count) AS top_venue_trade_count
    FROM eatlq.sender_venue_trade_profile
    GROUP BY chain_id, sender_address
) AS venue
    ON venue.chain_id = sp.chain_id
   AND venue.sender_address = sp.sender_address
LEFT JOIN (
    SELECT
        chain_id,
        sender_address,
        max(trade_touch_count) AS top_token_trade_touch_count,
        sum(trade_touch_count) AS total_token_touches,
        countIf(net_amount_normalized > 0.000000000001) AS net_buyer_token_count,
        countIf(net_amount_normalized < -0.000000000001) AS net_seller_token_count,
        countIf(abs(net_amount_normalized) <= 0.000000000001) AS net_flat_token_count
    FROM eatlq.sender_token_trade_profile
    GROUP BY chain_id, sender_address
) AS token
    ON token.chain_id = sp.chain_id
   AND token.sender_address = sp.sender_address;
