# Lottery vs Control 资金曲线对比（daily block bootstrap）

Lottery n=455 | Control n=455 | 500笔 × 10000 sims

## Baseline（daily block）

| 组 | 仓位 | 500 med | p5 | p25 | p75 | p95 | MDD p95 | ruin | drop_top1 med | drop_top3 med |
|----|------|---------|----|----|-----|-----|---------|------|---------------|---------------|
| lottery_v2 | 0.02pct | +2.6% | +1.5% | +2.1% | +3.0% | +3.4% | 0.3% | 0.0% | +1.4% | +0.1% |
| lottery_v2 | 0.05pct | +6.7% | +3.7% | +5.4% | +7.7% | +8.8% | 0.6% | 0.0% | +3.5% | +0.3% |
| lottery_v2 | 0.10pct | +13.7% | +7.4% | +11.0% | +15.8% | +18.2% | 1.2% | 0.0% | +7.0% | +0.6% |
| lottery_v2 | 0.15pct | +21.2% | +11.2% | +16.8% | +24.4% | +28.3% | 1.8% | 0.0% | +10.6% | +0.8% |
| lottery_v2 | 0.25pct | +37.1% | +19.1% | +29.0% | +42.9% | +50.5% | 3.2% | 0.0% | +18.0% | +1.3% |
| matched_control | 0.02pct | +0.5% | +0.3% | +0.4% | +0.8% | +1.2% | 0.3% | 0.0% | +0.4% | +0.1% |
| matched_control | 0.05pct | +1.3% | +0.9% | +1.1% | +1.9% | +3.1% | 0.6% | 0.0% | +1.1% | +0.1% |
| matched_control | 0.10pct | +2.6% | +1.7% | +2.2% | +3.9% | +6.3% | 1.3% | 0.0% | +2.2% | +0.2% |
| matched_control | 0.15pct | +3.9% | +2.6% | +3.2% | +5.8% | +9.6% | 1.9% | 0.0% | +3.3% | +0.3% |
| matched_control | 0.25pct | +6.4% | +4.2% | +5.4% | +9.8% | +16.2% | 3.2% | 0.0% | +5.5% | +0.5% |

## 压力测试（0.05% 仓位，daily block）

| 组 | stress | median | p5 | MDD p95 | drop_top1 |
|----|--------|--------|----|---------|-----------|
| lottery_v2 | remove_top1 | +3.5% | +1.8% | 0.7% | +0.3% |
| lottery_v2 | remove_top3 | +0.3% | -0.9% | 1.2% | -0.1% |
| lottery_v2 | remove_top5 | -0.8% | -1.6% | 1.7% | -0.8% |
| lottery_v2 | no_p1000 | +0.3% | -0.9% | 1.2% | -0.5% |
| lottery_v2 | halve_mega | +3.5% | +1.8% | 0.6% | +0.3% |
| matched_control | remove_top1 | +1.1% | -0.2% | 1.2% | +0.2% |
| matched_control | remove_top3 | +0.1% | -0.6% | 1.4% | -0.5% |
| matched_control | remove_top5 | -0.5% | -1.1% | 1.7% | -1.0% |
| matched_control | no_p1000 | +0.2% | -0.6% | 1.4% | +0.1% |
| matched_control | halve_mega | +1.1% | -0.1% | 1.2% | +0.2% |
| lottery_v2 | baseline_iid | +6.3% | +2.2% | 0.8% | +4.1% |
| matched_control | baseline_iid | +2.2% | -0.3% | 1.7% | +1.6% |

## 结论

Lottery median 更好 | Lottery p5 更好 | Lottery 回撤更低
