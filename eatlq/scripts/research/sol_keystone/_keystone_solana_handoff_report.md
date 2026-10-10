# KEYSTONE → Solana 离线验证 Handoff Report

生成：2026-06-25 | 零 RPC | V3 live 未触碰

## 1. 旧失败解剖 (Task 0)

见 `_sol_autopsy.md`。**裁定：旧聪明钱线 ≈ (A) 离线不可部署；执行折让未量化 ≈ (B)；本轮继续 KEYSTONE 结构性移植。**

## 2. RPC / 成本账

| 项 | 值 |
|----|-----|
| ClickHouse Sol 表 | **无**（fallback 缓存 JSONL） |
| 本轮 RPC 调用 | **0** |
| 现金成本 | **≈ $0** |
| 需付费上报 | **否** |

## 3. Universe

- 池/币数：**4227**（全部 pump.fun bonding curve）
- 有早期路径特征：**1488**
- 有价格 grid：**2102**
- pump:Raydium = **4227:0**
- 时间跨度：**06-03, 06-04, 06-11, 06-12, 06-13, 06-14, 06-15**
- entry_slot：**T+180s**

## 4. 基线（随机早期池）

- 全样本 TEST 等权均值：**-0.0435** (n=1122)
- 随机同 n 基线（500 次）：**-0.0473**
- 基线 EV 符号：**负**

## 5. 左尾模型

- TRAIN AUC：**1.000** | TEST AUC：**0.907**（KEYSTONE 参考 0.86）
- 剪后存活占比 TEST：**0.747104247104247**
- 剪后 fatal 率 TEST：**0.002583979328165375**

## 6. 凸性篮子 (offline_edge, 30m hold)

| 分段 | n | mean | median | win_rate | ≥+100% | ≤−50% |
|------|---|------|--------|----------|--------|-------|
| TRAIN | 179 | -0.014 | -0.030 | 0.03 | 0.01 | 0.00 |
| TEST | 243 | -0.034 | -0.030 | 0.01 | 0.00 | 0.00 |
| ALL | 422 | -0.026 | -0.030 | 0.02 | 0.00 | 0.00 |

## 7. 执行现实主义

| 指标 | offline | executable |
|------|---------|------------|
| TEST mean | -0.0343 | -0.0804 |
| **执行折让** | — | **+0.0462** |
| 信号衰减到 0（slot） | — | **0** |

## 8. 通过线

| # | 条件 | 结果 | 说明 |
|---|------|------|------|
| 1 offline > 基线 TEST | PASS | pruned=-0.03426725883598161 vs baseline=-0.04348493941232831 (仍负 EV，仅相对少亏) |
| 2 TEST AUC ≥ 0.75 | PASS | AUC=0.907 |
| 3 executable > 0 且 > 基线 | FAIL | exe=-0.08044256747795693 n=243 |
| 4 TRAIN/TEST 差距可接受 | PASS | train=-0.013934307855538158 test=-0.03426725883598161 |

**裁定：`solana_edge_offline_only`** (3/4 通过)

## 9. 给委托人三行

1. **边在不在**：剪左尾 offline TEST **-0.034** vs 基线 **-0.043** — **相对少亏 ~1pp，绝对 EV 仍负，非正 alpha**
2. **执行能不能拿到**：executable TEST **-0.080**，执行折让 **+0.0462**
3. **下一步最便宜动作**：**不建 geyser/Jito**；若仍想探 Sol，先扩 **零 RPC 早期路径样本**（12–16h 币 + OHLCV）验证左尾 AUC 是否稳；Raydium 需新 ingest（非本轮）

## 10. Changed files / commands

```bash
cd /opt/EATLQ/scripts/research/sol_keystone
bash ../base_v4/run_in_cgroup.sh python3 task0_autopsy.py
bash ../base_v4/run_in_cgroup.sh python3 sol_universe.py
bash ../base_v4/run_in_cgroup.sh python3 sol_realcost.py
bash ../base_v4/run_in_cgroup.sh python3 sol_lefttail.py
bash ../base_v4/run_in_cgroup.sh python3 sol_convexity.py
bash ../base_v4/run_in_cgroup.sh python3 sol_execution_realism.py
bash ../base_v4/run_in_cgroup.sh python3 sol_evaluate.py --write-report _keystone_solana_handoff_report.md
```

## 未知项 / 风险

- ClickHouse **无 Sol 链数据**；结论绑定 06-11~06-15 缓存样本
- 4h 出场 grid 不完整（缓存仅至 create+7200s）；主 hold 用 30m
- 2104 有 grid / 1488 有 earlypath → 交集驱动 ML（n≈1488）
- TRAIN AUC=1.0 提示训练集过拟合风险；TEST AUC 0.907 仍高但需更多天 OOS 验证
- **非上线结论**；V3 Base live 优先不变

