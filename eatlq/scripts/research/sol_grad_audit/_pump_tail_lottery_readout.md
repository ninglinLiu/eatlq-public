# Pump.fun T+15→T+90 右尾彩票策略审计

生成：2026-06-25T09:44:48.747294+00:00 | 成本=3%

## 1. 数据集摘要
- n=1596 有 grid 路径
- T+15 使用 T+0 回退：1.0

## 2. 基础结果
- mean=+0.113 median=-0.030 win=8.6%
- TEST mean=0.0411585207750181 p>1000=0.31%

## 3. Top winner 法医
- 见 `_pump_tail_top_winners.csv`（top20 逐笔）

## 4. 右尾敏感性
- original: mean=0.1134908937853581 median=-0.03
- drop_top_1: mean=0.09279731205865324 median=-0.03
- drop_top_3: mean=0.07225295120573201 median=-0.03
- drop_top_5: mean=0.05865038428676556 median=-0.03
- drop_top_10: mean=0.035341152178665396 median=-0.03
- drop_top_20: mean=0.008986727623638087 median=-0.03
- winsor_top_0.5%: mean=0.08321022178514129 median=-0.03
- winsor_top_1.0%: mean=0.05972424888719912 median=-0.03

## 5. 随机基准

- ALL: mean=0.1134908937853581 p>1000=0.003132832080200501
- T15_alive_proxy: mean=0.13115259673499285 p>1000=0.003518648838845883
- n15_ge_1: mean=0.13115259673499285 p>1000=0.003518648838845883
- n15_ge_5: mean=0.19570668054820148 p>1000=0.005302226935312832
- n15_ge_10: mean=0.23938937744806962 p>1000=0.008710801393728223
- random_draw_n=1596_x1000: mean=0.11228116594598947 p>1000=0.0030827067669172933

## 6. 可成交性

- top20 NEEDS_RPC/SPARSE 比例：100%

## 7. 亏损端

- P<=-10%: 6.1% P<=-25%: 4.1% max_streak: 100

## 8. 前置信号
见 JSON feature_contrast

## 9. 资金曲线

- 仓位0.1%: final=+19.7% maxDD=-0.6%
- 仓位0.2%: final=+43.0% maxDD=-1.2%
- 仓位0.3%: final=+70.3% maxDD=-1.8%
- 仓位0.5%: final=+140.2% maxDD=-3.1%
- 仓位1.0%: final=+447.2% maxDD=-6.0%

## 10. 分类

**UNTRADABLE_RIGHT_TAIL**

原因：
- median 深度负
- top winners 可成交性风险 100%
- p>1000 不高于随机

## 11–12. 见 forward plan / RPC plan 文件

## 13. 结论

### 测量摘要
- **右尾是否高于随机**：p(+1000%) ALL=0.003 vs random=0.003 → **无显著提升**
- **top winners 共性**：top5 均为 SPARSE_GRID；n_trades_15=11（early_buys 采集上限模式）；**均未毕业**；premium_15≈0（T+15 价=T+0 回退）
- **是否依赖 top1/top5**：drop_top5 后 mean=+0.059，p>1000=0
- **可成交性**：100% 样本 T+15 使用 T+0 grid 回退；top20 全部 NEEDS_RPC_REPLAY
- **亏损端**：90% 约 -3%（成本档）；P≤-25%=4.1%；最大连败=100
- **小仓位曲线**：0.1% 仓位 backtest +19.7%（drop_top5 后 +9.8%），但基于不可成交 grid

**分类：UNTRADABLE_RIGHT_TAIL** — **不建议 forward paper**

**阻断指标**：
1. T+15 入场价 100% 为 grid 代理（非真实 15s 成交价）
2. 极端右尾与随机抽样 p>1000 无差异
3. top winners 无法通过可成交性代理
4. median 恒为 -3%（典型小亏档）
