# Phase D: Right-Tail Timing Autopsy

> API hit100+ cohort from Phase B 14d | n=**168** | RPC=**876**

## 1. Cohort

| tier | count | threshold |
|------|-------|-----------|
| hit100 | 168 | ath_mc ≥ $15k |
| hit300 | 50 | ath_mc ≥ $40k |
| hit1000 | 20 | ath_mc ≥ $100k |

## 2. T+15→T+90 Full Replay（右尾 cohort）

| 指标 | 值 |
|------|-----|
| priced | 154 |
| median | -2.1% |
| mean | 112.3% |
| ≥+100% | 29.2% |
| ≥+300% | 13.6% |

## 3. API 右尾 vs T+15→T+90 脱节原因

| reason | count | share |
|--------|-------|-------|
| pre_entry_already_pumped | 45 | 26.8% |
| tradable_in_window | 45 | 26.8% |
| within_window_but_negative | 33 | 19.6% |
| untradable/failed_pricing | 19 | 11.3% |
| post_exit_pumped | 11 | 6.5% |
| sparse_missed_spike | 10 | 6.0% |
| migration_only_pumped | 5 | 3.0% |

## 4. 代表 token（按 tier / reason）

### Top T+15→T+90 full replay

| mint | day | ath | tiers | t15→90 | sparse | reason | max_window |
|------|-----|-----|-------|--------|--------|--------|------------|
| `DUxCy1NRTLqQ…` | 2026-06-06 | $112,172 | hit1000,hit300,hit100 | +1682% | 31% | tradable_in_window | t15_t90 |
| `7itKGMSqfpiL…` | 2026-06-03 | $30,348 | hit100 | +1370% | 68% | tradable_in_window | t15_t90 |
| `2mTQ8XyFP1Q7…` | 2026-06-03 | $38,889 | hit100 | +1195% | 73% | tradable_in_window | t15_t90 |
| `5moXMHJS63Rr…` | 2026-06-08 | $98,094 | hit300,hit100 | +1039% | -14% | tradable_in_window | t15_t90 |
| `78iFGNxVVty7…` | 2026-06-05 | $136,027 | hit1000,hit300,hit100 | +897% | -14% | tradable_in_window | t15_t90 |
| `8zAj1UyhVCwB…` | 2026-06-13 | $72,541 | hit300,hit100 | +888% | 29% | tradable_in_window | t15_t90 |
| `Gv92LUTXhGez…` | 2026-06-02 | $22,551 | hit100 | +752% | -27% | tradable_in_window | t15_t90 |
| `EVsxacA3CUw2…` | 2026-06-06 | $23,042 | hit100 | +643% | 33% | tradable_in_window | t15_t90 |

### Post-exit / migration-only 样本

- `9smMJxtru37j29w7…` ath=$1,139,613 t2ath=166691.0s grad=True | t15→90=-20% t90→300=+143% t300→grad=NA
- `32MVHer27WVVQwDd…` ath=$933,292 t2ath=311108.0s grad=True | t15→90=-2% t90→300=+1234% t300→grad=NA
- `CMzcCtnwPC3hcJfU…` ath=$141,733 t2ath=242860.0s grad=True | t15→90=-25% t90→300=+98% t300→grad=NA
- `286w6NybqXYYijJZ…` ath=$58,504 t2ath=306.0s grad=False | t15→90=+20% t90→300=+710% t300→grad=NA
- `9Fs3WStEVJWgLXCg…` ath=$48,412 t2ath=152.0s grad=False | t15→90=+67% t90→300=+129% t300→grad=NA

## 5. 结论

- **45/168** (27%) API 右尾在 T+15→T+90 **可交易兑现**（full ≥+100%）
- **16/168** (10%) 主要涨幅在 **T+90 之后**（post-exit / migration）
- **45/168** (27%) **T+15 前已 pump**，标准入场太晚
- **10/168** (6%) sparse 快照 **漏 spike**（full >> sparse）
- **19/168** (11%) **定价/可交易失败**
- 右尾 cohort T+15→T+90 median = **-2.1%**（API 标签 ≠ 该窗口可交易性）

**判定**：API hit100 衡量 universe 右尾频率；T+15→T+90 ex-ante 策略需单独验证 timing。 大部分脱节来自 **post-exit pump** 或 **pre-entry already pumped**，非 sparse  alone。

原始数据：`data/pump_lowcost/phase_d/tail_autopsy.json`

