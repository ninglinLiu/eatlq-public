-- R0 真值面板普查。回答 GOAL.md §3 的 6 个问题。
-- 全部只读。每段前的注释是它回答的问题编号。

-- Q1 宇宙有多大：真值池数 vs 面板池数
SELECT 'truth_all_cl_pools' AS k, uniqExact(pool) AS v FROM census.cl_swaps
UNION ALL
SELECT 'truth_pools_ge_10_swaps', uniqExact(pool) FROM (
  SELECT pool FROM census.cl_swaps GROUP BY pool HAVING count() >= 10)
UNION ALL
SELECT 'panel_v3_weth_pools_60d', uniqExact(lower(s.pool_address))
FROM eatlq.uniswap_v3_swaps_decoded s
INNER JOIN eatlq.discovered_pools p ON lower(s.pool_address) = lower(p.pool_address)
WHERE p.pool_type = 'v3' AND p.has_weth_leg = 1 AND s.block_time >= '2026-06-18';

-- Q2/Q3/Q4 池寿命的真实分布（首末成交跨度，小时）
WITH life AS (
  SELECT pool,
         count() AS n_swaps,
         (max(block_number) - min(block_number)) * 2 / 3600.0 AS span_h,
         uniqExact(toDate(block_time)) AS n_days
  FROM census.cl_swaps GROUP BY pool)
SELECT
  count()                        AS pools,
  round(quantile(0.50)(span_h),2) AS p50_h,
  round(quantile(0.75)(span_h),2) AS p75_h,
  round(quantile(0.90)(span_h),2) AS p90_h,
  round(quantile(0.99)(span_h),2) AS p99_h,
  countIf(span_h >= 24)          AS ge_1d,
  countIf(span_h >= 72)          AS ge_3d,     -- 决定「成熟币」假设是否可能
  countIf(span_h >= 168)         AS ge_7d,
  countIf(span_h >= 720)         AS ge_30d,
  countIf(n_days >= 30)          AS active_ge_30_days
FROM life;

-- Q5 场地拆分：UniV3 vs Aerodrome Slipstream
SELECT p.venue,
       uniqExact(s.pool) AS pools,
       count()           AS swaps,
       round(100.0 * count() / (SELECT count() FROM census.cl_swaps), 2) AS pct_swaps
FROM census.cl_swaps s LEFT JOIN census.pools p ON s.pool = p.pool
GROUP BY p.venue ORDER BY swaps DESC;

-- Q6 面板漏掉了什么：真值有、面板无的池，按成交量看是垃圾还是好东西
WITH panel AS (
  SELECT DISTINCT lower(pool_address) AS pool FROM eatlq.uniswap_v3_swaps_decoded
  WHERE block_time >= '2026-05-14'),
truth AS (
  SELECT pool, count() AS n_swaps,
         (max(block_number) - min(block_number)) * 2 / 3600.0 AS span_h
  FROM census.cl_swaps GROUP BY pool)
SELECT t.pool IN (SELECT pool FROM panel) AS in_panel,
       count()                             AS pools,
       sum(t.n_swaps)                      AS swaps,
       round(median(t.n_swaps),1)          AS med_swaps,
       round(median(t.span_h),2)           AS med_span_h,
       round(quantile(0.99)(t.span_h),2)   AS p99_span_h,
       countIf(t.span_h >= 72)             AS ge_3d
FROM truth t GROUP BY in_panel;

-- Q3 补充：只看 WETH 腿的池（可交易性最相关的子集）
WITH weth AS (SELECT lower('0x4200000000000000000000000000000000000006') AS a)
SELECT count()                          AS weth_pools,
       countIf(span_h >= 72)            AS ge_3d,
       countIf(span_h >= 168)           AS ge_7d,
       round(quantile(0.50)(span_h),2)  AS p50_h,
       round(quantile(0.90)(span_h),2)  AS p90_h
FROM (
  SELECT s.pool,
         (max(s.block_number) - min(s.block_number)) * 2 / 3600.0 AS span_h
  FROM census.cl_swaps s INNER JOIN census.pools p ON s.pool = p.pool
  WHERE p.token0 = (SELECT a FROM weth) OR p.token1 = (SELECT a FROM weth)
  GROUP BY s.pool);
