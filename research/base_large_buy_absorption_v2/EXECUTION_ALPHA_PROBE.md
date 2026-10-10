# EXECUTION_ALPHA_PROBE — 可成交滑点重建 + 可交易子集

Generated: `2026-07-21T08:40:34Z`  |  方法: `model_free_realized_impact_per_weth (fixes broken depth columns)`

## 总裁决: **TRADABLE_SUBSET_FOUND**

## 真实往返成本分布 ($50 clip, 含 LP 手续费 + 实测冲击)

- 覆盖(有 kappa 估计的池): `100.0%`
- 往返总成本 p10/p50/p90: `34` / `90` / `277` bps
  - 其中 LP 手续费中位 `60` bps + 实测滑点中位 `39` bps
- 低于 178bps 的比例: `76.5%`；低于 100bps: `63.5%`

## net-of-slippage 结果（全体可估池）

- 交易数 `516,658`，毛 PnL `308.0` WETH → **净 PnL `137.2` WETH ($411747)**
- 每笔净边际: **$0.80**，净胜率 `60.9%`

## 按往返滑点阈值筛子集 (net-of-slippage)

| 阈值 | 笔数 | 净PnL(USD) | 每笔净边际(USD) | 正月/总月 | 去掉最好月仍正 |
| --- | ---: | ---: | ---: | :--: | :--: |
| <50bps | 73,936 | -302044 | -4.09 | 2/8 | False |
| <100bps | 327,856 | 428726 | 1.31 | 4/9 | True |
| <178bps | 395,321 | 550537 | 1.39 | 4/9 | True |
| <300bps | 481,611 | 629910 | 1.31 | 5/9 | True |
| <600bps | 502,631 | 637541 | 1.27 | 5/9 | True |

## 含义

If NO_TRADABLE_SUBSET: even after cherry-picking only the deepest pools, once you charge each trade its own realized round-trip slippage the net edge is gone — the beta is real but not capturable at $50 clips. TRADABLE_SUBSET_FOUND would mean a depth-filtered slice survives and is worth a forward shadow.
