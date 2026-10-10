---
sop_applied: true
alpha_id: AR-GL2-C-EXOGENOUS-WEATHER-OOS
family: F6/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# GL2-C 外生天气门：冻结训练派生与严格 OOS 结果

> 纯历史只读重核；没有训练模型、下单、签名、广播、信号/Redis/live 修改或 GO。最终分级由审查 agent 逐行决定。

预注册 SHA256：`2ee3684425e66a42366b5f291c6fd016304d3c295bf263cdf8daecb1e0f662ea`。脚本确认该哈希与冻结锚完全一致后才计算结果。

## 人群与时间口径

- 主人群：GL1 冻结 KEYSTONE 选中 116 条；天气不参与选择。
- 次人群：GL1 同龄全宇宙 431 条；只作 secondary，不替代 KEYSTONE。
- 入场固定 +1 块，出场 +1/+5/+15 块；真成交优先，否则原子守卫精确报价；confirmed rug=-100%，unresolved 保留并逐格报分母。
- 每日天气只读 D 00:00 UTC 之前的值；训练 `<2026-04-15`，OOS `>=2026-04-15`。

## 五指标可用性与机器状态

| 指标 | 可用日 | 缺失日 | 状态 | 原因 |
|---|---:|---:|---|---|
| `pool_creation_rate_7d` | 102 | 0 | `OOS_UNPOWERED` | 本地冻结源覆盖 |
| `aggregate_weth_net_inflow_7d` | 0 | 102 | `UNAVAILABLE_NOT_TESTED` | UNAVAILABLE_NOT_TESTED: no local all-WETH-pool decoded swap source covers the full frozen calendar with auditable timestamps and WETH orientation |
| `eth_realized_volatility_7d` | 94 | 8 | `NO_STABLE_OOS_WIN` | 本地冻结源覆盖 |
| `base_gas_regime_7d` | 0 | 102 | `UNAVAILABLE_NOT_TESTED` | UNAVAILABLE_NOT_TESTED: no already-hashed full historical Base block-header gas-fill census; sparse candidate receipts are forbidden |
| `whole_universe_rug_rate_7d` | 0 | 102 | `UNAVAILABLE_NOT_TESTED` | UNAVAILABLE_NOT_TESTED: GL1 final five-evidence labels lack historical settlement timestamps needed to prove each rug was known strictly before D |

## 训练阈值与 OOS 开工占比

| 指标 | 训练方向 | 阈值 | 训练开日/占比 | OOS开日/占比 | 机器标签 |
|---|---|---:|---:|---:|---|
| `pool_creation_rate_7d` | high q=0.80 | 2830.685714 | 10/47 (21.3%) | 0/55 (0.0%) | `OOS_UNPOWERED` |
| `aggregate_weth_net_inflow_7d` | — | — | — | — | `UNAVAILABLE_NOT_TESTED` |
| `eth_realized_volatility_7d` | low q=0.50 | 0.02870375382 | 20/39 (51.3%) | 46/55 (83.6%) | `NO_STABLE_OOS_WIN` |
| `base_gas_regime_7d` | — | — | — | — | `UNAVAILABLE_NOT_TESTED` |
| `whole_universe_rug_rate_7d` | — | — | — | — | `UNAVAILABLE_NOT_TESTED` |

## `pool_creation_rate_7d`：逐窗分布

| 窗口/人群/出场 | true n/unresolved | mean | median | p10/p90 | win/fatal | random median/p95 | Δmedian | >p95 |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| OOS/KEYSTONE/+1 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| OOS/KEYSTONE/+5 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| OOS/KEYSTONE/+15 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| OOS/FULL_UNIVERSE_431/+1 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| OOS/FULL_UNIVERSE_431/+5 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| OOS/FULL_UNIVERSE_431/+15 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O1/KEYSTONE/+1 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O1/KEYSTONE/+5 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O1/KEYSTONE/+15 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O1/FULL_UNIVERSE_431/+1 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O1/FULL_UNIVERSE_431/+5 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O1/FULL_UNIVERSE_431/+15 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O2/KEYSTONE/+1 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O2/KEYSTONE/+5 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O2/KEYSTONE/+15 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O2/FULL_UNIVERSE_431/+1 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O2/FULL_UNIVERSE_431/+5 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |
| O2/FULL_UNIVERSE_431/+15 | 0/0 | — | — | —/— | —/— | —/— | — | 否 |

KEYSTONE +1/+1 去最大正收益 1%：原 n=0，实际移除 0 条，剩余 mean=—。

机器稳定规则：`OOS_UNPOWERED`；open_days_at_least_5=FAIL, primary_finite_n_at_least_8=FAIL, primary_plus1_gt_random_p95=FAIL, at_least_2_exits_gt_random_p95=FAIL, all_exit_deltas_positive=FAIL, at_least_2_powered_subwindows=FAIL, all_powered_subwindows_gt_random_median=FAIL, top1pct_removed_mean_positive=FAIL。

## `eth_realized_volatility_7d`：逐窗分布

| 窗口/人群/出场 | true n/unresolved | mean | median | p10/p90 | win/fatal | random median/p95 | Δmedian | >p95 |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| OOS/KEYSTONE/+1 | 22/2 | +15.12% | -8.01% | -36.40%/+75.05% | +40.91%/+0.00% | +19.14%/+26.77% | -4.03% | 否 |
| OOS/KEYSTONE/+5 | 22/2 | +17.22% | -7.62% | -36.40%/+75.05% | +40.91%/+0.00% | +21.00%/+29.08% | -3.78% | 否 |
| OOS/KEYSTONE/+15 | 22/2 | +17.54% | -6.93% | -32.60%/+75.05% | +40.91%/+0.00% | +21.35%/+29.52% | -3.82% | 否 |
| OOS/FULL_UNIVERSE_431/+1 | 132/24 | -30.36% | -12.80% | -106.77%/+12.78% | +17.42%/+18.94% | -27.31%/-22.43% | -3.04% | 否 |
| OOS/FULL_UNIVERSE_431/+5 | 131/25 | -29.99% | -13.53% | -106.80%/+13.03% | +17.56%/+19.08% | -26.97%/-21.93% | -3.03% | 否 |
| OOS/FULL_UNIVERSE_431/+15 | 131/25 | -29.66% | -12.07% | -106.53%/+12.71% | +17.56%/+19.08% | -26.64%/-21.60% | -3.02% | 否 |
| O1/KEYSTONE/+1 | 18/1 | +17.51% | -8.39% | -40.41%/+98.76% | +44.44%/+0.00% | +21.72%/+31.71% | -4.21% | 否 |
| O1/KEYSTONE/+5 | 18/1 | +20.08% | -7.79% | -40.41%/+109.91% | +44.44%/+0.00% | +23.92%/+34.32% | -3.84% | 否 |
| O1/KEYSTONE/+15 | 18/1 | +20.45% | -6.64% | -37.45%/+114.66% | +44.44%/+0.00% | +24.28%/+34.85% | -3.83% | 否 |
| O1/FULL_UNIVERSE_431/+1 | 35/2 | -1.11% | -8.60% | -66.45%/+49.15% | +40.00%/+5.71% | +1.68%/+7.05% | -2.79% | 否 |
| O1/FULL_UNIVERSE_431/+5 | 35/2 | +0.44% | -8.29% | -68.10%/+49.45% | +40.00%/+5.71% | +2.86%/+8.39% | -2.41% | 否 |
| O1/FULL_UNIVERSE_431/+15 | 35/2 | +0.78% | -8.29% | -68.10%/+49.67% | +40.00%/+5.71% | +3.19%/+8.77% | -2.41% | 否 |
| O2/KEYSTONE/+1 | 4/1 | +4.34% | -7.07% | -12.65%/+30.45% | +25.00%/+0.00% | +4.34%/+8.41% | +0.00% | 否 |
| O2/KEYSTONE/+5 | 4/1 | +4.36% | -7.03% | -12.62%/+30.45% | +25.00%/+0.00% | +4.36%/+8.41% | +0.00% | 否 |
| O2/KEYSTONE/+15 | 4/1 | +4.41% | -6.93% | -12.63%/+30.52% | +25.00%/+0.00% | +4.41%/+8.49% | +0.00% | 否 |
| O2/FULL_UNIVERSE_431/+1 | 97/22 | -40.91% | -15.22% | -107.02%/+0.00% | +9.28%/+23.71% | -41.12%/-36.43% | +0.21% | 否 |
| O2/FULL_UNIVERSE_431/+5 | 96/23 | -41.09% | -15.41% | -107.02%/+0.00% | +9.38%/+23.96% | -41.29%/-36.55% | +0.20% | 否 |
| O2/FULL_UNIVERSE_431/+15 | 96/23 | -40.77% | -15.41% | -107.02%/+0.00% | +9.38%/+23.96% | -40.94%/-36.15% | +0.18% | 否 |

KEYSTONE +1/+1 去最大正收益 1%：原 n=22，实际移除 1 条，剩余 mean=+1.58%。

机器稳定规则：`NO_STABLE_OOS_WIN`；open_days_at_least_5=PASS, primary_finite_n_at_least_8=PASS, primary_plus1_gt_random_p95=FAIL, at_least_2_exits_gt_random_p95=FAIL, all_exit_deltas_positive=FAIL, at_least_2_powered_subwindows=PASS, all_powered_subwindows_gt_random_median=FAIL, top1pct_removed_mean_positive=PASS。

## 限制与审查边界

- 聚合 WETH 净流入、Base gas regime 和全宇宙历史 rug rate 没有满足冻结的无前视全覆盖源，故不是零、不是失败，而是 `UNAVAILABLE_NOT_TESTED`。
- ETH daily 源是既有本地冻结文件；早期不足八个 close 的日期保持 missing。
- 全宇宙 431 行锚定本次运行前已有的 reaudit10 core/five-evidence 输入，没有等待或吸收 GL2-A 后续结果，避免看结果后改 C。
- 机器只发 `OOS_STABLE_WIN/NO_STABLE_OOS_WIN/OOS_UNPOWERED/UNAVAILABLE_NOT_TESTED`，不定 GL1 grade，不作 GO。

## 可复现

运行：`python3 scripts/research/base_gl2/gl2_c_weather_oos.py`。随机比较器每指标 10,000 次，seed 基础值 `20260715`，严格保持同 eligible 日与同开工天数。
