# Evidence index — 两个月复盘（证据回填）

生成：`2026-05-09`  
伴侣文档：`docs/retrospectives/2026-05-09_two_month_retrospective_evidence_backfill.md`  
Run 记录：`mvp/runs/run-20260509-two-month-retrospective-backfill.md`

本文件将「可审计结论」映射到仓库内路径、审计读 out 或 git 锚点。未列出的主张视为 **NEEDS_BACKFILL**。

## 1. 叙事 seed（聊天侧，非 git 源）

| 用途 | 路径 |
|------|------|
| 章节结构、路线叙述、前传三期、方法论反思 | `定现Alpha_LASZLO_两个月项目复盘_20260509.docx`（根目录） |

## 2. 可执行契约与原作者边界

| 结论 | 证据 |
|------|------|
| 文档入口在仓根 `Home.md`，非 `wiki/` 子目录 | `Home.md` L17–18；`mvp/audits/documentation_path_map.md` |
| 可执行契约以 `Architecture.md` + 源码为准；白皮书 v2 为叙事 | `whitepaper-v2.md` 前言；`Home.md` L20–23 |
| `top1/top5/LOO/OOS` 等为研究 guard，不得伪装成原作者硬门槛 | `mvp/audits/sprint028c_scoped_micro_alpha_policy.md`；`mvp/audits/sprint039f/v2_validation_charter.md` |
| 原作者四特征与信号公式；`meme_sniper` 等为 EATLQ 叠加后已剥离（与 Cursor eatlq-authorship skill 对齐） | `whitepaper-v2.md` / `Architecture.md`；`git show 5254937` — `refactor(meme-layer): remove EATLQ-introduced meme_sniper layer, realign with LASZLO` |

## 3. 经济 guard 与训练授权

| 结论 | 证据 |
|------|------|
| V2 7D scoped readout 否定性经济结论 | `mvp/runs/run-004-v2-7d-alpha-readout.md`（`FAILED_NO_7D_ALPHA`） |
| 静态 guard：模型指标不可覆盖上述 readout | `mvp/audits/failed_no_7d_alpha_guard.md` |
| Code-Review 顶层声明与 guard 一致 | `Code-Review.md` 文首 |
| Q4 holdout 曾产生 supersession 讨论但未授予 | `mvp/runs/run-037D-q4-holdout-readout.md`；`mvp/diagnostics/sprint037d_summary.json` → `supersession.supersedes_FAILED_NO_7D_ALPHA: false` |
| Sprint039F 明确不声称 supersede FAILED_NO_7D | `mvp/audits/sprint039f/v2_validation_charter.md` L15–18 |

## 4. 阶段性工程与数据审计（Sprint / Run 锚点）

| 阶段主题 | 证据路径 |
|----------|----------|
| Phase1–5 地基审查：重复 label、doc/代码漂移（如 `pending_sell`） | `mvp/runs/phase1-5-foundation-review.md`；`mvp/diagnostics/phase1_5_foundation_review.json` |
| 可执行回放 / quoteability-first，entry 成功率 ~50.3% | `mvp/audits/sprint037b_r4/sprint037b_r4_report.md`；`mvp/audits/sprint037b_r5/sprint037b_r5_report.md` |
| RPC 乘子与 R5 校准（039A 叙事） | `mvp/runs/run-039A-regime-discovery-r5-expansion-readout.md`；git `21162bb` |
| Scorecard / FP 尸检 / v2 charter | `mvp/audits/sprint039d/`；`mvp/audits/sprint039f/` |
| OOS：canonical gate 在选定周上 **0 行** → 覆盖不足 | `mvp/audits/sprint039g/oos_validation_summary.json`；`mvp/runs/run-039G-v2-guard-oos-validation-readout.md` |
| 全局 CH broad-sea、controller、共享合约与 funding graph 设计 | `mvp/audits/sprint045c_broad_sea_expansion_and_probe_readout.md`；`mvp/audits/sprint045e_shared_contract_and_funding_graph_readout.md`；`mvp/audits/sprint045f_controller_topic_readout.md` |
| Watchlist → alpha 可行性；paper smoke FAIL；quoteable 仅 ~11% | `mvp/audits/sprint046_watchlist_to_alpha_readout.md`；`mvp/diagnostics/sprint046_decision_flags.json` |
| ROI 归一化 copy-follow；集中度风险 | `mvp/audits/sprint049b_return_normalized_copy_follow_readout.md`；`mvp/diagnostics/sprint049b_decision_flags.json` |
| Sidecar 部署与 token 灭绝/池死判词 | `mvp/diagnostics/sprint049c_decision_flags.json`；`mvp/audits/sprint049c_copy_follow_shadow_readout.md`（如路径略有不同以仓内 glob 为准） |

## 5. Git 历史锚点（示例，非全量年表）

| 主题 | 提交（短 hash） | 说明 |
|------|-----------------|------|
| LASZLO 蓝图对齐、Phase A–F | `32bfe8b` | `align with LASZLO blueprint: phases A-F` |
| Meme 层移除、回到 LASZLO | `5254937` | 见上 |
| Block timestamp / backfill 工具链 | `72c8c22`, `dac3091`, `0a535ac` | dictGet backfill、wrapper、flusher 修复等 |
| Sprint039 系列（scorecard / OOS） | `c0102cf` … `cf220a3` | 见 `git log` 五月上旬簇集 |
| paper / safety：`sprint013` | `ac020cf` | `paper observation infra and safety gates` |

## 6. NEEDS_BACKFILL（本 index 刻意的空白）

| Topic | 说明 |
|-------|------|
| 「Sprint020–026 smart-money overlap 关闭 staging」逐文件映射 | Seed 宣称；仓内未发现 `mvp/**/sprint02{0-9}*_readout*` 系统性命名。**需** Dedicated 关键字搜索或历史分支 / 外部归档。 |
| Hyperliquid「三日复盘」PDF | Seed 引用 `/mnt/data/3日复盘(1).pdf`；**不在本仓库**。 |
| 前传三次失败（简化版 / Solana Hunter / HL） | **仅 seed 叙述**；无仓内独立 artifact 时保持 SEED_ONLY。 |
| 精确「墙钟两个月」起止 | 需与财务 / 人力记录或首笔 EATLQ commit 范围对齐；当前以 artifact 时间戳与 git 为代理。 |
