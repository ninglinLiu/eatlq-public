# Base Greenfield V1.1 Execution Truth Audit — 审查包（单文件合并）

> 生成时间：2026-07-20  
> 项目根：`/root/base_greenfield_directional_v0`  
> 本轮角色：**Base Greenfield V1.1 Execution Truth Audit Agent**  
> 标签：`HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC`（**不是**新的 sealed holdout confirmation）  
> **部署状态：禁止部署（`deploy_allowed=false`）**  
> 前序审查包：`GREENFIELD_R0_V1_审查包_完整合并.md`

---

## 0. 一句话结论

1. **裁决 `MIXED_FAILURE`**：组件发现 = `UNIVERSE_REGISTRY_CONTAMINATION_CONFIRMED` + `EXECUTION_PIPELINE_BUG_CONFIRMED`。
2. Holdout 480 行中仅 **100 / 480（20.8%）** 为 canonical 可交易宇宙；**380（79.2%）非法**——主因 registry `fee` 字段污染（fee=1 实为 100；fee=30→3000；fee=5→500；另有 135 行 fee=100 但链上池为 10000 / getPool 失败）。
3. V1 `HOLDOUT_FAIL_ALL`（P1 `sum(return)≈−114.6`）主要是**标签错误**：买侧 revert / 非法 fee 被记成 −100%。修正后 P1 **钱包 PnL ≈ −0.012 WETH**（0.5→0.488），与 −114 不是同一量纲。
4. Canonical-OK 上 buy path **21/21 `BUY_TX_SUCCESS`** → buy simulator / 真实 entry 不可执行 **未**被证实为主因；第二 archive / local fork 不可用 → RPC 交叉验证不足。
5. **`kill_g2=false`**，保留 G2 为 **`DISCOVERY_LEAD`**；建议 **全新 prospective shadow**；**不得部署**。真正 filled 样本过小（P1 n=8），不足以判 G2 方向失效。

---

## 1. 本轮硬边界

| 允许 | 禁止 |
|---|---|
| 审计 holdout 执行真值、修正 PnL 语义、钱包模拟、诊断性重算 P0/P1/P2 | 新特征、新模型、改 G2/C3、改 threshold、改 TB_A |
| 写 `out_v1_1/` | 修改 `out/`、`out_v1/` |
| 解码 revert、canonical 池校验、多路径 buy 验证 | 按修复后收益选规则、部署 |

**目标问题**：`HOLDOUT_FAIL_ALL` 来自 (1) universe 污染 (2) archive/RPC (3) buy sim (4) 真实不可成交 (5) 策略方向失效 —— 哪一类？

---

## 2. 路径索引（`out_v1_1/`）

| 产物 | 路径 | 说明 |
|---|---|---|
| 冻结 Manifest | `out_v1_1/EXECUTION_TRUTH_MANIFEST.json` | 输入 SHA256、地址、PnL 语义、审计代码 hash |
| 终裁 | `out_v1_1/FINAL_EXECUTION_TRUTH.json` | verdict + disposition |
| Canonical 审计 | `out_v1_1/CANONICAL_POOL_AUDIT.parquet` | 480 行全量 |
| Buy forensics | `out_v1_1/BUY_REVERT_FORENSICS.parquet` | 选中+对照 deep path |
| RPC 交叉 | `out_v1_1/RPC_CROSSCHECK.csv` | 第二 RPC 不可用已注明 |
| Buy 校准 | `out_v1_1/ATOMIC_BUY_CALIBRATION.csv` | quoter vs router |
| 修正 outcome 规格 | `out_v1_1/CORRECTED_OUTCOME_SPEC.md` | PnL 定义 |
| 修正 480 outcomes | `out_v1_1/CORRECTED_OUTCOMES_480.parquet` | 全 holdout |
| 政策比较 | `out_v1_1/CORRECTED_POLICY_COMPARISON.csv` | P0/P1/P2 诊断 |
| 钱包曲线 | `out_v1_1/WALLET_EQUITY_CURVES.parquet` | 0.5 WETH / max3 |
| 复现说明 | `out_v1_1/REPRODUCE.md` | |
| 运行日志 | `out_v1_1/RUN.log` | |
| 审计代码 | `src/v1_1/execution_truth_audit.py` | SHA256 见 Manifest |

输入（只读，未修改）SHA256 前缀：

| 输入 | SHA256（前 16） |
|---|---|
| profit_validation_manifest | `18795ad5338cdfd1…` |
| holdout_atomic_outcomes | `0ff9d5f088a94853…` |
| holdout_scores | `184e0cbe893ea28f…` |
| oof_g2 | `f347d5f0ecba999f…` |
| oof_c3 | `07e0b606f48e0d14…` |
| audit_code | `c051df0f3ed52fb0…` |

完整 SHA256 见下方 §8 Manifest 原文。

---

## 3. 裁决与处置

| 字段 | 值 |
|---|---|
| `verdict` | **`MIXED_FAILURE`** |
| `component_findings` | `UNIVERSE_REGISTRY_CONTAMINATION_CONFIRMED`, `EXECUTION_PIPELINE_BUG_CONFIRMED` |
| `kill_g2` | **false** |
| `keep_g2_discovery_lead` | **true** |
| `start_fresh_prospective_shadow` | **true** |
| `deploy_allowed` | **false** |
| G2 状态 | `DISCOVERY_LEAD_RETAINED` |
| 动作 | `START_FRESH_PROSPECTIVE_SHADOW` |

### 3.1 五假设对照

| # | 假设 | 结论 |
|---|---|---|
| 1 | canonical universe 污染 | **确认** — bad rate 79.2% |
| 2 | archive / RPC / state override | **未证实** — 无第二 archive、无 anvil；单 RPC 上 canonical-OK buy 成功 |
| 3 | buy simulator 错误 | **弱排除** — focus 内 21/21 canonical-OK → `BUY_TX_SUCCESS` |
| 4 | 真实 entry 不可执行 | **未确认** — P1 选中且 canonical-OK 的 8 笔全部成交 |
| 5 | G2 方向失效 | **样本不足** — filled n=8，略负，被污染主导；不 KILL |

### 3.2 主因证据（压缩）

- fee=1 → diagnostic repair **100**：193/193  
- fee=30 → **3000**：36/36  
- fee=5 → **500**：12/12  
- fee=100 且 `CANONICAL_NO_POOL`：135（失败原因含 `onchain_fee=10000!=row_fee=100`）  
- V1 P1 `sum_return_for_reference` = **−114.63** vs V1.1 `wallet_total_pnl_weth` = **−0.01195**

---

## 4. Canonical 池审计（480）

### 4.1 状态分布

| canonical_status | n | 占比 |
|---|---|---|
| CANONICAL_INVALID_FEE | 244 | 50.8% |
| CANONICAL_NO_POOL | 135 | 28.1% |
| CANONICAL_OK | 100 | 20.8% |
| CANONICAL_POOL_MISMATCH | 1 | 0.2% |

**可交易宇宙**：100 / 480。非法行 **不得**记为交易亏损 → `ENTRY_PREFLIGHT_REJECT`，`pnl_weth=0`。

### 4.2 fee_row × status

```
canonical_status  CANONICAL_INVALID_FEE  CANONICAL_NO_POOL  CANONICAL_OK  CANONICAL_POOL_MISMATCH
fee_row                                                                                          
1                                   193                  0             0                        0
2                                     1                  0             0                        0
3                                     1                  0             0                        0
4                                     1                  0             0                        0
5                                    12                  0             0                        0
30                                   36                  0             0                        0
100                                   0                135             4                        1
500                                   0                  0             3                        0
10000                                 0                  0            93                        0
```

### 4.3 非法 fee 的诊断性 repair（仅诊断，未用于策略）

```
repair   -1.0      100.0    500.0    3000.0
fee_row                                    
1              0      193        0        0
2              1        0        0        0
3              1        0        0        0
4              1        0        0        0
5              0        0       12        0
30             0        0        0       36
```

典型失败字符串：

- `invalid_fee=1;spacing=0|getPool_zero_or_fail|onchain_fee=100!=row_fee=1`（×193）
- `invalid_fee=30;…|onchain_fee=3000!=row_fee=30`（×36）
- `getPool_zero_or_fail|onchain_fee=10000!=row_fee=100`（×135，标为 NO_POOL）

校验项（每行）：`factory.getPool`、`feeAmountTickSpacing`、pool token0/1/fee/factory、WETH leg、bytecode、slot0、liquidity>0、fee 合法性。

---

## 5. Buy / Sell Path Forensics

### 5.1 范围

- 任一政策选中 ∪ 40 个随机未选中对照 = **188** deep rows  
- 生产等价：executor / WETH balance+allowance stateDiff / router `exactInputSingle` / quoter / amountIn=`16666666666666666`  
- `debug_traceCall`：revert 路径尝试；`local_fork`：**UNAVAILABLE_NO_ANVIL**  
- 第二 archive：`/tmp/rpc_url_b.txt` **缺失**

### 5.2 Buy class（focus 188）

| buy_class | n |
|---|---|
| ENTRY_PREFLIGHT_REJECT | 167 |
| BUY_TX_SUCCESS | 21 |

Atomic buy calibration：

| case | n | ok |
|---|---|---|
| BOTH_REJECT | 167 | True |
| QUOTE_AND_TX_OK | 21 | True |

→ quoter 与 router **无矛盾**；canonical-OK 上 fill rate = **100%**。

### 5.3 政策选中 × buy（focus）

| 政策 | 选中(focus内) | canonical OK | BUY_TX_SUCCESS | ENTRY_PREFLIGHT_REJECT |
|---|---|---|---|---|
| P0 | 131 | 9 | 9 | 122 |
| P1 | 122 | 8 | 8 | 114 |
| P2 | 41 | 7 | 7 | 34 |

### 5.4 Sell（仅 BUY_TX_SUCCESS 后）

全 holdout 修正 outcomes 中 `BUY_TX_SUCCESS`=21：

| exit_status | n |
|---|---|
| EXIT_SUCCESS | 10 |
| EXIT_PERMANENT_UNSELLABLE | 11 |

仅 `BUY_TX_SUCCESS + EXIT_PERMANENT_UNSELLABLE` 可产生接近 −100% 资本损失（≈ −0.0167 WETH / 笔，含 gas）。  
**不得**把 `ENTRY_PREFLIGHT_REJECT` / `ENTRY_TX_REVERT` 标为 `FATAL_POSITION`。

### 5.5 RPC Crosscheck

```
signal_id fee rpc_a_class rpc_b_class agree                                                                               note
                                            SECOND_ARCHIVE_RPC_UNAVAILABLE:/tmp/rpc_url_b.txt missing; crosscheck not executed
```

---

## 6. 修正 PnL 语义

# Corrected Outcome Spec (V1.1)

## Principle

Illegal / non-canonical / preflight-rejected opportunities are **not** position losses.

| State | pnl_weth |
|---|---|
| ENTRY_PREFLIGHT_REJECT | 0 |
| ENTRY_TX_REVERT | -buy_gas_weth |
| BUY_TX_SUCCESS + EXIT_SUCCESS / TEMP_RECOVERED | weth_out - weth_in - buy_gas - sell_gas |
| BUY_TX_SUCCESS + EXIT_PERMANENT_UNSELLABLE | recoverable_weth - weth_in - gas (≈ −amt_in) |
| INFRASTRUCTURE_UNKNOWN | NULL (excluded from filled EV; reported separately) |

`total_pnl_weth` = wallet WETH delta under capital constraints.  
`sum_return_for_reference` = legacy sum(return) for comparison only.

Label: **HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC** (not a new sealed confirmation).


### 6.1 全 holdout bucket（480）

| bucket | n |
|---|---|
| NO_FILL_PREFLIGHT | 380 |
| PREFLIGHT_ONLY_NO_FILL | 79 |
| FATAL_POSITION | 11 |
| ROUNDTRIP | 10 |

| buy_class | n |
|---|---|
| ENTRY_PREFLIGHT_REJECT | 380 |
| BUY_PREFLIGHT_PASS | 79 |
| BUY_TX_SUCCESS | 21 |

---

## 7. 诊断性重算 P0 / P1 / P2

> 策略与 threshold **完全冻结**。结果标签：`HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC`。

钱包冻结：`initial=0.5 WETH`，`max_concurrent=3`，`max_position=0.016666… WETH`。

### 7.1 政策比较表

| policy | model_selected | entry_preflight_reject | buy_filled | exit_success | fatal_position | fill_rate | total_pnl_weth | wallet_total_pnl_weth | wallet_return | max_drawdown | sum_return_for_reference | pnl_per_filled | capital_hours | canonical_ok_among_selected | canonical_bad_among_selected |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P0_C3_MOMENTUM | 131 | 122 | 9 | 7 | 2 | 0.068702 | -0.013915 | -0.013915 | -0.027829 | -0.039247 | -122.738897 | -0.001546 | 9.166667 | 9 | 122 |
| P1_G2_COMPETING | 122 | 114 | 8 | 6 | 2 | 0.065574 | -0.011946 | -0.011946 | -0.023893 | -0.037279 | -114.632616 | -0.001493 | 8.166667 | 8 | 114 |
| P2_C3_G2_VETO | 41 | 34 | 7 | 5 | 2 | 0.170732 | -0.009708 | -0.009708 | -0.019417 | -0.035041 | -34.510135 | -0.001387 | 7.166667 | 7 | 34 |

### 7.2 V1 vs V1.1（P1 对照）

| 指标 | V1（错误语义） | V1.1（修正） |
|---|---|---|
| Primary | P1_G2 | 同（冻结） |
| 选中数 | ~122 | 122 |
| buy OK / revert | 9 / 139 | filled 8；preflight reject 114（非法/不可报价） |
| 汇总 PnL | `total_net_weth_pnl≈−114.6`（实为 sum return） | `wallet_total_pnl_weth≈−0.012` |
| fatal 解读 | ~95%（含买失败） | fatal_position=2（仅成交后永久不可卖） |
| Holdout 标签 | `HOLDOUT_FAIL_ALL` | 测量修复诊断，**非**新 sealed 确认 |

### 7.3 钱包权益曲线摘要

| policy | n_events | final_wallet | min_wallet | wallet_return |
|---|---|---|---|---|
| P0_C3_MOMENTUM | 480 | 0.486085 | 0.465786 | -2.7829% |
| P1_G2_COMPETING | 480 | 0.488054 | 0.467754 | -2.3893% |
| P2_C3_G2_VETO | 480 | 0.490292 | 0.469992 | -1.9417% |

---

## 8. EXECUTION_TRUTH_MANIFEST（全文）

```json
{
  "created_utc": "2026-07-20T07:24:45Z",
  "role": "Base Greenfield V1.1 Execution Truth Audit",
  "immutability": "Must not modify out/ or out_v1/",
  "input_sha256": {
    "profit_validation_manifest": "18795ad5338cdfd17f37ddf39b85aec377bbd531f3ee285cd10d9542d81ffac8",
    "holdout_atomic_outcomes": "0ff9d5f088a94853585acd32bb34fc41ddc8ec553d036295c41346e9f475a7a3",
    "holdout_scores": "184e0cbe893ea28f752c314a2c7659dd9b531295a33d143878b5f16720db51e2",
    "dev_atomic_outcomes": "f1198ab7f9f8b5b0f5b465612ed490104be0266e3770e915475b8b5c8b6615d7",
    "panel": "32fc2d81d7d9b5d114b94d88d5a797c1b3e7c93ce6f6c24466e8dedeb4b89f44",
    "oof_c3": "07e0b606f48e0d14731e8b74923f23b521d72c91325cb14f41d790d214e13efa",
    "oof_g2": "f347d5f0ecba999f7e0af8f11335821379b6aa758fbedea1e5095cbf01e549dd",
    "design_freeze": "bd300cb459049ee34b634b086b97abc7eeccdc727ed15b78d96f1fbfe035d405",
    "final_greenfield": "c573b8e26c33733ff5043a15329f8d215df342fc78ea47d0f3afadd934df68ee",
    "final_profit_validation": "42f2caa9c6fe5e3c9579ed7b90651a1c5dd1f7cc047b3d8481da7450aa3557e0"
  },
  "g2_oof_sha256": "f347d5f0ecba999f7e0af8f11335821379b6aa758fbedea1e5095cbf01e549dd",
  "c3_oof_sha256": "07e0b606f48e0d14731e8b74923f23b521d72c91325cb14f41d790d214e13efa",
  "holdout_bounds": {
    "n_rows": 480,
    "decision_time_min": 1775187979,
    "decision_time_max": 1782804387,
    "n_pools": 164
  },
  "addresses": {
    "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
    "router": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "quoter": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    "weth": "0x4200000000000000000000000000000000000006",
    "executor": "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7"
  },
  "archive_rpc": {
    "A_url_sha256": "0b98445d8abca769ef04d3df92f47c07b9b84b6bbffcd882f32696f3349644d2",
    "B_url_sha256": null,
    "B_available": false
  },
  "pnl_semantics": {
    "ENTRY_PREFLIGHT_REJECT": "pnl_weth=0 (not a filled loss)",
    "ENTRY_TX_REVERT": "pnl_weth=-buy_gas_weth",
    "BUY_SUCCESS+SELL_SUCCESS": "weth_out - weth_in - gas",
    "BUY_SUCCESS+PERMANENT_UNSELLABLE": "recoverable - weth_in - gas (~-amt_in)",
    "total_pnl_weth": "wallet WETH delta, NOT sum(return)"
  },
  "status_taxonomy": {
    "buy": [
      "BUY_PREFLIGHT_PASS",
      "BUY_TX_SUCCESS",
      "ENTRY_PREFLIGHT_REJECT",
      "ENTRY_TX_REVERT",
      "INFRASTRUCTURE_UNKNOWN"
    ],
    "exit": [
      "EXIT_SUCCESS",
      "EXIT_TEMP_FAIL_RECOVERED",
      "EXIT_PERMANENT_UNSELLABLE",
      "EXIT_INFRASTRUCTURE_UNKNOWN"
    ]
  },
  "audit_code_sha256": "c051df0f3ed52fb061d074abbd1660761d6979cdbc330556fdd7832bbfdf21c0",
  "amt_in_wei": 16666666666666666,
  "wallet": {
    "initial_weth": 0.5,
    "max_concurrent": 3,
    "max_position_eth": 0.016666666666666666
  },
  "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
  "deploy_allowed": false,
  "manifest_refreshed_utc": "2026-07-20T07:25:45Z"
}
```

### 地址冻结

| 角色 | 地址 |
|---|---|
| factory | `0x33128a8fC17869897dcE68Ed026d694621f6FDfD` |
| router | `0x2626664c2603336E57B271c5C0b26F421741e481` |
| quoter | `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` |
| WETH | `0x4200000000000000000000000000000000000006` |
| executor | `0x4B7cd953871663474a0A25B81bddDf5C1f1195A7` |

Holdout 边界：n=480，pools=164，  
decision_time ∈ [1775187979, 1782804387]。

---

## 9. FINAL_EXECUTION_TRUTH（全文）

```json
{
  "created_utc": "2026-07-20T07:24:58Z",
  "role": "Base Greenfield V1.1 Execution Truth Audit",
  "verdict": "MIXED_FAILURE",
  "component_findings": [
    "UNIVERSE_REGISTRY_CONTAMINATION_CONFIRMED",
    "EXECUTION_PIPELINE_BUG_CONFIRMED"
  ],
  "kill_g2": false,
  "keep_g2_discovery_lead": true,
  "start_fresh_prospective_shadow": true,
  "deploy_allowed": false,
  "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
  "canonical_summary": {
    "n_holdout": 480,
    "n_canonical_ok": 100,
    "n_canonical_bad": 380,
    "canonical_bad_rate": 0.7916666666666666,
    "status_counts": {
      "CANONICAL_INVALID_FEE": 244,
      "CANONICAL_NO_POOL": 135,
      "CANONICAL_OK": 100,
      "CANONICAL_POOL_MISMATCH": 1
    },
    "fee_row_value_counts": {
      "1": 193,
      "100": 140,
      "10000": 93,
      "30": 36,
      "5": 12,
      "500": 3,
      "2": 1,
      "3": 1,
      "4": 1
    }
  },
  "legacy_holdout_artifact": {
    "buy_ok": 9,
    "buy_revert": 139,
    "note": "V1 labeled many ENTRY failures as -100% returns; total_net_weth_pnl used sum(return)"
  },
  "corrected_p1": {
    "policy": "P1_G2_COMPETING",
    "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
    "model_selected": 122,
    "preflight_pass": 8,
    "buy_filled": 8,
    "exit_success": 6,
    "entry_revert": 0,
    "entry_preflight_reject": 114,
    "fatal_position": 2,
    "fill_rate": 0.06557377049180328,
    "pnl_per_selected": -9.792210627549043e-05,
    "pnl_per_filled": -0.001493312120701229,
    "total_pnl_weth": -0.011946496965609833,
    "sum_return_for_reference": -114.6326156508845,
    "wallet_return": -0.023892993931219797,
    "wallet_final_weth": 0.4880535030343901,
    "wallet_total_pnl_weth": -0.011946496965609898,
    "max_drawdown": -0.03727939703247651,
    "capital_hours": 8.166666666666666,
    "canonical_ok_among_selected": 8,
    "canonical_bad_among_selected": 114
  },
  "corrected_p0": {
    "policy": "P0_C3_MOMENTUM",
    "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
    "model_selected": 131,
    "preflight_pass": 9,
    "buy_filled": 9,
    "exit_success": 7,
    "entry_revert": 0,
    "entry_preflight_reject": 122,
    "fatal_position": 2,
    "fill_rate": 0.06870229007633588,
    "pnl_per_selected": -0.00010621764790408308,
    "pnl_per_filled": -0.00154605687504832,
    "total_pnl_weth": -0.013914511875434884,
    "sum_return_for_reference": -122.73889654547399,
    "wallet_return": -0.02782902375086993,
    "wallet_final_weth": 0.48608548812456503,
    "wallet_total_pnl_weth": -0.013914511875434965,
    "max_drawdown": -0.03924741194230158,
    "capital_hours": 9.166666666666666,
    "canonical_ok_among_selected": 9,
    "canonical_bad_among_selected": 122
  },
  "p1_filled_n": 8,
  "p1_pnl_on_filled_weth": -0.011946496965609833,
  "p1_preflight_reject_share_among_selected": 0.9344262295081968,
  "rpc_a_calls": 204,
  "rpc_b_available": false,
  "manifest_path": "/root/base_greenfield_directional_v0/out_v1_1/EXECUTION_TRUTH_MANIFEST.json",
  "notes": [
    "Does not modify out/ or out_v1/",
    "Does not change G2/C3/thresholds/TB_A",
    "Not a new sealed holdout confirmation"
  ],
  "root_cause_attribution": {
    "primary": "UNIVERSE_REGISTRY_CONTAMINATION",
    "secondary": "EXECUTION_PIPELINE_BUG_V1_LABELING",
    "evidence": {
      "holdout_canonical_bad_rate": 0.7916666666666666,
      "fee1_rows": 193,
      "fee1_repair_to_100": 0,
      "fee30_repair_to_3000": 0,
      "canonical_ok_focus_buy_fill_rate": 1.0,
      "v1_sum_return_p1_reference": -114.6326156508845,
      "v1_1_wallet_pnl_p1_weth": -0.011946496965609898,
      "note": "V1 HOLDOUT_FAIL_ALL (~-114 sum return) collapses to ~-0.012 WETH wallet delta after treating illegal/preflight as non-fills"
    },
    "ruled_out_or_weak": {
      "buy_simulator_on_canonical_ok": "WEAKLY_RULED_OUT — 21/21 canonical-OK focus routes BUY_TX_SUCCESS on archive eth_call+stateDiff",
      "archive_rpc_state_override": "INCONCLUSIVE_NO_SECOND_RPC — single archive; local fork UNAVAILABLE_NO_ANVIL",
      "g2_directional_failure": "INSUFFICIENT_FILLED_N — n_filled=8, slightly negative but dominated by contamination; not KILL_G2",
      "real_entry_untradeability_on_canonical_ok": "NOT_CONFIRMED — fill rate ~100% among canonical-OK selected"
    }
  },
  "disposition": {
    "G2_status": "DISCOVERY_LEAD_RETAINED",
    "action": "START_FRESH_PROSPECTIVE_SHADOW",
    "deploy": false,
    "kill_g2": false,
    "reason": "HOLDOUT_FAIL_ALL explained primarily by registry fee contamination + V1 labeling buy failures as -100%; true filled sample too small/contaminated to confirm directional failure"
  },
  "updated_utc": "2026-07-20T07:25:36Z"
}
```

---

## 10. CORRECTED_POLICY_COMPARISON（CSV 原文）

```csv
policy,label,model_selected,preflight_pass,buy_filled,exit_success,entry_revert,entry_preflight_reject,fatal_position,fill_rate,pnl_per_selected,pnl_per_filled,total_pnl_weth,sum_return_for_reference,wallet_return,wallet_final_weth,wallet_total_pnl_weth,max_drawdown,capital_hours,canonical_ok_among_selected,canonical_bad_among_selected
P0_C3_MOMENTUM,HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC,131,9,9,7,0,122,2,0.06870229007633588,-0.00010621764790408308,-0.00154605687504832,-0.013914511875434884,-122.73889654547399,-0.02782902375086993,0.48608548812456503,-0.013914511875434965,-0.03924741194230158,9.166666666666666,9,122
P1_G2_COMPETING,HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC,122,8,8,6,0,114,2,0.06557377049180328,-9.792210627549043e-05,-0.001493312120701229,-0.011946496965609833,-114.6326156508845,-0.023892993931219797,0.4880535030343901,-0.011946496965609898,-0.03727939703247651,8.166666666666666,8,114
P2_C3_G2_VETO,HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC,41,7,7,5,0,34,2,0.17073170731707318,-0.00023679229208281886,-0.0013869262821993675,-0.009708483975395574,-34.510134871471635,-0.019416967950791175,0.4902915160246044,-0.009708483975395588,-0.0350413840422622,7.166666666666666,7,34
```

---

## 11. REPRODUCE

# Reproduce V1.1 Execution Truth Audit

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v1_1.execution_truth_audit
```

Requires archive RPC in `/tmp/rpc_url.txt`. Optional second archive: `/tmp/rpc_url_b.txt`.

**Immutability:** does not write to `out/` or `out_v1/`.

**Outputs:** `out_v1_1/` (see EXECUTION_TRUTH_MANIFEST.json for input SHA256).

**Label:** `HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC` — not a new sealed holdout confirmation.

**Verdict:** `MIXED_FAILURE`  
**G2:** DISCOVERY_LEAD retained; `kill_g2=False`; deploy forbidden.

Canonical reuse: if `out_v1_1/CANONICAL_POOL_AUDIT.parquet` exists it is reused; delete to force re-audit.


---

## 12. RUN.log（全文）

```
=== MANIFEST ===
=== CANONICAL AUDIT 480 ===
  reused CANONICAL_POOL_AUDIT.parquet n=480
{'CANONICAL_INVALID_FEE': 244, 'CANONICAL_NO_POOL': 135, 'CANONICAL_OK': 100, 'CANONICAL_POOL_MISMATCH': 1}
tradeable 100 / 480
=== BUY FORENSICS ===
  buy 0/188 rpcA=0
  buy 20/188 rpcA=56
  buy 40/188 rpcA=58
  buy 60/188 rpcA=58
  buy 80/188 rpcA=58
  buy 100/188 rpcA=58
  buy 120/188 rpcA=58
  buy 140/188 rpcA=58
  buy 160/188 rpcA=58
  buy 180/188 rpcA=58
=== SELL + CORRECTED OUTCOMES ===
=== POLICY DIAGNOSTIC ===
P0_C3_MOMENTUM {'model_selected': 131, 'buy_filled': 9, 'total_pnl_weth': -0.013914511875434884, 'wallet_total_pnl_weth': -0.013914511875434965, 'fill_rate': 0.06870229007633588, 'fatal_position': 2, 'entry_preflight_reject': 122}
P1_G2_COMPETING {'model_selected': 122, 'buy_filled': 8, 'total_pnl_weth': -0.011946496965609833, 'wallet_total_pnl_weth': -0.011946496965609898, 'fill_rate': 0.06557377049180328, 'fatal_position': 2, 'entry_preflight_reject': 114}
P2_C3_G2_VETO {'model_selected': 41, 'buy_filled': 7, 'total_pnl_weth': -0.009708483975395574, 'wallet_total_pnl_weth': -0.009708483975395588, 'fill_rate': 0.17073170731707318, 'fatal_position': 2, 'entry_preflight_reject': 34}
=== VERDICT ===
{
  "created_utc": "2026-07-20T07:24:58Z",
  "role": "Base Greenfield V1.1 Execution Truth Audit",
  "verdict": "MIXED_FAILURE",
  "component_findings": [
    "UNIVERSE_REGISTRY_CONTAMINATION_CONFIRMED",
    "EXECUTION_PIPELINE_BUG_CONFIRMED"
  ],
  "kill_g2": false,
  "keep_g2_discovery_lead": true,
  "start_fresh_prospective_shadow": true,
  "deploy_allowed": false,
  "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
  "canonical_summary": {
    "n_holdout": 480,
    "n_canonical_ok": 100,
    "n_canonical_bad": 380,
    "canonical_bad_rate": 0.7916666666666666,
    "status_counts": {
      "CANONICAL_INVALID_FEE": 244,
      "CANONICAL_NO_POOL": 135,
      "CANONICAL_OK": 100,
      "CANONICAL_POOL_MISMATCH": 1
    },
    "fee_row_value_counts": {
      "1": 193,
      "100": 140,
      "10000": 93,
      "30": 36,
      "5": 12,
      "500": 3,
      "2": 1,
      "3": 1,
      "4": 1
    }
  },
  "legacy_holdout_artifact": {
    "buy_ok": 9,
    "buy_revert": 139,
    "note": "V1 labeled many ENTRY failures as -100% returns; total_net_weth_pnl used sum(return)"
  },
  "corrected_p1": {
    "policy": "P1_G2_COMPETING",
    "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
    "model_selected": 122,
    "preflight_pass": 8,
    "buy_filled": 8,
    "exit_success": 6,
    "entry_revert": 0,
    "entry_preflight_reject": 114,
    "fatal_position": 2,
    "fill_rate": 0.06557377049180328,
    "pnl_per_selected": -9.792210627549043e-05,
    "pnl_per_filled": -0.001493312120701229,
    "total_pnl_weth": -0.011946496965609833,
    "sum_return_for_reference": -114.6326156508845,
    "wallet_return": -0.023892993931219797,
    "wallet_final_weth": 0.4880535030343901,
    "wallet_total_pnl_weth": -0.011946496965609898,
    "max_drawdown": -0.03727939703247651,
    "capital_hours": 8.166666666666666,
    "canonical_ok_among_selected": 8,
    "canonical_bad_among_selected": 114
  },
  "corrected_p0": {
    "policy": "P0_C3_MOMENTUM",
    "label": "HOLDOUT_MEASUREMENT_REPAIR_DIAGNOSTIC",
    "model_selected": 131,
    "preflight_pass": 9,
    "buy_filled": 9,
    "exit_success": 7,
    "entry_revert": 0,
    "entry_preflight_reject": 122,
    "fatal_position": 2,
    "fill_rate": 0.06870229007633588,
    "pnl_per_selected": -0.00010621764790408308,
    "pnl_per_filled": -0.00154605687504832,
    "total_pnl_weth": -0.013914511875434884,
    "sum_return_for_reference": -122.73889654547399,
    "wallet_return": -0.02782902375086993,
    "wallet_final_weth": 0.48608548812456503,
    "wallet_total_pnl_weth": -0.013914511875434965,
    "max_drawdown": -0.03924741194230158,
    "capital_hours": 9.166666666666666,
    "canonical_ok_among_selected": 9,
    "canonical_bad_among_selected": 122
  },
  "p1_filled_n": 8,
  "p1_pnl_on_filled_weth": -0.011946496965609833,
  "p1_preflight_reject_share_among_selected": 0.9344262295081968,
  "rpc_a_calls": 204,
  "rpc_b_available": false,
  "manifest_path": "/root/base_greenfield_directional_v0/out_v1_1/EXECUTION_TRUTH_MANIFEST.json",
  "notes": [
    "Does not modify out/ or out_v1/",
    "Does not change G2/C3/thresholds/TB_A",
    "Not a new sealed holdout confirmation"
  ]
}
```

---

## 13. 审查清单（建议第三方核对）

- [ ] `out/` / `out_v1/` 未被本轮修改（Manifest 输入 SHA 与磁盘一致）
- [ ] G2/C3 OOF、threshold、TB_A 未改
- [ ] 非法 fee 行未计入 −100% 仓位亏损
- [ ] `total_pnl_weth` 为钱包 WETH delta，不是 `sum(return)`
- [ ] 结果标为 diagnostic，未当作新的 sealed holdout pass
- [ ] `deploy_allowed=false`；未因修复后 PnL 改选 Primary
- [ ] 若需第二 RPC / anvil：当前缺口已在 RPC_CROSSCHECK / local_fork 标明

---

## 14. 与 R0/V1 审查包的衔接

| 轮次 | 文档 | 关键结论 |
|---|---|---|
| R0 Discovery | `GREENFIELD_R0_V1_审查包_完整合并.md` §Discovery | G2=`DISCOVERY_LEAD`，未打赢 C3（tick-path） |
| V1 Profit Validation | 同上 §V1 | DEV 上 G2 翻盘；Holdout=`HOLDOUT_FAIL_ALL`（−114） |
| **V1.1 Execution Truth** | **本文件** | Holdout 失败主因污染+标签；修正后 ≈−0.012 WETH；保留 DISCOVERY_LEAD；开 shadow；不部署 |

---

*本合并文件由 `out_v1_1/` 全部关键产物自动汇总生成，便于整份审查。*
