# KEYSTONE 出场 v1 — Walk-forward 报告（research only，未上 live）

**日期:** 2026-06-20 | **Walk-forward n=217** (Feb–Jun 2026) | **基准:** `live_hold4h`

## 结论（诚实版）

| 规则 | med | vs hold4h | 触发次数 | 建议 |
|------|-----|-----------|----------|------|
| **live_hold4h** | **+37.5%** | — | 基准 | **维持 live，不改** |
| death_v1 (P1) | +37.2% | −0.3pp | death_liq **3**/217 | 未达推广门槛 |
| fric_spike12 (P2) | +39.1% | +1.6pp | fric_spike **3**/217 | 样本太小，仅值得 paper 观察 |
| fric_spike20 (P2) | +39.1% | +1.6pp | 同左 | 12pp/20pp 无差别 → 几乎不触发 |
| combo_p1p2 | +37.2% | −0.3pp | — | 不上 |

**裁决：无一规则达到「可上 live」标准。** P2 v1 语义修正后不再像 v0 那样大规模误杀（v0 quote_dd 84/131），但触发太稀，+1.6pp med 可能是噪声。P1 真实 swap/liq bucket 数据已就位，但 `death_liq` 仅 3 笔 — 需调参网格 + buyer 流失特征后再跑。

## Pooled 结果

| 规则 | n | mean | med | WR | fatal |
|------|---|------|-----|-----|-------|
| live_hold4h | 217 | +71.6% | +37.5% | 71% | 10% |
| death_v1 | 217 | +71.5% | +37.2% | 72% | 10% |
| fric_spike12 | 217 | +72.4% | +39.1% | 72% | 10% |
| combo_p1p2 | 217 | +71.5% | +37.2% | 72% | 10% |

## 分月 med（gate+model）

| 月 | n | hold4h | death_v1 | fric_spike12 |
|----|---|--------|----------|--------------|
| 2026-02 | 89 | +37.5% | +37.5% | +39.1% |
| 2026-03 | 38 | +44.2% | +44.2% | +44.2% |
| 2026-04 | 66 | +52.3% | +51.5% | +52.3% |
| 2026-05 | 23 | −2.2% | −2.2% | −2.2% |
| 2026-06 | 1 | +127% | +127% | +127% |

May 全规则同步崩盘 — 出场规则救不了 regime，与此前结论一致。

## VPS / ClickHouse 安全（实盘优先）

详见 `vps_safety.md`。执行摘要：

- **741/743** gated 池 forward bucket 已缓存（2 超重池 OOM 跳过）
- 单池只读查询，1GiB/query，live 重查询时 **等待** 不抢占
- ~115min 后台 fetch，**executor / bridge / .env 零改动**
- 全程 CH 容器正常，live 服务 Up

## 上 live 闸门（仍未满足）

1. ✅ v1 walk-forward 已跑
2. ⏳ 参数网格 / P4 微结构
3. ⏳ 高级 code review
4. ⏳ 你书面同意 + paper ≥48h

## 复跑

```bash
# 仅当需续拉 bucket（通常不必）
nice -n 19 python3 scripts/research/base_lefttail/fetch_fwdbuckets_safe.py

# walk-forward（纯离线，零 CH/RPC）
python3 scripts/research/base_lefttail/exit_upgrade_v1_walkforward.py
```
