# GOAL：F03 继研 R0 — 动量伪装、同向、影子到期、翻向

**发起人**：用户（继续研究 F03）
**执行者**：Cursor Grok
**创建时间**：2026-08-17
**状态**：执行中
**工作区**：`/root/base_f03_continue_r0/`
**只读源**：`/root/base_wildcat_alpha/`（不改代码、不改已交付结论）

---

## 0. 硬约束

1. **不重写一个更强的 F03。** 不换宇宙、不重训、不把本轮拆解结果升格成新策略。
2. **不改 wildcat 仓库。** 异议和本轮数字只写在本目录。
3. **禁止真实资金操作。** `NO_LIVE.lock` 缺失则所有入口退出。
4. **禁止用未扣成本的 tick 收益当经济结论。** 沿用两腿 120 bps 成本代理。`USD_CAPACITY = UNVERIFIED`。
5. **最高状态仍是 SHADOW_CANDIDATE。** 本轮即使拆干净了，也不授权实盘。
6. **4 条影子信号的到期结果只作描述。** n=4，不得做统计裁决，不得因此升格或杀死 F03。
7. **预登记在看联合结果之前写死。** 见 §3。看完数字不准改门槛。

---

## 1. 已核实基线（不要重新发明）

来自 `/root/base_wildcat_alpha/FINAL_WILDCAT_REPORT.md` 与源码：

- 假设原文：direction-switch scarcity → 持续执行 / 信息扩散 → 4h 延续
- **代码没有数翻向。** `direction_persistence = sign(ret_0) * |flow_imbalance|`
- 深挖安慰剂打乱的是未来收益，不是小时内成交顺序
- 深挖重放：4h 多空价差 238.9 bps；扣 120 bps 后 118.9 bps
- 价差大部分来自 LOW（空头腿）；HIGH 平均约 +75 bps，中位数约 +5
- 影子账：4 条信号，`market_data_hour=2026-08-14T13:00:00Z`，成熟结果当时为 0
- 面板在信号小时截断，所以到期必须另查 ClickHouse，不能从原 parquet 读到未来

---

## 2. 本轮只回答四件事

1. **动量伪装：** 控制了 `sign(ret_0)` 和 `|ret_0|` 之后，`|flow_imbalance|` 还能不能分开 4h tick 收益？
2. **同向故事：** 代码用了绝对值，不管资金方向和价格是否一致。要求同向的公式，是不是比 abs 公式更强？若不是，嘴上的“同向单边流”就是假的。
3. **影子到期（描述）：** 那 4 条信号后面 4h / 24h 的 tick 怎么走？有没有精确小时？
4. **翻向（若 CH 够）：** 在 F03 信号小时里，真实方向切换次数是否比 `|imbalance|` 多解释一点东西？这是他们写下、深挖没做的证伪。

本轮不回答：买不买、月赚 1000、1000 美元容量、信贷正交（那是 Morpho R4）、报价闭环（信号时刻的历史报价本轮若没有 archive 就标 `DATA_INSUFFICIENT`）。

---

## 3. 预登记（计算前锁死）

复制到 `out/r0_preregister.json`，在加载本轮新结果列之前写入。

```
protocol            = 与 wildcat F03 深挖相同：校准 = 时间 40% 分位；两端各 20%；replay = 其后
outcome             = fwd_ret_4（|ret| > ln(100) 已在原面板隔离）
cost_proxy_bps      = 120
primary_A           = replay 上各特征的 quintile spread（bps）
zero_spread_bps     = 20          # |流| 在控制动量后的增量价差低于此视为 0
primary_B           = replay 后 30% holdout 上线性 R² 增量
r2_increment_zero   = 0.01
momentum_controls   = [sign(ret_0), abs(ret_0)]
aligned_feature     = sign(ret_0) * flow_imbalance     # 无 abs
abs_feature         = sign(ret_0) * abs(flow_imbalance) # 即 F03
veto_on_disagree    = 不在本轮发明新交易规则；只报告 agree vs disagree 的描述差
shadow_n_min        = 4 条全报；统计裁决禁止
flip_sample_max     = 400 个 F03 信号小时（分层抽 HIGH/LOW）
flip_zero           = 控制 |imbalance| 后，flip_rate 的增量 spread < 20 bps 且增量 R² < 0.01
do_not_claim_fill   = true
```

裁决词（只能选一个主裁决）：

- `MOMENTUM_ONLY` — |流| 增量价差 < 20 bps 且 holdout 增量 R² < 0.01
- `FLOW_ADDS_IN_TICK_SPACE` — 增量过门槛，但仍无报价
- `ALIGNED_STORY_FALSE` — abs 公式不弱于同向公式，且 HIGH 里存在大量方向不一致
- `UNDERPOWERED` — 对齐/抽样 n 不够
- `DATA_INSUFFICIENT` — 面板或 CH 读不到
- 影子到期单独用 `SHADOW_DESCRIPTIVE`，不覆盖主裁决

若 A 是动量、同向故事也垮，主裁决可以写成 `MOMENTUM_ONLY`，并在同段写 `ALIGNED_STORY_FALSE` 为附属。

---

## 4. 明确禁止

- 禁止把本轮拆解后的“更纯动量”或“更纯翻向”当成新的晋级策略
- 禁止 LIVE_GO / 实盘 / 改 EATLQ live
- 禁止年化美元 PnL
- 禁止把 4 条影子当样本外证明
- 禁止为了让信贷/桥看起来有用而改问题（那是别人的 GOAL）
