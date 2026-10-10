# Base Greenfield V1.2 Canonical Repair Replay — 审查包（单文件合并）

> 生成时间：2026-07-20  
> 项目根：`/root/base_greenfield_directional_v0`  
> 本轮角色：**Base Greenfield V1.2 Canonical Metadata Repair Replay Agent**  
> 标签：`HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC`（**不是** sealed confirmation）  
> **部署：禁止**（`deploy_allowed=false`）  
> 前序：`GREENFIELD_R0_V1_审查包_完整合并.md`、`GREENFIELD_V1_1_Execution_Truth_审查包_完整合并.md`

---

## 0. 一句话结论（用人话）

1. V1.1 说「fee 脏了、记账错了」——对；但 V1.2 用**修好的链上 fee**重放买卖后发现：**买大多能买成**，卖侧仍有未知；在已解析的卖出样本上 **G2 仍亏**（约 **−0.216 WETH** / 0.5 本金钱包口径），因此 **`kill_g2=true`**。
2. 脏 fee **只污染执行路径**，不污染特征/标签训练（lineage=`EXECUTION_ONLY_CONTAMINATION`）——所以不是「模型吃了错 fee 特征」，而是「用错 fee 去报价/swap」。
3. 裁决不是「已经证明能赚钱」，而是：`SELL_TRUTH_INCOMPLETE` + 方向性线索在 repair 后仍为负 → **杀 G2**；**不开** prospective shadow；**不**重建 Greenfield v2；**不部署**。
4. 相对 V1 的 −114：那是假灾难；相对 V1.1 的「保留 DISCOVERY_LEAD」：V1.2 在修好 fee 后把结论改成了 **KILL**。

---

## 1. 本轮做了什么

| 允许 | 禁止（冻结） |
|---|---|
| 全量 panel 链上 metadata / factory 校验 | 改分数、改选中决策、改 H/TB_A/阈值 |
| 用 **canonical fee** 重放 480 holdout 原子买卖 | 改 G2/C3 模型、新特征 |
| archive + debug_trace + local anvil fork 交叉 | 部署；把诊断当 sealed 确认 |
| 冻结 P0/P1/P2 选择下的钱包/PnL 诊断 | 按结果重选策略 |

选中数冻结不变：P0=131 / P1=122 / P2=41。

---

## 2. 路径索引（`out_v1_2/`）

| 产物 | 路径 |
|---|---|
| 终裁 | `out_v1_2/FINAL_CANONICAL_REPLAY.json` |
| 全量 canonical metadata（2399） | `out_v1_2/CANONICAL_METADATA_ALL.parquet` |
| 字段血缘影响 | `out_v1_2/FIELD_LINEAGE_IMPACT.csv` |
| 修复后 holdout outcomes（480） | `out_v1_2/REPAIRED_HOLDOUT_OUTCOMES.parquet` |
| RPC/fork 交叉（148 selected） | `out_v1_2/RPC_FORK_CROSSCHECK.csv` |
| 政策比较 | `out_v1_2/REPAIRED_POLICY_COMPARISON.csv` |
| 钱包曲线 | `out_v1_2/WALLET_EQUITY_CURVES.parquet` |
| 复现 | `out_v1_2/REPRODUCE.md` |
| 日志 | `out_v1_2/RUN.log` |
| 代码 | `src/v1_2/canonical_repair_replay.py`（SHA 见 REPRODUCE） |

输入只读：`out/`、`out_v1/`、`out_v1_1/`（见 FINAL `input_sha256`）。

---

## 3. 裁决与处置

| 字段 | 值 |
|---|---|
| `verdict` | **`SELL_TRUTH_INCOMPLETE`** |
| `lineage_verdict` | **`EXECUTION_ONLY_CONTAMINATION`** |
| `execution_truth_status` | `EXECUTION_TRUTH_INCOMPLETE` |
| `kill_g2` | **true** |
| `start_prospective_shadow` | **false** |
| `rebuild_greenfield_v2` | **false** |
| `deploy_allowed` | **false** |
| `sealed_confirmation` | false |
| `result_label` | `HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC` |

**为什么 kill G2：** 冻结 P1 选择下，buy filled≈120，sell-resolved≈90，resolved-sell 钱包 delta ≈ **−0.216 WETH**（非 top5 单笔主导）。卖侧仍有 unknown，故总 PnL 标为 incomplete，但已解析样本已足够否定「修好 fee 后 G2 立刻变正」。

**为什么不开 shadow / 不 rebuild v2：** 本轮诊断结论是杀当前 G2 线索，而不是「修好测量后继续 shadow 确认」；完整 execution truth 仍缺第二 archive + 部分 sell 注入失败。

---

## 4. Canonical Metadata（2399 全历史 + 480 holdout）

### 4.1 全量

| repair_class | n |
|---|---|
| METADATA_MATCH | 2007 |
| REPAIRABLE_FEE_UNIT_MISMATCH | 392 |

- `factory_validated_all` = 2399（全部通过 factory 校验路径）
- tokens match：2399/2399

### 4.2 Holdout（480）

| repair_class | n |
|---|---|
| REPAIRABLE_FEE_UNIT_MISMATCH | 380 |
| METADATA_MATCH | 100 |

- **0** true-invalid（与 V1.1「380 非法不可交易」不同叙事：V1.2 把错 fee 当成 **可修复单位错误**，用链上 canonical fee 重放）
- Holdout：100 METADATA_MATCH + 380 REPAIRABLE_FEE_UNIT_MISMATCH

### 4.3 字段血缘（fee 脏了影响谁？）

| field | evidence | finding | dirty_fee_impact | impact_scope | lineage_verdict |
| --- | --- | --- | --- | --- | --- |
| pool_discovery | src/build_panel.py:48-82 | fee_bps is selected/carried; pool inclusion and swap join use pool address/type/WETH leg | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| eligibility | src/build_panel.py:145-199 | swap count, PIT minute bars, two-sided swap signs; fee is not referenced | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| swap_features | src/build_panel.py:180-228 | tick/amount/sender/recipient values keyed by pool; fee is not referenced | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| liquidity_features | src/build_panel.py:201-228 | liquidity_raw and its PIT transforms; fee is not referenced | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| executable_depth_features | src/build_panel.py:201-228; src/features.py:17-29 | no Quoter depth feature exists; vol_depth is pre_vol/liq_log | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| friction | configs/design_freeze.json:51-55; src/build_panel.py | threshold is frozen but no fee-dependent friction field is computed/applied in panel construction | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| G2_features | src/features.py:17-75; src/models_routes.py:293-320 | fee is metadata only and excluded from FEATURE_COLS | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| C3_feature | src/models_routes.py:219-230 | C3 score is early_ret only | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| tick_path_labels | src/build_labels.py:23-205 | forward tick/liquidity path keyed by pool; fee is not referenced | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| atomic_labels | src/build_labels.py:191-229 | Discovery atomic_net_return is tick-path derived; fee enters calibration smoke only and does not alter labels | False | NONE | EXECUTION_ONLY_CONTAMINATION |
| v1_atomic_execution | src/v1/atomic_engine.py:64-199 | registry fee enters Quoter and Router calldata for buy/sell | True | EXECUTION_ONLY | EXECUTION_ONLY_CONTAMINATION |

要点：**特征 / 标签 / eligibility 不吃 registry fee**；只有 V1 原子执行的 Quoter/Router calldata 吃错 fee → `EXECUTION_ONLY_CONTAMINATION`。

---

## 5. Repair 后 Holdout Replay（480）

### 5.1 Buy / Exit 计数

| buy_status | n |
|---|---|
| BUY_SUCCESS | 477 |
| ENTRY_TX_REVERT | 2 |
| ENTRY_PREFLIGHT_REJECT | 1 |

| exit_status | n |
|---|---|
| EXIT_SUCCESS | 384 |
| EXIT_INFRASTRUCTURE_UNKNOWN | 61 |
| PERMANENT_UNSELLABLE | 32 |
| （无 exit / nan） | 3 |

解读：fee 修好后，**入口几乎都能成交**；亏损主要来自卖出路径上的真实不利结果 + 仍有一批 sell infrastructure unknown。

### 5.2 G2（P1）摘要（来自 FINAL）

| 指标 | 值 |
|---|---|
| selected | 122 |
| buy_filled | 120 |
| resolved_filled（有已知卖出 PnL） | 90 |
| exit_success | 79 |
| fatal | 11 |
| known_sell_wallet_delta_weth | **−0.21576** |
| unknown_selected | 30 |
| total_pnl_complete | false |
| pnl_scope | `RESOLVED_SELL_TRUTH_ONLY` |

---

## 6. 冻结政策诊断比较

| policy | model_selected | repairable_selected | buy_filled | resolved_filled | exit_success | fatal_position | fill_rate | total_pnl_weth | wallet_return | max_drawdown | unknown_selected | total_pnl_complete | pnl_scope | capital_hours |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P0_C3_MOMENTUM | 131 | 122 | 128 | 99 | 85 | 14 | 0.977099 | -0.282304 | -0.564607 | -0.299524 | 29 | False | RESOLVED_SELL_TRUTH_ONLY | 102.042222 |
| P1_G2_COMPETING | 122 | 114 | 120 | 90 | 79 | 11 | 0.983607 | -0.215758 | -0.431516 | -0.234947 | 30 | False | RESOLVED_SELL_TRUTH_ONLY | 95.767222 |
| P2_C3_G2_VETO | 41 | 34 | 41 | 33 | 28 | 5 | 1 | -0.024523 | -0.049047 | -0.054405 | 8 | False | RESOLVED_SELL_TRUTH_ONLY | 27.043333 |

### 6.1 三轮 Holdout 数字对照（P1）

| 轮次 | 叙事 | P1「总盈亏」口径 | 数量级 |
|---|---|---|---|
| V1 | 错 fee + revert 当 −100% | sum(return)≈−114.6 | 假灾难 |
| V1.1 | 非法行 pnl=0，几乎不成交 | wallet≈−0.012；filled≈8 | 测到了污染，filled 太少 |
| **V1.2** | **canonical fee 重放** | resolved-sell delta≈**−0.216**；buy≈120 | **修好后仍亏 → kill_g2** |

### 6.2 钱包曲线摘要

| policy | n_events | start≈ | final | min | return on 0.5 |
|---|---|---|---|---|---|
| P0_C3_MOMENTUM | 201 | 0.500000 | 0.217696 | 0.190236 | -56.46% |
| P1_G2_COMPETING | 183 | 0.500000 | 0.284242 | 0.256781 | -43.15% |
| P2_C3_G2_VETO | 67 | 0.500000 | 0.475477 | 0.444089 | -4.90% |

（曲线事件仅计入 resolved sell truth；incomplete 时勿当作完整权益。）

---

## 7. 双执行交叉（148 selected union）

| 项 | 值 |
|---|---|
| archive RPC A | EXECUTED |
| archive RPC B | **UNAVAILABLE** |
| debug_trace 行数 | 148 |
| local_fork buy 行数 | 146 |
| local_fork sell 行数 | 106 |

`archive_local_buy_received_equal`：{'True': 145, nan: 3}  
`archive_local_sell_received_equal`：{'True': 106, nan: 42}  
`execution_truth_status`：{'EXECUTION_TRUTH_INCOMPLETE': 148}  
`local_fork_buy_status`：{'LOCAL_FORK_TX_SUCCESS': 145, 'LOCAL_FORK_RECEIPT_TIMEOUT': 2, 'LOCAL_FORK_TX_REVERT': 1}  
`local_fork_sell_status`：{'LOCAL_FORK_TX_SUCCESS': 104, 'LOCAL_FORK_NOT_RUN': 37, 'LOCAL_FORK_RECEIPT_TIMEOUT': 5, 'LOCAL_FORK_TX_REVERT': 2}

缺口：无第二 archive；部分非标准 token 无法注入 exact exit inventory → 不能升级为完整 execution truth。

---

## 8. FINAL_CANONICAL_REPLAY.json（全文）

```json
{
  "created_utc": "2026-07-20T08:53:27Z",
  "role": "Base Greenfield V1.2 Canonical Metadata Repair Replay Agent",
  "verdict": "SELL_TRUTH_INCOMPLETE",
  "lineage_verdict": "EXECUTION_ONLY_CONTAMINATION",
  "execution_truth_status": "EXECUTION_TRUTH_INCOMPLETE",
  "result_label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
  "sealed_confirmation": false,
  "kill_g2": true,
  "start_prospective_shadow": false,
  "rebuild_greenfield_v2": false,
  "deploy_allowed": false,
  "frozen": {
    "policies": [
      "P0_C3_MOMENTUM",
      "P1_G2_COMPETING",
      "P2_C3_G2_VETO"
    ],
    "scores_unchanged": true,
    "selected_decisions_unchanged": true,
    "H_seconds": 3600,
    "TB_A": {
      "tp": 0.5,
      "sl": 0.35
    },
    "amountIn_wei": "16666666666666666",
    "wallet_weth": 0.5,
    "max_concurrent": 3,
    "executor": "0x4b7cd953871663474a0a25b81bdddf5c1f1195a7",
    "router": "0x2626664c2603336e57b271c5c0b26f421741e481",
    "gas_policy_weth": {
      "buy": 0.0001,
      "sell": 0.0001
    },
    "retry_blocks_on_infrastructure_unknown": [
      0,
      5,
      30,
      150
    ]
  },
  "canonical_metadata": {
    "historical_panel_rows": 2399,
    "holdout_rows_in_panel": 480,
    "repair_class_counts_all": {
      "METADATA_MATCH": 2007,
      "REPAIRABLE_FEE_UNIT_MISMATCH": 392
    },
    "repair_class_counts_holdout": {
      "REPAIRABLE_FEE_UNIT_MISMATCH": 380,
      "METADATA_MATCH": 100
    },
    "factory_validated_all": 2399
  },
  "replay": {
    "rows": 480,
    "buy_status_counts": {
      "BUY_SUCCESS": 477,
      "ENTRY_TX_REVERT": 2,
      "ENTRY_PREFLIGHT_REJECT": 1
    },
    "exit_status_counts": {
      "EXIT_SUCCESS": 384,
      "EXIT_INFRASTRUCTURE_UNKNOWN": 61,
      "PERMANENT_UNSELLABLE": 32,
      "nan": 3
    },
    "g2_filled_n": 90,
    "g2_repaired_filled_n": 82,
    "g2_filled_pnl_weth": -0.21555778205875875,
    "g2_non_top5_dominated": false,
    "g2_buy_filled_n": 120,
    "pnl_scope_note": "Policy PnL/wallet curves include resolved sell truth only; unresolved filled exits remain explicit and make total_pnl_complete=false."
  },
  "double_execution": {
    "selected_union_rows": 148,
    "archive_rpc_a": "EXECUTED",
    "archive_rpc_b": "UNAVAILABLE",
    "debug_trace_rows": 148,
    "local_fork_buy_rows": 146,
    "local_fork_sell_rows": 106
  },
  "policies": [
    {
      "policy": "P0_C3_MOMENTUM",
      "label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
      "model_selected": 131,
      "repairable_selected": 122,
      "true_invalid_selected": 0,
      "buy_filled": 128,
      "resolved_filled": 99,
      "atomic_exit_success": 85,
      "exit_success": 85,
      "atomic_fatal_position": 14,
      "fatal_position": 14,
      "fill_rate": 0.9770992366412213,
      "pnl_per_selected": -0.0021549888851623445,
      "pnl_per_filled": -0.0028515509490532032,
      "pnl_per_resolved_filled": -0.0028515509490532032,
      "total_pnl_weth": -0.2823035439562671,
      "known_sell_wallet_delta_weth": -0.2823035439562671,
      "total_pnl_complete": false,
      "pnl_scope": "RESOLVED_SELL_TRUTH_ONLY",
      "wallet_return": -0.5646070879125342,
      "max_drawdown": -0.29952448625408945,
      "capital_hours": 102.04222222222222,
      "top1_share": -0.03788151371848368,
      "top5_share": -0.1804931618847022,
      "pnl_ex_top5": -0.3332574032161908,
      "repaired_atomic_filled": 119,
      "unknown_selected": 29,
      "fatal_overlap_between_policies": "{\"P1_G2_COMPETING\": 11, \"P2_C3_G2_VETO\": 5}"
    },
    {
      "policy": "P1_G2_COMPETING",
      "label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
      "model_selected": 122,
      "repairable_selected": 114,
      "true_invalid_selected": 0,
      "buy_filled": 120,
      "resolved_filled": 90,
      "atomic_exit_success": 79,
      "exit_success": 79,
      "atomic_fatal_position": 11,
      "fatal_position": 11,
      "fill_rate": 0.9836065573770492,
      "pnl_per_selected": -0.0017685064103176822,
      "pnl_per_filled": -0.002397308689541747,
      "pnl_per_resolved_filled": -0.002397308689541747,
      "total_pnl_weth": -0.21575778205875723,
      "known_sell_wallet_delta_weth": -0.21575778205875723,
      "total_pnl_complete": false,
      "pnl_scope": "RESOLVED_SELL_TRUTH_ONLY",
      "wallet_return": -0.43151556411751446,
      "max_drawdown": -0.23494673926640464,
      "capital_hours": 95.76722222222222,
      "top1_share": -0.04956523686475224,
      "top5_share": -0.2361623241290431,
      "pnl_ex_top5": -0.2667116413186809,
      "repaired_atomic_filled": 112,
      "unknown_selected": 30,
      "fatal_overlap_between_policies": "{\"P0_C3_MOMENTUM\": 11, \"P2_C3_G2_VETO\": 5}"
    },
    {
      "policy": "P2_C3_G2_VETO",
      "label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
      "model_selected": 41,
      "repairable_selected": 34,
      "true_invalid_selected": 0,
      "buy_filled": 41,
      "resolved_filled": 33,
      "atomic_exit_success": 28,
      "exit_success": 28,
      "atomic_fatal_position": 5,
      "fatal_position": 5,
      "fill_rate": 1.0,
      "pnl_per_selected": -0.0005981282019932649,
      "pnl_per_filled": -0.0007431289782340564,
      "pnl_per_resolved_filled": -0.0007431289782340564,
      "total_pnl_weth": -0.02452325628172386,
      "known_sell_wallet_delta_weth": -0.02452325628172386,
      "total_pnl_complete": false,
      "pnl_scope": "RESOLVED_SELL_TRUTH_ONLY",
      "wallet_return": -0.04904651256344772,
      "max_drawdown": -0.054404567043498886,
      "capital_hours": 27.04333333333333,
      "top1_share": -0.43607934649061025,
      "top5_share": -2.0777770567890452,
      "pnl_ex_top5": -0.07547711554164752,
      "repaired_atomic_filled": 34,
      "unknown_selected": 8,
      "fatal_overlap_between_policies": "{\"P0_C3_MOMENTUM\": 5, \"P1_G2_COMPETING\": 5}"
    }
  ],
  "input_sha256": {
    "panel": "32fc2d81d7d9b5d114b94d88d5a797c1b3e7c93ce6f6c24466e8dedeb4b89f44",
    "holdout_scores": "184e0cbe893ea28f752c314a2c7659dd9b531295a33d143878b5f16720db51e2",
    "v11_pool_audit": "fb4d4a6cf8e16510741d29fa0f1fcb5b3c5052ae308e1a0fd6291a4066de8a1b",
    "v11_final": "086cbe8237f1252027bbd17e9f59588132224ef9c4fde5bf443b1c4078e050eb"
  },
  "rpc": {
    "A_url_sha256": "0b98445d8abca769ef04d3df92f47c07b9b84b6bbffcd882f32696f3349644d2",
    "B_url_sha256": null,
    "A_calls": 148,
    "B_calls": 0
  },
  "outputs": [
    "CANONICAL_METADATA_ALL.parquet",
    "FIELD_LINEAGE_IMPACT.csv",
    "REPAIRED_HOLDOUT_OUTCOMES.parquet",
    "RPC_FORK_CROSSCHECK.csv",
    "REPAIRED_POLICY_COMPARISON.csv",
    "WALLET_EQUITY_CURVES.parquet",
    "FINAL_CANONICAL_REPLAY.json",
    "REPRODUCE.md"
  ]
}
```

---

## 9. REPAIRED_POLICY_COMPARISON.csv（原文）

```csv
policy,label,model_selected,repairable_selected,true_invalid_selected,buy_filled,resolved_filled,atomic_exit_success,exit_success,atomic_fatal_position,fatal_position,fill_rate,pnl_per_selected,pnl_per_filled,pnl_per_resolved_filled,total_pnl_weth,known_sell_wallet_delta_weth,total_pnl_complete,pnl_scope,wallet_return,max_drawdown,capital_hours,top1_share,top5_share,pnl_ex_top5,repaired_atomic_filled,unknown_selected,fatal_overlap_between_policies
P0_C3_MOMENTUM,HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC,131,122,0,128,99,85,85,14,14,0.9770992366412213,-0.0021549888851623445,-0.0028515509490532032,-0.0028515509490532032,-0.2823035439562671,-0.2823035439562671,False,RESOLVED_SELL_TRUTH_ONLY,-0.5646070879125342,-0.29952448625408945,102.04222222222222,-0.03788151371848368,-0.1804931618847022,-0.3332574032161908,119,29,"{""P1_G2_COMPETING"": 11, ""P2_C3_G2_VETO"": 5}"
P1_G2_COMPETING,HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC,122,114,0,120,90,79,79,11,11,0.9836065573770492,-0.0017685064103176822,-0.002397308689541747,-0.002397308689541747,-0.21575778205875723,-0.21575778205875723,False,RESOLVED_SELL_TRUTH_ONLY,-0.43151556411751446,-0.23494673926640464,95.76722222222222,-0.04956523686475224,-0.2361623241290431,-0.2667116413186809,112,30,"{""P0_C3_MOMENTUM"": 11, ""P2_C3_G2_VETO"": 5}"
P2_C3_G2_VETO,HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC,41,34,0,41,33,28,28,5,5,1.0,-0.0005981282019932649,-0.0007431289782340564,-0.0007431289782340564,-0.02452325628172386,-0.02452325628172386,False,RESOLVED_SELL_TRUTH_ONLY,-0.04904651256344772,-0.054404567043498886,27.04333333333333,-0.43607934649061025,-2.0777770567890452,-0.07547711554164752,34,8,"{""P0_C3_MOMENTUM"": 5, ""P1_G2_COMPETING"": 5}"
```

---

## 10. FIELD_LINEAGE_IMPACT.csv（原文）

```csv
field,evidence,finding,dirty_fee_impact,impact_scope,lineage_verdict
pool_discovery,src/build_panel.py:48-82,fee_bps is selected/carried; pool inclusion and swap join use pool address/type/WETH leg,False,NONE,EXECUTION_ONLY_CONTAMINATION
eligibility,src/build_panel.py:145-199,"swap count, PIT minute bars, two-sided swap signs; fee is not referenced",False,NONE,EXECUTION_ONLY_CONTAMINATION
swap_features,src/build_panel.py:180-228,tick/amount/sender/recipient values keyed by pool; fee is not referenced,False,NONE,EXECUTION_ONLY_CONTAMINATION
liquidity_features,src/build_panel.py:201-228,liquidity_raw and its PIT transforms; fee is not referenced,False,NONE,EXECUTION_ONLY_CONTAMINATION
executable_depth_features,src/build_panel.py:201-228; src/features.py:17-29,no Quoter depth feature exists; vol_depth is pre_vol/liq_log,False,NONE,EXECUTION_ONLY_CONTAMINATION
friction,configs/design_freeze.json:51-55; src/build_panel.py,threshold is frozen but no fee-dependent friction field is computed/applied in panel construction,False,NONE,EXECUTION_ONLY_CONTAMINATION
G2_features,src/features.py:17-75; src/models_routes.py:293-320,fee is metadata only and excluded from FEATURE_COLS,False,NONE,EXECUTION_ONLY_CONTAMINATION
C3_feature,src/models_routes.py:219-230,C3 score is early_ret only,False,NONE,EXECUTION_ONLY_CONTAMINATION
tick_path_labels,src/build_labels.py:23-205,forward tick/liquidity path keyed by pool; fee is not referenced,False,NONE,EXECUTION_ONLY_CONTAMINATION
atomic_labels,src/build_labels.py:191-229,Discovery atomic_net_return is tick-path derived; fee enters calibration smoke only and does not alter labels,False,NONE,EXECUTION_ONLY_CONTAMINATION
v1_atomic_execution,src/v1/atomic_engine.py:64-199,registry fee enters Quoter and Router calldata for buy/sell,True,EXECUTION_ONLY,EXECUTION_ONLY_CONTAMINATION
```

---

## 11. REPRODUCE.md

# Base Greenfield V1.2 Canonical Repair Replay

Label: **HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC**. This is not sealed confirmation.

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v1_2.canonical_repair_replay 2>&1 | tee out_v1_2/RUN.log
```

Inputs remain read-only under `out/`, `out_v1/`, and `out_v1_1/`. Archive RPC A is read from `/tmp/rpc_url.txt`; an independent RPC B is read from `/tmp/rpc_url_b.txt` or `/tmp/rpc_url2.txt`. Local-fork transactions require `/root/.foundry/bin/anvil`.

The 2,399-row historical panel already contains all 480 holdout rows, so `CANONICAL_METADATA_ALL.parquet` has 2,399 unique signal rows, not 2,879 duplicated rows. Every row performs direct `token0/token1/fee/factory` calls at its decision block followed by canonical factory `getPool`.

Frozen execution: H=3600s, TB_A=+50%/-35%, amountIn=16666666666666666 wei, wallet=0.5 WETH, max_concurrent=3, executor=0x4b7cd953871663474a0a25b81bdddf5c1f1195a7, router=0x2626664c2603336e57b271c5c0b26f421741e481.

Gas debits and infrastructure retry schedule are inherited from the frozen V1 execution implementation: 0.0001 WETH per attempted leg and retry blocks [0,5,30,150] only for infrastructure-unknown sell calls.

Code SHA256: `68f094b44124a6e272e438fe069be07f5da0b3eba2fbedcbb02dbb68558aaf0b`  
Final verdict: `SELL_TRUTH_INCOMPLETE`  
Execution truth: `EXECUTION_TRUTH_INCOMPLETE`


---

## 12. RUN.log（节选）

```
=== CANONICAL METADATA ALL ===
[metadata] reuse direct cache rows=2399
{'METADATA_MATCH': 2007, 'REPAIRABLE_FEE_UNIT_MISMATCH': 392}
=== FIELD LINEAGE IMPACT ===
EXECUTION_ONLY_CONTAMINATION
=== REPAIRED ATOMIC REPLAY 480 ===
[replay] checkpoint rows=480
{'BUY_SUCCESS': 477, 'ENTRY_TX_REVERT': 2, 'ENTRY_PREFLIGHT_REJECT': 1}
{'EXIT_SUCCESS': 384, 'EXIT_INFRASTRUCTURE_UNKNOWN': 61, 'PERMANENT_UNSELLABLE': 32, nan: 3}
=== RPC / TRACE / LOCAL FORK CROSSCHECK ===
[crosscheck] 10/148
[crosscheck] 20/148
[crosscheck] 30/148
[crosscheck] 40/148
[crosscheck] 50/148
[crosscheck] 60/148
[crosscheck] 70/148
[crosscheck] 80/148
[crosscheck] 90/148
[crosscheck] 100/148
[crosscheck] 110/148
[crosscheck] 120/148
[crosscheck] 130/148
[crosscheck] 140/148
EXECUTION_TRUTH_INCOMPLETE
=== FROZEN POLICY DIAGNOSTIC ===
         policy                               label  model_selected  repairable_selected  true_invalid_selected  atomic_buy_filled  buy_filled  atomic_exit_success  exit_success  atomic_fatal_position  fatal_position  fill_rate  pnl_per_selected  pnl_per_filled  total_pnl_weth  wallet_return  max_drawdown  capital_hours  top1_share  top5_share  pnl_ex_top5  repaired_atomic_filled  unknown_selected              fatal_overlap_between_policies
 P0_C3_MOMENTUM HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC             131                  122                      0                128          99                   85            85                     14              14   0.755725         -0.002155       -0.002852       -0.282304      -0.564607     -0.299524      78.672778   -0.037882   -0.180493    -0.333257                     119                29 {"P1_G2_COMPETING": 11, "P2_C3_G2_VETO": 5}
P1_G2_COMPETING HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC             122                  114                      0                120          90                   79            79                     11              11   0.737705         -0.001769       -0.002397       -0.215758      -0.431516     -0.234947      71.953333   -0.049565   -0.236162    -0.266712                     112                30  {"P0_C3_MOMENTUM": 11, "P2_C3_G2_VETO": 5}
  P2_C3_G2_VETO HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC              41                   34                      0                 41          33                   28            28                      5               5   0.804878         -0.000598       -0.000743       -0.024523      -0.049047     -0.054405      22.355556   -0.436079   -2.077777    -0.075477                      34                 8 {"P0_C3_MOMENTUM": 5, "P1_G2_COMPETING": 5}
{
  "created_utc": "2026-07-20T08:53:27Z",
  "role": "Base Greenfield V1.2 Canonical Metadata Repair Replay Agent",
  "verdict": "SELL_TRUTH_INCOMPLETE",
  "lineage_verdict": "EXECUTION_ONLY_CONTAMINATION",
  "execution_truth_status": "EXECUTION_TRUTH_INCOMPLETE",
  "result_label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
  "sealed_confirmation": false,
  "kill_g2": true,
  "start_prospective_shadow": false,
  "rebuild_greenfield_v2": false,
  "deploy_allowed": false,
  "frozen": {
    "policies": [
      "P0_C3_MOMENTUM",
      "P1_G2_COMPETING",
      "P2_C3_G2_VETO"
    ],
    "scores_unchanged": true,
    "selected_decisions_unchanged": true,
    "H_seconds": 3600,
    "TB_A": {
      "tp": 0.5,
      "sl": 0.35
    },
    "amountIn_wei": "16666666666666666",
    "wallet_weth": 0.5,
    "max_concurrent": 3,
    "executor": "0x4b7cd953871663474a0a25b81bdddf5c1f1195a7",
    "router": "0x2626664c2603336e57b271c5c0b26f421741e481",
... (truncated) ...
      "repaired_atomic_filled": 119,
      "unknown_selected": 29,
      "fatal_overlap_between_policies": "{\"P1_G2_COMPETING\": 11, \"P2_C3_G2_VETO\": 5}"
    },
    {
      "policy": "P1_G2_COMPETING",
      "label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
      "model_selected": 122,
      "repairable_selected": 114,
      "true_invalid_selected": 0,
      "atomic_buy_filled": 120,
      "buy_filled": 90,
      "atomic_exit_success": 79,
      "exit_success": 79,
      "atomic_fatal_position": 11,
      "fatal_position": 11,
      "fill_rate": 0.7377049180327869,
      "pnl_per_selected": -0.0017685064103176822,
      "pnl_per_filled": -0.002397308689541747,
      "total_pnl_weth": -0.21575778205875723,
      "wallet_return": -0.43151556411751446,
      "max_drawdown": -0.23494673926640464,
      "capital_hours": 71.95333333333333,
      "top1_share": -0.04956523686475224,
      "top5_share": -0.2361623241290431,
      "pnl_ex_top5": -0.2667116413186809,
      "repaired_atomic_filled": 112,
      "unknown_selected": 30,
      "fatal_overlap_between_policies": "{\"P0_C3_MOMENTUM\": 11, \"P2_C3_G2_VETO\": 5}"
    },
    {
      "policy": "P2_C3_G2_VETO",
      "label": "HOLDOUT_CANONICAL_REPAIR_DIAGNOSTIC",
      "model_selected": 41,
      "repairable_selected": 34,
      "true_invalid_selected": 0,
      "atomic_buy_filled": 41,
      "buy_filled": 33,
      "atomic_exit_success": 28,
      "exit_success": 28,
      "atomic_fatal_position": 5,
      "fatal_position": 5,
      "fill_rate": 0.8048780487804879,
      "pnl_per_selected": -0.0005981282019932649,
      "pnl_per_filled": -0.0007431289782340564,
      "total_pnl_weth": -0.02452325628172386,
      "wallet_return": -0.04904651256344772,
      "max_drawdown": -0.054404567043498886,
      "capital_hours": 22.355555555555558,
      "top1_share": -0.43607934649061025,
      "top5_share": -2.0777770567890452,
      "pnl_ex_top5": -0.07547711554164752,
      "repaired_atomic_filled": 34,
      "unknown_selected": 8,
      "fatal_overlap_between_policies": "{\"P0_C3_MOMENTUM\": 5, \"P1_G2_COMPETING\": 5}"
    }
  ],
  "input_sha256": {
    "panel": "32fc2d81d7d9b5d114b94d88d5a797c1b3e7c93ce6f6c24466e8dedeb4b89f44",
    "holdout_scores": "184e0cbe893ea28f752c314a2c7659dd9b531295a33d143878b5f16720db51e2",
    "v11_pool_audit": "fb4d4a6cf8e16510741d29fa0f1fcb5b3c5052ae308e1a0fd6291a4066de8a1b",
    "v11_final": "086cbe8237f1252027bbd17e9f59588132224ef9c4fde5bf443b1c4078e050eb"
  },
  "rpc": {
    "A_url_sha256": "0b98445d8abca769ef04d3df92f47c07b9b84b6bbffcd882f32696f3349644d2",
    "B_url_sha256": null,
    "A_calls": 148,
    "B_calls": 0
  },
  "outputs": [
    "CANONICAL_METADATA_ALL.parquet",
    "FIELD_LINEAGE_IMPACT.csv",
    "REPAIRED_HOLDOUT_OUTCOMES.parquet",
    "RPC_FORK_CROSSCHECK.csv",
    "REPAIRED_POLICY_COMPARISON.csv",
    "WALLET_EQUITY_CURVES.parquet",
    "FINAL_CANONICAL_REPLAY.json",
    "REPRODUCE.md"
  ]
}
```

---

## 13. 审查清单

- [ ] 选中决策仍为 131/122/41，分数未改
- [ ] 使用的是 canonical fee replay，不是 registry 脏 fee
- [ ] PnL scope 标明 `RESOLVED_SELL_TRUTH_ONLY`，unknown 未假装成完整真相
- [ ] `kill_g2=true` 且 `deploy_allowed=false`
- [ ] 标签为 diagnostic，非 sealed confirmation
- [ ] lineage 支持「只脏执行、不脏特征」

---

## 14. 四轮故事线（给非技术读者）

| 轮次 | 一句话 | 对「能不能赚钱」的含义 |
|---|---|---|
| R0 Discovery | 找到 G2 线索，但没打赢简单动量 | 有假说，未证实 |
| V1 Profit | Holdout 账面炸了（−114） | 吓一跳，但后来证明测法有毒 |
| V1.1 Truth | 查出 fee 脏 + 记账错；当时 filled 太少，暂留 G2 | 清了假负，仍无真 edge |
| **V1.2 Repair Replay** | **修好 fee 能买了，已解析卖出上 G2 仍亏 → 杀 G2** | **当前这条 G2 线不能当赚钱原理** |

---

*由 `out_v1_2/` 关键产物自动汇总，便于整份审查。*
