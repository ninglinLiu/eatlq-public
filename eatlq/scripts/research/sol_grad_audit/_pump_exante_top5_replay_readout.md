# Ex-ante Top5% 链上 Replay 验证

RPC：**386** | 新跑：**111** | cohort：**112**

## 链上真实收益（10U, T+15→T+90）

| 分组 | n | p(+100%) | p(+300%) | p(+1000%) | median | mean |
|------|---|----------|----------|-----------|--------|------|
| exante_top5 | 56 | 5.36% | 1.79% | 1.79% | -0.121 | +0.393 |
| test_random5 | 55 | 1.82% | 1.82% | 0.00% | -0.032 | -0.104 |

## Grid 基线（TEST 全样本, n=1123）
- grid p(+100%)=1.69% | grid p(+1000%)=0.09%

## 裁定：**WEAK_OR_NO_LIFT**
- real p(+100%) lift top5/random = 2.9464285714285716
- real p(+1000%) lift top5/random = None

详见 `_pump_exante_top5_replay.json`
