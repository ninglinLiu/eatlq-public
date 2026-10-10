---
task_id: GL5
status: STOP_OBJECT_IDENTITY_MISMATCH
research_only: true
training_allowed: false
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
new_rpc_calls: 0
---

# GL5：保守 KEYSTONE 可实现出场重定价

## 裁定候选

**按任务书的 Step 0 硬门槛停止。** Arm-0 五档均逐位复现，证明历史 `+56%` 锚没有漂移；但产生 431/116 与 `+56%` 的冻结选择链路中**没有 F 名单**。历史 F-on 247 deployer + 33 skeleton 是另一套 644-pool、120-pass 回测，不能原样视为同一 estimand。若把它拼进来，就会违反“不许换 config 造新东西”。因此 Arm-1、同龄基线重定价、rug 洗白分解和 golden RPC 全部为 `NOT_RUN_STOP_GATE`，没有资格在本轮宣称 edge“幸存”或“崩塌”。

机器裁定候选为：`INCONCLUSIVE_OBJECT_NOT_DEFINED`。最终裁定仍交独立审查 agent 和人类；`GO=NOT_MADE`。

## Step 0.1：完整对象 pin

| 字段 | 431/116、+56% 冻结对象 |
|---|---|
| universe | W1–W3 同龄 431，模型选择 116 |
| 日期 | W1 2026-02-27 05:41:15→04-01；W2 04-01→05-01；W3 05-01→06-08 18:29:40，均左闭右开 |
| 决策时点 | `entry_block`，即 t0+10m |
| 模型 | HistGradientBoosting，300 trees/iterations、depth 4、lr .05、L2 1、leaf 20、seed 0 |
| 训练 | 只用 2026-02-27 05:41:15 之前；本轮未重训 |
| 选择表达式 | `(prob <= 0.2808669872465047) & fric.notna() & (fric >= -0.05) & regime.notna()` |
| F | **不存在：无 deployer/skeleton/blacklist 字段、版本或 SHA** |
| 仓位 | 0.001 WETH；每次已尝试腿 gas $0.075 |
| trigger | `_priced_live.json` 的 `legacy_exb`；缺失时 `entry_block+7200` |
| 出场规则 | −50% hard stop；从峰值回撤 35%；最大 7200 块约 4h |
| TP / ladder | **没有 TP 分支，也没有 TP ladder** |
| 执行窗 | 入/出各 +1/+3/+5/+10/+15 块；GL1/GL3 主窗 +1/+5/+15 |
| 旧出场价格 | 真卖优先；否则同块 Atomic 成功保护的 exact-size QV2；后续固定网格恢复；仅独立确认且不恢复 rug 才 −100%；其余 unresolved |

### 预注册勘误（不静默改文件）

预注册 SHA `909ad2a61585c674a59a95cf75bdddadb4d7465e78dc2272895b9537a1c696d9` 已冻结，不能回改。其 §1 写了“单一止盈上限/打印参数 10x”，这是源码核对后发现的事实错误：`keystone_live.exit_block()` 只有 hard stop、trail 和 hold-end，没有 TP 条件，全文也没有 10x TP 常量。机器锚按源码记 `take_profit_multiple=null`、`tp_ladder=null`。这项勘误没有查看或改变任何 Arm-1 结果，因为 Arm-1 从未运行；但它是额外的 config-identity 缺口，必须明示。

## Step 0.2：Arm-0 严格复现

从冻结的 116 行逐腿 JSON 独立按 reaudit10 的 NumPy 顺序重聚合；所有数字先做 Python `==`，五档绝对误差都是 0。

| 入场偏移 | 出场偏移 | resolved / 116 | unresolved | mean | median | 去 top-1% mean | 与硬编码锚 | 与 reaudit10 final |
|---:|---:|---:|---:|---:|---:|---:|---|---|
| +1 | +1 | 113 | 3 | 0.5663701436516096 | 0.2597229826746521 | 0.48288116031956935 | `==` | `==` |
| +1 | +3 | 113 | 3 | 0.5716764163421629 | 0.2597229826746521 | 0.4882830415270697 | `==` | `==` |
| +1 | +5 | 112 | 4 | 0.5658600160864686 | 0.2586536605039931 | 0.48133795277529695 | `==` | `==` |
| +1 | +10 | 111 | 5 | 0.5817845065592507 | 0.28751008126389255 | 0.4967792039603212 | `==` | `==` |
| +1 | +15 | 111 | 5 | 0.5768599229957799 | 0.2731717488361512 | 0.49397325802362163 | `==` | `==` |

证据构成同样精确闭合：

| 出场偏移 | guarded exact quote | confirmed LP rug | real sell | unresolved |
|---:|---:|---:|---:|---:|
| +1 | 107 | 2 | 4 | 3 |
| +3 | 100 | 2 | 11 | 3 |
| +5 | 101 | 3 | 8 | 4 |
| +10 | 100 | 3 | 8 | 5 |
| +15 | 101 | 3 | 7 | 5 |

这一步说明我们确实找到了历史 `+56%` 数值对象，但不说明它是 F-on。

## GL1 名称纠错

任务书把“GL1-12 IRON headline”与 `+56%` 合称。账本的实际分类是：

| 条目 | grade | 被本轮复核的事实 |
|---|---|---|
| GL1-01 | PINNED | 116 选择样本的旧模型 `+1/+5/+15` 点估计，+1 mean 为 +56.637% |
| GL1-12 | IRON | 431/116 全区间残差双边界与符号分离；不是 +56% 点估计本身 |

本轮也对 GL1-12 的三组 KEYSTONE interval、full-universe beta interval、selection-alpha interval 做了 JSON 逐值 `==`，全部通过。故不能因任务书名称混淆把 PINNED 抬成 IRON。

## F 身份对质

| 对象 | universe / pass | F 状态 | trail / 规模 | 能否与 +56% 同一视为 |
|---|---:|---|---|---|
| B8/reaudit10/GL3 | 431 / 116 | **无 F** | trail 35%，0.001 WETH | 这是 +56% 对象 |
| 2026-06-24 conservative live-config rebacktest | 644 / 120 | 247 deployer + 33 skeleton，F-on | trail 35%，约 $50 / 0.0302 ETH | **不能**；population、模型阈值、规模与证据模型均不同 |
| 2026-06-25 aggressive live rebacktest | 644 / 339 | F-off 245+33；保留 2 PARADAENGI 防线 | trail 25%，约 $50 | **不能**；报告 207 rug 进入属于另一对象 |

源码检查不仅是“没在报告里找到”：`prepare_rows()` 形成 `selected` 的表达式及其调用的 `load_base/features/regime/fit_forward` 均未出现 `blacklist/deployer/skeleton/safe_rug/cluster_blacklist`。F-on 回测另走 `live_config_rebacktest.py` 的 `_load_blacklists()` 与 `gate_masks()`，没有被 B8 选择链路调用。

因此“保守 F-on +56%”是把两项历史结果组合成了一个仓库里不存在的对象。要继续，必须由人类明确授权一个**新 estimand**：例如对 431 行补做 point-in-time F 映射并重新定义 population，或改为重价 644/120 F-on 回测。两者都会是 GL5-v2，而非本任务允许的“原样”对象。

## 三臂、分解和右尾状态

| 要求 | 状态 | 原因 |
|---|---|---|
| Arm-0 | PASS | 五档逐值 `==` |
| Arm-1 可实现出场 | `NOT_RUN_STOP_GATE` | 对象 F 身份失败 |
| 同龄 431 基线新模型 | `NOT_RUN_STOP_GATE` | 同上 |
| 报价救济比例 | 旧模型描述性可见：+1 为 107/116 | 不是 GL5 新重价结果 |
| 报价洗白 rug 比例 | `NOT_IDENTIFIED` | 前瞻扫描未启动 |
| F-on 碰 rug 数 | `NOT_IDENTIFIED_FOR_431/116` | 该对象无 F 字段 |
| +56% 报价幻觉份额 | `NOT_IDENTIFIED` | 禁止用未知强迫二选一 |
| Arm-1 右尾裁决 | `NOT_RUN_STOP_GATE` | 没有 Arm-1 returns |

尤其不能把 reaudit10 已经确认的 2–3 个 LP rug 当成“全部 rug”，也不能把 100–107 个 guarded quote 当成真钱成交。现有数据只足以说旧 headline 被精确复现、它高度依赖报价型证据；不足以量出这些报价里多少会在新延迟带内坍塌。

## Golden、RPC、安全与回归

- 新 archive RPC 调用：`0`；没有读取 RPC URL 或 `.env`。
- golden 链上复核：`NOT_RUN_STOP_GATE`，符合“对象身份失败不得继续”的预注册规则。
- 无下单、签名、广播、Redis/signals 写入、executor/strategy 启停或 live config 修改。
- 回归：`python3 -B -m unittest tests.test_base_gl5_step0`，8/8 PASS；断言覆盖 prereg SHA、五档 Arm-0、GL1 taxonomy、F stop、116 行逐腿状态、无 RPC/后门工作、GO 状态。
- 逐腿机器账本保留 116×5 个 Arm-0 cell，并对每行明确 `F_status=NOT_DEFINED_IN_SOURCE_ESTIMAND`、`arm1=NOT_RUN_STOP_GATE`。

## 复现

```bash
cd /opt/EATLQ
sha256sum -c mvp/diagnostics/base_gl5_preregistration_20260716.sha256
python3 -B scripts/research/base_gl5/gl5_step0_anchor.py
python3 -B -m unittest tests.test_base_gl5_step0
```

核心机器锚：`mvp/diagnostics/base_gl5_step0_anchor_20260716.json`。逐腿表：`mvp/diagnostics/base_gl5_per_leg_20260716.json`。所有最终 hash 在 `mvp/diagnostics/base_gl5_artifact_hashes_20260716.json`。

## 审查边界

Codex 不把停止解释为 edge 幸存或崩塌，也不自我背书。独立审查应逐行确认：Arm-0 是否真闭合、F 是否确实不在选择调用图、两个 644-pool F 回测是否不可同口径拼接、停门后是否零 RPC/零 Arm-1。只有人类能授权一个新 estimand。`GO=NOT_MADE`。
