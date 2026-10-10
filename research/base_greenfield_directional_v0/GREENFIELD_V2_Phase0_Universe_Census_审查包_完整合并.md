# Base Greenfield V2 Phase 0 — Universe Viability Census 审查包（单文件合并）

> 生成时间：2026-07-20  
> 项目根：`/root/base_greenfield_directional_v0`  
> 角色：**Base Greenfield V2 Orthogonal Directional Research Agent**  
> 阶段：`PHASE_0_UNIVERSE_VIABILITY_CENSUS`  
> **裁决：`NO_VIABLE_BASE_NEW_POOL_UNIVERSE`**  
> **停止研究：`stop_research=true`**（未进入 Phase 1–4；未训 Alpha；未做 confirmation）  
> **部署：禁止**（`deploy_allowed=false`）

---

## 0. 为什么 Codex 终端「看起来没显示全」

不是跑丢了，是 **终端 UI 把宽表截断了**：

1. Codex 最终回复里的表格只印出了 **1–6h / 6–24h / 1–3d** 三行，后面跟了一个孤立的 `|`，**`3–7d` 行和负对照 `15–60m` 被截掉**。
2. 完整数据一直在磁盘产物里：`out_v2/PHASE0_UNIVERSE_CENSUS.csv`（5 个 bucket）+ `PHASE0_FINAL.json`。
3. 本文件把表和门槛对照 **完整展开**，不依赖终端宽度。

---

## 1. 一句话结论

这轮 **不做策略、不训模型**，只问：Base 新池子在几个年龄桶上，样本供给 + 可买卖性是否够格开新研究。

结果：**四个正式桶全部未过冻结门槛** → `NO_VIABLE_BASE_NEW_POOL_UNIVERSE`。  
按协议 **停在 Alpha 训练之前**。`15–60m` 只做关闭的负对照，不参与晋级。

主因不是「完全买不了」，而是同时踩中：

- **样本太少**（独立机会 ~228–243，门槛 ≥300；每周 ~26–28，门槛 ≥30）
- **买成功率不够**（约 41–61%，门槛 ≥90%）
- **未来永久不可卖比例过高**（约 54–65%，门槛 ≤10%）
- **生产深度通过率不够**（约 29–52%，门槛 ≥80%）
- 部分桶 **infra unknown** 超标（6–24h 15.7%、3–7d 36.2%，门槛 ≤5%）

---

## 2. 路径索引（`out_v2/`）

| 产物 | 路径 | 说明 |
|---|---|---|
| 终裁 | `out_v2/PHASE0_FINAL.json` | verdict + 逐门槛 true/false + 分布分位 |
| 宇宙汇总表 | `out_v2/PHASE0_UNIVERSE_CENSUS.csv` | 5 bucket 全指标 |
| 机会明细 | `out_v2/PHASE0_CENSUS_OPPORTUNITIES.parquet` | 2601 行（含负对照） |
| 复现 | `out_v2/REPRODUCE.md` | |
| 日志 | `out_v2/RUN.log` | |
| 冻结配置 | `configs/v2_phase0_freeze.json` | 门槛事先冻结 |
| 代码 | `src/v2/phase0_census.py` | |

**未生成**：Phase 1–4 任何数据集 / 模型 / OOF / confirmation 产物。

隔离声明（来自 FINAL）：`old_line_inputs_used=false`（不读已关闭的 KEYSTONE/F10/G2/C3/P2）。

---

## 3. 冻结门槛（事先写死，不按结果改）

窗口：LABEL_DESIGN `2025-10-01T00:00:00Z` → `2025-12-01T14:30:00Z`  
名义金额：`amount_in_wei=16666666666666666`  
未来普查地平线：`21600s`（6h）

```json
{
  "min_independent_opportunities": 300,
  "min_opportunities_per_week": 30.0,
  "min_pool_breadth": 300,
  "min_token_breadth": 250,
  "min_deployer_breadth": 50,
  "min_deployer_coverage": 0.8,
  "min_canonical_metadata_coverage": 0.98,
  "min_atomic_buy_success": 0.9,
  "min_current_atomic_sell_success": 0.85,
  "max_future_permanent_unsellable_rate": 0.1,
  "max_infrastructure_unknown_rate": 0.05,
  "min_future_sell_resolution_rate": 0.85,
  "min_production_depth_pass_rate": 0.8
}
```

年龄锚点（秒）：

```json
{
  "15-60m_NEGATIVE_CONTROL": 1800,
  "1-6h": 10800,
  "6-24h": 43200,
  "1-3d": 172800,
  "3-7d": 432000
}
```

禁止用 MFE/MAE/实现收益来挑 universe；`15-60m` 仅负对照。

---

## 4. 完整宇宙表（Codex 没显示全的那张）

| Universe | PIT opps | /week | Buy success | Current sell | Future unsellable | Infra unknown | Depth pass | viability_pass |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1-6h | 228 | 25.91 | 61.40% | 88.57% | 54.29% | 6.58% | 52.19% | False |
| 6-24h | 229 | 26.02 | 52.40% | 82.50% | 57.50% | 15.72% | 41.48% | False |
| 1-3d | 238 | 27.04 | 60.08% | 78.32% | 63.64% | 7.98% | 44.12% | False |
| 3-7d | 243 | 27.61 | 40.74% | 76.77% | 64.65% | 36.21% | 29.22% | False |
| 15-60m_NEGATIVE_CONTROL | 181 | 20.57 | 81.77% | 97.97% | 64.19% | 3.31% | 77.35% | False |

补充广度 / 解析率：

| Universe | pool_breadth | token_breadth | deployer_breadth | canonical_cov | sell_resolution |
|---|---:|---:|---:|---:|---:|
| 1-6h | 228 | 228 | 202 | 100.00% | 93.57% |
| 6-24h | 229 | 229 | 201 | 100.00% | 93.33% |
| 1-3d | 238 | 238 | 207 | 100.00% | 90.21% |
| 3-7d | 243 | 243 | 212 | 100.00% | 88.89% |
| 15-60m_NEGATIVE_CONTROL | 181 | 181 | 173 | 100.00% | 97.97% |

CSV 原文：

```csv
age_bucket,raw_independent_opportunities,independent_opportunities,opportunities_per_day,opportunities_per_week,concurrent_q50,concurrent_q90,concurrent_q99,concurrent_max,canonical_metadata_coverage,atomic_buy_success,current_atomic_sell_success,future_permanent_unsellable_rate,future_sell_resolution_rate,infrastructure_unknown_rate,production_depth_pass_rate,pool_breadth,token_breadth,deployer_breadth,deployer_coverage,entry_slippage_q50,entry_slippage_q95,exit_slippage_q50,exit_slippage_q95,executable_depth_weth_q50,executable_depth_weth_q05,mfe_q25,mfe_q50,mfe_q75,mae_q25,mae_q50,mae_q75,viability_pass,promoted
1-3d,510,238,3.863375042272574,27.04362529590802,32.0,54.30000000000001,60.0,61,1.0,0.6008403361344538,0.7832167832167832,0.6363636363636364,0.9020979020979021,0.07983193277310924,0.4411764705882353,238,238,207,1.0,0.02765386832654171,0.996984296800928,0.9717440285298172,0.9999999999999615,0.06666666666666667,0.0,0.0,0.007149642523830759,0.16864156806210595,-0.09964501783213078,-0.014799260049328003,0.0,False,False
1-6h,527,228,3.701048359824146,25.907338518769023,26.5,48.30000000000001,56.0,56,1.0,0.6140350877192983,0.8857142857142857,0.5428571428571428,0.9357142857142857,0.06578947368421052,0.5219298245614035,228,228,202,1.0,0.027574153323759454,0.0771950962225377,0.6342877708713466,0.9999999999943463,0.06666666666666667,0.0,0.0,0.0,0.030898455102988873,-0.1738663068295207,-0.0007999600026663786,0.0,False,False
15-60m_NEGATIVE_CONTROL,544,181,2.938112952316537,20.56679066621576,19.0,30.0,34.19999999999999,35,1.0,0.8176795580110497,0.9797297297297297,0.6418918918918919,0.9797297297297297,0.03314917127071823,0.7734806629834254,181,181,173,1.0,0.010585636998938497,0.04952997864803786,0.010553445440474984,0.9923535856656797,0.13333333333333333,0.0,0.0007499625024997298,0.2549372533497415,0.7820358988566349,-0.7886355688786326,-0.15779211052594316,-0.01432428379774484,False,False
3-7d,510,243,3.9445383834967873,27.61176868447751,32.0,55.0,61.0,62,1.0,0.4074074074074074,0.7676767676767676,0.6464646464646465,0.8888888888888888,0.36213991769547327,0.29218106995884774,243,243,212,1.0,0.027654640778380646,0.9980958293441154,0.9745272637818413,0.9999999999999847,0.0,0.0,-9.999500033329732e-05,0.0,0.04239788014131807,-0.0670966452236425,-0.0003999800013331893,0.0,False,False
6-24h,510,229,3.717281028068989,26.020967196482925,30.0,54.0,60.0,61,1.0,0.5240174672489083,0.825,0.575,0.9333333333333333,0.1572052401746725,0.4148471615720524,229,229,201,1.0,0.02765006591677005,0.7851079983736607,0.9190951084205606,0.9999999999999818,0.0,0.0,-0.00012499375041662164,0.0,0.020348982567826003,-0.04652267390506658,-0.0019499025064992977,0.0,False,False
```

---

## 5. 门槛对照（✗ = 未过）

| Gate | Threshold | 1-6h | 6-24h | 1-3d | 3-7d |
|---|---|---|---|---|---|
| `min_independent_opportunities` | ≥ 300 | ✗ 228 | ✗ 229 | ✗ 238 | ✗ 243 |
| `min_opportunities_per_week` | ≥ 30.0 | ✗ 25.91 | ✗ 26.02 | ✗ 27.04 | ✗ 27.61 |
| `min_pool_breadth` | ≥ 300 | ✗ 228 | ✗ 229 | ✗ 238 | ✗ 243 |
| `min_token_breadth` | ≥ 250 | ✗ 228 | ✗ 229 | ✗ 238 | ✗ 243 |
| `min_deployer_breadth` | ≥ 50 | ✓ 202 | ✓ 201 | ✓ 207 | ✓ 212 |
| `min_deployer_coverage` | ≥ 0.8 | ✓ 100.00% | ✓ 100.00% | ✓ 100.00% | ✓ 100.00% |
| `min_canonical_metadata_coverage` | ≥ 0.98 | ✓ 100.00% | ✓ 100.00% | ✓ 100.00% | ✓ 100.00% |
| `min_atomic_buy_success` | ≥ 0.9 | ✗ 61.40% | ✗ 52.40% | ✗ 60.08% | ✗ 40.74% |
| `min_current_atomic_sell_success` | ≥ 0.85 | ✓ 88.57% | ✗ 82.50% | ✗ 78.32% | ✗ 76.77% |
| `max_future_permanent_unsellable_rate` | ≤ 0.1 | ✗ 54.29% | ✗ 57.50% | ✗ 63.64% | ✗ 64.65% |
| `max_infrastructure_unknown_rate` | ≤ 0.05 | ✗ 6.58% | ✗ 15.72% | ✗ 7.98% | ✗ 36.21% |
| `min_future_sell_resolution_rate` | ≥ 0.85 | ✓ 93.57% | ✓ 93.33% | ✓ 90.21% | ✓ 88.89% |
| `min_production_depth_pass_rate` | ≥ 0.8 | ✗ 52.19% | ✗ 41.48% | ✗ 44.12% | ✗ 29.22% |

### 5.1 各桶失败清单

| Bucket | Failed gates |
|---|---|
| 1-3d | min_independent_opportunities, min_opportunities_per_week, min_pool_breadth, min_token_breadth, min_atomic_buy_success, min_current_atomic_sell_success, max_future_permanent_unsellable_rate, max_infrastructure_unknown_rate, min_production_depth_pass_rate |
| 1-6h | min_independent_opportunities, min_opportunities_per_week, min_pool_breadth, min_token_breadth, min_atomic_buy_success, max_future_permanent_unsellable_rate, max_infrastructure_unknown_rate, min_production_depth_pass_rate |
| 15-60m_NEGATIVE_CONTROL | CLOSED_NEGATIVE_CONTROL (not eligible) |
| 3-7d | min_independent_opportunities, min_opportunities_per_week, min_pool_breadth, min_token_breadth, min_atomic_buy_success, min_current_atomic_sell_success, max_future_permanent_unsellable_rate, max_infrastructure_unknown_rate, min_production_depth_pass_rate |
| 6-24h | min_independent_opportunities, min_opportunities_per_week, min_pool_breadth, min_token_breadth, min_atomic_buy_success, min_current_atomic_sell_success, max_future_permanent_unsellable_rate, max_infrastructure_unknown_rate, min_production_depth_pass_rate |

### 5.2 用人话：每个正式桶为什么挂

| Bucket | 用人话 |
|---|---|
| **1–6h** | 机会不够（228/周26）、买成率只有 61%、未来永久不可卖 54%、深度通过 52%；当前卖成功率 89% 是少数过线项之一 |
| **6–24h** | 机会不够；买 52%；当前卖 82.5% 略低于 85%；未来不可卖 57.5%；infra unknown 15.7%；深度 41% |
| **1–3d** | 机会不够；买 60%；当前卖 78%；未来不可卖 64%；深度 44% |
| **3–7d** | 机会不够；买仅 41%；当前卖 77%；未来不可卖 65%；**infra unknown 36%**；深度仅 29% |
| **15–60m 负对照** | 故意不晋级；可交易性相对最好（买 82%、当前卖 98%），但仍有高未来不可卖（64%），且样本/深度也未达正式门槛 |

---

## 6. PHASE0_FINAL.json 关键字段

| 字段 | 值 |
|---|---|
| `verdict` | `NO_VIABLE_BASE_NEW_POOL_UNIVERSE` |
| `promoted_universe` | `null` |
| `stop_research` | `true` |
| `alpha_models_trained` | `false` |
| `old_line_inputs_used` | `false` |
| `deploy_allowed` | `false` |
| `freeze_sha256` | `b0fa7b1b823fe28f1168e552e5b6ac6d5a87c880f1fc1edb4fda5bda807ca3e7` |

### 6.1 终裁全文

```json
{
  "created_utc": "2026-07-20T10:15:20Z",
  "role": "Base Greenfield V2 Orthogonal Directional Research Agent",
  "phase": "PHASE_0_UNIVERSE_VIABILITY_CENSUS",
  "verdict": "NO_VIABLE_BASE_NEW_POOL_UNIVERSE",
  "promoted_universe": null,
  "stop_research": true,
  "alpha_models_trained": false,
  "old_line_inputs_used": false,
  "deploy_allowed": false,
  "freeze_sha256": "b0fa7b1b823fe28f1168e552e5b6ac6d5a87c880f1fc1edb4fda5bda807ca3e7",
  "viability_checks": {
    "1-3d": {
      "min_independent_opportunities": false,
      "min_opportunities_per_week": false,
      "min_pool_breadth": false,
      "min_token_breadth": false,
      "min_deployer_breadth": true,
      "min_deployer_coverage": true,
      "min_canonical_metadata_coverage": true,
      "min_atomic_buy_success": false,
      "min_current_atomic_sell_success": false,
      "max_future_permanent_unsellable_rate": false,
      "max_infrastructure_unknown_rate": false,
      "min_future_sell_resolution_rate": true,
      "min_production_depth_pass_rate": false,
      "eligible_for_promotion": false
    },
    "1-6h": {
      "min_independent_opportunities": false,
      "min_opportunities_per_week": false,
      "min_pool_breadth": false,
      "min_token_breadth": false,
      "min_deployer_breadth": true,
      "min_deployer_coverage": true,
      "min_canonical_metadata_coverage": true,
      "min_atomic_buy_success": false,
      "min_current_atomic_sell_success": true,
      "max_future_permanent_unsellable_rate": false,
      "max_infrastructure_unknown_rate": false,
      "min_future_sell_resolution_rate": true,
      "min_production_depth_pass_rate": false,
      "eligible_for_promotion": false
    },
    "15-60m_NEGATIVE_CONTROL": {
      "eligible_for_promotion": false,
      "reason": "CLOSED_NEGATIVE_CONTROL"
    },
    "3-7d": {
      "min_independent_opportunities": false,
      "min_opportunities_per_week": false,
      "min_pool_breadth": false,
      "min_token_breadth": false,
      "min_deployer_breadth": true,
      "min_deployer_coverage": true,
      "min_canonical_metadata_coverage": true,
      "min_atomic_buy_success": false,
      "min_current_atomic_sell_success": false,
      "max_future_permanent_unsellable_rate": false,
      "max_infrastructure_unknown_rate": false,
      "min_future_sell_resolution_rate": true,
      "min_production_depth_pass_rate": false,
      "eligible_for_promotion": false
    },
    "6-24h": {
      "min_independent_opportunities": false,
      "min_opportunities_per_week": false,
      "min_pool_breadth": false,
      "min_token_breadth": false,
      "min_deployer_breadth": true,
      "min_deployer_coverage": true,
      "min_canonical_metadata_coverage": true,
      "min_atomic_buy_success": false,
      "min_current_atomic_sell_success": false,
      "max_future_permanent_unsellable_rate": false,
      "max_infrastructure_unknown_rate": false,
      "min_future_sell_resolution_rate": true,
      "min_production_depth_pass_rate": false,
      "eligible_for_promotion": false
    }
  },
  "distribution_details": {
    "1-3d": {
      "concurrent_candidate_count": {
        "n": 238,
        "q05": 6.850000000000001,
        "q25": 23.0,
        "q50": 32.0,
        "q75": 42.0,
        "q95": 58.0
      },
      "entry_slippage": {
        "n": 143,
        "q05": 0.004483386126199374,
        "q25": 0.02636823312235792,
        "q50": 0.02765386832654171,
        "q75": 0.027654826334855387,
        "q95": 0.996984296800928
      },
      "current_exit_slippage": {
        "n": 112,
        "q05": 0.012259262968086677,
        "q25": 0.5017597666093947,
        "q50": 0.9717440285298172,
        "q75": 0.9995092579183604,
        "q95": 0.9999999999999615
      },
      "future_exit_slippage": {
        "n": 112,
        "q05": 0.012489408875588942,
        "q25": 0.5133093153801183,
        "q50": 0.9717440285298172,
        "q75": 0.9995092579183604,
        "q95": 0.9999999999999615
      },
      "executable_depth_weth": {
        "n": 238,
        "q05": 0.0,
        "q25": 0.0,
        "q50": 0.06666666666666667,
        "q75": 0.06666666666666667,
        "q95": 0.13333333333333333
      },
      "mfe_6h": {
        "n": 22,
        "q05": -0.013374331294578516,
        "q25": 0.0,
        "q50": 0.007149642523830759,
        "q75": 0.16864156806210595,
        "q95": 2.1690165509796344
      },
      "mae_6h": {
        "n": 22,
        "q05": -0.5811659421871073,
        "q25": -0.09964501783213078,
        "q50": -0.014799260049328003,
        "q75": 0.0,
        "q95": -0.0
      }
    },
    "1-6h": {
      "concurrent_candidate_count": {
        "n": 228,
        "q05": 4.350000000000001,
        "q25": 19.0,
        "q50": 26.5,
        "q75": 38.25,
        "q95": 53.64999999999998
      },
      "entry_slippage": {
        "n": 140,
        "q05": 0.0029815637386000128,
        "q25": 0.013104097634994455,
        "q50": 0.027574153323759454,
        "q75": 0.02765482632738883,
        "q95": 0.0771950962225377
      },
      "current_exit_slippage": {
        "n": 124,
        "q05": 0.0032417870162057883,
        "q25": 0.020395085052648948,
        "q50": 0.6342877708713466,
        "q75": 0.9757180700989921,
        "q95": 0.9999999999943463
      },
      "future_exit_slippage": {
        "n": 103,
        "q05": 0.010978173701236027,
        "q25": 0.03611215722226019,
        "q50": 0.8002241486592804,
        "q75": 0.9879740674099264,
        "q95": 0.9999999999999754
      },
      "executable_depth_weth": {
        "n": 228,
        "q05": 0.0,
        "q25": 0.0,
        "q50": 0.06666666666666667,
        "q75": 0.06666666666666667,
        "q95": 0.13333333333333333
      },
      "mfe_6h": {
        "n": 98,
        "q05": -0.02656367183854043,
        "q25": 0.0,
        "q50": 0.0,
        "q75": 0.030898455102988873,
        "q95": 0.39942502908133937
      },
      "mae_6h": {
        "n": 98,
        "q05": -2.255987202519521,
        "q25": -0.1738663068295207,
        "q50": -0.0007999600026663786,
        "q75": 0.0,
        "q95": 0.0001299935004332848
      }
    },
    "15-60m_NEGATIVE_CONTROL": {
      "concurrent_candidate_count": {
        "n": 181,
        "q05": 4.0,
        "q25": 14.0,
        "q50": 19.0,
        "q75": 25.0,
        "q95": 31.0
      },
      "entry_slippage": {
        "n": 148,
        "q05": 0.0007110866461495614,
        "q25": 0.0035342049981339496,
        "q50": 0.010585636998938497,
        "q75": 0.026193665085239792,
        "q95": 0.04952997864803786
      },
      "current_exit_slippage": {
        "n": 145,
        "q05": 0.0007010210319216004,
        "q25": 0.003505204182189914,
        "q50": 0.010553445440474984,
        "q75": 0.026655554091273403,
        "q95": 0.9923535856656797
      },
      "future_exit_slippage": {
        "n": 76,
        "q05": 0.0034571447422871715,
        "q25": 0.013673355678534255,
        "q50": 0.048396652666563234,
        "q75": 0.8997218818118493,
        "q95": 0.9999999999746554
      },
      "executable_depth_weth": {
        "n": 181,
        "q05": 0.0,
        "q25": 0.06666666666666667,
        "q50": 0.13333333333333333,
        "q75": 0.13333333333333333,
        "q95": 0.13333333333333333
      },
      "mfe_6h": {
        "n": 142,
        "q05": -0.008789560529296836,
        "q25": 0.0007499625024997298,
        "q50": 0.2549372533497415,
        "q75": 0.7820358988566349,
        "q95": 2.1472076414069434
      },
      "mae_6h": {
        "n": 142,
        "q05": -2.585015751366219,
        "q25": -0.7886355688786326,
        "q50": -0.15779211052594316,
        "q75": -0.01432428379774484,
        "q95": 0.0
      }
    },
    "3-7d": {
      "concurrent_candidate_count": {
        "n": 243,
        "q05": 6.100000000000001,
        "q25": 23.0,
        "q50": 32.0,
        "q75": 43.0,
        "q95": 58.89999999999998
      },
      "entry_slippage": {
        "n": 99,
        "q05": 0.004645302047622391,
        "q25": 0.02724471423358249,
        "q50": 0.027654640778380646,
        "q75": 0.027654826334855387,
        "q95": 0.9980958293441154
      },
      "current_exit_slippage": {
        "n": 76,
        "q05": 0.012744438704417327,
        "q25": 0.6837405955065794,
        "q50": 0.9745272637818413,
        "q75": 0.9998709708341479,
        "q95": 0.9999999999999847
      },
      "future_exit_slippage": {
        "n": 76,
        "q05": 0.012607477034893733,
        "q25": 0.5121302096514888,
        "q50": 0.9745272637818413,
        "q75": 0.9998936947663095,
        "q95": 0.9999999999999847
      },
      "executable_depth_weth": {
        "n": 243,
        "q05": 0.0,
        "q25": 0.0,
        "q50": 0.0,
        "q75": 0.06666666666666667,
        "q95": 0.13333333333333333
      },
      "mfe_6h": {
        "n": 25,
        "q05": -0.011959402039862357,
        "q25": -9.999500033329732e-05,
        "q50": 0.0,
        "q75": 0.04239788014131807,
        "q95": 1.513584322044985
      },
      "mae_6h": {
        "n": 25,
        "q05": -0.2578871058595738,
        "q25": -0.0670966452236425,
        "q50": -0.0003999800013331893,
        "q75": 0.0,
        "q95": -0.0
      }
    },
    "6-24h": {
      "concurrent_candidate_count": {
        "n": 229,
        "q05": 6.4,
        "q25": 22.0,
        "q50": 30.0,
        "q75": 42.0,
        "q95": 57.599999999999994
      },
      "entry_slippage": {
        "n": 120,
        "q05": 0.004075613069370559,
        "q25": 0.026291648555788444,
        "q50": 0.02765006591677005,
        "q75": 0.027654826334855387,
        "q95": 0.7851079983736607
      },
      "current_exit_slippage": {
        "n": 99,
        "q05": 0.011597352250832804,
        "q25": 0.23116691294081027,
        "q50": 0.9190951084205606,
        "q75": 0.9979840855081054,
        "q95": 0.9999999999999818
      },
      "future_exit_slippage": {
        "n": 100,
        "q05": 0.012195194139146798,
        "q25": 0.3966722295498281,
        "q50": 0.9241976150385163,
        "q75": 0.9983528106963884,
        "q95": 0.9999999999999808
      },
      "executable_depth_weth": {
        "n": 229,
        "q05": 0.0,
        "q25": 0.0,
        "q50": 0.0,
        "q75": 0.06666666666666667,
        "q95": 0.13333333333333333
      },
      "mfe_6h": {
        "n": 48,
        "q05": -0.02159892007199222,
        "q25": -0.00012499375041662164,
        "q50": 0.0,
        "q75": 0.020348982567826003,
        "q95": 0.8214589277380371
      },
      "mae_6h": {
        "n": 48,
        "q05": -0.38555572253511117,
        "q25": -0.04652267390506658,
        "q50": -0.0019499025064992977,
        "q75": 0.0,
        "q95": 0.0005549722518497992
      }
    }
  }
}
```

---

## 7. 冻结配置全文（`configs/v2_phase0_freeze.json`）

```json
{
  "status": "FROZEN_BEFORE_PHASE0_RESULTS",
  "role": "Base Greenfield V2 Orthogonal Directional Research",
  "label_design_start_utc": "2025-10-01T00:00:00Z",
  "label_design_end_utc": "2025-12-01T14:30:00Z",
  "universe": "Base canonical Uniswap V3 WETH new pools",
  "independent_opportunity_definition": "one fixed-age decision per pool per bucket",
  "age_anchors_seconds": {
    "15-60m_NEGATIVE_CONTROL": 1800,
    "1-6h": 10800,
    "6-24h": 43200,
    "1-3d": 172800,
    "3-7d": 432000
  },
  "census_future_horizon_seconds": 21600,
  "amount_in_wei": "16666666666666666",
  "depth_multiples": [
    1,
    2,
    4,
    8
  ],
  "depth_slippage_ceiling": 0.1,
  "economic_unsellable_recovery_floor": 0.1,
  "pit_history": {
    "min_swaps": 12,
    "min_active_minutes": 6
  },
  "block_mapping": {
    "method": "latest observed canonical-candidate swap block at or before wall-clock decision",
    "max_lag_seconds": 120
  },
  "viability_gates": {
    "min_independent_opportunities": 300,
    "min_opportunities_per_week": 30.0,
    "min_pool_breadth": 300,
    "min_token_breadth": 250,
    "min_deployer_breadth": 50,
    "min_deployer_coverage": 0.8,
    "min_canonical_metadata_coverage": 0.98,
    "min_atomic_buy_success": 0.9,
    "min_current_atomic_sell_success": 0.85,
    "max_future_permanent_unsellable_rate": 0.1,
    "max_infrastructure_unknown_rate": 0.05,
    "min_future_sell_resolution_rate": 0.85,
    "min_production_depth_pass_rate": 0.8
  },
  "single_promotion_tiebreak": [
    "current_atomic_sell_success_desc",
    "future_sell_resolution_rate_desc",
    "future_permanent_unsellable_rate_asc",
    "independent_opportunities_desc",
    "age_bucket_lexical"
  ],
  "selection_prohibitions": [
    "no alpha model",
    "no predicted return",
    "no realized return or MFE/MAE in universe promotion",
    "no KEYSTONE/F10/G2/C3/P2 pass score threshold or model input"
  ],
  "closed_negative_control": "15-60m only",
  "deploy_allowed": false
}
```

---

## 8. REPRODUCE.md

# Reproduce Base Greenfield V2 Phase 0

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v2.phase0_census 2>&1 | tee out_v2/RUN.log
```

Phase 0 trains no alpha model. Closed KEYSTONE/F10/G2/C3/P2 artifacts are not read. The only imported V1.2 code is generic RPC/state-override execution plumbing.

Freeze SHA256: `b0fa7b1b823fe28f1168e552e5b6ac6d5a87c880f1fc1edb4fda5bda807ca3e7`  
Code SHA256: `3ae296cc67ab0f9a9ef505fac74bf0b090269df815ecb5dbac5763fc4fe96762`  
Verdict: `NO_VIABLE_BASE_NEW_POOL_UNIVERSE`


---

## 9. RUN.log（节选）

```
[raw] reuse pools=584 swaps=477571
[candidates] reuse rows=2601
[canonical] reuse pools=544
             age_bucket  raw_independent_opportunities  independent_opportunities  opportunities_per_day  opportunities_per_week  concurrent_q50  concurrent_q90  concurrent_q99  concurrent_max  canonical_metadata_coverage  atomic_buy_success  current_atomic_sell_success  future_permanent_unsellable_rate  future_sell_resolution_rate  infrastructure_unknown_rate  production_depth_pass_rate  pool_breadth  token_breadth  deployer_breadth  deployer_coverage  entry_slippage_q50  entry_slippage_q95  exit_slippage_q50  exit_slippage_q95  executable_depth_weth_q50  executable_depth_weth_q05   mfe_q25  mfe_q50  mfe_q75   mae_q25   mae_q50   mae_q75  viability_pass  promoted
                   1-3d                            510                        238               3.863375               27.043625            32.0            54.3            60.0              61                          1.0            0.600840                     0.783217                          0.636364                     0.902098                     0.079832                    0.441176           238            238               207                1.0            0.027654            0.996984           0.971744           1.000000                   0.066667                        0.0  0.000000 0.007150 0.168642 -0.099645 -0.014799  0.000000           False     False
                   1-6h                            527                        228               3.701048               25.907339            26.5            48.3            56.0              56                          1.0            0.614035                     0.885714                          0.542857                     0.935714                     0.065789                    0.521930           228            228               202                1.0            0.027574            0.077195           0.634288           1.000000                   0.066667                        0.0  0.000000 0.000000 0.030898 -0.173866 -0.000800  0.000000           False     False
15-60m_NEGATIVE_CONTROL                            544                        181               2.938113               20.566791            19.0            30.0            34.2              35                          1.0            0.817680                     0.979730                          0.641892                     0.979730                     0.033149                    0.773481           181            181               173                1.0            0.010586            0.049530           0.010553           0.992354                   0.133333                        0.0  0.000750 0.254937 0.782036 -0.788636 -0.157792 -0.014324           False     False
                   3-7d                            510                        243               3.944538               27.611769            32.0            55.0            61.0              62                          1.0            0.407407                     0.767677                          0.646465                     0.888889                     0.362140                    0.292181           243            243               212                1.0            0.027655            0.998096           0.974527           1.000000                   0.000000                        0.0 -0.000100 0.000000 0.042398 -0.067097 -0.000400  0.000000           False     False
                  6-24h                            510                        229               3.717281               26.020967            30.0            54.0            60.0              61                          1.0            0.524017                     0.825000                          0.575000                     0.933333                     0.157205                    0.414847           229            229               201                1.0            0.027650            0.785108           0.919095           1.000000                   0.000000                        0.0 -0.000125 0.000000 0.020349 -0.046523 -0.001950  0.000000           False     False
{
  "created_utc": "2026-07-20T10:15:20Z",
  "role": "Base Greenfield V2 Orthogonal Directional Research Agent",
  "phase": "PHASE_0_UNIVERSE_VIABILITY_CENSUS",
  "verdict": "NO_VIABLE_BASE_NEW_POOL_UNIVERSE",
  "promoted_universe": null,
  "stop_research": true,
  "alpha_models_trained": false,
  "old_line_inputs_used": false,
  "deploy_allowed": false,
  "freeze_sha256": "b0fa7b1b823fe28f1168e552e5b6ac6d5a87c880f1fc1edb4fda5bda807ca3e7",
  "viability_checks": {
    "1-3d": {
      "min_independent_opportunities": false,
      "min_opportunities_per_week": false,
      "min_pool_breadth": false,
      "min_token_breadth": false,
      "min_deployer_breadth": true,
      "min_deployer_coverage": true,
      "min_canonical_metadata_coverage": true,
      "min_atomic_buy_success": false,
      "min_current_atomic_sell_success": false,
      "max_future_permanent_unsellable_rate": false,
      "max_infrastructure_unknown_rate": false,
      "min_future_sell_resolution_rate": true,
      "min_production_depth_pass_rate": false,
      "eligible_for_promotion": false
    },
    "1-6h": {
      "min_independent_opportunities": false,
      "min_opportunities_per_week": false,
      "min_pool_breadth": false,
      "min_token_breadth": false,
      "min_deployer_breadth": true,
      "min_deployer_coverage": true,
      "min_canonical_metadata_coverage": true,
      "min_atomic_buy_success": false,
      "min_current_atomic_sell_success": true,
      "max_future_permanent_unsellable_rate": false,
      "max_infrastructure_unknown_rate": false,
      "min_future_sell_resolution_rate": true,
... (truncated) ...
        "q25": 0.23116691294081027,
        "q50": 0.9190951084205606,
        "q75": 0.9979840855081054,
        "q95": 0.9999999999999818
      },
      "future_exit_slippage": {
        "n": 100,
        "q05": 0.012195194139146798,
        "q25": 0.3966722295498281,
        "q50": 0.9241976150385163,
        "q75": 0.9983528106963884,
        "q95": 0.9999999999999808
      },
      "executable_depth_weth": {
        "n": 229,
        "q05": 0.0,
        "q25": 0.0,
        "q50": 0.0,
        "q75": 0.06666666666666667,
        "q95": 0.13333333333333333
      },
      "mfe_6h": {
        "n": 48,
        "q05": -0.02159892007199222,
        "q25": -0.00012499375041662164,
        "q50": 0.0,
        "q75": 0.020348982567826003,
        "q95": 0.8214589277380371
      },
      "mae_6h": {
        "n": 48,
        "q05": -0.38555572253511117,
        "q25": -0.04652267390506658,
        "q50": -0.0019499025064992977,
        "q75": 0.0,
        "q95": 0.0005549722518497992
      }
    }
  }
}
```

---

## 10. 和前几轮 Greenfield 的关系（避免混淆）

| 轮次 | 问的问题 | 结论 |
|---|---|---|
| R0–V1.2 | 旧 15–60m 线 + G2/C3 能不能赚钱 | G2 已 kill；执行/fee 坑已拆清 |
| **V2 Phase 0（本轮）** | **换更老年龄桶后，宇宙本身是否够格做新研究** | **不够格；停在训练前** |

本轮 **不是**「又训了一个挂掉的策略」，而是：**连开题的可交易宇宙都没晋升成功**。

---

## 11. 审查清单

- [ ] 五个 bucket 的 census 数字与 CSV 一致（含被终端截掉的 3–7d）
- [ ] 门槛来自冻结文件，非事后改阈值
- [ ] 无 Phase 1–4 / 无 Alpha / 无 OOF
- [ ] 未读取已关闭 G2/C3/KEYSTONE 分数
- [ ] `deploy_allowed=false`，`promoted_universe=null`

---

*由 `out_v2/` 全部关键产物汇总；用于替代 Codex 终端被截断的宽表展示。*
