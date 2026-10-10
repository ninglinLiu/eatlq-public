# Phase C 汇总（Top 5% by n180 → Full Replay T+15→T+90）

> 数据源：`phase_c/full_replay.json` | n=**207** | ok=**176** | RPC=**1,369**（≈41k credits）  
> 对比：Phase B 14d（2026-06-02 → 06-15，n=4,154）

---

## 1. 运行结果

| 项 | 值 |
|----|-----|
| 选取规则 | `n_trades_180s` top 5% |
| 候选数 | 207（n180 ≥ 1535，median=1909） |
| Full replay 成功 | **176 / 207**（85%） |
| 失败原因 | 31 × `NEEDS_RPC_REPLAY`（链上 tx 不足） |
| RPC | 1,369 calls ≈ **0.04M credits**（~5 min） |
| 修复效果 | 旧 run 仅 2/207 ok（`missing_bc_or_created`）；本次正常 |

---

## 2. Full Replay 收益（10U 真实滑点）

| 指标 | Top 5% full | Top 5% sparse | 全样本 sparse |
|------|-------------|---------------|---------------|
| median | **−11.5%** | −3.0% | −3.0% |
| mean | −10.6% | −4.4% | −3.4% |
| p5 / p95 | −61% / +47% | — / +2% | — / +24% |
| ≥ +100% | **1.7%**（3 币） | 0% | 0.26% |
| ≥ +300% | 0% | 0% | 0.03% |
| ≤ −25% | **35.2%** | 7.5% | 7.5% |
| win rate | 28.4% | — | — |

**等权组合**（176 币 × 10U）：总 PnL **−187U**（−10.6% mean）。

Full vs sparse 同币对比（n=174）：full 中位比 sparse **低 5.4pp**；相关 r=0.13。

---

## 3. 三个右尾赢家（sparse 完全漏掉）

| day | full 10U | sparse | ath_mc | grad | n180 |
|-----|----------|--------|--------|------|------|
| 06-15 | **+218%** | −3% | $96,951 | ✓ | 1880 |
| 06-10 | **+133%** | −3% | $9,992 | ✗ | 1590 |
| 06-14 | **+114%** | −6% | $27,135 | ✗ | 1549 |

06-15 毕业 mega（ath ~$97k）在 sparse 快照里被压成 −3% cost floor；full replay 才还原真实路径。

---

## 4. API 标签（Top 5% cohort）

| 指标 | Top 5% | 全样本 |
|------|--------|--------|
| graduation | 1.4% | 0.5% |
| hit100 | 4.3% | 4.0% |
| hit300 | 1.4% | 1.2% |

n180 筛选对 API 右尾仅有边际提升（+0.3pp hit100），**不足以改变 aggregate**。

---

## 5. 结论

### Sparse replay 不可信
- Top 5% sparse median = −3.0%，与 random 5% 相同 → **n180 无法在 sparse 层产生 ex-ante edge**。
- 45% 全样本 sparse = 恰好 −3%（cost floor）；mega 被系统性低估。

### Full replay 揭示真实左尾更重
- 高 n180 币在 T+15→T+90 **波动更大**：35% fatal25 vs 7.5% 全样本。
- Median −11.5% << sparse −3% → **高活跃 ≠ 更好入场**；反而是更陡的下跌曲线。

### 右尾存在但不可交易
- 3/176（1.7%）full ≥ +100%，含 1 个毕业 mega；sparse/API 均无法在 ex-ante 识别。
- 等权组合仍亏 −10.6%；3 个赢家不足以覆盖 62 个 ≤−25% 和大量小亏。

### 最终判定

**T+15→T+90 ex-ante 右尾在 n180 高活跃 cohort 上不可交易。**

- 无 median edge（full < sparse < 0）
- 右尾依赖不可预测的 mega（1/176），且左尾 fat
- 不建议基于此信号做 forward paper 或规则调参

---

## 6. 成本账

| Phase | RPC calls | credits | 结论 |
|-------|-----------|---------|------|
| A | 0 | 0 | 复用 labeled |
| B | ~43,673 | ~1.31M | 14d 覆盖 |
| C | 1,369 | ~41k | Top 5% full replay |
| **合计** | ~45k | **~1.35M** | Build 套餐内 $0 overage |

---

## 7. 可选后续（未执行）

1. 对 3 个 mega 做路径解剖（T+15 入场 timing vs ATH timing）
2. 换 score（如 `alive_at_180s` × n180）重跑 Phase C top 1%
3. 05-17~06-01 backfill（修 sample enum + CachedRPC）补全 30d
