# Base Greenfield V3 — Narrow-Market Rescue 审查包（单文件合并）

> 生成时间：2026-07-20  
> 项目根：`/root/base_greenfield_directional_v0`  
> 角色：**Base Uniswap New-Pool Narrow-Market Rescue Research Agent**  
> 阶段：`V3_NARROW_MARKET_RESCUE`  
> **裁决：`LOTTERY_DEPENDENT_REJECT`**（holdout 一次性确认；未开 untouched）  
> **场所否决：禁止**（`venue_rejection=false`，`stop_research=false`）  
> **部署：禁止**（`deploy_allowed=false`）

---

## 1. 一句话结论

在**固定** Base UniV3 WETH 新池场所内，对本轮注册的条件性假设（双头 Risk/Opportunity + $1000 固定本金月提现钱包）做了完整 OOF → INTERNAL_HOLDOUT 检验。

结果：**未确认可重复的小容量正交优势**。OOF 各路线钱包 PnL 均为负；最优 OOF 路线 `H_TRADEABLE_RISK_SPLIT` 在 holdout 上仍为负，且独立右尾事件不足 → 终裁 `LOTTERY_DEPENDENT_REJECT`。

这**不是**「建议换市场」，也**不是** Phase0 式场所一票否决。Phase0 门槛仅作诊断对照；研究在场所内继续受假设级失败约束。

---

## 2. 与 V2 Phase0 的关系

| 项 | V2 Phase0 | V3 Rescue |
|---|---|---|
| 问题 | 宇宙是否够格开题 | 条件状态是否可重复捕捉右尾 |
| 场所失败 → 停研 | `stop_research=true` | **禁止**；`stop_research=false` |
| Alpha / OOF | 未做 | 已做 |
| 本金模型 | 无 | $1000 固定本金 + 月末提现、不复利 |
| 裁决词 | `NO_VIABLE_BASE_NEW_POOL_UNIVERSE` | `LOTTERY_DEPENDENT_REJECT` 等七词表 |

`DIAGNOSTIC_PHASE0_CROSSWALK.csv`：五桶 Phase0 `viability_pass=False`，但 `rescue_action=DIAGNOSTIC_ONLY_NOT_VENUE_VETO`。

---

## 3. 路径索引（`out_v3/`）

| 产物 | 路径 |
|---|---|
| 终裁 | `out_v3/RESCUE_FINAL.json` |
| 假设台账 | `out_v3/HYPOTHESIS_LEDGER.csv` |
| OOF 记分卡 | `out_v3/OOF_SCORECARD.csv` |
| 右尾审计 | `out_v3/RIGHT_TAIL_AUDIT.csv` |
| 月提现 | `out_v3/MONTHLY_WITHDRAW.csv` |
| 钱包曲线 | `out_v3/WALLET_EQUITY_CURVE.parquet` |
| Phase0 诊断对照 | `out_v3/DIAGNOSTIC_PHASE0_CROSSWALK.csv` |
| Holdout 确认 | `out_v3/confirmation/CONFIRMATION.json` |
| 复现 | `out_v3/REPRODUCE.md` |
| 日志 | `out_v3/RUN.log` |
| 冻结 | `configs/v3_rescue_freeze.json` |
| 假设注册表 | `configs/v3_hypothesis_registry.json` |
| 入口 | `python -m src.v3.run_rescue` |

---

## 4. 冻结要点（事先写死）

- 场所固定：Base / UniV3 / WETH 新池  
- 正式桶：`1-6h` / `6-24h` / `1-3d` / `3-7d`；`15-60m` 负对照  
- 分割：LABEL_DESIGN 20% / DEV 60% / INTERNAL_HOLDOUT 10% / UNTOUCHED 10%  
- 本金：`initial_usd=1000`，`eth_usd_freeze=3000` → `initial_weth=1/3`  
- `amount_in_wei=16666666666666666`，`max_concurrent=3`  
- 月目标 $1000 提现；**不复利**  
- LatchPi：**未接入**（`DATA_LINEAGE_INSUFFICIENT`）  
- 旧线 KEYSTONE/F10/G2/C3/P2 分数：**未使用**（`old_line_inputs_used=false`）

复现：

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v3.run_rescue 2>&1 | tee out_v3/RUN.log
```

---

## 5. 样本与标签摘要

| 指标 | 值 |
|---|---|
| Panel 行数 | 2601 |
| 正式桶行数 | 2057 |
| 负对照 | 544 |
| DEV OOF 覆盖 | 1017 rows（formal DEV walk-forward） |
| 全样本 right_tail_25 率 | 4.4% |
| 全样本 fatalish 率 | 6.3% |

双头标签分开建模：Risk（buy_fail / unsellable / liq_collapse / infra / recovery）与 Opportunity（right_tail_25/50/100、MFE、TP-before-SL、conditional return）。Utility 显式组合，非单标签黑箱。

---

## 6. OOF 记分卡（主结果）

| Policy | Verdict | Sel | PnL WETH | Haircut WETH | Fatal sel | RT events | Miss top5 |
|---|---|---:|---:|---:|---:|---:|---:|
| H_TRADEABLE_RISK_SPLIT | NOT_CONFIRMED | 309 | −0.730 | −0.731 | 4.85% | 3 | −0.761 |
| H_FULL_DUAL | NOT_CONFIRMED | 305 | −0.851 | −0.852 | 5.57% | 4 | −0.882 |
| H_FLOW_PERSIST | NOT_CONFIRMED | 306 | −1.074 | −1.075 | 5.88% | 5 | −1.106 |
| H_PATH_STATE | NOT_CONFIRMED | 305 | −1.127 | −1.128 | 7.21% | 5 | −1.159 |
| H_PARTICIPANT | NOT_CONFIRMED | 305 | −1.377 | −1.378 | 5.90% | 5 | −1.407 |
| H_LIQ_REGIME | LOTTERY_DEPENDENT | 307 | −1.632 | −1.633 | 8.79% | 4 | −1.632 |
| H_DEPLOYER_PIT | NOT_CONFIRMED | 366 | −1.878 | −1.879 | 9.02% | 4 | −1.910 |
| C0_NO_TRADE | BASELINE | 0 | 0 | 0 | — | 0 | — |
| C1_RANDOM (matched) | BASELINE | 305 | −0.999 | −0.999 | 3.61% | 4 | −0.999 |
| C2_PRICE_PATH | BASELINE | 1017 | −3.144 | −3.145 | 4.82% | 11 | −3.182 |
| C3_TRANSPARENT | BASELINE | 21 | −0.063 | −0.064 | 9.52% | 4 | −0.094 |

最佳 OOF（损失最小）：`H_TRADEABLE_RISK_SPLIT`，仍为负期望，未优于「不交易」；相对 C1 略好但仍亏损。

钱包月提现（最佳 OOF 曲线）：2025-10 / 2025-11 提现均为 0；未命中 $1000/月目标；固定本金回报 0；未归零。

---

## 7. 确认（一次性）

| 步骤 | 执行 | 结果 |
|---|---|---|
| INTERNAL_HOLDOUT | 是（n=257，选中 156） | PnL ≈ −0.85 WETH；独立右尾事件=1；`LOTTERY_DEPENDENT_REJECT` |
| UNTOUCHED | **否**（holdout 不合格） | `untouched_skip_reason=holdout_not_eligible_or_insufficient_rows` |

Holdout 未通过后**禁止**回改特征/阈值再扫 untouched。

---

## 8. 终裁 JSON（摘要）

```json
{
  "verdict": "LOTTERY_DEPENDENT_REJECT",
  "deploy_allowed": false,
  "venue_rejection": false,
  "stop_research": false,
  "old_line_inputs_used": false,
  "latchpi_status": "DATA_LINEAGE_INSUFFICIENT",
  "best_oof_policy": "H_TRADEABLE_RISK_SPLIT",
  "closing_statement": "在现有数据、现有特征与已检验假设范围内，尚未确认可重复的 Base Uniswap 新池小容量优势。"
}
```

允许的失败表述：具体假设/特征族失败、右尾不可重复、执行风险主导、数据 lineage 不足。  
**禁止**：建议换 CEX/永续/其他链；声称整个市场无任何规律。

---

## 9. 管线组件

```
configs/v3_rescue_freeze.json
configs/v3_hypothesis_registry.json
src/v3/run_rescue.py
  → build_panel → build_labels → features
  → dual_head_model (OOF) → baselines + wallet_monthly + oof_eval
  → confirmation (holdout / untouched once)
  → finalize
```

测试：`tests/test_v3_rescue.py` + 扩展后的 `tests/test_hard_bans.py`（含 g2_/c3_/venue_rejection）。

---

## 10. 审查清单

- [x] 场所固定；无 venue rejection 文案  
- [x] Phase0 门槛仅诊断（`DIAGNOSTIC_PHASE0_CROSSWALK.csv`）  
- [x] 假设注册表事先冻结；双头 + utility 分解  
- [x] $1000 固定本金 + 月提现不复利  
- [x] 主结果为 OOF；holdout 一次；untouched 因不合格未开  
- [x] 旧线分数未入模；LatchPi 未用  
- [x] `deploy_allowed=false`  
- [x] 终裁词在白名单内  

---

*由 `out_v3/` 全部关键产物汇总。*
