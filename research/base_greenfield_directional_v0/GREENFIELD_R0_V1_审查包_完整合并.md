# Base Greenfield R0 Discovery + V1 Profit Validation — 审查包（单文件合并）

> 生成时间：2026-07-20  
> 项目根：`/root/base_greenfield_directional_v0`  
> 用途：方便整份丢给第三方 / 自己审查。  
> **部署状态：禁止部署（`deploy_allowed=false`）**

---

## 0. 一句话结论

1. **Discovery（R0）**：在 PIT Greenfield universe（非 KEYSTONE pass）上，正式模型 `G2_HGB_COMPETING` 以 tick-path OOF EV **+6.7%** 打赢 `C1_PRICE_ONLY`（−15.0%），verdict=`DISCOVERY_LEAD`；但未打赢对照 `C3_SIMPLE_MOMENTUM`（+9.7%）。截面大多为 singleton，`rank_ic=null`。
2. **Profit Validation（V1）**：换成**生产名义原子买卖**（$50@3000）后，DEV 上排序翻转——**G2 最优、C3 变差**：
   - P1_G2：`total_net_weth_pnl ≈ +4.54`
   - P0_C3：`≈ −13.38`
   - P2_C3+G2 veto：`≈ −0.19`
   - 因此 **`kill_g2=false`**，Primary = **`P1_G2_COMPETING`**
3. **SEALED_HOLDOUT（一次、不可回改）**：**`HOLDOUT_FAIL_ALL`**。Primary 持有约 122 笔，`total_net_weth_pnl ≈ −114.6`，fatal/sell_failure ≈ 95%（买侧 `DETERMINISTIC_REVERT` 主导）。次级 P0/P2 同样全负。
4. **不得部署**。Discovery / V1 均未修改生产 KEYSTONE。

---

## 1. 两轮角色与硬边界

| 轮次 | 角色 | 允许 | 禁止 |
|---|---|---|---|
| R0 | Greenfield Directional Alpha Research | 新建 panel / 标签 / G1–G3 / C0–C3 | KEYSTONE pass 作训练宇宙；KS/F10 score 作特征；改生产 |
| V1 | Profit Validation | 冻结 artifact、OOF 审计、原子 PnL、P0/P1/P2、一次 holdout | 新特征、新模型、调参、改 SEALED_HOLDOUT / Manifest |

可复用基建：ClickHouse、atomic sell sim、trial ledger 模式、executor 资金约束文档、rug 校准样本。

---

## 2. 路径索引

### 2.1 Discovery（`out/`）

| 产物 | 路径 |
|---|---|
| 终裁 | `/root/base_greenfield_directional_v0/out/FINAL_GREENFIELD.json` |
| OOF 计分卡 | `out/metrics/oof_scorecard.csv` |
| Trial ledger | `out/metrics/trial_ledger.csv` |
| Panel | `out/panel/greenfield_panel_15-60m.parquet` |
| Panel manifest | `out/panel/manifest.json` |
| Labels | `out/labels/labels_full.parquet` |
| Features | `out/features/feature_matrix.parquet` |
| C3 OOF | `out/predictions/oof_C3_SIMPLE_MOMENTUM.parquet` |
| G2 OOF | `out/predictions/oof_G2_HGB_COMPETING.parquet` |
| 设计冻结 | `configs/design_freeze.json` |
| Hard bans | `configs/hard_bans.json` |
| Charter | `CHARTER.md` |

### 2.2 Profit Validation（`out_v1/`）

| 产物 | 路径 |
|---|---|
| 终裁 | `out_v1/FINAL_PROFIT_VALIDATION.json` |
| Manifest（冻结） | `out_v1/PROFIT_VALIDATION_MANIFEST.json` |
| Manifest SHA256 | `out_v1/PROFIT_VALIDATION_MANIFEST.sha256` = `18795ad5338cdfd17f37ddf39b85aec377bbd531f3ee285cd10d9542d81ffac8` |
| Artifact freeze | `out_v1/ARTIFACT_FREEZE.json` |
| OOF provenance | `out_v1/OOF_PROVENANCE.csv` |
| Atomic label spec | `out_v1/ATOMIC_LABEL_SPEC.md` |
| Atomic calibration | `out_v1/ATOMIC_CALIBRATION.csv` + `ATOMIC_CALIBRATION_SUMMARY.json` |
| DEV 政策比较 | `out_v1/DEV_POLICY_COMPARISON.csv` |
| DEV atomic outcomes | `out_v1/DEV_ATOMIC_OUTCOMES.parquet` |
| Primary 选择 | `out_v1/PRIMARY_SELECTION.json` |
| Holdout 结果 | `out_v1/SEALED_HOLDOUT_RESULT.json` |
| Holdout scores / outcomes | `out_v1/HOLDOUT_SCORES.parquet`, `HOLDOUT_ATOMIC_OUTCOMES.parquet` |
| Shadow spec | `out_v1/SHADOW_SPEC.md` |

---

## 3. Discovery 冻结设计

| 项 | 值 |
|---|---|
| Universe | Base UniV3 WETH（`discovered_pools`: v3 + has_weth_leg）；**非** KEYSTONE live-pass |
| 日历 | decision_time ∈ [2025-10-01, 2026-07-01) |
| 主 age bucket | **15–60m**（锚点 20/30/40/50m） |
| Panel | 2399 行 / 764 pools / 4332 universe pools |
| 切分 | LABEL_DESIGN 479 / DEV 1440 / SEALED_HOLDOUT 480 |
| Primary horizon | **H = 3600s**（label-design 冻结） |
| Primary barrier | **TB_A**：TP +50% / SL −35% |
| Embargo | 1 day；残差仅 prior-train fold |
| 名义（Discovery 标签） | 研究路径 tick-path；V1 才用生产 $50 |

Eligibility（PIT）：池存在、age 在桶内、≥6 个 pre 分钟、决策前 10m 有买有卖、liq>0。禁止未来 quote / priced_ok / 存活筛选。

---

## 4. Discovery OOF 结果（tick-path 主表）

来源：`out/metrics/oof_scorecard.csv`  
主表强制 `row_in_training_set=false`（V1 审计确认 OK）。

| model_id | OOF EV (accepted) | vs C1 incremental | worst_fold | n_accepted |
|---|---:|---:|---:|---:|
| C0_RANDOM | −5.8% | +9.3pp | −30.0% | 245 |
| **C1_PRICE_ONLY** | **−15.0%** | 0 | −27.0% | 325 |
| C2_SELLABILITY_ONLY | −13.7% | +1.3pp | −27.0% | 338 |
| **C3_SIMPLE_MOMENTUM** | **+9.7%** | +24.7pp | −23.6% | 146 |
| G1_HGB_RANK | −13.8% | +1.3pp | −27.0% | 336 |
| **G2_HGB_COMPETING** | **+6.7%** | **+21.7pp** | −9.0% | 167 |
| G3_HMM | −4.2% | +10.8pp | −16.5% | 278 |

### Discovery 终裁（`FINAL_GREENFIELD.json`）

- `result`: `CANDIDATES`（n=1）
- 候选：`G2_HGB_COMPETING`
- `verdict`: **`DISCOVERY_LEAD`**
- `oof_ev`: +0.067；`price_only_oof_ev`: −0.150；`incremental_ev`: +0.217
- `failure_modes`: `does_not_beat_C3_SIMPLE_MOMENTUM`
- `rank_ic`: null（多数决策时刻仅 1 个候选）
- `decay_status`: ACTIVE（晚段 EV 高于早段）
- `top5_dependency`: 很高（≈1.47，收益集中）
- SEALED_HOLDOUT：**未用于调参**

**Discovery 解读（审查要点）**

- 打赢 C1 → 允许 `DISCOVERY_LEAD`；未打赢 C3 → 不能当已确认 alpha。
- 截面供给极薄（≈2393 decision_times / 2399 rows）→ XS rank 路线信息量不足。
- Discovery 主 PnL 是 **forward tick 路径**，不是生产原子成交。

---

## 5. V1：冻结与 OOF 审计

### 5.1 Artifact freeze

`out_v1/ARTIFACT_FREEZE.json` 记录了 C3 公式、G2 配方、H/barrier、universe、split、OOF parquet SHA256、源码 SHA256、Python/sklearn 版本。  
Discovery `out/` / `configs/` **未被 V1 修改**（抽查 oof_g2 / FINAL_GREENFIELD / design_freeze hash 一致）。

### 5.2 OOF provenance

状态：**`OOF_PROVENANCE_OK`**

| model | n_rows | n_selected | all `row_in_training_set=false` |
|---|---:|---:|---|
| C3_SIMPLE_MOMENTUM | 1089 | 146 | true |
| G2_HGB_COMPETING | 1089 | 167 | true |
| C1_PRICE_ONLY | 1089 | 325 | true |

导出：`out_v1/OOF_PROVENANCE.csv`

---

## 6. V1：生产级 Atomic 标签

### 6.1 定义（`ATOMIC_LABEL_SPEC.md`）

| 项 | 冻结值 |
|---|---|
| amt_in | `16666666666666666` wei（$50 @ 3000；≤ 0.02 ETH cap） |
| entry delay | decision_block **+1** |
| router / quoter | SwapRouter02 / QuoterV2（Base） |
| gas debit | 0.0002 WETH（成功路径扣减） |
| 出场时机 | TB_A 路径触发或 H=3600s（tick 仅定 timing） |
| **Primary PnL** | **`realized_net_weth_return`（原子）** |
| Tick path | comparison only |

状态：`SELL_SUCCESS` / `DETERMINISTIC_REVERT` / `ECONOMICALLY_UNSELLABLE` / `INFRASTRUCTURE_UNKNOWN`

### 6.2 校准（`ATOMIC_CALIBRATION_SUMMARY.json`）

| 指标 | 值 |
|---|---|
| n_success | 3（GL4 已知成功卖） |
| n_rug | 5（JOTCHUA/BOT/PARADAENGI…） |
| n_thin_probes | 15 |
| false_sellable_rate | **0.0** |
| false_unsellable_rate | **0.0** |
| unknown_rate | 0.261（含 thin probes） |
| weth_out_error_median | ~2e-7 |
| success_only | **false** |
| calibration_pass | **true** |
| 缺口 | `recover_after_temp_fail` **不在校准集**（已文档化） |

---

## 7. V1：三个冻结政策

| ID | 定义（不新搜阈值） |
|---|---|
| **P0_C3_MOMENTUM** | 现有 C3：`score=early_ret`；train-fold q60 + top-k/非重叠（OOF selected 原样） |
| **P1_G2_COMPETING** | 现有 G2 OOF selected（EU = 0.5·p_tp − 0.35·p_sl − 1·p_fatal） |
| **P2_C3_G2_VETO** | C3 入场候选 ∧ G2 EU > 0 ∧ `p_fatal < 0.35`（STACK 原冻结规则，非新搜） |

---

## 8. V1：DEV 公平比较（原子 Primary PnL）

来源：`out_v1/DEV_POLICY_COMPARISON.csv`，mode=`raw_production`

| policy | n_acc | total_net_weth_pnl | ev_accepted | ev_per_opp | pnl / capital-hour | fatal_rate | max_dd | ES_5% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **P1_G2_COMPETING** | 158* | **+4.54** | +0.029 | — | **+0.029** | 0.297 | −16.35 | — |
| P2_C3_G2_VETO | — | **−0.19** | −0.002 | — | −0.002 | 0.366 | −17.65 | — |
| P0_C3_MOMENTUM | — | **−13.38** | −0.100 | — | −0.102 | 0.381 | −21.88 | — |

\*具体 n_accepted_labeled 以 CSV 为准；DEV atomic union ≈ 205 笔机会级模拟。

另有 matched trade count / matched capital-hours / wallet_constrained（0.5 ETH、≤3 并发）行，同文件。

### DEV 原子状态分布（`DEV_ATOMIC_OUTCOMES.parquet`）

- buy：BUY_OK 202 / DETERMINISTIC_REVERT 3  
- sell：SELL_SUCCESS 131 / DETERMINISTIC_REVERT 44 / ECONOMICALLY_UNSELLABLE 16 / INFRASTRUCTURE_UNKNOWN 14  

### Primary 选择

```json
{
  "primary_policy": "P1_G2_COMPETING",
  "ranked": ["P1_G2_COMPETING", "P2_C3_G2_VETO", "P0_C3_MOMENTUM"],
  "kill_g2": false,
  "secondary_policies": ["P0_C3_MOMENTUM", "P2_C3_G2_VETO"]
}
```

**关键翻转**：tick-path 上 C3 > G2；**生产原子口径下 G2 > C3**。复杂模型无额外优先级——是 PnL / capital-hour / 风险共同决定。

---

## 9. Manifest（打开 Holdout 前冻结）

`PROFIT_VALIDATION_MANIFEST.json`  
SHA256：`18795ad5338cdfd17f37ddf39b85aec377bbd531f3ee285cd10d9542d81ffac8`  
创建后**禁止修改**（已校验未篡改）。

要点：

- Primary：`P1_G2_COMPETING`
- amt_in：16666666666666666 wei
- wallet：0.5 ETH，max concurrent 3
- Holdout 范围：480 rows，164 pools，`decision_time` ∈ [1775187979, 1782804387]
- `deploy_allowed`: false

---

## 10. SEALED_HOLDOUT（一次性）

状态：**`HOLDOUT_FAIL_ALL`**

操作：DEV 上拟合冻结配方阈值 → 一次性打分 holdout → 原子模拟。未因 holdout 改模型/阈值/宇宙/标签/出场。

### Primary（P1_G2）

| 指标 | 值 |
|---|---:|
| n_opportunities | 480 |
| n_accepted_labeled | 122 |
| acceptance_rate | 25.4% |
| **total_net_weth_pnl** | **−114.63** |
| ev_per_accepted | −0.940 |
| ev_per_opportunity | −0.239 |
| pnl_per_capital_hour | −14.33 |
| max_drawdown | −114.95 |
| expected_shortfall_5pct | −1.0 |
| fatal_rate | **0.951** |
| sell_failure_rate | **0.951** |
| median / trimmed_mean | −1.0 / −1.0 |
| rank_ic | null |

### Secondaries（同样失败）

| policy | n_acc | total_net_weth_pnl | fatal_rate |
|---|---:|---:|---:|
| P0_C3_MOMENTUM | 131 | −122.74 | 0.947 |
| P2_C3_G2_VETO | 41 | −34.51 | 0.878 |

### Holdout 原子状态（`HOLDOUT_ATOMIC_OUTCOMES.parquet`）

- buy：DETERMINISTIC_REVERT **139** / BUY_OK **9**
- sell：DETERMINISTIC_REVERT 140 / SELL_SUCCESS 7 / ECONOMICALLY_UNSELLABLE 1  
→ 失败主因是**买侧不可执行/revert**，不是“模型在可成交样本上小亏”。

### 数据质量观察（未据此改策略）

- Discovery labels 中存在非法 UniV3 **`fee=1`** 行（registry 噪声）；V1 未修改 discovery artifact。
- Holdout 段买失败率远高于 DEV，可能叠加：fee 脏数据、池生命周期末端、archive 块可用性差异。  
- 审查时应区分：**策略失效** vs **标签/执行口径在 holdout 段系统性不可成交**。

---

## 11. Prospective Shadow

`out_v1/SHADOW_SPEC.md`

- 同时记录 P1（primary）+ P0/P2（secondary）
- **零资金**；决策先落盘再哈希；不得按实时盈亏改策略
- Recorder stub：`src/v1/shadow_recorder.py`（需人类批准后另启，本轮未启动）

---

## 12. 裁决词汇与最终标签

| 阶段 | 标签 |
|---|---|
| Discovery | `DISCOVERY_LEAD`（G2；未打赢 C3） |
| V1 DEV Primary | `P1_G2_COMPETING`（`kill_g2=false`） |
| V1 Holdout | **`HOLDOUT_FAIL_ALL`** |
| 部署 | **`deploy_allowed=false`** |

可选审查判语（供指挥侧使用，非代码自动输出）：

- 若强调“原子 holdout 已杀死确认”→ **策略确认失败，保持研究/影子，不上资金**。
- 若强调“holdout 买侧 95% revert”→ 需在**下一轮（新版本）**单独做执行标签健康检查；**不得**回改本轮 Manifest / holdout。

---

## 13. 建议审查顺序

1. 本导读 §0 + §10（holdout 失败）  
2. `out_v1/FINAL_PROFIT_VALIDATION.json`  
3. `out_v1/PROFIT_VALIDATION_MANIFEST.json` + `.sha256`  
4. `out_v1/DEV_POLICY_COMPARISON.csv`（看 C3↔G2 翻转）  
5. `out_v1/ATOMIC_CALIBRATION.csv` / summary  
6. `out_v1/OOF_PROVENANCE_AUDIT.json`  
7. Discovery：`out/FINAL_GREENFIELD.json` + `out/metrics/oof_scorecard.csv`  
8. 需要时再看 atomic outcomes parquet 与 `fee=1` 分布

---

## 14. 附录：关键数字速查

```
Discovery OOF (tick-path):
  C1_PRICE_ONLY          EV = -15.0%
  C3_SIMPLE_MOMENTUM     EV = +9.7%
  G2_HGB_COMPETING       EV = +6.7%   → DISCOVERY_LEAD

V1 DEV atomic ($50):
  P0_C3                  total_net_weth_pnl ≈ -13.38
  P1_G2                  total_net_weth_pnl ≈ +4.54   → PRIMARY
  P2_C3_G2_VETO          total_net_weth_pnl ≈ -0.19
  kill_g2 = false

V1 SEALED_HOLDOUT:
  P1_G2                  total_net_weth_pnl ≈ -114.63 → HOLDOUT_FAIL_ALL
  P0_C3                  ≈ -122.74
  P2                     ≈ -34.51

Manifest SHA256:
  18795ad5338cdfd17f37ddf39b85aec377bbd531f3ee285cd10d9542d81ffac8
```

---

## 15. 附录：嵌入终裁 JSON

### 15.1 Discovery `FINAL_GREENFIELD.json`（摘要字段）

- result: `CANDIDATES`
- candidate_id: `G2_HGB_COMPETING`
- verdict: `DISCOVERY_LEAD`
- oof_ev: `0.0671673003735786`
- price_only_oof_ev: `-0.1502355652699869`
- incremental_ev: `0.2174028656435655`
- notes.beats_c3: `false`
- sealed_holdout_untouched_for_tuning: `true`
- deploy_allowed: `false`

完整文件：`/root/base_greenfield_directional_v0/out/FINAL_GREENFIELD.json`

### 15.2 V1 `FINAL_PROFIT_VALIDATION.json`

完整文件：`/root/base_greenfield_directional_v0/out_v1/FINAL_PROFIT_VALIDATION.json`

关键字段：

- oof_audit: `OOF_PROVENANCE_OK`
- calibration_pass: `true`
- primary_policy: `P1_G2_COMPETING`
- kill_g2: `false`
- holdout_verdict: **`HOLDOUT_FAIL_ALL`**
- discovery_artifacts_modified: `false`
- deploy_allowed: `false`

---

*本文件为审查便利合并件；权威机器可读产物仍以 `out/` 与 `out_v1/` 下 JSON/CSV/Parquet 为准。*
