# Base 全 DEX × Keystone 加减换 — R0 出数

**日期**：2026-08-20  
**预登记**：`/root/base_dex_search_r0/GOAL.md`（出数前冻结）  
**LIVE_TRADING = FORBIDDEN。** 不改生产 Keystone，不 ARM r3。

## 裁决

**NO LEAD。** 76,500 个配方、71 个族：0 个 `SURVIVE_T1`，0 个过美元门。  
在 $1,000 袖子、单笔 $25/$50 上，没有任何一族能每月净赚 $5,000。

未过滤 1e38 坏点的第一遍纸面曾把 `liq_only` 均值打到 10^37，那是第 46 轮同一类 tick 溢出，已按 `MAX_ABS_RET=5` 丢掉后再判。夹住坏点不是新假设。

## 账户算术（出数前写死，这里只代入）

| | $25 | $50 |
|---|---|---|
| 并行槽位 | 40 | 20 |
| 1h 每月循环 | 730 | 730 |
| 4h 每月循环 | 182 | 182 |
| 要月末 ≥ $6,000 的每笔 EV（复利） | 1h +0.25% / 4h +0.99% | 同左 |
| 要月末 ≥ $6,000 的每笔 EV（不复利重铺袖子） | 1h +0.69% / 4h +2.74% | 同左 |

CL 上最好的纸面格（`aero_cl` $50 4h `liq_only`）是 **−5 bps / 4h，上界仍负**。  
离线性门槛还差约 **2.8 个百分点 / 笔**；乘上 35.1% 卖不出之后，混合 EV ≈ **−35% / 笔**，一个月线性约 −$64,000，不是「差一点」。

## 普查（第一周那张表）

一年成交量截断后进配方的场：

| 场 | 成交笔数 | 便宜池 @$50 | 便宜池 @$25 |
|---|---|---|---|
| aero_cl | 194M | 194 | 200 |
| univ3 | 173M | 444 | 515 |
| sushi_v3 | 10.1M | 14 | 16 |
| alienbase_cl | 10.0M | 12 | 12 |
| other:0x38015d05 | 2.8M | 7 | 11 |
| other:0x70fe4a44 | 2.1M | 无 year-panel | 无 |
| univ2 | 1.1M | 275（代理） | 280 |
| aero_vamm | 0.98M | 2 | 2 |
| univ4 | 无 CH 表 | 确认格 | 确认格 |

`eatlq.aerodrome_vamm_swaps_mv` 与 `uniswap_v2_swaps_decoded` 是 26162/26162 对重叠。V2 成交从 2026-02-23 才有。  
`sushi_v3` / `alienbase_cl` 在 `edge.bars_universe_50` 里都记成 `unknown`（26 池），两场共用同一薄样本，不是独立宇宙。

## 配方空间

9 场 × 2 尺寸 × 5 时域 × 5 进场 × 5 出场 × 34 特征组 = **76,500**。  
分层砸，不是 76,500 次原子回测。

盖章：`FAMILY_DEAD` 15,980 · `KS_INAPPLICABLE` 32,640 · `DATA_GAP` 27,880 · `LEAD` 0。

生产 5 特征（`ns_pre` 等）不在一年面板上。`ks_as_is` / `p_fatal_*` / `ks_complement` 记 `KS_INAPPLICABLE`，没有用别的特征顶替后仍叫「原样」。  
加减换用三组替换（只留流量 / 只留路径 / 只留流动性）+ 成交量 + 随机，外加 4h 路径出场（时间 / 止盈止损 / −50% / 只止损）。

## Tier 1（去掉 |ret|>500% 之后）

所有已测 CL 族：`all paper rules have hi<0`。

| 场 × 尺寸 × 时域 | 随机 | 最好的选择规则 | 上界 |
|---|---|---|---|
| aero_cl $50 4h | −64 bps | liq_only −5 bps | −4 bps |
| aero_cl $50 1h | −64 bps | liq_only −7 bps | −6 bps |
| aero_cl $50 10m | −58 bps | liq_only −9 bps | −9 bps |
| univ3 $50 4h | −70 bps | liq_only −8 bps | −7 bps |
| univ3 $25 1h | −65 bps | liq_only −35 bps | −34 bps |
| sushi / alienbase（同一 unknown 26 池） | −38 至 −63 bps | 与随机无区别 | 全负 |

10m 用 `edge.rot_15m2` 一根 15 分钟条代理，收益按 sqrtPrice 比的平方。n ≈ 70 万–140 万，上界仍然全负。  
1m：无面板，`DATA_GAP`。  
univ4：确认格，Initialize 112/112 非正、中位 −2.6%，`v4_alpha_candidate_no`。  
`other:*`：有成交、无一年面板，`DATA_GAP`，不编特征。

V2 / vAMM：$25 univ2 便宜切片上**随机已经为正**（1h +2.2% [+1.7, +2.7]；4h +16.0% [+15.0, +17.0]），成交量/笔数选择与随机**数字相同**——不是选择边。aero_vamm 同口径为负（1h −84 bps）。$50 univ2 便宜条 <200，`UNDERPOWERED`。  
这正是预登记的族死条件：随机已非负且选择不更好。univ2 中位价 9e−12，+16% 纸面很像剩余灰尘跳价，不是可成交边。35.1% 卖不出混合后 4h EV ≈ **−24.7%**。

## Tier 2 / 3 / 美元门

没有 T1 幸存族，容量门和原子抽样没有对象可晋升。  
35.1% 卖不出（`base_stop_fill_r0`）若套在最好的 CL 纸面上，混合 EV 仍 ≈ −35%。  
美元门：`n_lead = 0`。没有正结果，不跑仓位级 preflight。

## 不重开

同一对手方、同一宇宙：C 做市、A3/A5 411 池小时条、跨所价差、排放、cbBTC、V4 Initialize、univ3 上已跑过的 F8/F10/F11/F12。  
本轮换了场地和 $25/$50 容量门，结论仍是死。不要用「再加第 6 个特征」重开。

## 文件

`GOAL.md` · `src/census_venues.py` · `src/v2_panel.py` · `src/recipes.py` · `src/tier1.py` · `src/tier2.py` · `src/tier3.py` · `src/dollar_gate.py` · `src/stamp.py`  
`out/venue_census.json` · `out/recipes.json` · `out/tier1.json` · `out/dollar_gate.json` · `out/recipe_verdicts.json` · `out/SUMMARY.json`
