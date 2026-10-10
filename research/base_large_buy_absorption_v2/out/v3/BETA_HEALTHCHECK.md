# BETA_HEALTHCHECK — 广撒网新池 beta 体检

Generated: `2026-07-21T08:33:47Z`

## 总裁决: **BETA_DIES_UNDER_FRICTION**

| 组件 | 结果 |
| --- | --- |
| `SURVIVES_REMOVING_TOP_1PCT` | **PASS** |
| `MONTHLY_AND_LOO_STABLE` | **PASS** |
| `SURVIVES_REALISTIC_FRICTION` | **FAIL** |

## 1. 右尾集中度 / 胜率赔率

- 交易数: `516,742`，总 PnL: `307.849` WETH
- 胜率: `62.0%`，平均盈/亏: `0.00424` / `-0.00537`，赔率: `0.7906820763575007`
- Top 1% 占总盈利: `0.07956207135822077`；去掉 top1% 后总额: `283.356` → 仍正? **True**
- Top 0.1% 占比: `0.007949860022695385`；去掉后仍正? **True**

## 2. 稳定性 / LOO

- 有数据月: `9`，其中正月: `7` (`78%`)
- 去掉最好一个月后仍正? **True** (best month PnL `114.802`)
- 去掉最好一个 token 后仍正? **True** (best token PnL `17.661`, tokens `919`)

## 3. 真实摩擦压力

> 数据质量: On-chain depth columns are broken (depth_impact_*=5.0 sentinel; depth_weth_to_move_1pct~1e-15), so v1/v2/v3 'real V3 depth control' was effectively inert. Friction here is flat-parametric, not depth-derived.

- 每笔净边际: **$1.79** / 每 $50 仓位
- 盈亏平衡往返滑点: **200 bps** (超过即转亏)
  - 滑点 `0 bps` → `923547` USD (正)
  - 滑点 `50 bps` → `665176` USD (正)
  - 滑点 `100 bps` → `406805` USD (正)
  - 滑点 `150 bps` → `148434` USD (正)
  - 滑点 `178 bps` → `3746` USD (正)
  - 滑点 `200 bps` → `-109937` USD (负)
  - 滑点 `300 bps` → `-626679` USD (负)
  - 滑点 `600 bps` → `-2176905` USD (负)

- 中等摩擦(100bps+$1 gas)下盈亏平衡赢家折价率: **0.0**

## 含义

Regime is a positive but fat-tailed/fragile long-new-pools beta; if any component FAILs, the ideal-fill backtest PnL is not a dependable live edge.
