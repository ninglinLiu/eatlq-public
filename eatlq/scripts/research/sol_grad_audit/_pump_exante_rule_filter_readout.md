# 旧 top5% 纯规则过滤矩阵（56 币，链上 real）

mega_proxy（R9）：`same_dev_same_buyer_cluster >= 0.95`

## 三个核心问题

**Q1 fast_flip>0.6（R1）**：删掉 fatal 9/24 (38%) | KKK2 kept=False | winner kept=0
**Q2 slope<0（R2）**：fatal removed 75% | KKK2 kept=False | winner kept=0
**Q3 dev>200（R3）**：fatal removed 46% | KKK2 kept=True (dev=4) | winner kept=1/3

## 规则矩阵

| Rule | retain | fatal↓ | winner | mega | KKK2 | p100 | p1000 | median | drop_top1 |
|------|--------|--------|--------|------|------|------|-------|--------|-----------|
| baseline | 56 | 0 | 3 | 1 | ✓ | 5.4% | 1.8% | -12.1% | -15.7% |
| R1_fast_flip_gt_0.6 | 42 | 9/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -7.3% | -17.5% |
| R2_slope_buys_lt_0 | 10 | 18/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -40.9% | -39.7% |
| R3_dev_created_gt_200 | 36 | 11/24 | 1/3 | 1/1 | ✓ | 2.8% | 2.8% | -4.6% | -17.4% |
| R4_bot_ratio_gt_0.35 | 32 | 12/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -9.1% | -16.8% |
| R4_bot_ratio_gt_0.40 | 35 | 12/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -6.4% | -15.3% |
| R4_bot_ratio_gt_0.50 | 40 | 11/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -6.5% | -14.9% |
| R5_R1_OR_R2 | 10 | 18/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -40.9% | -39.7% |
| R6_R1_OR_R2_OR_R3 | 5 | 21/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -46.9% | -46.2% |
| R7_R1_AND_R2 | 42 | 9/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -7.3% | -17.5% |
| R8_R1_OR_R2bot35 | 34 | 11/24 | 0/3 | 0/1 | ✗ | 0.0% | 0.0% | -9.7% | -18.4% |
| R9_R1orR2_unless_mega | 56 | 0/24 | 3/3 | 1/1 | ✓ | 5.4% | 1.8% | -12.1% | -15.7% |
| R10_fast_flip_gt_1.5 | 47 | 7/24 | 2/3 | 1/1 | ✓ | 4.3% | 2.1% | -6.6% | -13.1% |
| R13_flip0_slope_neg_dev200 | 48 | 3/24 | 3/3 | 1/1 | ✓ | 6.2% | 2.1% | -7.3% | -15.9% |

## 结论

- **R1/R2 单独不可用**：KKK2 与全部 winner 的 fast_flip≥1、slope=−1，与 fatal 重叠
- **R3 保留 KKK2 但误杀 2 个 winner**（dev>200）；高频 dev 不是纯负信号
- **R9（R1|R2 + mega_proxy 豁免）**：保留 KKK2 + 全部 winner，但 fatal 删除有限（cluster=1 的 fatal 也被豁免）

**综合最高分规则**：R3_dev_created_gt_200

### 补充规则（探索）
- **R10 flip>1.5**：保留 KKK2(flip=1.0)，删极端刷量 fatal；p100≈4.3%，drop_top1≈−13%
- **R13 flip≤0.6 & slope<0 & dev>200**：精准 fatal 画像，保留全部 winner+KKK2，但只删 3/24 fatal

### Core / Lottery 拆分（旧 top5 内）
- Core（dev≤200, n=36）：median=-4.6%，p1000=2.8%
- Lottery（cluster≥0.95 & dev≤50, n=21）：median=-2.4%，p1000=4.8%

**R9 mega_proxy 过宽**：旧 top5 里 21/56 满足 cluster≥0.95，fatal 也大量 cluster=1 → R9 等于不过滤

详见 `_pump_exante_rule_filter.csv`
