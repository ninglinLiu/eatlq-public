# 全维度对账 — QuoterV2 vs fork 真买真卖（2026-06-24）

状态：只读分析。回答「只用 QuoterV2 维度会漏掉多少撤池/蜜罐」，并量化两维度差异。SYSTEM_PANIC 状态不受影响。

---

## 维度定义（真实度分级）

| 维度 | 怎么测 | 能看见 | 盲区 |
|---|---|---|---|
| **QuoterV2(同源尺子)** | 历史块 `eth_call` 报买价+卖价 | 价格/滑点;翻转后 quote=0 | **看不见 lp_pull 时机**(撤池无 swap);gas-bomb;MEV;反应型 |
| **fork 真买真卖** | 主网 fork 到历史块,全新钱包真买真卖 + 读 `WETH.balanceOf` | 真实买卖结果 + 深度坍塌(lp_pull) | 反应型 rug;MEV/三明治;进场延迟 |

---

## 对账一:同池同块(224 笔幸存子集,set131+217 去重)

> 这 224 笔是**已过 QuoterV2/friction 尺子**的「成交幸存子集」,在**退出块**上比对两维度。

| | fork 可卖 | fork 不可卖 |
|---|---:|---:|
| **QuoterV2 判可卖/可盈利** | **219** | **1** ← QuoterV2 漏掉 |
| **QuoterV2 判致命(−100%)** | 1 | 3 |

- **QuoterV2 漏判(说可卖、fork 真卖不出):1 / 224 = 0.45%**,就是 `0x75d2c083…`(transfer_toggle,与 0.26% / P0 同一个)。
- 反向:QuoterV2 误判致命但 fork 可卖 = 1(保守误杀,无害)。
- 两维度都判致命 = 3(一致)。

### 滑点对账(两维度都判可卖的 219 笔)
- fork 真实滑点:**中位 −0.60%,均值 −0.83%,最差 −1.99%**
- **真实滑点 < −3% 的:0 笔**
- → 在幸存子集上,**QuoterV2 的摩擦估计不乐观、与真实成交吻合**。这一段是「同源尺子」可信的证据。

---

## 对账二:全持有窗多点(644 集) — QuoterV2 的结构性盲区

> 单块对账只看退出块,看不到「持有窗中途撤池」。644 多点 fork(进场/+30/+300/+1800/+7200 块,读 `WETH.balanceOf`)才暴露撤池。

| 机制 | n | QuoterV2 能否看见 |
|---|---:|---|
| delayed_rug 总数 | **262** | — |
| **lp_pull（撤 LP，深度坍塌）** | **260** | **结构性看不见时机**——撤池那刻无 swap，QuoterV2 靠不上 |
| transfer_toggle（深度在、卖 revert） | 2 | 翻转后 QuoterV2 能读出 quote=0 |

**关键**：QuoterV2 维度对「单块快照能不能卖」准（漏判仅 1）；但对「**lp_pull 撤池的发生与时机**」**结构性失明**——260 个 lp_pull 全靠 fork 读链上 WETH 余额才看得见。这就是「只用 QuoterV2 会漏掉多少撤池」的真正答案：

> **单块能否卖：QuoterV2 只漏 1（0.45%）。**
> **撤池机制/时机：QuoterV2 看不见 260 个 lp_pull 中的任何一个的「撤池事件」本身**——它只能在恰好查询到深度坍塌后的块时被动看到 quote=0。

---

## 三句话结论

1. **QuoterV2 不乐观、不撒谎**：在幸存子集上，它与 fork 真买真卖一致率 99.5%，滑点估计偏保守（最差 −1.99%，无 −3% 以下）。同源尺子本身没系统性高估收益。
2. **QuoterV2 有结构性盲区**：lp_pull 撤池（260 个，主导威胁）不产生 swap，QuoterV2 看不见时机；必须靠 fork 读 `WETH.balanceOf` 这一独立维度。
3. **fork 真买真卖也不是终点真相**：它仍漏「反应型 rug + MEV + 进场延迟」（见 `cluster_blacklist_fp_validation` 与 `fastrug_survivorship` 报告）；这部分只能靠 live 真金 forward + size 兜底。

---

## changed files / commands
- 只读分析脚本（临时）；产物 `_dimension_reconcile.json`
- 数据源：`_set131_forkreplay.json` / `_set217_forkreplay.json`（per-pool `qv2_net` + `real_tradeable_at_exit` + `real_slippage`）、`_labeled_universe.json`（262 rug 机制）
