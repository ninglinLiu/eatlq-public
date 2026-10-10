# KEYSTONE 出场升级 — 回测裁决（仅 research，未上 live）

Holdout gate+model n=131 | 基准: `live_hold4h` med=+0.444

## 结论摘要

- **尚无规则在中位数上稳定击败 `live_hold4h` 基准**；上 live 前需更多验证或 richer forward 特征。

## 规则结果

| 规则 | 族 | n | mean | med | WR | fatal |
|------|----|---|------|-----|-----|-------|
| baseline_trail35 | baseline | 131 | +59.7% | +30.5% | 68% | 4% |
| live_hold4h | baseline | 131 | +65.2% | +44.4% | 75% | 7% |
| trail50 | P3 | 131 | +64.1% | +41.7% | 75% | 5% |
| death_silent3 | P1 | 131 | +64.8% | +41.7% | 75% | 7% |
| death_gap3 | P1 | 131 | +64.8% | +41.7% | 75% | 7% |
| quote_dd25 | P2 | 131 | +52.7% | +15.1% | 57% | 4% |
| quote_dd15 | P2 | 131 | +43.1% | +7.7% | 54% | 4% |
| micro_decay | P4 | 131 | +57.7% | +30.5% | 73% | 5% |
| combo_death+quote | P1+P2 | 131 | +52.2% | +13.3% | 57% | 4% |

## 上 live 闸门（必须全部满足）

1. 本脚本 + walk-forward 复验通过
2. 代码 review（你 + 高级 review）
3. 你书面同意
4. Paper-forward ≥48h

## 局限

- P1/P4 使用 `_fwdpath` tick 代理活性，**无 per-bucket swap/buyer 计数**；完整版需扩展 ClickHouse SQL。
- P2 依赖 `_sellcurve.json`（QuoterV2 真卖价），仅覆盖 gated 池 forward 块。
- 未含 MEV / live 延迟。

复跑: `python3 scripts/research/base_lefttail/exit_upgrade_backtest.py`
