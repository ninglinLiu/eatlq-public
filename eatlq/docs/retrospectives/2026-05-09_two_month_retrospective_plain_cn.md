# 两个月项目在干什么：给创始人看的中文简报

生成：**2026-05-09**  
依据：`2026-05-09_two_month_retrospective_evidence_backfill.md`、同日系 `evidence_index.md`、`mvp/runs/run-20260509-two-month-retrospective-backfill.md`（不扩扫仓库）。

---

## 1. 这两个月一句话总结

团队把「白皮书 + AI 码字」收敛成「**可看证据链的工程**」：契约文档、离线读数、gates。经济上，**有一份限定的 Run-004 V2 七日样本读数不过关**（挂上 **`FAILED_NO_7D_ALPHA`**），所以**训练和上线宣称被硬性挡住**——这是**这一关没过**，不是「整个 LASZLO/EATLQ 项目判死刑」；主线工程仍可继续垒，聪明钱跟单只是**旁路**，其挫折**不能**当成全盘失败。（证据：`mvp/runs/run-004-v2-7d-alpha-readout.md`、`mvp/audits/failed_no_7d_alpha_guard.md`）

---

## 2. 项目经历的十个关键阶段（Sprint 名压缩成叙事）

每个阶段只留主要证据，细节可查审计版。

| # | 阶段（人话） | 发生了什么 | 证据（1–3 个路径） |
|---|----------------|------------|---------------------|
| 1 | **把话说清楚** | 根目录 `Home` / `Architecture` / 简化架构与白皮书对齐方式固定下来：叙事可以错，**以 Architecture + 代码为准**。 | `Home.md`、`Architecture.md`、`Architecture-Simplified.md` |
| 2 | **地基体检** | Phase1–5 审查：发现 label 重复键风险、文档与执行器行为可能不一致等。 | `mvp/runs/phase1-5-foundation-review.md` |
| 3 | **回到 LASZLO** | 去掉团队自己加的 meme 层，重新贴原作者蓝图。 | `git 5254937`（见 evidence index） |
| 4 | **经济 reality check** | Run-004 **V2、限定 7 日**样本上，收益分布整体偏负，挂上 **「七日未过 alpha 线」** 结论。 | `mvp/runs/run-004-v2-7d-alpha-readout.md` |
| 5 | **规矩写死** | 上述读数变成 **guard**：**不准**用「模型分数好看」去覆盖这条经济结论。 | `mvp/audits/failed_no_7d_alpha_guard.md` |
| 6 | **可执行性变实** | 「先能报价、能回放」一挂，入口成功率从工程上抬到约一半量级（可验证指标）。 | `mvp/audits/sprint037b_r4/sprint037b_r4_report.md` |
| 7 | **换样本再考** | Q4 holdout 读数做了「能不能顶替上面 guard」的讨论，**最终没有授予顶替**（严格标准下仍未过）。 | `mvp/runs/run-037D-q4-holdout-readout.md`、`mvp/diagnostics/sprint037d_summary.json` |
| 8 | **样本外卡壳** | 新版 scorecard / guard 在 **选定的样本外周**上，**没有凑够可评的行数** → 不是简单「模型坏了」，是 **覆盖不够**。 | `mvp/audits/sprint039g/oos_validation_summary.json` |
| 9 | **钱包线：别认错人** | 全海扫描、controller、共享合约：分清 **大家打同一个合约** 和 **同一笔钱养的一群人**。 | `mvp/audits/sprint045e_shared_contract_and_funding_graph_readout.md` |
| 10 | **跟单线：纸面复现** | Watchlist → 纸面跟单：大多路由**无法同等条件报价**、缺事件级时间戳，**整体未过**；随后用 **ROI** 看跟单、sidecar **只观察不上线**。 | `mvp/audits/sprint046_watchlist_to_alpha_readout.md`、`mvp/audits/sprint049b_return_normalized_copy_follow_readout.md`、`mvp/diagnostics/sprint049c_decision_flags.json` |

另有「**小范围 micro-alpha 怎么验**」的方法论设计（028–029 一带），与**论文级泛化**不是一回事——证据：`mvp/audits/sprint028c_scoped_micro_alpha_policy.md`。

---

## 3. 三个早期失败项目带来的教训（叙事来自 seed，**仓内无独立证据包**）

以下三段**仅在前一份复盘 docx 里展开**，本仓库未附 Hyperliquid PDF 等原件，标为 **证据不足 / 需回填**。

| 前传 | 教训（对现在的意义） |
|------|----------------------|
| **过度简化的翻版** | 「能跑」不等于「能交易、能验收」；现在用 readout + flags 就是在补这一课。 |
| **Solana / 三明治** | 执行语义和抢跑现实会杀死纸面策略；后面强调 Quoter、可执行回放是同一逻辑。 |
| **Hyperliquid / 聪明钱跟单** | 榜单、短期收益、地址利润**极易幸存者偏差**；所以现在跟单必须 **ROI、集中度、旁路**，且**失败域不扩散到白皮书主线**。 |

---

## 4. 当前已经打牢的东西（有证据支撑）

- **文档与契约分层**：入口、架构、简化视图、白皮书怎么用——见 `Home.md` 等。  
- **「经济读数 > 模型自嗨」**：`failed_no_7d_alpha_guard.md` 把规矩写死。  
- **可执行漏斗上的工程进步**：quoteability / entry 成功率可读数——`sprint037b_r4`/`r5`。  
- **数据与回填工具链**：块时间一类的 backfill（evidence index 里的相关 commit）。  
- **分析方法资产**：scorecard_v2、OOS 脚本、ROI 侧读数、钱包/图谱审计——哪怕结论是「没过」或「缺覆盖」，方法在仓里。  

---

## 5. 当前仍没证明的东西

- **在 guard 定义的口径下**，尚未有一份新的、被正式接受的经济读数 **取代** 「七日读数不过关」这一条（037D **未 granting supersession**：`mvp/diagnostics/sprint037d_summary.json`）。  
- **样本外周的 guard 经济效果**：选定周 **0 行**可评 → `NEEDS_MORE_OOS_COVERAGE` 量级的问题（见 `oos_validation_summary.json`）。  
- **Watchlist copy-follow** 作为主策略：**纸面 smoke 没过**，且大量事件不在可报价通道、缺训练用时间切分（`sprint046` readout）。  
- **文档与实现对齐**：例如 `pending_sell`、ingest 路径与白皮书字面——审查里仍打过 **漂移** 警告（`phase1-5-foundation-review.md`）。  

---

## 6. 当前不能做什么（Decision flags → 人话）

下面把英文枚举翻成口语，并以审计版摘要为准；**完整键值仍以** `mvp/diagnostics/sprint046_decision_flags.json`、`sprint049c_decision_flags.json` **等原件为准**。

| 英文 / 记号 | 人话 |
|-------------|------|
| **FAILED_NO_7D_ALPHA** | 「在存档的那份 **Run-004 V2、七天、限定范围**读数里，**没达到团队定义的 alpha 经济线**。**不是**说这仓库以后永远做不出东西；**是**说在这条读数被正式替代之前，别拿模型指标当尚方宝剑。」 |
| **failed_no_7d_alpha_guard（绑定的禁止项）** | 在 guard 有效期间：**不许**把当前状况当成「可以放开训练、提模、影子实盘、真仓、Phase6 猎杀、白皮书已完全兑现」的授权。（细则见 `failed_no_7d_alpha_guard.md` 原文。） |
| **supersedes_FAILED_NO_7D_ALPHA: false（037D）** | 「后来做的 Q4 holdout **没有**在规则上『翻案』成功，**七日线读数仍是门槛**。」 |
| **NEEDS_MORE_OOS_COVERAGE** | 「样本外想验证新 guard，**数据覆盖不够**，得换周、扩包或先说明为何这一 slice 本来就空。」 |
| **LIVE_ALLOWED = false** | **不准**按当前证据链去开真钱。 |
| **TRAINING_ALLOWED = false** | **不准**进入「正式训练/提模」流程（与 046 等 flags 一致）。 |
| **MODEL_PROMOTION_ALLOWED = false** | **不准**把模型升格为可上线候选。 |
| **NEEDS_ARCHIVE_BACKFILL_FOR_TRAINING（046 一侧摘要）** | 「想走到训练，还缺**归档级数据/时间戳**一类回填。」 |
| **MAINLINE_CONTINUES_WHITEPAPER = true（049C 摘要）** | 「**白皮书 narrative 的主线工程还可以继续**」——**不等于**可以 live，也**不等于**跟单线成功。 |
| **SMART_MONEY_FOLLOW_IS_SIDECAR** | 「聪明钱/跟单是**旁路**；不能抢主故事的决策权。」 |
| **SIDECAR / PAPER_ONLY** | 「只用来做**纸上/影子观测**，别把侧车输出当主业信号。」 |
| **HIGH_CONC_ALPHA_VERDICT: EXTINCT（049C 叙述级）** | 「一批**高集中度**历史候选，后来池子/代币侧**活跃度没了**，不能像还有当下 alpha 那样用。」 |

---

## 7. 当前应该继续做什么（工程顺序，不是替你做投资决策）

1. **把样本外弄明白**：先有 **非空的 OOS slice**，再谈 guard 好不好使（对齐 `oos_validation_summary` 的结论）。  
2. **046 的卡点**：归档时间戳、可报价路由 / lane——否则跟单与训练时钟都站不住。  
3. **旁路照旧观察**：049 侧车按约定窗口看 market_ticks **命中**，再决定是否扩大池子订阅范围。（见 049c flags 里的 scope caveat。）  
4. **文档对线**：executor 状态机、`pending_sell` 等与 `Architecture` 对齐，避免「读起来安全、跑得不一样」。  

路线级优先级（budget 花在扩 OOS 还是扩 archive）——留给负责人拍板，本篇不替你决定。

---

## 8. 最关键的十条教训

1. **产出多 ≠ 系统可用**。  
2. **绝对赚了多少 ≠ 收益率，更不等于 alpha**。  
3. **`FAILED_NO_7D_ALPHA` = 那一关读数没过**，别偷换成「项目完了」。  
4. **聪明钱/跟单挂了 ≠ 白皮书整条主线挂**；sidecar 失败就留在 sidecar。  
5. **模型指标不能推翻已存档的经济 guard**（除非你走通 guard 文档里定义的替代读数）。  
6. **研究用 top-N、LOOCV、集中度**——是我们自己加的尺子，不是原作者圣旨。  
7. **共享合约地址 ≠ 同一资金来源**。  
8. **没有事件级时间就谈不上靠谱的时间切分**。  
9. **小范围 smoke / 5m** 验证了「这一小块 pipeline」——别吹成论文外推。  
10. **每条 sprint：readout + decision_flags**，后人少靠聊天考古。

---

## 9. 下一步建议

| Composer / 日常能做的 | 建议交给 GPT‑5.5 / Opus 的 |
|-------------------------|-----------------------------|
| 把 `NEEDS_BACKFILL` 清单装订成可查表格；补 Sprint020–026 若找到的零散路径 | **资金规模与风险胃口**下的 go/no-go、是否加倍买数据/RPC |
| Operations 与君上最近一次本机启动流程对齐 | **白皮书条款 vs 源码**逐条对齐的「算不算兑现」裁决 |
| 写清 Patch F 重复键计数 vs Run-004 7D CSV 歧义（审计版要求单列 reconciling） | 新假设前的 **Charter**（什么算过线、过什么线） |

---

## 10. 三百字大白话结论

这几个月像在盖楼：前半段砖头堆得快，但没验收就可能空鼓。现在大家有了**规矩**——尤其是一份**七天限定的离线读数**，说「这一波 V2 样本里看不出稳定正期望」，于是就树了一块牌子：**在没拿出新的、被认可的经济读数之前，别训练、别升格模型、别当真钱**。同时工程上把**能报价、能回放**做实了，后面又发现**样本外数据不够评**、**跟单大多走专有路由抄不了**，聪明钱那条线就**老实放在旁路**里观察。白皮书说的主架构**还能继续搭**，但**赚钱这件事还在证伪/求证中**，不是「模型一好就翻盘」。早年三个玩具项目教会你：**简、链上残酷、榜单骗人**——这些教训现在化成**门禁和旁路**，别再把叙事当取证。

---

**Changed file（本轮）**：`docs/retrospectives/2026-05-09_two_month_retrospective_plain_cn.md`（新增）
