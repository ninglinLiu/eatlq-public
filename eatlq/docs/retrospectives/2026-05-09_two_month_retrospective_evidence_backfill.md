# 两个月项目复盘（证据回填版）

生成日期：**2026-05-09**  
Seed：`定现Alpha_LASZLO_两个月项目复盘_20260509.docx`（章节结构对齐 seed，事实以仓内 artifact 补强）  
伴侣：`docs/retrospectives/2026-05-09_two_month_retrospective_evidence_index.md`  
Run：`mvp/runs/run-20260509-two-month-retrospective-backfill.md`

---

## 1. Executive conclusion（中文摘要）

- **起点与谱系**：工程主线证据始于本仓库的版本化契约（根目录 `Home.md` / `Architecture.md` / Operations / Whitepaper v2）与 MVP run–audit–diagnostics 链路；seed 描述的「五个月接触、三月起高强度投入、三期前传」属 **SEED_ONLY**，仓内 **NEEDS_BACKFILL**（除叙事外无独立工件）。
- **路线变化**：从「文档叙事 + 广铺代码」收敛到「artifact + decision_flags + guard」；经济上 Run-004 V2 **7D scoped readout** 已给出 **`FAILED_NO_7D_ALPHA`**（**非**「全项目证伪」）；随后 Q4 holdout、scorecard_v2、OOS、钱包 rediscovery、ROI 归一化 sidecar 等为 **增量验证与旁路**，均未在证据上解除 `failed_no_7d_alpha_guard` 所绑定的核心 readout。
- **失败类型**：**认知**：将「产出量 / 绝对 PnL」误作可交易优势（seed + `sprint046`/`049b` readout 对治）。**工程**：ingest/Redis/label/Quoter/重复键与 doc–code 漂移（`phase1-5-foundation-review.md` 等）。**数据/验证**：OOS 覆盖为零行、copy-follow 可报价比例过低、无 per-event 时间戳导致无法 chron split（`sprint039g/oos_validation_summary.json`，`sprint046_watchlist_to_alpha_readout.md`）。
- **已修复 vs 纸面**：**已用 commit/读 out 支持**：meme 层移除与 LASZLO 再对齐（`5254937`）；CH 制度发现 + quoteability 提升（`sprint037b_r4`/`r5`）；block_ts / backfill 工具链（`72c8c22` 等）；039 系列 scorecard 与 OOS **显式**结论。**仅文档或部分一致**：架构简化与 Code-Review 写明 `pending_sell` 等与 executor 实现仍可能不一致（`phase1-5-foundation-review.md` §7）；**INGESTED**：白皮书 v2 写 `eth_subscribe("logs")`，与 foundation review 指出的 **newHeads + eth_getLogs** 路径需以代码为准。
- **当前阶段**：**白皮书主线工程仍可进行**；**训练 / 提升 / live / Phase6 授权性主张**仍受 `mvp/audits/failed_no_7d_alpha_guard.md` 约束；**聪明钱跟单**在 decision_flags 中多为 **SIDECAR / PAPER**，与主线解耦（`sprint049c_decision_flags.json` 等）。
- **最短路径（工程，非策略结论）**：补齐 OOS 非空 slice 或扩大 bundle（`NEEDS_MORE_OOS_COVERAGE`）；按 `sprint046` 阻塞项推进 **archive 时间戳 / 可报价 lane**；侧线按 `049c` 条件观察 shadow，再决定是否扩张 `pool_discovery`。**路线级 go/no-go** 见 §8 `question_for_reviewer`。

---

## 2. Created / changed files

| 文件 | 动作 |
|------|------|
| `docs/retrospectives/2026-05-09_two_month_retrospective_evidence_backfill.md` | 新增 |
| `docs/retrospectives/2026-05-09_two_month_retrospective_evidence_index.md` | 新增 |
| `mvp/runs/run-20260509-two-month-retrospective-backfill.md` | 新增 |

---

## 3. Seed 章节映射（不重写目录，仅回填证据角色）

| Seed 章节 | 仓内承接方式 |
|-----------|----------------|
| 0 证据边界 | 本文件 + `evidence_index.md`；仍以 artifact 为先 |
| 1 Executive | §1 摘要；指标细节指回 `mvp/runs/run-004-v2-7d-alpha-readout.md` |
| 2–5 谱系与时间线 | §4 表；前传三段 **NEEDS_BACKFILL** |
| 6–8 方法与 V2/V3/smart-money | §5 §6；**禁止**将 overlap 失败泛化为项目失败 |
| 9–11 白皮书本体与聪明钱 | `whitepaper-v2.md`；sidecar：`s049a`/`049b`/`049c` |
| 12–14 Agent 协作 | §7 |

---

## 4. Timeline 表（按阶段；日期以 artifact 时间戳或 sprint 簇为主）

| stage_name | date_or_sprint_range | what_happened | evidence_paths | decision_flags / lesson |
|------------|---------------------|---------------|------------------|-------------------------|
| 契约与文档地基 | ongoing | 根目录 wiki 三位一体；白皮书记「与实现不符以 Architecture 为准」 | `Home.md`, `Architecture.md`, `Architecture-Simplified.md`, `mvp/audits/documentation_path_map.md` | **lesson**：叙事 ≠ 契约 |
| Phase1–5 地基审查 | run 记录在 2026-04 量级（见 artifact） | 重复 label keys、executor/doc 漂移、wallet 路径风险显性化 | `mvp/runs/phase1-5-foundation-review.md`, `mvp/diagnostics/phase1_5_foundation_review.json` | **engineering_failure** + **doc drift** |
| LASZLO 再对齐 / meme 剥离 | git: `2026-04-22` | 移除 EATLQ 引入的 meme_sniper 层 | `git 5254937` | **guard**：回归原作者特征边界（对照 `Code-Review.md` / skill） |
| V2 7D 经济 readout | readout 控制性结论 | Scoped 7D 样本上 mean/profit_factor 否定 | `mvp/runs/run-004-v2-7d-alpha-readout.md` | `FAILED_NO_7D_ALPHA` |
| failed_no_7d guard 冻结 | 2026-05-02 | 禁止用模型指标覆盖经济 readout | `mvp/audits/failed_no_7d_alpha_guard.md` | **我方 guard** |
| Sprint037 可执行漏斗 | 2026-05-07 附件 | Quoteability-first；entry ~50.3% | `mvp/audits/sprint037b_r4/sprint037b_r4_report.md`, `mvp/audits/sprint037b_r5/sprint037b_r5_report.md` | **engineering win**（可验证指标） |
| Sprint037D holdout | 2026-05-07 | 形式主义 supersession 字段存在但 **未授予** | `mvp/runs/run-037D-q4-holdout-readout.md`, `mvp/diagnostics/sprint037d_summary.json` | **validation_failure** on strict criteria |
| Sprint038–039 scorecard / OOS | 2026-05-07–08 | v2 guard 原型；OOS 周上 canonical routed **0 行** | `mvp/audits/sprint039f/`, `mvp/audits/sprint039g/oos_validation_summary.json`, `mvp/runs/run-039G-v2-guard-oos-validation-readout.md` | `NEEDS_MORE_OOS_COVERAGE` |
| Sprint045 钱包/图 | 2026-05-08 | Broad-sea、controller、共享合约 vs funding 区分 | `mvp/audits/sprint045e_shared_contract_and_funding_graph_readout.md`, `mvp/audits/sprint045f_controller_topic_readout.md` | **认知**：`tx.to` 重叠 ≠ 资金同源 |
| Sprint046 watchlist→alpha | 2026-05-09 | Paper smoke FAIL；quoteable ~11%；无事件级时间戳 | `mvp/audits/sprint046_watchlist_to_alpha_readout.md`, `mvp/diagnostics/sprint046_decision_flags.json` | **data/validation** |
| Sprint049 ROI & sidecar | 2026-05-09 | ROI 归一化；集中度；sidecar 影子跑 | `mvp/audits/sprint049b_return_normalized_copy_follow_readout.md`, `mvp/diagnostics/sprint049c_decision_flags.json`, `mvp/audits/sprint049c_copy_follow_shadow_readout.md` | **SIDECAR / PAPER** |
| Scoped micro-alpha 政策 | Sprint028–029 设计簇 | Feature factory + bounded validation 设计包 | `mvp/audits/sprint028c_scoped_micro_alpha_policy.md`, `mvp/diagnostics/sprint028c_scoped_candidate_ranking_summary.json` | **我方方法**；≠ 论文级泛化 gate |

---

## 5. 失败分类（带证据）

### 5.1 认知失败

- 将「绝对利润 / 榜单地址」等价于 alpha — **seed**；反证框架 — `sprint049b_return_normalized_copy_follow_readout.md`（ROI 与集中度）。
- 将「共享执行合约」等同「资金同源」— 已驳 — `sprint045e_shared_contract_and_funding_graph_readout.md`。

### 5.2 工程失败（已部分修复，需对照具体 readout）

- 离线重复 `(tx_hash, log_index)` label 冲突 — `phase1-5-foundation-review.md`（与后续 7D **不同 CSV 上下文**；禁止混论 **NEEDS_BACKFILL** 若需统一 reconciling 叙事）。
- Ingest 路径与白皮书措辞不一致 — `phase1-5-foundation-review.md` §3（WARN）。

### 5.3 数据 / 样本 / 验证失败

- OOS：无 canonical gate 行 — `mvp/audits/sprint039g/oos_validation_summary.json`。
- Copy-follow：可报价占比过低、训练时间切分不可行 — `sprint046_watchlist_to_alpha_readout.md`。
- Holdout 经济判据未过 — `mvp/diagnostics/sprint037d_summary.json` `supersession`.

---

## 6. 已修复 vs「看起来修好」

| 主题 | 状态 | 证据 |
|------|------|------|
| Meme 层偏离原作者 | 代码向 LASZLO 收敛 | `git 5254937` |
| Quoteability / entry 成功率 | 量化改善（工程） | `sprint037b_r4`/`r5` |
| Block 时间 / backfill | 工具与修复提交 | `git 72c8c22`, `dac3091`, `0a535ac` |
| `pending_sell` / live 语义 | **可能仍漂移** | `phase1-5-foundation-review.md` vs `Architecture.md` |
| OOS / guard 经济证明 | **未完结** | `oos_validation_summary.json` |

---

## 7. 原作者路线 vs 我方 guard vs Agent 风险 vs 实验证伪

| 类别 | 说明 | 例 |
|------|------|-----|
| 原作者白皮书路线 | Base、V3、Quoter、Feature Store、Phase6 叙事 | `whitepaper-v2.md` |
| 我方后加 guard | `FAILED_NO_7D_ALPHA`、训练 gate、scorecard/OOS charter | `failed_no_7d_alpha_guard.md`, `sprint039f/v2_validation_charter.md` |
| Agent 过度自信 | seed：「继续即认可」；对策：readout + flags | `定现…docx` §5.1；本仓 `mvp/runs/*` |
| 实验证伪（局部） | 7D scoped；046 paper；037D holdout | 各 readout |

---

## 8. Decision flags（合并快照；以 JSON 原件为准）

- **全局**：`FAILED_NO_7D_ALPHA` 仍绑定 `mvp/runs/run-004-v2-7d-alpha-readout.md`（见 `failed_no_7d_alpha_guard.md`）。
- **046**：`LIVE/TRAINING/PROMOTION = false`；`NEEDS_ARCHIVE_BACKFILL_FOR_TRAINING` 等 — `mvp/diagnostics/sprint046_decision_flags.json`。
- **049C**：白皮书主线继续；跟单 SIDECAR；多 token extinct — `mvp/diagnostics/sprint049c_decision_flags.json`。

### question_for_reviewer（路线级，本 Agent 不决断）

1. 是否追加资源做 **OOS bundle 扩容**（相对继续调 guard 阈值）？  
2. **Archive / per-event timestamp** 与 **platform-router lane** 的工程优先级是否高于继续扩 wallet 榜单？  
3. `FAILED_NO_7D_ALPHA` 的 supersession 标准是否仅以 Q4 holdout 为锚，抑或需平行于 **young_pool_onset_flow** readout family 的另一条正经济链？  

---

## 9. NEEDS_BACKFILL 列表

- 前传三项目：**SEED_ONLY**（docx）；无仓内 PDF/目录。  
- Sprint020–026 smart-money overlap：**INFERRED from seed**；缺少系统命名 audit 路径（见 `evidence_index.md` §6）。  
- 「强模型 review 确切日期与 PR」：叙事存在；**NEEDS_BACKFILL** git/PR。  
- 精确人力墙钟边界：**NEEDS_BACKFILL**。  
- Patch F E2 duplicate 计数 vs 7D `duplicate_conflict=0`：**NEEDS_BACKFILL** 统一 reconciling doc（禁止在本复盘断定同一数据集）。

---

## 10. Agent 工作方式教训（写入协作规范）

1. **每 sprint 必落**：readout Markdown + `decision_flags.json` + 可追溯命令/范围声明。  
2. **区分**：白皮书叙事 / Architecture 契约 / 经济 readout / 模型指标 — `Code-Review.md` 已写原则。  
3. **禁止**：用 scoped 5m/smoke PASS 顶替论文级或 live 授权。  
4. **禁止**：将 `topn/LOO/OOS/concentration`  guard 改写为原作者硬需求 — 见 `sprint028c_scoped_micro_alpha_policy.md`。  
5. **smart-money**：overlap 负面结论 **仅限该假设**；infra（tx_from、簇诊断）可复用 — seed 与本仓 `049` sidecar 设计一致即可。  

---

## 11. Composer 下一步 vs 需 GPT-5.5 / Opus

| Composer 可继续做 | 建议更强模型 |
|--------------------|--------------|
| 扩大 `NEEDS_BACKFILL` 的 grep/年表 PR 装订 | 单次多文件 **路线仲裁**（与资金/合规相关） |
| 将 Sprint020–026 零散日志聚合成单列 index | Whitepaper ↔ 源码 **深度对齐裁决** |
| Runbook：把 Operations 段落与最近一次 `laszlo.sh` 实测对齐 | 新经济假设的 **事前 charter** |

---

## 12. Evidence index summary

详见 `docs/retrospectives/2026-05-09_two_month_retrospective_evidence_index.md`（按结论 → 路径/提交映射）。

---

## 13. Next reviewer questions

同 §8 `question_for_reviewer`。
