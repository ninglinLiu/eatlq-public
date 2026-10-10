-- Sprint038A: Recipient-keyed profile views
-- ============================================================
-- Mirrors the sender_*_v1 view chain (016–027) but GROUP BY
-- recipient_address instead of sender_address.
--
-- RATIONALE (from sprint038a readout):
--   On Base L2 Uniswap V3, `sender` is always the router/aggregator
--   (e.g. Universal Router 0x6ff…b43, SwapRouter02 0x2626…e481).
--   The `recipient` field is the actual EOA / wallet.
--   Therefore sender-keyed ranking is systematically biased
--   (SENDER_KEYED_RANKING_TRUSTED = false per sprint038a decision flags).
--
-- GUARD: These views are DIAGNOSTIC ONLY.
--   They are not evidence of alpha, not training features, not live signals.
--   Recipient addresses must remain in _AUDIT_ONLY columns.
--   FAILED_NO_7D_ALPHA_ACTIVE = true -- not superseded by these views.
--   IDENTITY_CLAIM_ALLOWED = false
--
-- ClickHouse best-practices applied:
--   - Per query-join-filter-before: each JOIN sub-select is pre-filtered
--   - Per schema-types-avoid-nullable: views inherit base table types; no
--     additional Nullable wrapping is introduced
--   - Views only; no new MergeTree tables, no PRIMARY KEY needed
-- ============================================================

DROP VIEW IF EXISTS eatlq.recipient_feature_store_v1;
DROP VIEW IF EXISTS eatlq.recipient_token_pnl_v1;
DROP VIEW IF EXISTS eatlq.recipient_pnl_summary_v1;
DROP VIEW IF EXISTS eatlq.recipient_venue_trade_profile_v1;
DROP VIEW IF EXISTS eatlq.recipient_token_trade_profile_v1;
DROP VIEW IF EXISTS eatlq.recipient_swap_profile_v1;

-- ── 1. Per-recipient aggregate swap profile ──────────────────────────────────
-- Mirrors: eatlq.sender_swap_profile
-- Change: GROUP BY recipient_address
CREATE VIEW eatlq.recipient_swap_profile_v1
AS
SELECT
    chain_id,
    recipient_address,
    count()                                         AS trade_count,
    countIf(protocol_family = 'uniswap_v2')        AS v2_trade_count,
    countIf(protocol_family = 'uniswap_v3')        AS v3_trade_count,
    uniqExact(venue_address)                        AS distinct_venues,
    uniqExact(sold_token_address)                   AS distinct_sold_assets,
    uniqExact(bought_token_address)                 AS distinct_bought_assets,
    min(block_time)                                 AS first_trade_at,
    max(block_time)                                 AS latest_trade_at,
    max(latest_ingested_at)                         AS latest_ingested_at
FROM eatlq.swap_trade_events_all
GROUP BY chain_id, recipient_address;

-- ── 2. Per-recipient × venue trade profile ───────────────────────────────────
-- Mirrors: eatlq.sender_venue_trade_profile
CREATE VIEW eatlq.recipient_venue_trade_profile_v1
AS
SELECT
    src.chain_id                                    AS chain_id,
    src.recipient_address                           AS recipient_address,
    src.protocol_family                             AS protocol_family,
    src.venue_address                               AS venue_address,
    argMax(src.venue_label,   src.row_latest)       AS venue_label,
    argMax(src.fee_bps,       src.row_latest)       AS fee_bps,
    count()                                         AS trade_count,
    uniqExact(src.tx_hash)                          AS distinct_txs,
    uniqExact(src.sold_token_address)               AS distinct_sold_assets,
    uniqExact(src.bought_token_address)             AS distinct_bought_assets,
    min(src.block_time)                             AS first_trade_at,
    max(src.block_time)                             AS latest_trade_at,
    max(src.row_latest)                             AS latest_ingested_at
FROM (
    SELECT
        chain_id,
        recipient_address,
        protocol_family,
        venue_address,
        venue_label,
        fee_bps,
        tx_hash,
        sold_token_address,
        bought_token_address,
        block_time,
        latest_ingested_at AS row_latest
    FROM eatlq.swap_trade_events_all
    -- pre-filter: only rows with usable block_time (per query-join-filter-before)
    WHERE block_time IS NOT NULL
        OR latest_ingested_at IS NOT NULL
) AS src
GROUP BY
    src.chain_id,
    src.recipient_address,
    src.protocol_family,
    src.venue_address;

-- ── 3. Per-recipient × token trade profile ───────────────────────────────────
-- Mirrors: eatlq.sender_token_trade_profile
CREATE VIEW eatlq.recipient_token_trade_profile_v1
AS
SELECT
    chain_id,
    recipient_address,
    token_address,
    argMax(token_symbol,   row_latest)  AS token_symbol,
    argMax(token_decimals, row_latest)  AS token_decimals,
    count()                             AS trade_touch_count,
    countIf(token_role = 'sold')        AS sold_trade_count,
    countIf(token_role = 'bought')      AS bought_trade_count,
    sumIf(amount_normalized, token_role = 'sold')                               AS total_sold_amount_normalized,
    sumIf(amount_normalized, token_role = 'bought')                             AS total_bought_amount_normalized,
    sumIf(if(token_role='bought', amount_normalized, -amount_normalized), 1)    AS net_amount_normalized,
    min(block_time)                     AS first_trade_at,
    max(block_time)                     AS latest_trade_at,
    max(row_latest)                     AS latest_ingested_at
FROM (
    SELECT
        chain_id,
        recipient_address,
        sold_token_address   AS token_address,
        sold_token_symbol    AS token_symbol,
        sold_token_decimals  AS token_decimals,
        'sold'               AS token_role,
        sold_amount_normalized AS amount_normalized,
        block_time,
        latest_ingested_at   AS row_latest
    FROM eatlq.swap_trade_events_all
    WHERE sold_token_address IS NOT NULL

    UNION ALL

    SELECT
        chain_id,
        recipient_address,
        bought_token_address   AS token_address,
        bought_token_symbol    AS token_symbol,
        bought_token_decimals  AS token_decimals,
        'bought'               AS token_role,
        bought_amount_normalized AS amount_normalized,
        block_time,
        latest_ingested_at     AS row_latest
    FROM eatlq.swap_trade_events_all
    WHERE bought_token_address IS NOT NULL
)
GROUP BY chain_id, recipient_address, token_address;

-- ── 4. Per-recipient × token PnL (round-trip aware) ──────────────────────────
-- Mirrors: eatlq.sender_token_pnl_v1
-- Round-trip = bought AND sold > 1% of bought amount
CREATE VIEW eatlq.recipient_token_pnl_v1
AS
SELECT
    rttp.chain_id,
    rttp.recipient_address,
    rttp.token_address,
    rttp.bought_trade_count,
    rttp.sold_trade_count,
    rttp.total_bought_amount_normalized,
    rttp.total_sold_amount_normalized,
    rttp.net_amount_normalized,
    if(
        rttp.total_bought_amount_normalized > 0
        AND rttp.total_sold_amount_normalized > 0
        AND rttp.total_sold_amount_normalized > rttp.total_bought_amount_normalized * 0.01,
        1, 0
    )                                                           AS has_round_trip,
    if(has_round_trip = 1 AND rttp.net_amount_normalized < 0, 1, 0) AS is_win,
    rttp.first_trade_at,
    rttp.latest_trade_at,
    dateDiff('second', rttp.first_trade_at, rttp.latest_trade_at) AS hold_seconds
FROM eatlq.recipient_token_trade_profile_v1 AS rttp
WHERE rttp.bought_trade_count > 0;

-- ── 5. Per-recipient PnL summary ─────────────────────────────────────────────
-- Mirrors: eatlq.sender_pnl_summary_v1
-- Minimum 3 distinct tokens traded (same threshold as sender view)
CREATE VIEW eatlq.recipient_pnl_summary_v1
AS
SELECT
    pnl.chain_id,
    pnl.recipient_address,
    count()                                             AS tokens_traded,
    countIf(pnl.has_round_trip = 1)                    AS round_trips,
    countIf(pnl.is_win = 1)                            AS wins,
    if(
        round_trips > 0,
        toFloat64(wins) / toFloat64(round_trips),
        0.0
    )                                                   AS win_rate,
    avg(pnl.hold_seconds)                               AS avg_hold_seconds,
    min(pnl.first_trade_at)                             AS first_ever_trade,
    max(pnl.latest_trade_at)                            AS latest_trade,
    dateDiff('day', first_ever_trade, latest_trade)     AS active_span_days
FROM eatlq.recipient_token_pnl_v1 AS pnl
GROUP BY pnl.chain_id, pnl.recipient_address
HAVING tokens_traded >= 3;

-- ── 6. Per-recipient feature store ───────────────────────────────────────────
-- Mirrors: eatlq.sender_feature_store_v1
-- Provides the same feature vector shape as the sender view for
-- drop-in compatibility in any future sidecar scoring pipeline.
-- NOTE: these features must NOT enter retrain_model.py FEATURE_COLUMNS
--       without explicit ML training gate + label authorization
--       (per sprint029b_wallet_sidecar_design.md §4 denylist).
CREATE VIEW eatlq.recipient_feature_store_v1
AS
SELECT
    sp.chain_id                                                         AS chain_id,
    sp.recipient_address                                                AS recipient_address,
    sp.trade_count                                                      AS trade_count,
    sp.v2_trade_count                                                   AS v2_trade_count,
    sp.v3_trade_count                                                   AS v3_trade_count,
    if(sp.trade_count = 0, CAST(0,'Float64'),
        toFloat64(sp.v2_trade_count) / toFloat64(sp.trade_count))       AS v2_trade_share,
    if(sp.trade_count = 0, CAST(0,'Float64'),
        toFloat64(sp.v3_trade_count) / toFloat64(sp.trade_count))       AS v3_trade_share,
    sp.distinct_venues                                                  AS distinct_venues,
    sp.distinct_sold_assets                                             AS distinct_sold_assets,
    sp.distinct_bought_assets                                           AS distinct_bought_assets,
    ifNull(act.active_days, 0)                                          AS active_days,
    ifNull(act.active_hours, 0)                                         AS active_hours,
    ifNull(act.distinct_senders, 0)                                     AS distinct_senders,
    if(ifNull(act.active_days, 0) = 0, CAST(0,'Float64'),
        toFloat64(sp.trade_count) / toFloat64(act.active_days))         AS avg_trades_per_active_day,
    if(ifNull(act.active_hours, 0) = 0, CAST(0,'Float64'),
        toFloat64(sp.trade_count) / toFloat64(act.active_hours))        AS avg_trades_per_active_hour,
    ifNull(venue.top_venue_trade_count, 0)                              AS top_venue_trade_count,
    ifNull(token.top_token_trade_touch_count, 0)                        AS top_token_trade_touch_count,
    ifNull(token.total_token_touches, 0)                                AS total_token_touches,
    if(sp.trade_count = 0, CAST(0,'Float64'),
        toFloat64(ifNull(venue.top_venue_trade_count, 0)) / toFloat64(sp.trade_count))
                                                                        AS venue_concentration_ratio,
    if(ifNull(token.total_token_touches, 0) = 0, CAST(0,'Float64'),
        toFloat64(ifNull(token.top_token_trade_touch_count, 0)) / toFloat64(token.total_token_touches))
                                                                        AS token_concentration_ratio,
    ifNull(token.net_buyer_token_count, 0)                              AS net_buyer_token_count,
    ifNull(token.net_seller_token_count, 0)                             AS net_seller_token_count,
    ifNull(token.net_flat_token_count, 0)                               AS net_flat_token_count,
    toUInt8(sp.v2_trade_count > 0 AND sp.v3_trade_count > 0)           AS cross_protocol_flag,
    toUInt8(sp.distinct_venues > 1)                                     AS multi_venue_flag,
    sp.first_trade_at                                                   AS first_trade_at,
    sp.latest_trade_at                                                  AS latest_trade_at,
    sp.latest_ingested_at                                               AS latest_ingested_at
FROM eatlq.recipient_swap_profile_v1 AS sp
-- Per query-join-filter-before: subselects are pre-aggregated
LEFT JOIN (
    SELECT
        chain_id,
        recipient_address,
        uniqExactIf(toDate(block_time), isNotNull(block_time)) AS active_days,
        uniqExactIf(toStartOfHour(block_time), isNotNull(block_time)) AS active_hours,
        uniqExact(sender_address) AS distinct_senders
    FROM eatlq.swap_trade_events_all
    GROUP BY chain_id, recipient_address
) AS act
    ON act.chain_id = sp.chain_id
   AND act.recipient_address = sp.recipient_address
LEFT JOIN (
    SELECT
        chain_id,
        recipient_address,
        max(trade_count) AS top_venue_trade_count
    FROM eatlq.recipient_venue_trade_profile_v1
    GROUP BY chain_id, recipient_address
) AS venue
    ON venue.chain_id = sp.chain_id
   AND venue.recipient_address = sp.recipient_address
LEFT JOIN (
    SELECT
        chain_id,
        recipient_address,
        max(trade_touch_count)                                      AS top_token_trade_touch_count,
        sum(trade_touch_count)                                      AS total_token_touches,
        countIf(net_amount_normalized >  0.000000000001)            AS net_buyer_token_count,
        countIf(net_amount_normalized < -0.000000000001)            AS net_seller_token_count,
        countIf(abs(net_amount_normalized) <= 0.000000000001)       AS net_flat_token_count
    FROM eatlq.recipient_token_trade_profile_v1
    GROUP BY chain_id, recipient_address
) AS token
    ON token.chain_id = sp.chain_id
   AND token.recipient_address = sp.recipient_address;
