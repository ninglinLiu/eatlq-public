# 预登记：Base 全 DEX × Keystone 加减换搜索

**日期**：2026-08-20，写于任何配方收益数字之前
**账户**：总仓 **$1,000**（约 0.5 ETH），单笔 **$S ∈ {25, 50}**，并行槽位 `floor(1000/S)`
**目标**：每月净赚 **$5,000**（袖子 5 倍）。算不出这条就不得叫 LEAD。

**LIVE_TRADING = FORBIDDEN。** 不改 Keystone 生产模型，不 ARM r3，不广播交易。

---

## 1 假设

在 Base 上，成交量大的 DEX 不只是 Uniswap V3。执行器已经会买卖 V2 / Aerodrome vAMM；
研究面板几乎只覆盖 `univ3` + `aero_cl`。Keystone 的生产规则（5 特征、`p_fatal≤0.27`、
−50%/4h）只在 V3 新池上试过加减的一半（只加、少减）。

**H1**：把场地扩到 V2 / vAMM / V4 / 其它有成交的 factory，并把每个旋钮双向动
（加、减、换、反转），在 $25/$50 上存在一族选择规则，其可成交净收益显著优于随机，
且按实测命中率能在 $1,000 上每月拿出 $5,000。

一万种是**配方空间的基数**，不是一万次完整原子回测。过不了便宜门的整族丢掉。

## 2 宇宙（点时）

- 链：仅 Base。Uniswap 没有 V1（Base 上）/ V5 / V6。
- 第一轮场地：普查成交量后截断的 factory，至少包括
  `univ3`、`aero_cl`、`univ2`、`aero_vamm`、`univ4`（若有成交）、以及榜上其余能命名的场。
- 面板尽量覆盖 2025-08-18 ~ 2026-08-17；某场没有回填则记缺口，不编特征。
- 单笔 $S 的点时成本：`cost_bps_med ≤ 100` 且库存可行。装不下 $S 的池不得进该尺寸的族。

## 3 配方空间（出数前枚举）

笛卡尔积，写入 `out/recipes.json`，计数必须在跑 Tier 1 之前报出：

| 轴 | 取值 |
|---|---|
| venue | 普查后的 5–8 个场 |
| size_usd | 25, 50 |
| horizon | 1m, 10m, 1h, 4h, ks4h（生产 14400s） |
| entry | ks_as_is, p_fatal_loose, p_fatal_tight, ks_complement, random |
| exit | time, tp_sl, ks_50_4h, sl_only, time_only |
| features | 生产 5 特征的全部非空子集 + {flow_only, path_only, liq_only} |

已在 **univ3 + 同一标签** 上跑过的 F8/F10/F11/F12 不重跑。换场地或换 $25/$50 容量门才算新配方。

禁止重开（同一对手方、同一宇宙）：C 做市、A3/A5 在 411 池小时条、跨所价差、排放补贴、cbBTC。
V4 Initialize 进场已有 `v4_alpha_candidate_no`：只做一格确认，不新开项目。

## 4 分层（缺一层不得报 LEAD）

1. **Tier 1**：纸面收益 + 随机对照。随机已打平或选择不更好 → 该族 `FAMILY_DEAD`。
2. **Tier 2**：$25/$50 点时成本与容量。装不下并行槽位一半的池 → `CAPACITY_DEAD`。
3. **Tier 3**：原子/真实卖出抽样，标签按 `keystone_r2_atomic/out/atomic_label_spec.md`
   （SUCCESS / DETERMINISTIC_REVERT / ECONOMICALLY_UNSELLABLE / INFRASTRUCTURE_UNKNOWN）。
   纸面右尾不算。
4. 正结果必须过 `/root/preflight/preflight.py`。费率/收入必须是测量值。

## 5 对照（缺一不可）

- 随机：同一场地、同一尺寸、同一持有、同样次数。
- Keystone 生产规则原样移植到该场地（特征缺失则记 `KS_INAPPLICABLE`，不得用别的特征顶替后仍叫「原样」）。

## 6 裁决

- `FAMILY_DEAD`：随机对照已非负且选择不显著更好，或原子上界 < 0。
- `CAPACITY_DEAD`：过门池装不下并行槽位的一半。
- `UNDERPOWERED`：区间跨零；必须报出要排除「每月 $5,000」还缺多少。
- `LEAD`：过随机、过容量、过原子、过 preflight、两个时间半段同号。
  **且**按实测命中率、止损/卖不出比例，能在 $1,000 上每月拿出 $5,000。
  算不出就降格。仍禁止交易。

## 7 美元门（LEAD 的硬前置）

并行槽位 `K = floor(1000/S)`。一个月内槽位循环次数由 horizon 决定。
令 `p` = 可成交命中率，`w` = 平均赢幅，`L` = 平均亏后剩余本金比例
（卖不出记 0）。月末净值必须 ≥ $6,000（本金 + $5,000）。做不到则不是 LEAD。

## 8 事前怀疑

1. V3 新池宇宙在更大尺寸上已经 `NO_VIABLE`； $25/$50 可能只是让更多薄池「看起来可成交」，
   卖出腿仍然死。
2. V2 / vAMM 的边若存在，更可能是容量幻觉或税币，不是知情流。
3. 一万格里会有假阳性；族级杀死优先于格子。
4. 每月 5 倍不是「松一点的 150% 月化」，是更硬的美元约束。
