DROP VIEW IF EXISTS eatlq.sender_behavior_labels_v1;
DROP VIEW IF EXISTS eatlq.sender_preference_profile_v1;

CREATE VIEW eatlq.sender_preference_profile_v1
AS
SELECT
    sp.chain_id AS chain_id,
    sp.sender_address AS sender_address,
    ifNull(venue.top_protocol_family, 'unknown') AS top_protocol_family,
    venue.top_venue_address AS top_venue_address,
    venue.top_venue_label AS top_venue_label,
    venue.top_venue_fee_bps AS top_venue_fee_bps,
    ifNull(venue.top_venue_trade_count, 0) AS top_venue_trade_count,
    if(
        sp.trade_count = 0,
        CAST(0, 'Float64'),
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / toFloat64(sp.trade_count)
    ) AS top_venue_trade_share,
    token.top_token_address AS top_token_address,
    token.top_token_symbol AS top_token_symbol,
    token.top_token_decimals AS top_token_decimals,
    ifNull(token.top_token_trade_touch_count, 0) AS top_token_trade_touch_count,
    if(
        ifNull(token.total_token_touches, 0) = 0,
        CAST(0, 'Float64'),
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / toFloat64(token.total_token_touches)
    ) AS top_token_trade_share,
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
        argMax(protocol_family, tuple(trade_count, latest_ingested_at, venue_address)) AS top_protocol_family,
        argMax(venue_address, tuple(trade_count, latest_ingested_at, venue_address)) AS top_venue_address,
        argMax(venue_label, tuple(trade_count, latest_ingested_at, venue_address)) AS top_venue_label,
        argMax(fee_bps, tuple(trade_count, latest_ingested_at, venue_address)) AS top_venue_fee_bps,
        argMax(trade_count, tuple(trade_count, latest_ingested_at, venue_address)) AS top_venue_trade_count
    FROM eatlq.sender_venue_trade_profile
    GROUP BY chain_id, sender_address
) AS venue
    ON venue.chain_id = sp.chain_id
   AND venue.sender_address = sp.sender_address
LEFT JOIN (
    SELECT
        chain_id,
        sender_address,
        argMax(token_address, tuple(trade_touch_count, latest_ingested_at, token_address)) AS top_token_address,
        argMax(token_symbol, tuple(trade_touch_count, latest_ingested_at, token_address)) AS top_token_symbol,
        argMax(token_decimals, tuple(trade_touch_count, latest_ingested_at, token_address)) AS top_token_decimals,
        argMax(trade_touch_count, tuple(trade_touch_count, latest_ingested_at, token_address)) AS top_token_trade_touch_count,
        sum(trade_touch_count) AS total_token_touches
    FROM eatlq.sender_token_trade_profile
    GROUP BY chain_id, sender_address
) AS token
    ON token.chain_id = sp.chain_id
   AND token.sender_address = sp.sender_address;

CREATE VIEW eatlq.sender_behavior_labels_v1
AS
SELECT
    fs.chain_id AS chain_id,
    fs.sender_address AS sender_address,
    multiIf(
        fs.v2_trade_count > 0 AND fs.v3_trade_count = 0, 'v2_only',
        fs.v3_trade_count > 0 AND fs.v2_trade_count = 0, 'v3_only',
        fs.v2_trade_share >= 0.8, 'v2_leaning',
        fs.v3_trade_share >= 0.8, 'v3_leaning',
        'mixed'
    ) AS protocol_preference,
    multiIf(
        fs.venue_concentration_ratio >= 0.9, 'single_venue',
        fs.venue_concentration_ratio >= 0.6, 'concentrated',
        fs.venue_concentration_ratio >= 0.35, 'multi_venue',
        'distributed'
    ) AS venue_concentration_band,
    multiIf(
        fs.token_concentration_ratio >= 0.9, 'single_token',
        fs.token_concentration_ratio >= 0.6, 'concentrated',
        fs.token_concentration_ratio >= 0.35, 'multi_token',
        'distributed'
    ) AS token_concentration_band,
    multiIf(
        fs.avg_trades_per_active_day >= 20, 'hyperactive',
        fs.avg_trades_per_active_day >= 5, 'active',
        fs.avg_trades_per_active_day >= 1, 'steady',
        'sporadic'
    ) AS activity_band,
    multiIf(
        fs.distinct_recipients >= 20, 'high',
        fs.distinct_recipients >= 5, 'medium',
        fs.distinct_recipients >= 2, 'low',
        'single'
    ) AS recipient_diversity_band,
    multiIf(
        fs.net_buyer_token_count > fs.net_seller_token_count * 2 AND fs.net_buyer_token_count > 0, 'net_buyer',
        fs.net_seller_token_count > fs.net_buyer_token_count * 2 AND fs.net_seller_token_count > 0, 'net_seller',
        'balanced'
    ) AS flow_bias,
    fs.cross_protocol_flag AS cross_protocol_flag,
    fs.multi_venue_flag AS multi_venue_flag,
    fs.latest_ingested_at AS latest_ingested_at
FROM eatlq.sender_feature_store_v1 AS fs;
