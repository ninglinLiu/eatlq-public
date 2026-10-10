---
task_id: GL5-v2
status: INCONCLUSIVE
target_object: 2026-06-24_Fon_644_to_120
research_only: true
training_allowed: false
live_allowed: false
broadcast_allowed: false
go_decision: NOT_MADE
new_rpc_calls: 0
---

# GL5v2：644/120 F-on 的可实现出场重定价

## 裁定候选

**INCONCLUSIVE。** 2026-06-24 F-on 对象自己的旧 headline 已完整逐位复现：月化 `+8.5743804128%`、120 笔平均 return `+142.8024144341%`、fatal 2/120、MDD `-0.3931798477%`。这不是 +56%。

更重要的是，旧证据并非“多数真卖”：QV2 lens 的 120/120 是**无 Atomic 守卫的报价**，fork lens 的 120/120 是**模拟**，原脚本使用的真实 receipt SELL 为 0。两个 lens 是同 120 笔的两把尺，不能相加成 240 笔。

新 canonical swap 扫描在 +1/+5/+15 块分别找到 31/34/38 笔达到 0.0302 ETH 反事实仓位覆盖的真实 SELL；另有小额真卖或稍后真卖把“至少活着”的证据扩到 62/64/64 笔。没有确认任何 reserve collapse，但由于授权 archive RPC 未注入、完整 transfer-net 也不能覆盖 644 全区间，主档仍有 58/120 连“存活”都无法证明，89/120 无法进入点估计。F-on rug 率只能诚实写成 `[0%,48.33%]`（+1）与 `[0%,46.67%]`（+5/+15），因此既不能确认“F-on ⇒ rug≈0”，也不能说 edge 已崩。

## Step 0：对象 pin

| 字段 | 冻结值 |
|---|---|
| 母体 | `_pools.tsv` 1275 行 |
| labeled universe | 644 |
| F-on pass | 120；两个 lens pool 集合完全相同 |
| source formal/observed | 2025-10-01 00:54:35Z → 2026-06-01 22:23:01Z |
| 644 observed | 2025-10-01 02:07:57Z → 2026-06-01 22:23:01Z |
| 120 observed | 2025-10-01 02:07:57Z → 2026-05-15 16:56:53Z；blocks 36,171,321→46,037,731 |
| learner | HistGradientBoosting：max_iter 300、depth 4、lr .05、L2 1、leaf 20、seed 0 |
| train split | 1275 的 60% 时间分位 2026-02-03 21:45:33Z；765/510 |
| model gate | `ns_pre>=5`、`p<=0.27` |
| execution gates | entry WETH≥3 ETH、friction≥−5% |
| F | 245 safe-rug deployer + 2 PARADAENGI = 247；33 skeleton |
| notional | 0.0302 ETH，按固定 1658.75 为 $50.09425 |
| costs | round-trip gas $0.15 + haircut 2% |
| legacy exit | −50% hard stop；peak 回撤 35%；最多 7200 blocks；无 TP/ladder |
| source exclusions | sell-sim、external risk、E-window 1800s、MEV 没有重放 |

120 笔 exit trigger 构成：hold-end 68、trail 49、hard stop 2、silent 1；`_realgas_replay` 对 120 笔均记录 `status=ok, entry_sell=ok, exit_sell=ok`。后者是 fork 模拟，不是链上成交。

## Arm-0：原 JSON 逐位复现

在内存调用冻结 2026-06-24 代码，未覆盖历史 JSON。两个 lens 的 `live_full_gates`、gate breakdown、blocked reasons 与 fork crosstab 共 70 个叶字段全部 Python `==`，误差 0。

| headline | QV2 lens | fork lens |
|---|---:|---:|
| pass_n | 120 | 120 |
| mean return | +142.802414% | +142.802414% |
| monthly | +8.574380% | +8.574380% |
| MDD | −0.393180% | −0.393180% |
| fatal | 2 / 1.666667% | 2 / 1.666667% |
| delayed rug traded | 0/262 | 0/262 |
| delayed rug blocked | 262/262 | 262/262 |
| tradeable universe | 384 | 382 |
| miskilled winners | 12 / $1,617.93 | 11 / $1,597.39 |

这组 headline 本身已经显示极重右尾：平均单笔 +142.8%，但用 $50 fixed stake 和 $10,000 起始权益换算出的月化只有 +8.57%。两者不是同一量纲，不能互换宣传。

## 原对象自己的证据构成

| lens（同一 120 笔） | 真 receipt SELL | Atomic guarded quote | 无守卫 QV2 | fork simulation | confirmed rug | unresolved |
|---|---:|---:|---:|---:|---:|---:|
| QV2 | 0 | 0 | **120** | 0 | 0 | 0 |
| fork | 0 | 0 | 0 | **120** | 0 | 0 |

原代码没有读取 receipt，也没有调用 D5。其 `fork_crosstab_on_live_passed` 只覆盖 65/120：64 个 QV2 ok/fork ok，1 个 QV2 bad/fork ok。因此，问题“F-on 版是不是也高度依赖报价”的答案是：**是，而且原 QV2 headline 是 100% 裸报价；另一个 headline 是 100% fork 模拟。** 它不是 92%，而是按各自 lens 计 100%。

## Arm-1：真实 SELL 优先的新口径

target 为 legacy exit + offset；真卖窗口为 target 到 target+15。entry 用 entry_block 后最多 900 块内第一笔真实 BUY 价；只有 SELL token size 覆盖 0.0302 ETH 反事实仓位才进入点估计。

| offset | full-size REAL_SELL | 小额 SELL 未解 | 稍后存活未解 | 无存活/储备证据 | survival evidence | resolved mean | median | 去 top1 mean | population QV2界 | 形式状态 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| +1 | 31 | 20 | 11 | 58 | 62/120 | +154.073% | +77.126% | +133.985% | [−36.070%, +143.703%] | INCONCLUSIVE |
| +5 | 34 | 19 | 11 | 56 | 64/120 | +146.180% | +55.654% | +113.813% | [−31.897%, +143.873%] | INCONCLUSIVE |
| +15 | 38 | 22 | 4 | 56 | 64/120 | +111.355% | +57.809% | +96.969% | [−34.642%, +143.920%] | INCONCLUSIVE |

已解决点估计看起来强，且在匹配的 resolved pair 上真实 SELL 的正利润比旧报价略高 2.10%–2.31%；这只能说明**能找到足额真卖的条件子集**没有显出报价高估，不能代表剩余 68%–74%。双边界跨过 0，才是完整 120 population 的合规答案。

### 同龄 644 baseline

| offset | resolved / 644 | unresolved | resolved mean | selected resolved alpha | population QV2界 |
|---:|---:|---:|---:|---:|---:|
| +1 | 64 | 580 | +88.799% | +65.273pp | [−83.308%, +76.345%] |
| +5 | 65 | 579 | +95.133% | +51.047pp | [−82.372%, +76.731%] |
| +15 | 64 | 580 | +79.225% | +32.131pp | [−84.260%, +76.610%] |

`resolved alpha` 只比较各自有足额真卖的稀疏子集，不是完整因果 alpha；baseline 有约 90% unresolved，不能用它给 F-on 背书。

## “F-on 是否真挡住 rug”独立对质

| offset | 旧 label 的 rug pass | 新确认 collapse | 有链上 SELL 存活证据 | 无存活证据 | confirmed-rug 区间 | 裁定 |
|---:|---:|---:|---:|---:|---:|---|
| +1 | 0 | 0 | 62 | 58 | [0%, 48.33%] | 未识别 |
| +5 | 0 | 0 | 64 | 56 | [0%, 46.67%] | 未识别 |
| +15 | 0 | 0 | 64 | 56 | [0%, 46.67%] | 未识别 |

本轮没有把黑名单的“262/262 blocked”当答案，而是扫描固定区间内 498,137 条 canonical swaps，TSV 内容 SHA256 `80dc2bdf96d509e3f3c17fd82ef5ad20a0950e8baa9fba67e2043b804674d794`。发现 0 个确认 collapse 的原因不是证明没有 rug，而是缺少全 644 的完整 `balanceOf/transfer-net` 前瞻区间；缺证据者没有被记成安全或全损。

与 aggressive F-off 的 207 rug 对照只能说明历史 label gate 的巨大差异。真实扫描能确认约一半 F-on 行在目标附近仍有 SELL；另一半仍不能裁定。因此“F-on ⇒ rug 成交≈0”**没有被独立证成**，但也没有发现反例。

## 报价幻觉与右尾

| offset | 原 quote/sim 依赖 | matched resolved pairs | resolved-pair profit illusion | 全 population 是否识别 |
|---:|---:|---:|---:|---|
| +1 | 100% | 31 | −2.10% | 否 |
| +5 | 100% | 34 | −2.31% | 否 |
| +15 | 100% | 38 | −2.26% | 否 |

负值代表匹配真卖子集实际略优于旧报价，不代表“报价幻觉为负”可以外推。由于 82–89 个 unresolved，完整 headline 有多少是报价幻觉仍是 `NOT_IDENTIFIED`。

resolved-only 的 pooled mean、去 top1 mean、top1≤25%、MC terminal p5>100R、MDD p95≤20R 均通过；没有任何 rolling 30d 窗达到 n>=20，因此 rolling 门未测试；`unresolved=0` 失败。三个 offset 形式状态全部 INCONCLUSIVE。

## Golden、RPC 与回归

- 预注册要求的 `prettiest-blue-aura` 环境变量未注入；未读取 `.env`，没有替换成 public RPC。新 RPC=0。
- deterministic golden 选了 5 腿：3 个 REAL_SELL、2 个 UNRESOLVED；没有可确认 collapse 类别，故无法满足“至少 2 rug”的类别愿望，已按预注册披露 shortfall。
- golden 对账包含 canonical entry BUY/exit SELL 的 tx/log/block/整数、冻结 fork 状态与 hash；archive `balanceOf`/atomic 标 `RPC_NOT_RUN_ENV_UNSET`。
- 初次回归暴露 90% 阈值边界的浮点误判；用预注册 `1e-15` 数值容差修复包含端点比较。最终 10/10 PASS，未改变任何结果行，因为本轮没有 reserve-complete collapse。
- 无下单、签名、广播、Redis/signals、live config 写入或服务启停。`GO=NOT_MADE`。

## 复现

```bash
cd /opt/EATLQ
sha256sum -c mvp/diagnostics/base_gl5v2_preregistration_20260716.sha256
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python3 -B scripts/research/base_gl5v2/gl5v2_reprice.py
python3 -B -m unittest tests.test_base_gl5v2
```

机器结果：`mvp/diagnostics/base_gl5v2_Fon644_repricing_20260716.json`。644×3 逐腿账本：`mvp/diagnostics/base_gl5v2_Fon644_per_leg_20260716.json`。最终完整 hash ledger 在独立终审落盘后生成。

## 最终边界

本轮推翻的是“保守 F-on headline 已有真实成交支撑”这一隐含说法；没有推翻或确认其完整历史 edge。已解决真卖子集强，但完整双界跨零，rug upper bound 太宽。裁定候选只能是 `INCONCLUSIVE`。不自判 GO；最终由独立审查 agent 与人类裁定。
