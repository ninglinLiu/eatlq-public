---
task_id: GL5
review_type: independent_line_review
review_status: PASS
sop_applied: true
alpha_id: GL5_CONSERVATIVE_REALIZABLE_EXIT_REPRICING
family: F4
next_gate: HUMAN_MUST_DEFINE_A_NEW_ESTIMAND_BEFORE_ANY_ARM1_WORK
research_only: true
go_decision: NOT_MADE
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
  broadcast_allowed: false
---

# GL5 独立逐行终审

## 审查结论

**PASS：支持 `STOP_OBJECT_IDENTITY_MISMATCH`，无实质性 blocker。** 我没有运行 GL5 生成脚本来取得统计结论，而是直接从冻结的 reaudit10 逐笔源文件独立重算 116×5 个 cell，并逐项读取 B8/B5/legacy exit/F-on/F-off 源码与 JSON。证据支持以下有限结论：旧模型的 `+56%` 数值对象可精确复现，但该 431/116 对象没有 F；所以任务书要求的“保守 F-on +56%”不是仓库中已经存在的单一 estimand。按 Step 0 停止是正确执行，不得把停止解释为 edge 幸存或崩塌。`GO=NOT_MADE`。

## 逐项裁定

| # | 审查项 | 裁定 | 独立证据 |
|---:|---|---|---|
| 1 | prereg 冻结与 10x TP 勘误 | **PASS** | `reports/GL5_preregistration_20260716.md` SHA256 为 `909ad2a61585c674a59a95cf75bdddadb4d7465e78dc2272895b9537a1c696d9`，mtime 早于脚本、Step 0 输出与测试；sidecar 内容匹配。预注册 §1 的“10x”事实错误没有被静默回改。主报告在“预注册勘误”中明确披露；`keystone_live.exit_block()` 只有 hard stop、35% trail、hold-end，无 TP 条件或 TP ladder。机器锚相应为 `take_profit_multiple=null`、`tp_ladder=null`。 |
| 2 | Arm-0 116 行、五个 exit offset 独立重算 | **PASS** | 直接读取 `base_reaudit10_per_trade_20260715.json`，按 reaudit10 相同 NumPy 顺序计算 mean/median/top-1% removed，并与 `base_reaudit10_final_20260715.json` 做 Python `==`。五档统计与 evidence counts 全部逐值相等；GL5 per-leg 的每个 1:1/3/5/10/15 cell 也与原逐笔源完全相等。 |
| 3 | B8 选择调用图没有 F；两套 644 对照是不同 estimand | **PASS** | `prepare_rows()` 动态加载固定 B5 文件，只调用 `load_base/features/regime/fit_forward`，随后选择式仅为 `prob<=cut`、`fric>=-0.05` 与 regime 非空。逐函数检查未见 deployer/skeleton/blacklist 过滤；legacy `_priced_live.json` 的生成函数也不含 F。相反，06-24 脚本另行调用 `_load_blacklists()`/`gate_masks()`：644 pool、247 deployer（245+2）、33 skeleton、F-on 后 120。06-25 aggressive 是同一 644 母体，F-off 后 339，组合成交中 207 delayed-rug。它们的人群、模型 gate、notional、trail 与执行证据口径均不同，不能 graft 到 431/116。 |
| 4 | Step 0 停止与后续未运行 | **PASS** | 任务要求先确认 F-on 版本且禁止换 config；预注册进一步把“无可追溯 F”写为硬停止门。Arm-0 通过而 F identity 失败，故 `STOP_OBJECT_IDENTITY_MISMATCH` 符合任务书。GL5 范围仅有 Step 0 脚本；该脚本没有 RPC client、网络调用、Redis、executor 或 Arm-1 实现。机器锚及全部 116 行均标 `arm1=NOT_RUN_STOP_GATE`；baseline/decomposition/golden 同样未运行。GL5 新 RPC 计数为 0。 |
| 5 | GL1 taxonomy | **PASS** | 直接读取 GL4 更新后的 GL1 JSON：GL1-01 grade=`PINNED`，其 +1/+5/+15 mean 与 Arm-0 相等；GL1-12 grade=`IRON`，其 KEYSTONE interval、full-universe beta interval、selection-alpha interval 与 GL3 JSON 三组逐值相等。任务书把 `+56%` 称作 GL1-12 headline 是名称混淆，主报告已纠正且没有把 PINNED 抬成 IRON。 |
| 6 | 回归测试 | **PASS** | 独立执行 `python3 -B -m unittest -v tests.test_base_gl5_step0`：8 tests，8 passed，0 failed。覆盖 prereg SHA、Arm-0 headline、GL1 taxonomy、F stop、后门工作/RPC、116 行 stop ledger、分离 estimand 与 GO 状态。 |
| 7 | 安全与 GO | **PASS** | 工件一致为 research-only；training/live/broadcast flags 均 false；`go_decision=NOT_MADE`。本审查未读 `.env`、未调用 RPC、未启动或停止服务、未写 Redis/signals、未签名/下单/广播。 |

## Arm-0 独立重算明细

| exit offset | resolved | unresolved | mean | median | top-1% removed mean | evidence（quote/rug/real/unresolved） | 与 final |
|---:|---:|---:|---:|---:|---:|---|---|
| +1 | 113 | 3 | 0.5663701436516096 | 0.2597229826746521 | 0.48288116031956935 | 107 / 2 / 4 / 3 | `==` |
| +3 | 113 | 3 | 0.5716764163421629 | 0.2597229826746521 | 0.4882830415270697 | 100 / 2 / 11 / 3 | `==` |
| +5 | 112 | 4 | 0.5658600160864686 | 0.2586536605039931 | 0.48133795277529695 | 101 / 3 / 8 / 4 | `==` |
| +10 | 111 | 5 | 0.5817845065592507 | 0.28751008126389255 | 0.4967792039603212 | 100 / 3 / 8 / 5 | `==` |
| +15 | 111 | 5 | 0.5768599229957799 | 0.2731717488361512 | 0.49397325802362163 | 101 / 3 / 7 / 5 | `==` |

独立对 GL5 per-leg 副本的检查结果：pool key 集合 `==` 原 116 行；每个 pool 的五个 Arm-0 cell `==` 原 `matrix` 对应 cell；不存在补行、漏行或重聚合漂移。

## 对象身份核验

### 431/116、+56% 对象

- `scripts/research/base_b8/b8_rpc_evidence.py::b5_module()` 将加载路径固定为 `scripts/research/base_b5/b5_offline_reverify.py`。
- `prepare_rows()` 的 selected 表达式是：`(prob <= cut) & fric.notna() & (fric >= -0.05) & regime.notna()`。
- B5 的 `load_base()` 只装载 pool/fwdpath/`_priced_live.json`；`features()` 构造市场特征；`regime()` 做时间窗映射；`fit_forward()` 拟合固定 HistGradientBoosting 并取训练集 60% fatal-prob quantile。四个函数均没有 F 名单输入。
- `keystone_live.py::exit_block()` 的退出只有 `HARD_STOP=log(0.5)`、`TRAIL=0.35` 和最多 `HOLD_BLOCKS=7200` 路径；无 TP。

### 单独的 F 对照对象

| 对象 | source facts | 独立裁定 |
|---|---|---|
| 2026-06-24 conservative | 644 labeled pools；`_load_blacklists()` 读 245 个 safe-rug deployer 再加 2 个 PARADAENGI，共 247；33 skeleton；`gate_masks().live_full`；JSON 两镜头均 `pass_n=120` | F-on 真实存在，但不在 B8/B5 调用图，且使用 p≤0.27、depth≥3 ETH、0.0302 ETH、2% haircut 等另一配置 |
| 2026-06-25 aggressive | JSON `labeled_universe=644`、`aggressive_live_no_F=339`、F blacklist OFF、TRAIL=.25；QV2/Fork portfolio 均有 207 delayed-rug | F-off 的 207 rug 是该 644 estimand 的事实，不能作为 431/116 的 rug 数或分母 |

因此不能通过“挑一个 F 版本”修补原对象；这样做会产生新 estimand，违反 Step 0 的不换 config 条款。

## 停止后的完整性

| 应停项目 | 工件状态 | 审查 |
|---|---|---|
| Arm-1 repricing | `NOT_RUN_STOP_GATE` | PASS |
| 同龄 431 baseline repricing | `NOT_RUN_STOP_GATE` | PASS |
| quote-wash decomposition | `NOT_RUN_STOP_GATE` | PASS |
| golden 3–5 腿 | `NOT_RUN_STOP_GATE` | PASS；停止门优先于 golden |
| 新 archive RPC | `0` | PASS；GL5 Step 0 程序结构上没有 RPC/网络路径 |
| edge survivor/collapse | `INCONCLUSIVE_OBJECT_NOT_DEFINED` | PASS；没有强迫二选一 |

## 非实质性说明

1. 报告列出的 W3 正式 regime 上界是 2026-06-08 18:29:40，但 B5 自身声明 KEYSTONE 源的实际最大时间约为 2026-06-01 22:23，故 W3 是 partial coverage。该事实不改变冻结的 431/116 或本轮停止裁定，但后续新 estimand 应在日期表中同时写 formal window 与 observed coverage。
2. 审查落盘时，主报告所引用的 `mvp/diagnostics/base_gl5_artifact_hashes_20260716.json` 尚未生成。这是顺序上可修复的 packaging 项：总 hash ledger 应在本审查两份工件落盘后生成并包含它们。它不影响上述源证据与 Step 0 裁定；若最终交付仍缺该 ledger，则交付完整性应降为 FAIL。

## 证据 SHA256

| 路径 | SHA256 |
|---|---|
| `reports/GL5_task_spec_conservative_realizable_exit_repricing_20260716.md` | `fd194617f154ef958215199c58d369b68b4b407b757c327f16a135de456b358c` |
| `reports/GL5_preregistration_20260716.md` | `909ad2a61585c674a59a95cf75bdddadb4d7465e78dc2272895b9537a1c696d9` |
| `reports/GL5_conservative_realizable_exit_repricing_20260716.md` | `4abf65355d6e4c20abbbf97126749ce5c9ec4b5c92e854ab85cbd692d336ce3e` |
| `reports/GL5_plain_language_20260716.md` | `ebb00f3b440b57af19b8ad4feb7967ca89df4eaa18f41d1e00499a3b2dded955` |
| `mvp/diagnostics/base_gl5_step0_anchor_20260716.json` | `ad4558a1a48df94fa2206533e7947d0ea759ca207f721d6c87cf0e34c773badb` |
| `mvp/diagnostics/base_gl5_per_leg_20260716.json` | `1d8a7b527899b8085087a5547c363e32b7a16db0417ac34e84d034a45844a5e1` |
| `mvp/diagnostics/base_gl5_test_evidence_20260716.json` | `c62b685581921f687de6ef81d731562fc27634efda71947c4e5382b31c2c6533` |
| `scripts/research/base_gl5/gl5_step0_anchor.py` | `20df37575974cd94488b8a6628a4f0800a5abad59bf33a4a0af383c761e85e6d` |
| `tests/test_base_gl5_step0.py` | `764d6e74270203bdd1307843b781635da2b675ab386d86c80c42f00a417cae25` |
| `scripts/research/base_b8/b8_rpc_evidence.py` | `4d0123dc9aa9290c07342a66e473f836ed5c7221385b6186762a8b7c93920fee` |
| `scripts/research/base_b5/b5_offline_reverify.py` | `22f5c9e787e6a79695f9db4dc9679b10ea6cfd455e9b16ac8c190c86a5e5dc17` |
| `scripts/research/base_lefttail/keystone_live.py` | `626eaca39d43bf52049bb989e4579777a4bf60d1ea7d0e40730944178177a745` |
| `mvp/diagnostics/base_reaudit10_per_trade_20260715.json` | `65126c50ed5680c8d0363379662fcd88a0d6b339c6af78fbd873cf4ade7bb410` |
| `mvp/diagnostics/base_reaudit10_final_20260715.json` | `7af0f54247956fa069b0d82bb686f0d9c09bdb76da07cf63e9eb21998c45bcb7` |
| `mvp/diagnostics/base_gl1_golden_truth_ledger_gl4_20260716.json` | `e89c43e1d932bd2a2035430942af8c316bfbd88771de43b787948ac0a216bc9a` |
| `mvp/diagnostics/base_gl3_gl12_alpha_beta_20260715.json` | `e42ac92d29e75fa1ad4fbd12e25365a1672f4aeeef50f6edffa84bd1fe5a32a9` |
| `mvp/audits/live_config_rebacktest_20260624/live_config_rebacktest.json` | `41803d1b17f4a6761585501a0bfa79591db2160039a8aeddc8bcabb49d37d92d` |
| `mvp/audits/aggressive_live_rebacktest_20260625/aggressive_live_rebacktest.json` | `e15c9f49255f60dc75db85c8520517c295ec6b87734a3bd6cc756586ef979e58` |

## 最终边界

本审查只确认“应停且停得对”，不确认 KEYSTONE edge 幸存，也不确认其崩塌。继续工作的唯一合规入口是由人类显式定义 GL5-v2 的新 estimand（431 加 point-in-time F，或 644/120 F-on 母体择一），然后重新预注册。独立审查不自判 GO；`GO=NOT_MADE`。
