# Base V3 Actor-Flow OOS Harness — 审查包

> 生成时间：2026-07-20T16:26:59Z
> 项目根：`/root/base_actor_flow_oos_v0`
> 阶段：`ACTOR_FLOW_OOS_V0`
> **Information 裁决：`PROSPECTIVE_CONFIRMATION_REQUIRED`**
> **Copy 裁决：`COPY_EDGE_GO`**
> `deploy_allowed=False`

---

## 1. 人话结论

最佳臂 `WHALE_EVENT` 相对 `POOL_ONLY` 有正增量且通过复制侧约束，但信息侧未过全部稳健门（fold/置换/LOO 等）→ `PROSPECTIVE_CONFIRMATION_REQUIRED`。禁止实盘；仅可进 prospective shadow / paper-forward。

## 2. 地基门（Phase 1）

| 指标 | 值 |
|---|---|
| passed | True |
| sampled_error_rate | 0.0 |
| unresolved_coverage | 0.014613403716915774 |
| resolvable_scorable_coverage | 0.9853865962830842 |
| audit_mode | debug_trace |

## 3. 六臂记分卡

| arm | information_verdict | copy_verdict | selected_trades | total_wallet_pnl_weth | incremental_vs_pool_only_weth | bootstrap_p05_weth | pct_months_hit_1000_usd | ruin |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| POOL_ONLY | BASELINE | BASELINE | 29194 | 25.3953 | 0 | 10.9424 | 0 | False |
| DIAMOND | POOL_STRUCTURE_SUFFICES | COPY_EDGE_NO_GO | 86 | -0.0198734 | -25.4152 | -0.173719 | 0 | False |
| WHALE_EVENT | PROSPECTIVE_CONFIRMATION_REQUIRED | COPY_EDGE_GO | 19684 | 38.2502 | 12.8549 | 32.7226 | 0 | False |
| SMART_ALPHA | POOL_STRUCTURE_SUFFICES | COPY_EDGE_NO_GO | 69 | 0.0254239 | -25.3699 | -0.122648 | 0 | False |
| BOT_REGIME | POOL_STRUCTURE_SUFFICES | COPY_EDGE_NO_GO | 28783 | 22.5259 | -2.86941 | 14.2044 | 0 | False |
| INDEPENDENT_CONSENSUS | PROSPECTIVE_CONFIRMATION_REQUIRED | COPY_EDGE_GO | 30103 | 29.2651 | 3.86978 | 12.0696 | 0 | False |
| CAPACITY_MATCHED_RANDOM | CONTROL | CONTROL | 29194 | 25.3398 | -0.0554772 | 18.0059 | 0 | False |

## 4. 纪律边界

- 未重启 abs-PnL / winrate top-N / winner-only / EOA-only / MFE 主目标 / 触发价 trailing 证明
- 双裁决分离：INFORMATION vs COPY
- `deploy_allowed=false`
- 失败如实结案，不扩大搜索硬凑赢家

## 5. 路径

```bash
/root/base_actor_flow_oos_v0/.venv/bin/python -m src.run_all 2>&1 | tee logs/RUN.log
```

- 终裁：`/root/base_actor_flow_oos_v0/out/ACTOR_FLOW_FINAL.json`
- 记分卡：`/root/base_actor_flow_oos_v0/out/oos/scorecard.csv`
- 地基门：`/root/base_actor_flow_oos_v0/out/phase1/RESOLUTION_GATE.json`

