DROP VIEW IF EXISTS eatlq.sender_token_pnl_v1;
DROP VIEW IF EXISTS eatlq.sender_pnl_summary_v1;

CREATE VIEW eatlq.sender_token_pnl_v1
AS
SELECT
    stp.chain_id,
    stp.sender_address,
    stp.token_address,
    stp.bought_trade_count,
    stp.sold_trade_count,
    stp.total_bought_amount_normalized,
    stp.total_sold_amount_normalized,
    stp.net_amount_normalized,
    if(stp.total_bought_amount_normalized > 0 AND stp.total_sold_amount_normalized > 0
       AND stp.total_sold_amount_normalized > stp.total_bought_amount_normalized * 0.01,
       1, 0) AS has_round_trip,
    if(has_round_trip = 1 AND stp.net_amount_normalized < 0, 1, 0) AS is_win,
    stp.first_trade_at,
    stp.latest_trade_at,
    dateDiff('second', stp.first_trade_at, stp.latest_trade_at) AS hold_seconds
FROM eatlq.sender_token_trade_profile AS stp
WHERE stp.bought_trade_count > 0;

CREATE VIEW eatlq.sender_pnl_summary_v1
AS
SELECT
    pnl.chain_id,
    pnl.sender_address,
    count() AS tokens_traded,
    countIf(pnl.has_round_trip = 1) AS round_trips,
    countIf(pnl.is_win = 1) AS wins,
    if(round_trips > 0, toFloat64(wins) / toFloat64(round_trips), 0.0) AS win_rate,
    avg(pnl.hold_seconds) AS avg_hold_seconds,
    min(pnl.first_trade_at) AS first_ever_trade,
    max(pnl.latest_trade_at) AS latest_trade,
    dateDiff('day', first_ever_trade, latest_trade) AS active_span_days
FROM eatlq.sender_token_pnl_v1 AS pnl
GROUP BY pnl.chain_id, pnl.sender_address
HAVING tokens_traded >= 3;
