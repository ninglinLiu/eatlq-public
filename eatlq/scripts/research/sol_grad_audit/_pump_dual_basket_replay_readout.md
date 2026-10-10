# Core + Lottery 双篮子 Replay

**Cohort**：Core 33 | Lottery 56 | **新 RPC 72** | RPC 253  
**规则锁死**：
- Core = TEST top56(base_score) ∩ dev≤200 ∩ flip≤1.5 → 按 tail_score 取满
- Lottery = TEST ∩ cluster≥0.95 ∩ dev≤50 ∩ ∉Core → tail_score top56

---

## Core 指标（real 10U）

| 子集 | n | p(+100%) | p(+300%) | median | fatal_rate | drop_top1 | drop_top3 |
|------|---|----------|----------|--------|------------|-----------|-----------|
| combined | 33 | 0.0% | 0.0% | **−2.4%** | 21.2% | **−12.7%** | −14.0% |
| known_replay | 7 | 0.0% | 0.0% | −2.4% | **0.0%** | −3.5% | −7.2% |
| **new_replay_only** | **26** | **0.0%** | **0.0%** | **−3.0%** | **26.9%** | −15.8% | −17.6% |

## Lottery 指标（real 10U）

| 子集 | n | p(+300%) | p(+1000%) | max | median | drop_top1 |
|------|---|----------|----------|-----|--------|-----------|
| combined | 56 | 3.6% | 0.0% | **+761%** | −7.8% | −6.5% |
| known_replay | 10 | 0.0% | 0.0% | +24% | −29.4% | −33.3% |
| **new_replay_only** | **46** | **4.3%** | **0.0%** | **+761%** | **−5.7%** | **−1.8%** |

Lottery new top：`38NLFD… +761%` | `F51APu… +435%` | `CwWNBj… +126%`

---

## 与旧 top5 对比

| | 旧 top5 | Core combined | Core **new** | Lottery **new** |
|--|---------|---------------|--------------|-----------------|
| median | −12.1% | **−2.4%** | −3.0% | −5.7% |
| p(+100%) | 5.4% | 0% | **0%** | — |
| p(+1000%) | 1.8% | 0% | 0% | 0% |
| drop_top1 | −15.7% | **−12.7%** | −15.8% | **−1.8%** |
| fatal_rate | 43% | 21% | **27%** | — |

---

## 关键发现

1. **Core median 目标达成**（−12% → −2.4% combined），但 **new replay 零右尾**（p100=0%）。
2. **Lottery new 有真实大赢家**（+761%），drop_top1 **−1.8%** 大幅改善；但无 +1000%（KKK2 未入 cohort）。
3. **KKK2 被 base 池排除**：base_score 排名 **#312/1123**（不在 top56），虽 dev=4、flip=1.0 满足过滤但进不了 Core；Lottery 也因其不在 tail 头部而未选入。
4. **known vs new 分化**：Core known fatal=0%；new fatal=27% — 新样本更痛，说明规则在 unseen 上右尾捕获不足。

---

## 裁定

| 篮子 | new replay 结论 |
|------|----------------|
| **Core** | median OK，**右尾失败** — 不宜单独 forward |
| **Lottery** | **有 +300%/+761%**，drop_top1 好 — 结构有价值，需扩池 |
| **双篮子** | 方向对；下一步：**Lottery 去掉 top56 限制**（cluster≥0.95 & dev≤50 全池 tail 排序）+ KKK2 强制纳入验证 |

仓位建议：Core 1x + Lottery 0.25x（Lottery new max +761% 可补偿 Core 零右尾）。

详见 `_pump_dual_basket_replay.json` / `.csv`
