---
task_id: KS-FWD-R1
target_estimand: frozen_Fon644_forward_recency_post_F_ready
date: 2026-08-14
frozen_before_any_forward_scoring: true
inherits:
  - GL6_preregistration_sha256: fa216ded00bd36420f907f529793049fc2dae7e8d286bf0496f6903d34c8c64a
  - GL6_model_identity_erratum_sha256: 5cf1a7da94ceddc341692466e5811bf1adffa54b16d66fc03cf52490573bb9d2
  - GL5v3_historical_anchor: 2026-06-24_Fon_644_to_120
research_only: true
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
archive_rpc_available: false
---

# KS-FWD-R1 预注册：冻结 KEYSTONE 保守选择器近窗衰减

本文件在任何前瞻特征评分、模型打分、漏斗计数或收益计算之前冻结。
不得按结果修改窗口、模型、F 名单、阈值、成本、出场或裁定。

本轮是 GL6 的**同一冻结对象**在 F 名单齐备之后的近窗复验，不是新策略、不重训、不调参。

## 0. 已知限制（先于结果）

授权 archive RPC 在本机环境中**未注入**（`GL5V3_ARCHIVE_RPC` / `FORK_ARCHIVE_RPC` / `RESEARCH_ARCHIVE_RPC_URL` 均为空；GL5v3 任务书只留占位符）。因此：

- **不能**复现 GL5v3/GL6 的 `WETH.balanceOf` 深度门、QuoterV2 摩擦门、atomic buy→sell、exact-size quote。
- **不能**对前瞻代币做点时 `eth_getCode` skeleton（本地 `base_account_code_*` 只覆盖更早区块；`_token_skeleton_644.json` 只覆盖历史 644 代币）。
- 正式 F-on 点识别几乎必然失败；这本身就是结果的一部分，不是事后借口。

本轮因此分成两层，禁止混写：

1. **正式层（可裁 EDGE_*）**：definite point-in-time F-on。身份规则与 GL6 相同。N<30 或 F-unresolved>20% → 只能 `INSUFFICIENT_FORWARD_N`。
2. **描述层（不可裁 EDGE_*）**：ClickHouse 可复现的信号漏斗 + 成交/流动性代理。用于回答「信号还在不在」，以及给出**非可实现**的方向观察。

## 1. 冻结对象（一字不改）

- 模型：`keystone_live.load` + `risk_rebacktest.fit_model`，只在旧 `_pools.tsv` 的 60% 时间切片（截止 `2026-02-03 21:45:33Z`）按固定 HistGBM 参数重建。forward 行不得进入 fit。
- Step-0 锚必须先通过：labeled=644、F-on=120、aggressive gate=339。失败则停止。
- 特征：每池首笔 swap 后 600 秒；15 维与 GL6 相同；全序 `(block_time, block_number, tx_hash, log_index)`。
- 门槛：`MIN_PRE>=5`、`p_fatal<=0.27`。
- F 名单冻结在 2026-06-24：245 safe-rug deployer + 2 PARADAENGI = 247；33 safe skeleton。禁止增删。
- 出场：trail=0.35、hard stop=50%、max hold=7200 Base blocks。
- 仓位/成本：0.0302 ETH；gas $0.15；haircut 2%；`SIZE_USD=50.09425`；`PESS=-1-0.15/SIZE_USD-0.02`。
- offset：`+1/+5/+15` 加在 legacy exit block 上。

## 2. 窗口与分层（已用 ClickHouse head 钉死）

- F 齐备时刻：`2026-06-24T11:47:16Z`。
- 正式前瞻窗：首笔 swap ∈ `[2026-06-24T11:47:16Z, 2026-08-14T14:22:59Z]`。
- 对应观测块界：`47756145`–`49964016`（查询时刻 CH head）。
- 宇宙：`chain_id=8453` 且 `discovered_pools.pool_type='v3'` 且 `has_weth_leg=1` 的 Uniswap v3 WETH 对。
- 预注册分层（先于结果）：
  - S0：`2026-06-24T11:47:16Z`–`2026-06-30T23:59:59Z`
  - S1：`2026-07-01T00:00:00Z`–`2026-07-31T23:59:59Z`
  - S2：`2026-08-01T00:00:00Z`–`2026-08-14T14:22:59Z`
- 完整区间不得删池。`MIN_PRE<5` 必须留在分母。

## 3. Point-in-time F（与 GL6 相同，不放宽）

对每个 model-pass 行：

- `t0 < F_READY` → `F_NOT_AVAILABLE_PIT`（本窗理论上不应出现）。
- creator 取自 2026-06-24 `deployer_funding_graph.json`；skeleton 取自 `_token_skeleton_644.json`。
- creator∈247 或 skeleton∈33 → `F_BLOCK`。
- creator 与 skeleton **都有**且都不命中 → `F_PASS`。
- 其余 → `F_IDENTITY_UNRESOLVED`。

禁止：倒灌今天的新 rug 名单；把缺身份当成 pass；用 Blockscout/Basescan 现查 creator（本轮不引入新外部身份源）。

- definite selected = model pass 且 `F_PASS`（深度/摩擦因无 archive 保持 `GATE_UNRESOLVED`，因此 **definite 在本轮几乎必为 0**；不得改规则去凑 N）。
- possible selected = model pass 且不是 `F_BLOCK`。
- 同龄 baseline = 全部 model pass。

## 4. 描述层代理（仅观察，不进入 EDGE_*）

这些代理**不是** GL5v3 可实现出场。报告必须写 `CH_PROXY`，不得写成 archive atomic / QuoterV2。

### 4.1 信号漏斗（主问题 1）

逐层报告：raw 新池、`MIN_PRE` block、`MIN_PRE` pass、model pass、F_BLOCK / F_PASS / F_IDENTITY_UNRESOLVED。按 S0/S1/S2 与全窗各出一份。

「信号还在」的预注册定义：全窗 model-pass ≥ 1。若 =0，描述为信号层熄火，仍不自动等于 `EDGE_GONE`（因为正式 selected 未识别）。

### 4.2 流动深度代理 `depth_flow`

用预窗内最大 `|WETH_raw|/1e18`。`>=3` 记 `DEPTH_FLOW_PASS`，否则 `DEPTH_FLOW_UNRESOLVED`。
这是「预窗已出现 ≥3 ETH 成交」的点时观察，**不是** `balanceOf>=3`。只作分层，不作正式门。

### 4.3 流动性坍塌代理（主问题 3 的弱证据）

- `entry_L` = 预窗最后一笔 swap 的 `liquidity`。
- 对每个 offset，在 `[target-1, target+15]` 内取时间上最接近 target 的 swap 为 `target_L`；若该带无 swap，取 target 之前最后一笔。
- `lag_L`：`target+15` 之后第一笔，若无则用带内最后一笔。
- `CONFIRMED_COLLAPSE`：`entry_L>0` 且 `target_L/entry_L<=0.05` 且 `lag_L/entry_L<=0.05`（等号属坍塌）。
- `PROXY_ALIVE`：`entry_L>0` 且 `target_L/entry_L>0.05`。
- 其余 `UNRESOLVED`。

敏感性（只改计数，不改主收益）：0.10 与 0.01。

这是 in-range L 坍塌，不是 WETH 储备坍塌。L=0 可能是价格走出全部仓位，不一定是撤池。必须在报告里原样复述。

### 4.4 纸面 tick 收益与双界（主问题 2 的弱证据）

- 入场 tick = 预窗最后 tick；出场沿用 GL6 `exit_block` 路径规则。
- `raw = exp((exit_lp-entry_lp))-1`；`net = raw - 0.15/SIZE_USD - 0.02`。
- `PROXY_ALIVE`：点值 = net。
- `CONFIRMED_COLLAPSE`：点值 = `PESS`。
- `UNRESOLVED`：下界=`PESS`，上界=net（无出场 tick 则上界=0，不凭空给盈利）。

对 possible / model-pass / depth_flow_pass 分别出 +1/+5/+15 的均值双界、median、win rate、confirmed-collapse 率界、去 top1 下界、top1 正贡献。

禁止把描述层双界全正当成 `EDGE_INTACT`。

## 5. 正式裁定（只看 definite F-on）

继承 GL6：

- `INSUFFICIENT_FORWARD_N`：definite <30，或 possible 中 F-unresolved >20%，或选择边界跨越下列裁定。
- `EDGE_INTACT`：三档 selected 均值下界>0、alpha 下界>0、confirmed-rug 上界≤2.5%、右尾全过，且 forward 均值下界 ≥ 历史 +1 下界 1.3672943342 的 50%。
- `EDGE_DECAYED`：点识别充分且三档均值下界>0，但任一均值上界 < 对应历史下界的 50%，或 alpha 含 0；且未达 GONE。
- `EDGE_GONE`：主档 +1 均值上界≤0，或 alpha 上界≤0，或 confirmed-rug 下限>10%。

右尾：N<30 或 F 未识别 → `right_tail_pass=false`。
`GO=NOT_MADE`。禁止训练、禁止改 live、禁止 send/sign/broadcast、禁止读私钥。

## 6. 对照锚（只对照，不重估）

GL5v3 历史 F-on 120：+1 收益 `[+136.73%,+147.70%]`，rug `[0%,2.50%]`。
GL6 至 2026-07-16：definite=0；gate-identified 7 笔 +1/+5 +19.93%，+15 −24.53%，+15 坍塌 4/7。

本轮不重跑 GL6 旧窗，只把新窗与这两锚并排。
