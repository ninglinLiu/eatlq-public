---
task_id: KS-FWD-R1
status: INSUFFICIENT_FORWARD_N
target_object: 2026-06-24_Fon_644_to_120
window: 2026-06-24T11:47:16Z → 2026-08-14T14:22:59Z
research_only: true
live_allowed: false
go_decision: NOT_MADE
preregistration_sha256: d17e5ec1f7c528a22e0bb644af9489473cc6f76be1a372ded10da96d1c2043b8
---

# KS-FWD-R1：冻结 KEYSTONE 保守选择器近窗衰减

## 裁定

**`INSUFFICIENT_FORWARD_N`。不能裁 `EDGE_INTACT`、`EDGE_DECAYED` 或 `EDGE_GONE`。`GO=NOT_MADE`。**

definite point-in-time F-on = **0**。98 个 model-pass 的 F 身份全部是 `F_IDENTITY_UNRESOLVED`（冻结的 2026-06-24 creator/skeleton 图不含这些新币）。这与 GL6 相同，不是新发现。

本机没有可用 archive RPC，因此 GL5v3 的深度门、摩擦门、atomic/exact-size 双界**没有复现**。下面分两层，禁止混写。

## 对你三个问题的直接回答

### 1. 信号还在不在？

**模型层还在，而且不稀。** 冻结 HistGBM（只在 2026-02-03 之前的 `_pools.tsv` 上重建，forward 未进训练）在近窗仍打出 98 个 `p_fatal<=0.27`。

| 层 | 全窗 | 6/24–6/30 | 7 月 | 8/1–8/14 |
|---|---:|---:|---:|---:|
| raw 新 v3 WETH 池 | 5712 | — | — | — |
| `MIN_PRE<5` 拦截 | 5437 | — | — | — |
| `MIN_PRE>=5` | 275 | 26 | 226 | 23 |
| 冻结模型 pass | **98** | 14 | 75 | 9 |
| definite F-on | **0** | 0 | 0 | 0 |

7 月密度最高；8 月前两周仍有 9 个 model-pass，不是零。

**但 98 不是保守选择器会下的单。** 预窗最大单笔 WETH 成交中位 0.053 ETH、最大 1.5 ETH，**0/98** 达到 3 ETH。GL6 里 125 个 model-pass 有 117 个被 `balanceOf>=3` 挡掉。本轮的 `depth_flow` 代理方向一致：这些信号几乎全是薄池。没有 archive 就不能把 98 写成「F-on 选中集」。

### 2. 单笔双界还是否全正？

**正式层：不识别。** definite=0，没有 F-on 收益双界。

**描述层（CH 纸面 tick + 流动性，不是可实现卖出）对 98 个 model-pass：**

| offset | 均值双界 | 中位双界 | 胜率 | 去 top1 下界 | top1 正贡献 |
|---|---|---|---|---|---|
| +1 | **[+138.3%, +153.6%]** | **[−2.95%, −2.30%]** | 20.4% | +69.0% | 42.7% |
| +5 / +15 | [+19.1%, +153.6%] | [−102.3%, −2.30%] | 15.3%–20.4% | −45.1% | 38.9%–75.6% |

均值双界看起来像历史 +137%～+149%，但这是同一类右尾幻觉：最大三笔纸面约 +6852% / +6244% / +1772%，中位是小亏，胜率两成。8 月 9 笔的 +1 均值双界是 **[−57.9%, −2.3%]**，胜率 0，**这一层已经不是全正**。

GL5v3「三档双界全正」在近窗**没有被复现**。本轮也没有资格用描述层去改写那条历史结论。

### 3. rug 率有没有回升？

**没有确认回升，也没有确认仍是 2.5%。**

in-range `liquidity` 代理下，三档 **confirmed collapse = 0/98**。+1 的严格率界是 **[0%, 16.3%]**（16 条未决），+15 是 **[0%, 67.3%]**（66 条未决，主要是 offset 带内没有成交）。

这是 L 坍塌，不是 WETH 储备坍塌；L=0 也可能只是价格走出区间。上界变宽是未决，不是确认撤池。与 GL5v3 的 `[0%, 2.5%]` 不能直接比。

## 与历史锚并排

| 指标 | GL5v3 历史 F-on 120 | GL6 至 7/16 | 本轮 6/24–8/14 |
|---|---|---|---|
| definite F-on | 120 | 0 | **0** |
| 可观察 pass | — | gate 7 | model 98；depth_flow 0 |
| +1 均值 | [+136.7%, +147.7%] | 候选 +19.9% | 描述层 [+138%, +154%]（中位负） |
| +15 均值 | [+147.7%, +149.0%] | 候选 −24.5% | 描述层 [+19%, +154%] |
| +1 rug | [0%, 2.5%] | 候选 14.3% | 代理 [0%, 16.3%] |
| 8 月近窗 | — | 未覆盖 | 9 笔，+1 双界全负，胜率 0 |

方向观察（不可裁）：模型还在出信号；能通过 3 ETH 深度代理的为 0；纸面均值被 2–3 个极端值拉起；**最近的 8 月切片已经转负**。这与「历史 headline 原样活到现在」不相容，但不足以在预注册形式上写成 `EDGE_GONE`。

## 方法与安全

- Step-0 锚复现：644 / 120 / 339。
- 预注册 SHA `d17e5ec1f7c528a22e0bb644af9489473cc6f76be1a372ded10da96d1c2043b8`，跑前冻结。
- 只读 ClickHouse；未改 `/opt/EATLQ`；未碰 live / Redis / 私钥；无 send/sign/broadcast。
- 产物：`out/forward_decay.json`、`out/per_leg.json`、`out/feature_census.json`。

## 若要真正裁衰减

需要两样现在没有的东西，缺一不可：

1. 对前瞻代币的点时 creator + skeleton（`eth_getCode` + 创建交易），对照**冻结**的 247/33 名单，不得倒灌新 rug。
2. archive RPC，按 GL5v3 做 `balanceOf` / atomic / exact-size，把深度、摩擦、可实现出场补回。

在那之前，继续调参或把 98 笔纸面均值当「还能翻倍」都会重蹈口径掉包。
