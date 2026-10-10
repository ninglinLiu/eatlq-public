# Base Greenfield V4 — Orthogonal Hypothesis Round 审查包

> 生成时间：2026-07-20  
> 项目根：`/root/base_greenfield_directional_v0`  
> 角色：**Base Uniswap New-Pool Narrow-Market Rescue Research Agent**  
> 阶段：`V4_ORTHOGONAL_HYPOTHESIS_ROUND`  
> 父轮：V3 `LOTTERY_DEPENDENT_REJECT`  
> **裁决：`LOTTERY_DEPENDENT_REJECT`**  
> **一轮正交搜索已完成并如实结案**（`one_shot_round_complete=true`）  
> **禁止**失败后继续扩大搜索硬凑赢家；**禁止**场所否决；`deploy_allowed=false`

---

## 1. 人话结论

V3 那几条「软 utility + 老特征族」失败后，按纪律**新开一轮**：

- 新冻结 6 条**结构正交**假设（硬否决 / 只做审计成交 / 分桶专家 / 短出场 / 极稀疏容量 / 负对照避雷迁移）
- Holdout/Untouched **用新 salt 重密封**，并把 V3 已偷看过的确认样本从 V4 确认集里烧掉
- 每条假设 **只搜 1 次**

结果：六条全军覆没（OOF 全负）。相对最好的是 `H_EXIT_BARRIER_SHORT`（约 −0.21 WETH），holdout 仍失败 → 终裁 `LOTTERY_DEPENDENT_REJECT`。  
**到此如实结案**，不继续扫到赚为止。

---

## 2. 正交性（相对 V3）

| V4 假设 | 相对 V3 正交点 |
|---|---|
| H_HARD_FATAL_VETO | 风险改为硬门槛否决，不是软扣分 |
| H_ATOMIC_AUDITED_ONLY | 样本定义：只留决策时已审计可买可卖 |
| H_BUCKET_SPECIALIST | 分生命周期桶各自建模，不池化 |
| H_EXIT_BARRIER_SHORT | 选单目标改为短持有/TP-before-SL，不是 6h 终值 |
| H_SPARSE_CAPACITY_CAP | 极稀疏 + deployer 每周上限 |
| H_NEGCTRL_AVOID_TRANSFER | 用 15–60m 负对照学「避雷」再迁移 |

V3 的 `H_FLOW_PERSIST`…`H_FULL_DUAL` **禁止复用**。

---

## 3. 重密封

- 方法：`salted_signal_hash`  
- salt：`V4_RESEAL_20260720_ORTHOGONAL`  
- V3 confirmation 样本标记 521；挪入 V4 DEV 426  
- V4 splits：DEV 1709 / LD 504 / HOLDOUT 190 / UNTOUCHED 198  

---

## 4. OOF 记分卡

| Policy | Verdict | Sel | PnL WETH | RT events |
|---|---|---:|---:|---:|
| H_EXIT_BARRIER_SHORT | NOT_CONFIRMED | 259 | −0.215 | 6 |
| H_HARD_FATAL_VETO | NOT_CONFIRMED | 306 | −0.363 | 5 |
| H_ATOMIC_AUDITED_ONLY | LOTTERY | 41 | −0.411 | 3 |
| H_SPARSE_CAPACITY_CAP | LOTTERY | 112 | −0.439 | 1 |
| H_BUCKET_SPECIALIST | NOT_CONFIRMED | 263 | −0.632 | 6 |
| H_NEGCTRL_AVOID_TRANSFER | NOT_CONFIRMED | 368 | −2.193 | 6 |
| C0_NO_TRADE | BASELINE | 0 | 0 | 0 |
| C1_RANDOM | BASELINE | 306 | −0.868 | 2 |

硬否决路线 fatal 率低于宇宙（1.6% vs 3.9%），说明「少踩雷」有一点作用，但**仍不足以产生正钱包期望**。

---

## 5. 确认

| 步骤 | 结果 |
|---|---|
| INTERNAL_HOLDOUT（`H_EXIT_BARRIER_SHORT`） | `LOTTERY_DEPENDENT_REJECT` |
| UNTOUCHED | 未执行（holdout 不合格） |

---

## 6. 路径

```bash
.venv/bin/python -m src.v4.run_orthogonal 2>&1 | tee out_v4/RUN.log
```

- 冻结：`configs/v4_orthogonal_freeze.json`、`configs/v4_hypothesis_registry.json`  
- 终裁：`out_v4/RESCUE_FINAL.json`  
- 代码：`src/v4/`

---

## 7. 结案边界

在现有数据与 **V3+V4 已登记并测完的假设** 范围内：尚未确认可重复的 Base UniV3 新池小容量优势。

- 不得声称整个市场无规律  
- 不得建议换 CEX/永续/其他链  
- 不得为凑正收益再开无新机制的「V5 刷分轮」；若再开轮，必须有**新的事先正交机制 + 新密封**，且接受再次失败结案  

---

## 8. 审查清单

- [x] 未复用 V3 失败假设 ID  
- [x] 每条 max_searches=1  
- [x] Holdout/Untouched 重密封 + burn V3 confirmation  
- [x] 无 venue rejection  
- [x] `expand_search_after_failure=false`  
- [x] `deploy_allowed=false`  
- [x] 失败如实结案  

---

*由 `out_v4/` 产物汇总。*
