-- Daily health view for pool discovery.
--
-- Tracks how many new pools the discovery layer has observed per day so the
-- pool-discovery healthcheck can flag a stalled discovery loop.  LASZLO's
-- architecture treats pool discovery as a mandatory upstream for strategy (see
-- Architecture-Simplified.md §1.2); if this drops to zero for a rolling window
-- it indicates the WebSocket / poller path is broken.
--
-- Verdict is computed entirely by the healthcheck script
-- (scripts/pool-discovery-healthcheck.sh); this view only exposes the daily
-- count so the script can query multi-day rollups cheaply.

DROP VIEW IF EXISTS eatlq.discovered_pool_daily_health_v1;

CREATE VIEW eatlq.discovered_pool_daily_health_v1
AS
SELECT
    toDate(discovered_at) AS health_date,
    toUInt64(uniqExact(lower(pool_address))) AS discovered_pools_new
FROM eatlq.discovered_pools
WHERE chain_id = toUInt64(8453)
GROUP BY health_date
ORDER BY health_date DESC;

-- Example:
-- SELECT * FROM eatlq.discovered_pool_daily_health_v1 ORDER BY health_date DESC LIMIT 14;
