---
sop_applied: true
alpha_id: AR-REAUDIT10-SECONDS
family: F1/F4/F6/F7
next_gate: HUMAN_LINE_REVIEW_ONLY
decision_flags:
  training_allowed: false
  live_allowed: false
---

# 十条可疑结论独立重审：历史秒级执行

> 纯回测/固定 archive 只读；没有下单、签名、广播、改 live 或服务启停；不作 GO。真钱延迟测试不在本轮。

## 终审摘要

预注册哈希 `c942e7f286f9dda5cc384f0c91f4e98172d2e6307fd80e66869817580ca2534b`。#1 三态裁定：**活-待真钱延迟**；原先“KEYSTONE 已死”被**推翻**。

历史可证部分很清楚：+1 到 +15 块的出场均值约 +56.6% 至 +58.2%，去头部 1% 后仍约 +48.1% 至 +49.7%；曲线没有随 2–30 秒档明显衰减。入场从 +1 延至 +15 块，均值从 +56.6% 降到 +53.0%，仍为正。换句话说，这批历史样本给出的不是“必须亚 10 秒，否则崩”，而是至少覆盖约 30 秒的宽延迟预算。

但预注册的形式状态仍是 `INCONCLUSIVE`：每个出场点有 3–5/116 个 unresolved，按冻结优先级它们不能当 FAIL、也不能当 -100%。除 `primary_unresolved_eq_0` 外，每个出场点的全部已测试右尾门都通过。三态“活”是历史可行性裁定，不是 GO，也没有把 unresolved 偷换成成功。

## 出场延迟预算前沿（入场固定 +1 块）

| 出场偏移 | 约秒数 | resolved/116 | unresolved | mean | median | top1移除 | 条件 alpha | 无条件 resolved mean/覆盖 | 形式右尾 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| +1块 | ~2s | 113/116 | 3 | +56.64% | +25.97% | +48.29% | +37.94% | +1.37% / 285/431 | INCONCLUSIVE |
| +3块 | ~6s | 113/116 | 3 | +57.17% | +25.97% | +48.83% | +38.31% | +1.66% / 284/431 | INCONCLUSIVE |
| +5块 | ~10s | 112/116 | 4 | +56.59% | +25.87% | +48.13% | +38.19% | +1.28% / 283/431 | INCONCLUSIVE |
| +10块 | ~20s | 111/116 | 5 | +58.18% | +28.75% | +49.68% | +39.03% | +1.91% / 282/431 | INCONCLUSIVE |
| +15块 | ~30s | 111/116 | 5 | +57.69% | +27.32% | +49.40% | +38.73% | +1.85% / 282/431 | INCONCLUSIVE |

`条件 alpha` 只比较 resolved 的同窗口基线；无条件基线在 +1 块仍有 146/431 unresolved，因此不能把 +37.9pp 外推成完整宇宙的无缺口因果 alpha。旧的 −68% 同样不能保留，因为它把这些未知强行写成全损。

### 完整 5×5 均值矩阵

行是入场成交偏移，列是出场成交偏移；全部扣每次尝试的 $0.075 gas。

| 入\出 | +1 | +3 | +5 | +10 | +15 |
|---:|---:|---:|---:|---:|---:|
| +1 | +56.64% | +57.17% | +56.59% | +58.18% | +57.69% |
| +3 | +56.93% | +57.46% | +56.88% | +58.43% | +57.93% |
| +5 | +55.85% | +56.38% | +55.73% | +57.30% | +56.80% |
| +10 | +53.23% | +53.76% | +53.12% | +54.65% | +54.16% |
| +15 | +53.05% | +53.57% | +53.03% | +54.68% | +54.16% |

### 预注册右尾逐档

| 出场块 | pooled>0 | 去top1>0 | powered滚动窗≥−10% | ≥2 powered regime 正收益且正alpha | MC终值p5>100R | MC回撤p95≤20R | OOS mean>0 | unresolved=0 |
|---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| +1 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| +3 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| +5 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| +10 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| +15 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |

## 入场延迟预算前沿（出场固定 +1 块）

| 入场偏移 | 约秒数 | resolved/116 | unresolved | mean | median | top1移除 |
|---:|---:|---:|---:|---:|---:|---:|
| +1块 | ~2s | 113/116 | 3 | +56.64% | +25.97% | +48.29% |
| +3块 | ~6s | 113/116 | 3 | +56.93% | +26.16% | +48.59% |
| +5块 | ~10s | 113/116 | 3 | +55.85% | +26.16% | +47.49% |
| +10块 | ~20s | 113/116 | 3 | +53.23% | +17.71% | +44.82% |
| +15块 | ~30s | 113/116 | 3 | +53.05% | +24.02% | +44.64% |

## W2：有毒宇宙与可赚钱子集同时成立

| 时点 | W2 fresh V3 n | 原子买失败 | 原子卖失败 | healthy | instant-hp率 |
|---:|---:|---:|---:|---:|---:|
| 2m | 408 | 40 | 304 | 64 | +74.51% |
| 5m | 408 | 33 | 311 | 64 | +76.23% |
| 10m | 408 | 33 | 311 | 64 | +76.23% |

74.5% 不是暂时的 2 分钟噪声：304/408 在 2m 原子卖失败，10m 反而是 311/408。可是 KEYSTONE 的真实 t0+10m 选择在 W2 resolved n=60，+1块 mean=+54.08%、同窗口 alpha=+30.52%，仅 1 unresolved。故“W2 全域有毒”成立，而“KEYSTONE 因此在 W2 亏”被推翻；正确结论是模型/摩擦门选中了 W2 的健康小岛。

## QuoterV2 与 Atomic：不再横跳

| 池龄 | fresh V3 n | instant-hp | QV2已测疾病例 | QV2未知疾病例 | QV2合理但Atomic严重失败 | Wilson 95%上界 |
|---:|---:|---:|---:|---:|---:|---:|
| 2m | 1265 | 308 | 306 | 2 | 0 | +1.24% |
| 5m | 1265 | 315 | 313 | 2 | 0 | +1.21% |
| 10m | 1265 | 316 | 314 | 2 | 0 | +1.21% |

2m 的复核精确闭合旧锚点：已测 instant-hp 306 个中 0 个出现“QuoterV2 给出≥90%合理卖价、但 Atomic 严重失败”，另 2 个 QV2 未知。W2 的 74.5% 是 Atomic 卖失败；这批病例中 QV2 自己也 adverse/失败，所以两句话并不矛盾。QuoterV2 可作卖侧异常过滤器，但不能单独证明真钱成交。

## Rug 五证据与分类

| 分类 | n | 是否允许直接−100% |
|---|---:|---|
| CONFIRMED_LP_RUG | 235 | 仅确认且不恢复时允许 |
| TRANSFER_RESTRICTION_RUG | 0 | 仅确认且不恢复时允许 |
| TEMPORARY_UNSELLABLE | 1 | 不允许 |
| LIQUIDITY_IMPAIRMENT | 0 | 不允许 |
| UNRESOLVED | 26 | 不允许 |

每池交叉了：后续真成交、固定块原子可卖、WETH 深度、区间 pool/WETH/token 日志、原因交易 receipt/trace、B−1/B/+1/+3/+5/+10/+15 恢复。结果为 235 个确认且不恢复的 LP rug、1 个暂时不可卖后恢复、26 个 unresolved；没有把 26 个未知降级成全损。KEYSTONE +1块主口径中实际计入 2 个确认 LP rug、3 个 unresolved。

两个 transfer-toggle 另做有界二分：首个经济坏块为 46,693,930 和 45,007,595；相邻块 hash 与原子整数在 toggle artifact。它们证明状态可在一块内翻转，但不证明公开 pending 一定能提前看见。

## D5 与 forward LP 监控

| 入场偏移 | D5挡单 | mean边际 |
|---:|---:|---:|
| +1块 | 0 | +0.00% |
| +3块 | 0 | +0.00% |
| +5块 | 0 | +0.00% |
| +10块 | 0 | +0.00% |
| +15块 | 0 | +0.00% |

D5 在冻结 KEYSTONE 子集的五个入场偏移均挡单 0、收益边际 0。既有 forward LP 探针测 24 个重点 rug：real loss=4、对 real loss 的 prewarning=0、rescue proven=0；两个 prewarning 都发生在策略已经退出的池。故“这套 D5/区块级监控没有增益”成立；但“世界上只有速度有用”仍需修正，因为 archive 不能识别 pre-sequencer/pending 首见顺序。

## B3 两分钟加速

冻结真信号仍是 W1 n=17、W2 n=0、W3 n=0。秒级补跑覆盖其中 15 个 V3；47 个 V2 不适用 V3 Atomic 工具，2 个缺冻结进/出锚点，均保留未测。15 个可测 V3 在 +1/+3/+5/+10/+15 块 mean 分别为 +26.31%, +22.86%, +30.88%, +33.32%, +36.73%。这能修正 W1 的执行收益，却不能把 W2/W3 的零触发变成样本，因此原 INCONCLUSIVE 结论成立。

## 天气门

冻结的因果阈值为 61.4161%；55 个 OOS eligible 天只开放 2 天，而这两天 KEYSTONE 交易 n=0。五个秒级出场档都没有跑赢同开工占比随机门。裁定为“修正”：这一个门在本样本确实无增量价值，但 n=0 只能说明阈值/机会错配，不能证明所有外生天气门普遍无用。

## 十条逐条终裁

| # | 原结论 | 裁定 | 重算锚点与残余质疑 |
|---:|---|---|---|
| 1 | KEYSTONE 已死/edge 衰减 | **推翻** | +1..+15块 mean +56.64%..+57.69%，已测经济右尾全过；3–5 unresolved 保留。三态=活-待真钱延迟。 |
| 2 | t0+600s 是合理执行延迟 | **修正** | t0+10m 只保留作因果特征窗；真正执行 +1..+15块，入场 mean +56.64%→+53.05%。 |
| 3 | Base 瞎买均值−68% | **推翻** | +1块无条件 resolved 285/431、mean +1.37%，146 unresolved；−68% 的 missing=loss 口径被推翻，但完整宇宙仍未点识别。 |
| 4 | W2 74.5%毒且 KEYSTONE 在W2亏 | **修正** | W2 2m instant-hp=304/408；10m=311/408，毒性成立；KEYSTONE W2 n=60 mean=+54.08%，亏损推论推翻。 |
| 5 | 防rug只靠速度，monitor/D5无值 | **修正** | D5挡0、边际0；block monitor 对 real loss 预警0、救援0。历史实现无值成立，“只可能靠速度”过宽。 |
| 6 | 单块LP pull点态看不见且无解 | **修正** | 235 个确认LP rug；相邻块可由深度/Atomic/trace确定。canonical 点态确实只能事后，pending是否可抢先未被 archive 识别。 |
| 7 | B3 W3 n=0无法认定 | **成立** | 真 W2/W3 仍0；15个V3秒级 W1 为正不增加信号密度。 |
| 8 | D5对KEYSTONE边际0 | **成立** | 五个入场偏移全部 blocked=0、mean delta=0，成立。 |
| 9 | QV2合理价谎报罕见/可作卖侧过滤 | **成立** | 2m 0/306，未知2，Wilson上界 +1.24%；成立但不能替代 Atomic/真成交。 |
| 10 | 天气门无用/救不回 | **修正** | 开放2/55天、交易0、未胜随机门；本门无值，普遍无用的表述需收窄。 |

## Golden、RPC ledger 与复现

Golden 三例：健康整数往返池 `0x0019…3092`（block 42,702,284）；确认 LP rug `0x000a…ae92`（WETH 21.4916 → 560 wei，block 41,613,806→807）；transfer-toggle `0x1070…82ee`（首坏 block 46,693,930）。固定整数、block hash、request/response hash 在 `tests/fixtures/base_reaudit10_golden_20260715.json`。

总 ledger：HTTP attempts=47,959，successful=41,029，error/revert attempts=6,930，估算 credits=964,420。方法仅含 `['debug_traceTransaction', 'eth_blockNumber', 'eth_call', 'eth_getBlockByNumber', 'eth_getLogs', 'eth_getTransactionReceipt']`；没有 send/sign/broadcast 方法。

主结果 SHA256 `7af0f54247956fa069b0d82bb686f0d9c09bdb76da07cf63e9eb21998c45bcb7`。关键源 hash 与四段 ledger hash 在 `mvp/diagnostics/base_reaudit10_final_20260715.json` 的 `source_hashes`/`rpc_ledger`。预注册 SHA256 `c942e7f286f9dda5cc384f0c91f4e98172d2e6307fd80e66869817580ca2534b`。

## SOP/守卫复核

1. `FAILED_NO_7D_ALPHA` 仍有效；没有 supersede。
2. label freeze 仍为 1800s/0.006/v2,v3；本轮是独立 research estimand，没有改 label。
3. 没有训练、重训或模型 promotion。
4. 没有写 signals/Redis/live config。
5. executor/strategy 在执行前确认未运行，本轮没有启停任何服务。
6. 没有读取 SEND/PRIVATE_KEY/签名变量；artifact 不含 RPC URL。
7. 所有链上调用固定 block，并记录 block hash 与请求/响应 hash。
8. QuoterV2 从不单独充当成交证据。
9. unresolved 没有被记作 −100%；只有五证据确认且不恢复的 rug 才允许。
10. 所有裁定均为 research evidence；GO=`NOT_MADE`。

```bash
cd /opt/EATLQ
export FORK_ARCHIVE_RPC=$(grep -E '^POOL_DISCOVERY_RPC_URL=' infra/compose/.env | cut -d= -f2-)
export LABEL_RPC_URL="$FORK_ARCHIVE_RPC"
python3 -B scripts/research/base_reaudit10/reaudit10_rpc.py
# 若按 checkpoint 分段：reaudit10_rugs_resume.py / reaudit10_b3_seconds_rpc.py / reaudit10_transfer_toggle_probe.py
python3 -B scripts/research/base_reaudit10/reaudit10_finalize_ledger.py
python3 -B scripts/research/base_reaudit10/reaudit10_analyze.py
python3 -B -m unittest tests.test_base_reaudit10
```
