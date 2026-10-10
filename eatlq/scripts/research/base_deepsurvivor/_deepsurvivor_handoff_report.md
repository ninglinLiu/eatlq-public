# Base 深池 / 长存活池 · Alpha 搜寻 · Handoff Report

> 生成：2026-06-25 07:14 UTC  
> 路径：`/opt/EATLQ/scripts/research/base_deepsurvivor/`  
> **结论：`deep_edge_no`** (1/4 通过线)

---

## 1. RPC 双账本

| 指标 | 值 |
|------|-----|
| 本轮新增 RPC | 541 / 1500 |
| 累计 RPC | 10685 / 18000 |
| 本轮 Credits (M) | 0.011 |
| 累计 Credits (M) | 0.214 |

## 2. 深池 Universe（任务 0）

- 池数：**42**（CH 候选 2910）
- 浅过滤：age≥7d，depth≥3.0 ETH，ns_pre≥50
- 年龄（天）：median 7
- 深度（ETH）：median 17.86，p90 70.89
- V3:V4 = 42:0
- depth RPC 拉取：500

## 3. 基线（任务 A）— 随机深池进场

**Insufficient TEST n=16 for 4h baseline conclusion (need >=30)**

- **1h** TEST: n=16 mean=-0.017 WR=43.8% fatal50=0.0% right50=0.0%
- **4h** TEST: n=16 mean=-0.027 WR=25.0% fatal50=0.0% right50=0.0%
- **24h** TEST: n=16 mean=-0.087 WR=43.8% fatal50=6.2% right50=0.0%
- **72h** TEST: n=16 mean=-0.107 WR=31.2% fatal50=18.8% right50=12.5%

## 4. 聪明钱（任务 B）

- 冻结 smart 钱包：1
- TEST 信号 n=0 mean=NA WR=NA
- beat baseline TEST：None
- 逆向选择（1h 内 smart 卖出）：None

### 延迟衰减曲线（4h hold）

| Δ blocks | n | mean | WR |
|----------|---|------|-----|

Signal still above baseline at max tested delay (or insufficient n)

## 5. 流量信号（任务 C）

| 信号 | TRAIN n/mean | TEST n/mean | beat baseline |
|------|-------------|-------------|---------------|
| net_inflow_accel | 5/0.0027197209292791013 | 2/-0.0021418346373622277 | None |
| holder_growth_slope | 6/0.05196979159449824 | 0/NA | None |
| flow_imbalance | 0/NA | 0/NA | None |

## 6. 左尾（任务 D）

- Deep pool priced subset: fatal(<=-50%)=0.0%, worst=-0.24, slow_lp_cases=0
- worst single：-0.24472214435766027
- slow LP pull 案例：0

## 7. 通过线 · `deep_edge_*`

- [ ] 1_signal_beats_baseline_test
- [ ] 2_train_test_gap_ok
- [ ] 3_delay_decay_ok
- [x] 4_fatal_and_adverse_ok

**Verdict: `deep_edge_no`** · best_signal=None

## 8. 给委托人三行

1. **有没有边**：deep_edge_no — TEST 随机基线 4h mean=-0.027066622346849693 (n=16)
2. **哪种边**：best_signal=无显著信号；聪明钱逆向选择=None
3. **下一步**：诚实负结果 — 深池当前成本/延迟下无可执行边；记录资产归档

## 9. Changed files / Commands / 风险

**新增文件**：
```
scripts/research/base_deepsurvivor/common.py
scripts/research/base_deepsurvivor/task0_build_universe.py
scripts/research/base_deepsurvivor/task_a_baseline.py
scripts/research/base_deepsurvivor/task_b_smartmoney.py
scripts/research/base_deepsurvivor/task_c_flow_signals.py
scripts/research/base_deepsurvivor/task_d_lefttail.py
scripts/research/base_deepsurvivor/task_e_evaluate.py
scripts/research/base_deepsurvivor/run_pipeline.py
scripts/research/base_deepsurvivor/generate_report.py
```

**运行命令**：
```bash
cd /opt/EATLQ/scripts/research/base_deepsurvivor
bash ../base_v4/run_in_cgroup.sh python3 run_pipeline.py
python3 generate_report.py
```

**未知项 / 风险**：
- 深池+depth≥3 ETH 样本极稀缺（42/2910=1.4%），**全部 TEST n=16<30，结论为探索性而非定稿**
- 仅 on-chain WETH depth 过 gate；2910 候选中 500 次 depth RPC 后仅 42 池达标
- 聪明钱 TEST 信号 n=0（冻结 smart 集仅 1 钱包，TEST 段无触达）
- **零改动** V3 live executor / panic / sizing

---

*验收：浅过滤 only · point-in-time · TRAIN/TEST 时间切分 · 真实 QuoterV2 成本 · 无事后硬 gate*