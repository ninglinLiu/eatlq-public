# B5 KEYSTONE CHA 重验（离线回测 + archive 只读补证）

> research-only；核心回测 RPC=0；LP/golden 补证只读；不作 GO 判断。缺失证据按 adverse。

## 结论摘要

本报告只给证伪证据，不给 GO。forward/OOS 参数只用 W1 前数据拟合，W1–W3 原样验证。

## 真实成交价、无条件与分层基线

| 口径 | 窗口 | n | mean | alpha vs unconditional | top1移除 mean | fatal | D5挡掉/缺失 |
|---|---|---:|---:|---:|---:|---:|---:|
| unconditional | W1 | 194 | -73.65% | +0.00% | -78.52% | +82.47% | 0/0 |
| unconditional | W2 | 91 | -39.28% | +0.00% | -45.05% | +54.95% | 0/0 |
| unconditional | W3 | 146 | -78.46% | +0.00% | -81.57% | +78.77% | 0/0 |
| **unconditional** | **pooled** | **431** | **-68.02%** | **+0.00%** | **-73.02%** | **+75.41%** | — |
| friction_only | W1 | 165 | -69.83% | +3.81% | -75.53% | +81.21% | 0/0 |
| friction_only | W2 | 71 | -22.46% | +16.81% | -29.65% | +43.66% | 0/0 |
| friction_only | W3 | 37 | -14.85% | +63.61% | -19.88% | +29.73% | 0/0 |
| **friction_only** | **pooled** | **273** | **-50.06%** | **+15.30%** | **-55.72%** | **+64.47%** | — |
| keystone_full | W1 | 38 | +16.06% | +89.70% | +0.96% | +39.47% | 0/0 |
| keystone_full | W2 | 61 | -18.48% | +20.79% | -26.80% | +44.26% | 0/0 |
| keystone_full | W3 | 17 | -1.02% | +77.44% | -11.46% | +11.76% | 0/0 |
| **keystone_full** | **pooled** | **116** | **-4.61%** | **+51.67%** | **-13.95%** | **+37.93%** | — |
| keystone_full_d5_on | W1 | 38 | +3.38% | +77.03% | -12.06% | +47.37% | 5/5 |
| keystone_full_d5_on | W2 | 61 | -24.45% | +14.83% | -32.87% | +47.54% | 2/2 |
| keystone_full_d5_on | W3 | 17 | -28.62% | +49.84% | -40.79% | +35.29% | 6/6 |
| **keystone_full_d5_on** | **pooled** | **116** | **-15.94%** | **+40.33%** | **-25.48%** | **+45.69%** | — |
| keystone_general | W1 | 36 | -3.14% | +70.50% | -9.34% | +41.67% | 0/0 |
| keystone_general | W2 | 55 | -9.83% | +29.45% | -18.91% | +40.00% | 0/0 |
| keystone_general | W3 | 22 | -17.54% | +60.92% | -24.33% | +27.27% | 0/0 |
| **keystone_general** | **pooled** | **113** | **-9.20%** | **+48.65%** | **-16.59%** | **+38.05%** | — |

成交采用 decoded swap 的 `amount0_raw/amount1_raw` 平均执行价；0.001 WETH 容量、每尝试腿 $0.075 gas。限价从冻结 trailing/hard-stop 触发 tick 得到，之后约 60 分钟内第一笔方向、价格和容量均合格的真实卖单作为成交证据；无成交或缺失入场证据按 -100% 再扣已尝试腿 gas。`unconditional` 为无条件基线，`friction_only` 为同执行口径的分层基线。

注意：KEYSTONE 数据最大时间为 2026-06-01 22:23 UTC，故 W3 仅是冻结 B3 W3（截至 2026-06-08 18:29:40）的 partial-window/OOS，不外推为完整 W3。

## 防 rug 三杠杆

### 1. 快出场时序（跨 regime）

| 窗口 | 出场后 flip | 出场前/同块 flip | 未知 |
|---|---:|---:|---:|
| W1 | 11 | 1 | 0 |
| W2 | 40 | 2 | 0 |
| W3 | 2 | 0 | 7 |

W3 仅 2 个样本有 exit timing、7 个未知；未知按 adverse，不能从观测到的 0 个 before-exit 得出时序优势。

### 2. Forward LP monitor

全冻结标签 delayed rug=262，已见真实损失 4；offset≤300 的早翻转=22，其中真实损失=2。现有 checkpoint 只能在 flip 当块/之后检测，不能证明可执行的 pre-flip lead；保守可救回 **0**。

### 3. D5 原子门边际

冻结标签覆盖 644，其中 instant_hp=0。forward 选择 116 例，覆盖 103，覆盖交集内可验证拒绝=0，故覆盖交集的 D5 直接边际为 0；其余缺失按 -100% 不可卖并扣 gas，不作有利推断。开/关收益见上表。

## 迁移 RH 上界

`keystone_general` 使用相同学习器/训练窗，仅保留市价可得的价格与活动特征；其收益是可迁移性上界，而非异链实证。KEYSTONE 本版本未使用 holder/储备，故真值/代理偏差不适用。

## 右尾与 power

- keystone_full: `{'status': 'RUN', 'paths': 10000, 'seed': 20260714, 'terminal_p5': 75.4233625488439, 'max_drawdown_p95': 30.2069749934018}`
- keystone_full_d5_on: `{'status': 'RUN', 'paths': 10000, 'seed': 20260714, 'terminal_p5': 66.74215567178305, 'max_drawdown_p95': 36.528449091203214}`
- keystone_general: `{'status': 'RUN', 'paths': 10000, 'seed': 20260714, 'terminal_p5': 72.23181965905145, 'max_drawdown_p95': 30.402021261322147}`

n<20 的窗只描述，不判 merit。复用 B3 冻结数值门后，逐项结果为 `{'pooled_mean_gt_0': False, 'each_powered_regime_mean_ge_minus_10pct': False, 'at_least_2_powered_regimes_positive_mean_and_alpha': False, 'pooled_top1_positive_contribution_le_15pct': False, 'pooled_top1_removed_mean_gt_0': False, 'mc_terminal_p5_gt_100R': False, 'mc_mdd_p95_le_20R': False}`；证据状态 **FAIL**。这不是 GO 决策。

### Forward LP archive 补证

对 early≤300 或实际真损的并集 24 池做 pinned `WETH.balanceOf(pool)`：24 全测得，真损 4 例中 prewarning= 0、可证明新增救回=0；另有 2 例出现 prewarning，但策略此前已退出，非增量救回。

## Golden 与复现

本地 decoded 整数 golden 共 5 例，逐例含 pool、block、tx 和 amount0/amount1 raw integer。archive receipt+block 独立核对：`True`；输入 SHA-256 同存。

```bash
cd /opt/EATLQ
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 scripts/research/base_b5/b5_offline_reverify.py
python3 -m unittest tests/test_base_b5_offline.py
```
