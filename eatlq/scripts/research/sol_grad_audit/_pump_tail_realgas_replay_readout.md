# T+15→T+90 真实小单 Replay 结果

生成：2026-06-25 | RPC：**126 次** | 样本：**40**（top10 + 对照 30，去重后）

---

## 裁定摘要

| 维度 | 结果 |
|------|------|
| **可成交性（top10）** | **10/10 成功** buy+sell（bonding curve 储备解码） |
| **停止线「top10 >30% 失败」** | **未触发** |
| **Grid vs 链上真实（10U）** | **Grid 严重高估 top 赢家**（T+0 回退入场价）；spot 滑点仅 **~2–8%** |
| **Forward paper** | **仍不建议** — 见下文 |

**Replay 裁定：`TRADABLE_TAIL_GRID_INFLATED`**

---

## 1. 执行概况

-  cohort：top10 + loss20(20) + small_profit(8 可用) + boundary(2 可用) → **40 币**
-  每币：getSignaturesForAddress + ≤16 getTransaction
-  模拟档位：5U / 10U / 20U / 50U（SOL≈$150 换算）
-  Pump 费：1%/side，bonding curve constant-product

---

## 2. Top10 链上 vs Grid

| mint (前8) | Grid net | Real 10U | Spot T+15→90 | 结论 |
|------------|----------|----------|--------------|------|
| KKK2… | +3312% | **+3061%** | +3138% | 右尾**可成交**；grid≈real（此币 T+0≈T+15） |
| HPZK… | +1680% | **+260%** | +268% | **Grid 高估 ~6×**（入场价回退错误） |
| BfvM… | +1612% | **+354%** | +364% | Grid 高估 ~4.5× |
| GMHm… | +1102% | **+93%** | +98% | Grid 高估 ~12× |
| 9HL2… | +1076% | **+535%** | +550% | Grid 高估 ~2× |
| H423… | +848% | **+5%** | +7% | **Grid 完全失真** |
| 4AoW… | +799% | **+40%** | +43% | Grid 高估 ~20× |
| 6QBk… | +745% | **+101%** | +106% | Grid 高估 ~7× |

**Top10 中位 real return（10U）：+219%**  
**Top10 中位 spot return：+268%**（链上储备价，无小单滑点）  
**小单 vs spot 滑点：约 −2% ~ −8%**（10U 档）

→ 极端右尾在 **真实 T+15 储备** 下**可以**在 bonding curve 买卖；之前 +11% 全样本 mean **主要来自 grid 入场价错误**，不是纯数学幻觉。

---

## 3. 对照组（10U）

|  cohort | n | median real |
|--------|---|-------------|
| top10 | 10 | **+219%** |
| loss20 | 20 | −2.6% |
| small_profit | 8 | −0.1% |
| boundary | 2 | −0.1% |
| **全体** | 40 | **−1.8%** |

→ **无筛选的全样本 T+15→T+90 仍为负**；右尾只在 ex-post top 赢家上成立。

---

## 4. 停止条件检查

| 条件 | 阈值 | 实测 |
|------|------|------|
| top winner 无法买卖 | >30% | **0%** ✓ |
| 小单 vs grid 中位折损 | >50% | 全体 median haircut 27%（被 grid 错误扭曲）；**spot vs real 滑点 <10%** |
| sell_fail | 高 | **0/40** |

---

## 5. 对原分类的影响

| 原裁定 | Replay 后 |
|--------|-----------|
| `UNTRADABLE_RIGHT_TAIL` | **部分推翻**：top 右尾**可链上成交** |
| Grid +11% mean | **解释**：T+0 代理 + 极少数真右尾；链上 top10 med **+219%** |
| Forward paper | **仍否**：无 ex-ante 信号在 T+15 前识别 top10；全样本 med **−1.8%** |

---

## 6. 若继续（可选）

1. **Forward paper 仅作「无筛选 T+15 全样本」** — 预期 med ≈ −2%，验证是否稳定（≥200 笔 / 14 天）
2. **或**：在 T+15 前特征上再跑一轮「能否提高 p(+100%)」— 与本次 replay 正交
3. **不建议**直接实盘 top10 追单

---

## 产物

- [`_pump_tail_realgas_replay.json`](file:///opt/EATLQ/scripts/research/sol_grad_audit/_pump_tail_realgas_replay.json)
- [`_pump_tail_realgas_replay.csv`](file:///opt/EATLQ/scripts/research/sol_grad_audit/_pump_tail_realgas_replay.csv)
- [`_pump_tail_realgas_checkpoint.jsonl`](file:///opt/EATLQ/scripts/research/sol_grad_audit/_pump_tail_realgas_checkpoint.jsonl)

## 复现

```bash
cd /opt/EATLQ/scripts/research/sol_grad_audit
bash ../base_v4/run_in_cgroup.sh python3 _pump_tail_realgas_replay.py
```

（删除 checkpoint jsonl 可全量重跑；已有 checkpoint 会 skip 已完成 mint）
