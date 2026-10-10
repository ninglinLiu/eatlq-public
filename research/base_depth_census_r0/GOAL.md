# GOAL：Base 深度普查 — 先量深度，再谈策略

**创建**：2026-08-17  
**工作区**：`/root/base_depth_census_r0/`  
**性质**：**仪器轮，不是策略轮。** 本轮不提出任何 alpha 假设，不做回测，不产生候选。  
**只读源**：`eatlq` ClickHouse（`--readonly=1`）、archive RPC（`eth_call` + state-override）  
**不改**：`/opt/EATLQ` live、任何既有工作区

---

## 0. 硬约束

1. `NO_LIVE.lock` 缺失则所有入口退出。禁止签名、广播、swap、LP、授权。
2. 本轮最高状态是 `INSTRUMENT_FACT`。不得输出候选、不得输出 SHADOW_CANDIDATE、不得输出 LIVE_GO。
3. **主账是 SwapRouter02 `eth_call` + state-override 的原子来回。** QuoterV2 只能坐对照列；只有在同一格子上路由器与 Quoter 一致时，才允许用 Quoter 补路由器 slot 探测失败的格子，并且必须标注来源。
4. 禁止用「假定的 120 bps 成本」替代实测。
5. 禁止在看到深度结果之后回头修改宇宙定义使结论变好看。
6. 池注册表新鲜度必须先核验。若 `discovered_pools` 的 `max(created_block)` 落后于链头，则「活跃池减少」不得写成市场事实。

---

## 1. 本轮只回答三个数

1. **Base 上有多少个池具有「持续」深度？** 持续 = 60 天窗口里活跃天数够多，不是 1–3 天的一次性冲量。
2. **在这些池上，$50 / $250 / $1000 / $5000 的原子来回真实成本是多少 bps？**
3. **有信号的名字和有深度的名字，是不是同一批？**

不回答：买什么、什么时候买、有没有 alpha、月赚多少。

**这一轮存在的理由**：`/root/CUMULATIVE_KILL_LEDGER.md` 第 3 节显示，30 条研究线里头号杀手是深度（8 条线），而**没有任何一轮先测过深度**——每一轮都先假定宇宙，再事后发现深度不够。本轮把顺序倒过来。

---

## 2. 冻结口径

```
窗口          = 2026-06-18 .. 2026-08-17（60 天）
venue         = eatlq.discovered_pools 全部 pool_type + eatlq.aerodrome_vamm_swaps_mv
UniV3 宇宙    = pool_type='v3' AND has_weth_leg=1，HAVING n_swaps>=5
Aero 宇宙     = aerodrome_vamm_swaps_mv，HAVING n_swaps>=20
持续性度量    = uniqExact(toDate(block_time))，即 60 天里的活跃天数
成交量度量    = WETH 腿绝对值之和 / 1e18（只对 UniV3，Aero 表无 token 地址）
router        = SwapRouter02 0x2626664c2603336E57B271c5C0b26F421741e481
quoter        = QuoterV2     0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a（对照）
holder        = 0x4B7cd953871663474a0A25B81bddDf5C1f1195A7（模拟地址，非策略钱包）
fee           = 该池链上 fee() 实读，不用 meta.fee_bps
sizes         = $50 / $250 / $1000 / $5000 @ ETH=$3000
探测块        = 链头 − 20
slot 探测     = balanceOf/allowance 槽位扫描 0..39 × {sol, vyper} 两种布局
失败分类      = SUCCESS / DETERMINISTIC_REVERT / INFRASTRUCTURE_UNKNOWN
```

`INFRASTRUCTURE_UNKNOWN` 一律不得当成「卖不掉」，必须单独报告并说明原因。

---

## 3. 裁决词

- `DEPTH_MAP_UNTRUSTWORTHY` — 池注册表不新鲜，或路由器与 Quoter 在同格子上不一致
- `NO_PERSISTENT_DEPTH_TIER` — 具有持续深度的池少于 10 个，无法构成横截面宇宙
- `DEPTH_EXISTS_DISJOINT_FROM_SIGNAL` — 有深度的名字与有信号的名字不重叠
- `DEPTH_EXISTS_OVERLAPPING_SIGNAL` — 两者重叠，可以据此设计后续策略轮
- `DATA_INSUFFICIENT` — 数据不足以回答

---

## 4. 交付物

- `out/universe_raw.tsv` — UniV3 WETH 腿 60 天池级活跃度与成交量
- `out/aero_activity.tsv` — Aerodrome vAMM 60 天 pair 级活跃度
- `out/depth_probe.json` — 原子来回实测（路由器主账 + Quoter 对照）
- `out/probe_targets.json` — 冻结的探测目标
- `src/depth_probe.py` — 可复现的探测器
- `reports/R0_DEPTH_CENSUS.md` — 报告

---

## 5. 验收（打回条件）

1. 没有先核验池注册表新鲜度 → 打回。
2. 用 Quoter 当主账，或没有标注哪些格子是 Quoter 补的 → 打回。
3. 把 `INFRASTRUCTURE_UNKNOWN` 写成「卖不掉」→ 打回。
4. 输出任何 alpha 候选或交易建议 → 打回。
5. 「活跃池下降」没有排除采集/注册中断就写成市场萎缩 → 打回。
